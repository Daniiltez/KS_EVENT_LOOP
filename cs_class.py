from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional


# ============================================================
# Utility
# ============================================================

_MISSING = object()


def get_path(data: dict, *path: str, default: Any = None) -> Any:
    """
    Безопасно получить значение по цепочке ключей.

    get_path(data, "player", "state", "health")
    """
    current = data

    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default

        current = current[key]

    return current


def changed(old: Any, new: Any) -> bool:
    return old != new


# ============================================================
# Player state
# ============================================================

@dataclass
class PlayerState:
    """
    Последнее известное состояние конкретного player.steamid.
    """

    steamid: str

    name: Optional[str] = None
    team: Optional[str] = None
    observer_slot: Optional[int] = None
    activity: Optional[str] = None

    health: Optional[int] = None
    armor: Optional[int] = None
    helmet: Optional[bool] = None

    flashed: Optional[int] = None
    smoked: Optional[int] = None
    burning: Optional[int] = None

    money: Optional[int] = None
    equip_value: Optional[int] = None

    round_kills: Optional[int] = None
    round_killhs: Optional[int] = None

    kills: Optional[int] = None
    assists: Optional[int] = None
    deaths: Optional[int] = None
    mvps: Optional[int] = None
    score: Optional[int] = None

    weapons: Dict[str, dict] = field(default_factory=dict)

    # Полный последний player-блок.
    raw: Dict[str, Any] = field(default_factory=dict)

    # Время последнего пакета.
    timestamp: Optional[int] = None

    # Был ли этот player когда-нибудь замечен живым.
    was_alive: bool = False

    # Был ли зафиксирован death.
    dead: bool = False


# ============================================================
# Main GSI engine
# ============================================================

class CS2GSI:
    """
    Stateful обработчик Counter-Strike GSI.

    Один вызов process_line() = один GSI JSON packet.

    Основная идея:

        provider.steamid
            ↓
        владелец GSI

        player.steamid
            ↓
        текущий player / наблюдаемый игрок

    Никогда не следует считать player.steamid постоянным
    SteamID стримера.
    """

    def __init__(
        self,
        owner_steamid: Optional[str] = None,
        *,
        event_callback: Optional[Callable[[str, dict], None]] = None,
        strict_json: bool = False,
    ):
        # ----------------------------------------------------
        # Configuration
        # ----------------------------------------------------

        self.owner_steamid = owner_steamid

        self.event_callback = event_callback

        self.strict_json = strict_json

        # ----------------------------------------------------
        # Global state
        # ----------------------------------------------------

        self.connected = False

        self.last_timestamp: Optional[int] = None

        self.map_name: Optional[str] = None
        self.map_mode: Optional[str] = None
        self.map_phase: Optional[str] = None

        self.round_number: Optional[int] = None
        self.round_phase: Optional[str] = None
        self.round_win_team: Optional[str] = None
        self.bomb_state: Optional[str] = None

        self.team_ct: Dict[str, Any] = {}
        self.team_t: Dict[str, Any] = {}

        # ----------------------------------------------------
        # Current GSI player
        # ----------------------------------------------------

        self.current_player_steamid: Optional[str] = None

        self.current_player_name: Optional[str] = None

        # ----------------------------------------------------
        # All players we've seen
        # ----------------------------------------------------

        self.players: Dict[str, PlayerState] = {}

        # ----------------------------------------------------
        # Last raw packet
        # ----------------------------------------------------

        self.last_packet: Optional[dict] = None

        # ----------------------------------------------------
        # Current round
        # ----------------------------------------------------

        self.round_active = False

        # Number of round kills belonging to the owner.
        self.owner_round_kills = 0
        self.owner_round_headshots = 0

        # ----------------------------------------------------
        # Statistics
        # ----------------------------------------------------

        self.total_packets = 0
        self.invalid_packets = 0

    # ========================================================
    # Public API
    # ========================================================

    def process_line(self, line: str) -> bool:
        """
        Обработать одну JSON-строку.

        Возвращает:
            True  — пакет успешно обработан
            False — пакет невалидный
        """

        if not isinstance(line, str):
            return False

        line = line.strip()

        if not line:
            return False

        try:
            data = json.loads(line)

        except json.JSONDecodeError as exc:
            self.invalid_packets += 1

            if self.strict_json:
                raise ValueError(
                    f"Invalid GSI JSON: {exc}"
                ) from exc

            self._emit(
                "invalid_packet",
                {
                    "line": line,
                    "error": str(exc),
                },
            )

            return False

        if not isinstance(data, dict):
            self.invalid_packets += 1

            if self.strict_json:
                raise ValueError("GSI packet must be a JSON object")

            self._emit(
                "invalid_packet",
                {
                    "line": line,
                    "error": "root is not an object",
                },
            )

            return False

        self.process_packet(data)

        return True

    # ========================================================

    def process_packet(self, data: dict) -> None:
        """
        Обработать уже распарсенный JSON.
        """

        self.total_packets += 1

        provider = data.get("provider") or {}

        timestamp = provider.get("timestamp")

        if timestamp is not None:
            self.last_timestamp = timestamp

        # ----------------------------------------------------
        # Automatically determine owner SteamID
        # ----------------------------------------------------

        provider_steamid = provider.get("steamid")

        if provider_steamid is not None:

            provider_steamid = str(provider_steamid)

            if self.owner_steamid is None:
                self.owner_steamid = provider_steamid

                self._emit(
                    "owner_detected",
                    {
                        "steamid": self.owner_steamid,
                    },
                )

            elif provider_steamid != self.owner_steamid:

                # Это может означать, что GSI начал приходить
                # от другого клиента.
                self._emit(
                    "provider_changed",
                    {
                        "old_steamid": self.owner_steamid,
                        "new_steamid": provider_steamid,
                    },
                )

                self.owner_steamid = provider_steamid

        # ----------------------------------------------------
        # First packet
        # ----------------------------------------------------

        if not self.connected:
            self.connected = True

            self._emit(
                "connected",
                {
                    "owner_steamid": self.owner_steamid,
                    "timestamp": timestamp,
                },
            )

        # ----------------------------------------------------
        # Process map
        # ----------------------------------------------------

        self._process_map(data)

        # ----------------------------------------------------
        # Process round
        # ----------------------------------------------------

        self._process_round(data)

        # ----------------------------------------------------
        # Process current player
        # ----------------------------------------------------

        self._process_player(data)

        # ----------------------------------------------------
        # Save packet
        # ----------------------------------------------------

        self.last_packet = deepcopy(data)

    # ========================================================
    # Map
    # ========================================================

    def _process_map(self, data: dict) -> None:

        map_data = data.get("map")

        if not isinstance(map_data, dict):
            return

        new_name = map_data.get("name")
        new_mode = map_data.get("mode")
        new_phase = map_data.get("phase")

        # ----------------------------------------------------
        # Map changed
        # ----------------------------------------------------

        if (
            self.map_name is not None
            and new_name is not None
            and new_name != self.map_name
        ):
            self._emit(
                "map_changed",
                {
                    "old_map": self.map_name,
                    "new_map": new_name,
                },
            )

        self.map_name = new_name or self.map_name
        self.map_mode = new_mode or self.map_mode

        # ----------------------------------------------------
        # Map phase
        # ----------------------------------------------------

        if new_phase != self.map_phase:

            self._emit(
                "map_phase_changed",
                {
                    "old_phase": self.map_phase,
                    "new_phase": new_phase,
                    "map": self.map_name,
                },
            )

            self.map_phase = new_phase

        # ----------------------------------------------------
        # Score
        # ----------------------------------------------------

        team_ct = map_data.get("team_ct")

        if isinstance(team_ct, dict):
            self.team_ct = deepcopy(team_ct)

        team_t = map_data.get("team_t")

        if isinstance(team_t, dict):
            self.team_t = deepcopy(team_t)

        # ----------------------------------------------------
        # Round number
        # ----------------------------------------------------

        new_round_number = map_data.get("round")

        if isinstance(new_round_number, int):

            if (
                self.round_number is not None
                and new_round_number != self.round_number
            ):
                self._emit(
                    "map_round_number_changed",
                    {
                        "old_round": self.round_number,
                        "new_round": new_round_number,
                    },
                )

            self.round_number = new_round_number

    # ========================================================
    # Round
    # ========================================================

    def _process_round(self, data: dict) -> None:

        round_data = data.get("round")

        if not isinstance(round_data, dict):
            return

        new_phase = round_data.get("phase")
        new_win_team = round_data.get("win_team")
        new_bomb = round_data.get("bomb")

        # ----------------------------------------------------
        # Round phase
        # ----------------------------------------------------

        if new_phase != self.round_phase:

            old_phase = self.round_phase

            self.round_phase = new_phase

            # ----------------------------
            # Round start
            # ----------------------------

            if new_phase in ("freezetime", "live"):

                if new_phase == "freezetime":

                    self._emit(
                        "round_start",
                        {
                            "round": self.round_number,
                            "phase": new_phase,
                        },
                    )

                    self.round_active = True

                    self.owner_round_kills = 0
                    self.owner_round_headshots = 0

            # ----------------------------
            # Round over
            # ----------------------------

            if new_phase == "over":

                self.round_active = False

                self._emit(
                    "round_end",
                    {
                        "round": self.round_number,
                        "old_phase": old_phase,
                        "win_team": new_win_team,
                        "bomb": new_bomb,
                    },
                )

        # ----------------------------------------------------
        # Win team
        # ----------------------------------------------------

        if (
            new_win_team is not None
            and new_win_team != self.round_win_team
        ):
            self.round_win_team = new_win_team

        # ----------------------------------------------------
        # Bomb
        # ----------------------------------------------------

        if new_bomb != self.bomb_state:

            old_bomb = self.bomb_state

            self.bomb_state = new_bomb

            # planted
            if new_bomb == "planted":

                self._emit(
                    "bomb_planted",
                    {
                        "old_state": old_bomb,
                        "new_state": new_bomb,
                        "round": self.round_number,
                    },
                )

            # exploded
            elif new_bomb == "exploded":

                self._emit(
                    "bomb_exploded",
                    {
                        "old_state": old_bomb,
                        "new_state": new_bomb,
                        "round": self.round_number,
                    },
                )

            # defused
            elif new_bomb == "defused":

                self._emit(
                    "bomb_defused",
                    {
                        "old_state": old_bomb,
                        "new_state": new_bomb,
                        "round": self.round_number,
                    },
                )

    # ========================================================
    # Player
    # ========================================================

    def _process_player(self, data: dict) -> None:

        player = data.get("player")

        if not isinstance(player, dict):
            return

        steamid = player.get("steamid")

        if steamid is None:
            return

        steamid = str(steamid)

        # ----------------------------------------------------
        # Detect player switch
        # ----------------------------------------------------

        previous_current = self.current_player_steamid

        if previous_current != steamid:

            # Не считаем первый пакет "переключением".
            if previous_current is not None:

                self._emit(
                    "player_switched",
                    {
                        "old_steamid": previous_current,
                        "new_steamid": steamid,
                        "old_name": self.current_player_name,
                        "new_name": player.get("name"),
                        "observer_slot": player.get("observer_slot"),
                    },
                )

            self.current_player_steamid = steamid
            self.current_player_name = player.get("name")

        # ----------------------------------------------------
        # Get/create PlayerState
        # ----------------------------------------------------

        state = self.players.get(steamid)

        if state is None:

            state = PlayerState(
                steamid=steamid
            )

            self.players[steamid] = state

            self._emit(
                "player_discovered",
                {
                    "steamid": steamid,
                    "name": player.get("name"),
                    "team": player.get("team"),
                    "observer_slot": player.get("observer_slot"),
                },
            )

        # ----------------------------------------------------
        # Process player fields
        # ----------------------------------------------------

        self._process_player_identity(
            state,
            player,
        )

        self._process_player_state(
            state,
            player,
        )

        self._process_player_stats(
            state,
            player,
        )

        self._process_weapons(
            state,
            player,
        )

        # ----------------------------------------------------
        # Save raw state
        # ----------------------------------------------------

        state.raw = deepcopy(player)

        state.timestamp = self.last_timestamp

    # ========================================================
    # Identity
    # ========================================================

    def _process_player_identity(
        self,
        state: PlayerState,
        player: dict,
    ) -> None:

        name = player.get("name")

        if (
            name is not None
            and state.name is not None
            and name != state.name
        ):
            self._emit(
                "player_name_changed",
                {
                    "steamid": state.steamid,
                    "old_name": state.name,
                    "new_name": name,
                },
            )

        if name is not None:
            state.name = name

        team = player.get("team")

        if (
            team is not None
            and state.team is not None
            and team != state.team
        ):
            self._emit(
                "player_team_changed",
                {
                    "steamid": state.steamid,
                    "old_team": state.team,
                    "new_team": team,
                },
            )

        if team is not None:
            state.team = team

        observer_slot = player.get("observer_slot")

        if observer_slot is not None:
            state.observer_slot = observer_slot

        activity = player.get("activity")

        if activity is not None:

            if activity != state.activity:

                self._emit(
                    "player_activity_changed",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "old_activity": state.activity,
                        "new_activity": activity,
                    },
                )

            state.activity = activity

    # ========================================================
    # Player state
    # ========================================================

    def _process_player_state(
        self,
        state: PlayerState,
        player: dict,
    ) -> None:

        player_state = player.get("state")

        if not isinstance(player_state, dict):
            return

        # ----------------------------------------------------
        # Health
        # ----------------------------------------------------

        new_health = player_state.get("health")
        old_health = state.health

        if new_health is not None:

            # Damage
            if (
                old_health is not None
                and new_health < old_health
                and new_health > 0
            ):
                self._emit(
                    "damage",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "old_health": old_health,
                        "new_health": new_health,
                        "damage": old_health - new_health,
                    },
                )

            # Death
            if (
                old_health is not None
                and old_health > 0
                and new_health <= 0
            ):
                self._handle_death(state)

            # Spawn / revival
            if (
                old_health is not None
                and old_health <= 0
                and new_health > 0
            ):
                self._emit(
                    "player_spawned",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "health": new_health,
                    },
                )

                state.dead = False

            if new_health > 0:
                state.was_alive = True

            state.health = new_health

        # ----------------------------------------------------
        # Armor
        # ----------------------------------------------------

        new_armor = player_state.get("armor")

        if new_armor is not None:

            if (
                state.armor is not None
                and new_armor != state.armor
            ):
                self._emit(
                    "armor_changed",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "old_armor": state.armor,
                        "new_armor": new_armor,
                    },
                )

            state.armor = new_armor

        # ----------------------------------------------------
        # Helmet
        # ----------------------------------------------------

        helmet = player_state.get("helmet")

        if helmet is not None:
            state.helmet = helmet

        # ----------------------------------------------------
        # Flash
        # ----------------------------------------------------

        flashed = player_state.get("flashed")

        if flashed is not None:

            if (
                state.flashed is not None
                and flashed != state.flashed
            ):
                self._emit(
                    "flash_changed",
                    {
                        "steamid": state.steamid,
                        "old": state.flashed,
                        "new": flashed,
                    },
                )

            state.flashed = flashed

        # ----------------------------------------------------
        # Smoke
        # ----------------------------------------------------

        smoked = player_state.get("smoked")

        if smoked is not None:
            state.smoked = smoked

        # ----------------------------------------------------
        # Burning
        # ----------------------------------------------------

        burning = player_state.get("burning")

        if burning is not None:
            state.burning = burning

        # ----------------------------------------------------
        # Money
        # ----------------------------------------------------

        money = player_state.get("money")

        if money is not None:

            if (
                state.money is not None
                and money != state.money
            ):
                self._emit(
                    "money_changed",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "old_money": state.money,
                        "new_money": money,
                        "delta": money - state.money,
                    },
                )

            state.money = money

        # ----------------------------------------------------
        # Equipment value
        # ----------------------------------------------------

        equip_value = player_state.get("equip_value")

        if equip_value is not None:
            state.equip_value = equip_value

        # ----------------------------------------------------
        # Round kills
        # ----------------------------------------------------

        round_kills = player_state.get("round_kills")

        if round_kills is not None:

            if (
                state.round_kills is not None
                and round_kills > state.round_kills
            ):
                delta = round_kills - state.round_kills

                self._emit(
                    "kill",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "count": delta,
                        "total_round_kills": round_kills,
                        "round": self.round_number,
                    },
                )

                if state.steamid == self.owner_steamid:
                    self.owner_round_kills += delta

            state.round_kills = round_kills

        # ----------------------------------------------------
        # Round headshots
        # ----------------------------------------------------

        round_killhs = player_state.get("round_killhs")

        if round_killhs is not None:

            if (
                state.round_killhs is not None
                and round_killhs > state.round_killhs
            ):
                delta = round_killhs - state.round_killhs

                self._emit(
                    "headshot",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "count": delta,
                        "total_round_headshots": round_killhs,
                        "round": self.round_number,
                    },
                )

                if state.steamid == self.owner_steamid:
                    self.owner_round_headshots += delta

            state.round_killhs = round_killhs

    # ========================================================
    # Player statistics
    # ========================================================

    def _process_player_stats(
        self,
        state: PlayerState,
        player: dict,
    ) -> None:

        stats = player.get("match_stats")

        if not isinstance(stats, dict):
            return

        # ----------------------------------------------------
        # Kills
        # ----------------------------------------------------

        kills = stats.get("kills")

        if kills is not None:

            if (
                state.kills is not None
                and kills > state.kills
            ):
                self._emit(
                    "match_kill",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "count": kills - state.kills,
                        "kills": kills,
                    },
                )

            state.kills = kills

        # ----------------------------------------------------
        # Assists
        # ----------------------------------------------------

        assists = stats.get("assists")

        if assists is not None:

            if (
                state.assists is not None
                and assists > state.assists
            ):
                self._emit(
                    "assist",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "count": assists - state.assists,
                        "assists": assists,
                    },
                )

            state.assists = assists

        # ----------------------------------------------------
        # Deaths
        # ----------------------------------------------------

        deaths = stats.get("deaths")

        if deaths is not None:

            if (
                state.deaths is not None
                and deaths > state.deaths
            ):
                self._emit(
                    "match_death",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "count": deaths - state.deaths,
                        "deaths": deaths,
                    },
                )

            state.deaths = deaths

        # ----------------------------------------------------
        # MVP
        # ----------------------------------------------------

        mvps = stats.get("mvps")

        if mvps is not None:

            if (
                state.mvps is not None
                and mvps > state.mvps
            ):
                self._emit(
                    "mvp",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "count": mvps - state.mvps,
                        "mvps": mvps,
                    },
                )

            state.mvps = mvps

        # ----------------------------------------------------
        # Score
        # ----------------------------------------------------

        score = stats.get("score")

        if score is not None:

            if (
                state.score is not None
                and score != state.score
            ):
                self._emit(
                    "score_changed",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "old_score": state.score,
                        "new_score": score,
                        "delta": score - state.score,
                    },
                )

            state.score = score

    # ========================================================
    # Weapons
    # ========================================================

    def _process_weapons(
        self,
        state: PlayerState,
        player: dict,
    ) -> None:

        weapons = player.get("weapons")

        if not isinstance(weapons, dict):
            return

        # ----------------------------------------------------
        # Detect removed weapons
        # ----------------------------------------------------

        old_weapon_keys = set(state.weapons.keys())
        new_weapon_keys = set(weapons.keys())

        for weapon_id in old_weapon_keys - new_weapon_keys:

            self._emit(
                "weapon_removed",
                {
                    "steamid": state.steamid,
                    "name": state.name,
                    "weapon_id": weapon_id,
                    "weapon": deepcopy(
                        state.weapons[weapon_id]
                    ),
                },
            )

        # ----------------------------------------------------
        # Process every weapon
        # ----------------------------------------------------

        for weapon_id, weapon in weapons.items():

            if not isinstance(weapon, dict):
                continue

            old_weapon = state.weapons.get(weapon_id)

            if old_weapon is None:

                self._emit(
                    "weapon_added",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "weapon_id": weapon_id,
                        "weapon": deepcopy(weapon),
                    },
                )

                state.weapons[weapon_id] = deepcopy(weapon)

                continue

            # ------------------------------------------------
            # Weapon name
            # ------------------------------------------------

            old_name = old_weapon.get("name")
            new_name = weapon.get("name")

            if (
                new_name is not None
                and old_name is not None
                and new_name != old_name
            ):
                self._emit(
                    "weapon_changed",
                    {
                        "steamid": state.steamid,
                        "weapon_id": weapon_id,
                        "old_weapon": old_name,
                        "new_weapon": new_name,
                    },
                )

            # ------------------------------------------------
            # Weapon state
            # ------------------------------------------------

            old_weapon_state = old_weapon.get("state")
            new_weapon_state = weapon.get("state")

            if (
                new_weapon_state is not None
                and new_weapon_state != old_weapon_state
            ):

                self._emit(
                    "weapon_state_changed",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "weapon_id": weapon_id,
                        "weapon": new_name,
                        "old_state": old_weapon_state,
                        "new_state": new_weapon_state,
                    },
                )

                # --------------------------------------------
                # Active weapon
                # --------------------------------------------

                if new_weapon_state == "active":

                    self._emit(
                        "weapon_equipped",
                        {
                            "steamid": state.steamid,
                            "name": state.name,
                            "weapon_id": weapon_id,
                            "weapon": new_name,
                        },
                    )

                # --------------------------------------------
                # Reload
                # --------------------------------------------

                if new_weapon_state == "reloading":

                    self._emit(
                        "weapon_reload",
                        {
                            "steamid": state.steamid,
                            "name": state.name,
                            "weapon_id": weapon_id,
                            "weapon": new_name,
                        },
                    )

            # ------------------------------------------------
            # Ammo
            # ------------------------------------------------

            old_clip = old_weapon.get("ammo_clip")
            new_clip = weapon.get("ammo_clip")

            if (
                old_clip is not None
                and new_clip is not None
            ):

                if new_clip < old_clip:

                    self._emit(
                        "weapon_shot",
                        {
                            "steamid": state.steamid,
                            "name": state.name,
                            "weapon_id": weapon_id,
                            "weapon": new_name,
                            "old_ammo": old_clip,
                            "new_ammo": new_clip,
                            "shots": old_clip - new_clip,
                        },
                    )

                elif new_clip > old_clip:

                    self._emit(
                        "weapon_ammo_changed",
                        {
                            "steamid": state.steamid,
                            "name": state.name,
                            "weapon_id": weapon_id,
                            "weapon": new_name,
                            "old_ammo": old_clip,
                            "new_ammo": new_clip,
                        },
                    )

            # ------------------------------------------------
            # Reserve ammo
            # ------------------------------------------------

            old_reserve = old_weapon.get("ammo_reserve")
            new_reserve = weapon.get("ammo_reserve")

            if (
                old_reserve is not None
                and new_reserve is not None
                and new_reserve != old_reserve
            ):
                self._emit(
                    "weapon_reserve_changed",
                    {
                        "steamid": state.steamid,
                        "name": state.name,
                        "weapon_id": weapon_id,
                        "weapon": new_name,
                        "old_reserve": old_reserve,
                        "new_reserve": new_reserve,
                    },
                )

            # ------------------------------------------------
            # Save
            # ------------------------------------------------

            state.weapons[weapon_id] = deepcopy(weapon)

    # ========================================================
    # Death
    # ========================================================

    def _handle_death(
        self,
        state: PlayerState,
    ) -> None:

        # ----------------------------------------------------
        # Защита от повторного death-события.
        #
        # GSI может прислать один и тот же snapshot
        # несколько раз.
        # ----------------------------------------------------

        if state.dead:
            return

        state.dead = True

        self._emit(
            "player_death",
            {
                "steamid": state.steamid,
                "name": state.name,
                "team": state.team,
                "round": self.round_number,
                "kills": state.kills,
                "deaths": state.deaths,
                "round_kills": state.round_kills,
                "round_killhs": state.round_killhs,
            },
        )

    # ========================================================
    # Events
    # ========================================================

    def _emit(
        self,
        event: str,
        data: dict,
    ) -> None:

        # Добавляем общую информацию ко всем событиям.

        event_data = deepcopy(data)

        event_data.setdefault(
            "timestamp",
            self.last_timestamp,
        )

        event_data.setdefault(
            "owner_steamid",
            self.owner_steamid,
        )

        event_data.setdefault(
            "current_player_steamid",
            self.current_player_steamid,
        )

        event_data.setdefault(
            "map",
            self.map_name,
        )

        event_data.setdefault(
            "round",
            self.round_number,
        )

        if self.event_callback is not None:

            try:
                self.event_callback(
                    event,
                    event_data,
                )

            except Exception:
                # Ошибка callback не должна ломать GSI.
                pass

    # ========================================================
    # Convenience API
    # ========================================================

    def get_owner(self) -> Optional[PlayerState]:
        """
        Вернуть состояние игрока, который запустил GSI.
        """

        if self.owner_steamid is None:
            return None

        return self.players.get(
            self.owner_steamid
        )

    # --------------------------------------------------------

    def get_current_player(self) -> Optional[PlayerState]:
        """
        Вернуть player, который находится в текущем GSI packet.
        """

        if self.current_player_steamid is None:
            return None

        return self.players.get(
            self.current_player_steamid
        )

    # --------------------------------------------------------

    def get_player(
        self,
        steamid: str,
    ) -> Optional[PlayerState]:

        return self.players.get(
            str(steamid)
        )

    # --------------------------------------------------------

    def is_spectating_other_player(self) -> bool:
        """
        True, если текущий player отличается от owner.

        ВАЖНО:
        Это означает, что GSI сейчас сообщает состояние
        другого player, но не обязательно, что игрок
        действительно "наблюдает" его в обычном смысле.
        """

        if (
            self.owner_steamid is None
            or self.current_player_steamid is None
        ):
            return False

        return (
            self.current_player_steamid
            != self.owner_steamid
        )

    # --------------------------------------------------------

    def get_snapshot(self) -> dict:
        """
        Удобный сериализуемый snapshot текущего состояния.
        """

        return {
            "owner_steamid": self.owner_steamid,

            "current_player": (
                self.current_player_steamid
            ),

            "current_player_name": (
                self.current_player_name
            ),

            "spectating_other": (
                self.is_spectating_other_player()
            ),

            "map": {
                "name": self.map_name,
                "mode": self.map_mode,
                "phase": self.map_phase,
                "round": self.round_number,
                "round_phase": self.round_phase,
                "round_win_team": self.round_win_team,
                "bomb": self.bomb_state,
            },

            "team_ct": deepcopy(self.team_ct),
            "team_t": deepcopy(self.team_t),

            "players": {
                steamid: {
                    "steamid": player.steamid,
                    "name": player.name,
                    "team": player.team,
                    "observer_slot": player.observer_slot,
                    "activity": player.activity,

                    "health": player.health,
                    "armor": player.armor,
                    "helmet": player.helmet,

                    "flashed": player.flashed,
                    "smoked": player.smoked,
                    "burning": player.burning,

                    "money": player.money,
                    "equip_value": player.equip_value,

                    "round_kills": player.round_kills,
                    "round_killhs": player.round_killhs,

                    "kills": player.kills,
                    "assists": player.assists,
                    "deaths": player.deaths,
                    "mvps": player.mvps,
                    "score": player.score,

                    "weapons": deepcopy(
                        player.weapons
                    ),

                    "dead": player.dead,
                }
                for steamid, player
                in self.players.items()
            },
        }

    # --------------------------------------------------------

    def reset(self) -> None:
        """
        Полностью сбросить состояние.
        """

        self.__init__(
            owner_steamid=self.owner_steamid,
            event_callback=self.event_callback,
            strict_json=self.strict_json,
        )