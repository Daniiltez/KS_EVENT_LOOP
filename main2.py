from __future__ import annotations

import json
import random
import subprocess
import threading
import time
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, render_template_string, request

from cs_class import CS2GSI, PlayerState


# ============================================================
# CONFIG
# ============================================================

HOST = "127.0.0.1"
PORT = 59873

BASE_DIR = Path(__file__).resolve().parent
LOG_FILE = BASE_DIR / "log.txt"
MUSIC_DIR = BASE_DIR / "music"

# Минимальный промежуток между ивентами.
# Это защита от ситуации, когда CS2 начинает часто присылать
# пакеты со статистикой.
EVENT_COOLDOWN = 12.0

# Длительность эффектов.
REVERSE_MOUSE_DURATION = 12.0
DROP_WEAPON_DURATION = 3.0
MUSIC_DURATION = 15.0

# Шансы проверяются при обновлении соответствующей статистики.
# Значения — проценты.
EVENT_CHANCES = {
    "kill": {
        "music": 0.06,
        "drop_weapon": 0.04,
        "reverse_mouse": 0.5,
    },
    "assist": {
        "music": 0.08,
        "drop_weapon": 0.03,
        "reverse_mouse": 0.05,
    },
    "death": {
        "music": 0.10,
        "reverse_mouse": 0.04,
        "exit_game": 0.03,
    },
    "mvp": {
        "music": 0.15,
        "drop_weapon": 0.05,
        "reverse_mouse": 0.05,
    },
    "score": {
        "music": 0.05,
        "drop_weapon": 0.02,
        "reverse_mouse": 0.02,
    },
}

# Приоритеты: если одновременно подходят несколько событий,
# выбирается одно, а не несколько сразу.
EVENT_PRIORITY = (
    "exit_game",
    "drop_weapon",
    "reverse_mouse",
    "music",
)


# ============================================================
# FLASK
# ============================================================

app = Flask(__name__)


# ============================================================
# GLOBAL STATE
# ============================================================

gsi = CS2GSI(
    event_callback=None
)

state_lock = threading.RLock()


# Последнее событие для веб-интерфейса.
last_event = {
    "name": None,
    "started": 0.0,
    "duration": 0.0,
    "text": "",
}

# Время последнего запущенного ивента.
last_event_time = 0.0

# Набор уже обработанных "изменений".
# Нужен, чтобы один и тот же пакет не смог повторно запустить
# событие при повторной обработке.
processed_event_keys: set[tuple] = set()


# ============================================================
# ACTIVE EVENT MANAGER
# ============================================================

class EventManager:
    def __init__(self):
        self.lock = threading.RLock()

        self.active_event: Optional[str] = None
        self.active_until: float = 0.0

        self.reverse_mouse_until: float = 0.0
        self.music_process: Optional[subprocess.Popen] = None

    # --------------------------------------------------------

    def can_start(self) -> bool:
        now = time.monotonic()

        with self.lock:
            if self.active_event is not None:
                if now < self.active_until:
                    return False

                self._finish_current_event()

            if now - last_event_time < EVENT_COOLDOWN:
                return False

            return True

    # --------------------------------------------------------

    def start(self, event_name: str) -> bool:
        global last_event_time

        with self.lock:
            if not self.can_start():
                return False

            now = time.monotonic()

            try:
                if event_name == "music":
                    duration = self._start_music()

                elif event_name == "drop_weapon":
                    duration = self._start_drop_weapon()

                elif event_name == "reverse_mouse":
                    duration = self._start_reverse_mouse()

                elif event_name == "exit_game":
                    duration = self._start_exit_game()

                else:
                    return False

            except Exception as exc:
                print(f"[EVENT] Ошибка запуска {event_name}: {exc}")
                return False

            self.active_event = event_name
            self.active_until = now + duration

            last_event_time = now

            event_text = {
                "music": "🎵 Случайная музыка",
                "drop_weapon": "🔫 Оружие выброшено",
                "reverse_mouse": "🖱 Реверсивное управление мышью",
                "exit_game": "💀 Выход из игры",
            }.get(event_name, event_name)

            with state_lock:
                last_event.update(
                    {
                        "name": event_name,
                        "started": time.time(),
                        "duration": duration,
                        "text": event_text,
                    }
                )

            print(
                f"[EVENT] {event_text} "
                f"({duration:.1f} сек.)"
            )

            return True

    # --------------------------------------------------------

    def update(self):
        with self.lock:
            if (
                self.active_event is not None
                and time.monotonic() >= self.active_until
            ):
                self._finish_current_event()

    # --------------------------------------------------------

    def _finish_current_event(self):
        event = self.active_event

        if event == "reverse_mouse":
            self.reverse_mouse_until = 0.0

        elif event == "music":
            if self.music_process is not None:
                try:
                    self.music_process.terminate()
                except Exception:
                    pass

                self.music_process = None

        self.active_event = None
        self.active_until = 0.0

    # --------------------------------------------------------

    def _start_music(self) -> float:
        MUSIC_DIR.mkdir(exist_ok=True)

        extensions = {
            ".mp3",
            ".wav",
            ".ogg",
            ".flac",
            ".m4a",
        }

        files = [
            p for p in MUSIC_DIR.iterdir()
            if p.is_file()
            and p.suffix.lower() in extensions
        ]

        if not files:
            raise RuntimeError(
                f"В папке {MUSIC_DIR} нет музыки"
            )

        selected = random.choice(files)

        # Используем системный Media Player через PowerShell.
        # Windows Media Player/другой ассоциированный проигрыватель
        # не требуется: используется System.Windows.Media.MediaPlayer.
        ps = f"""
Add-Type -AssemblyName PresentationCore
$player = New-Object System.Windows.Media.MediaPlayer
$player.Open([Uri]::new('{selected.as_posix()}'))
$player.Play()
Start-Sleep -Seconds {int(MUSIC_DURATION)}
$player.Stop()
"""

        self.music_process = subprocess.Popen(
            [
                "powershell",
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                ps,
            ],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        print(f"[EVENT] Музыка: {selected.name}")

        return MUSIC_DURATION

    # --------------------------------------------------------

    def _start_drop_weapon(self) -> float:
        # Используем CS2-консоль через pyautogui.
        # Команда drop выбрасывает текущее активное оружие.
        #
        # Импортируем только когда событие реально запускается,
        # чтобы pyautogui не был обязательным для запуска сервера.
        import pyautogui

        pyautogui.press("g")

        return DROP_WEAPON_DURATION

    # --------------------------------------------------------

    def _start_reverse_mouse(self) -> float:
        # Само состояние хранится здесь.
        # Перехват мыши выполняется отдельным daemon-thread.
        duration = REVERSE_MOUSE_DURATION

        self.reverse_mouse_until = (
            time.monotonic() + duration
        )

        return duration

    # --------------------------------------------------------

    def _start_exit_game(self) -> float:
        # ВАЖНО:
        # Это намеренно завершает процесс CS2.
        # Ивент одноразовый: после закрытия игры GSI перестанет
        # присылать пакеты.
        subprocess.Popen(
            [
                "taskkill",
                "/IM",
                "cs2.exe",
                "/F",
            ],
            creationflags=subprocess.CREATE_NO_WINDOW,
        )

        # Небольшое окно отображения события на OBS.
        return 3.0


event_manager = EventManager()


# ============================================================
# REVERSE MOUSE
# ============================================================

# def reverse_mouse_worker():
#     """
#     Реверсирует направление движения мыши во время активного
#     reverse_mouse event.

#     Работает только на Windows.

#     Важный момент:
#     это не блокировка мыши, а изменение относительного движения.
#     """

#     try:
#         import pyautogui
#     except ImportError:
#         print(
#             "[MOUSE] pyautogui не установлен. "
#             "Реверс мыши недоступен."
#         )
#         return

#     last_x, last_y = pyautogui.position()

#     while True:
#         time.sleep(0.01)

#         with event_manager.lock:
#             until = event_manager.reverse_mouse_until

#         if until <= 0:
#             last_x, last_y = pyautogui.position()
#             continue

#         if time.monotonic() >= until:
#             with event_manager.lock:
#                 event_manager.reverse_mouse_until = 0.0
#             continue

#         try:
#             x, y = pyautogui.position()

#             dx = x - last_x
#             dy = y - last_y

#             if dx or dy:
#                 # Возвращаем курсор обратно относительно движения.
#                 pyautogui.moveRel(
#                     -dx * 2,
#                     -dy * 2,
#                     duration=0
#                 )

#                 # Сохраняем фактическую текущую позицию.
#                 last_x, last_y = pyautogui.position()

#             else:
#                 last_x, last_y = x, y

#         except Exception as exc:
#             print(f"[MOUSE] Ошибка: {exc}")
#             time.sleep(0.2)

def reverse_mouse_worker():
    """
    Открывает случайное видео из поддиректории /vid в VLC на весь экран.
    """
    import os
    import random
    import subprocess
    from pathlib import Path

    # Определяем путь к папке vid относительно текущего файла
    script_dir = Path(__file__).parent
    vid_dir = script_dir / 'vid'

    if not vid_dir.exists() or not vid_dir.is_dir():
        print("[VIDEO] Папка /vid не найдена.")
        return

    # Расширения видеофайлов, которые будем искать
    video_exts = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v'}

    # Собираем все файлы с подходящими расширениями
    videos = [
        f for f in vid_dir.iterdir()
        if f.is_file() and f.suffix.lower() in video_exts
    ]

    if not videos:
        print("[VIDEO] В папке /vid нет видеофайлов.")
        return

    chosen = random.choice(videos)
    video_path = str(chosen.absolute())

    try:
        # Запускаем VLC в полноэкранном режиме
        # --play-and-exit закроет плеер после окончания видео (опционально)
        subprocess.Popen(
            ['vlc', '--fullscreen', '--play-and-exit', video_path],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        print(f"[VIDEO] Запущено видео: {chosen.name}")
    except FileNotFoundError:
        print("[VIDEO] VLC не найден. Убедитесь, что он установлен и доступен в PATH.")
    except Exception as e:
        print(f"[VIDEO] Ошибка запуска VLC: {e}")

threading.Thread(
    target=reverse_mouse_worker,
    daemon=True,
).start()


# ============================================================
# EVENT TRIGGER
# ============================================================

def try_trigger_for_stat(event_type: str, event_data: dict):
    """
    Проверяет шанс события после изменения статистики.

    За одно статистическое изменение запускается максимум один
    ивент.
    """

    chances = EVENT_CHANCES.get(event_type)

    if not chances:
        return

    # Защита от повторной обработки одного изменения.
    timestamp = event_data.get("timestamp")

    key = (
        event_type,
        timestamp,
        event_data.get("steamid"),
        event_data.get("kills"),
        event_data.get("deaths"),
        event_data.get("assists"),
        event_data.get("mvps"),
        event_data.get("new_score"),
    )

    with state_lock:
        if key in processed_event_keys:
            return

        processed_event_keys.add(key)

        # Чтобы set не рос бесконечно.
        if len(processed_event_keys) > 5000:
            processed_event_keys.clear()

    # Нас интересует именно статистика стримера.
    steamid = event_data.get("steamid")

    if steamid != gsi.owner_steamid:
        return

    # Если сейчас уже идёт ивент — новый не запускаем.
    if not event_manager.can_start():
        return

    candidates = []

    for event_name, chance in chances.items():

        if random.random() < chance:
            candidates.append(event_name)

    if not candidates:
        return

    # Если выпало несколько — выбираем одно по приоритету.
    selected = None

    for priority_event in EVENT_PRIORITY:
        if priority_event in candidates:
            selected = priority_event
            break

    if selected is None:
        selected = random.choice(candidates)

    event_manager.start(selected)


# ============================================================
# GSI CALLBACK
# ============================================================

def on_gsi_event(event: str, data: dict):
    """
    Callback от CS2GSI.
    """

    # Нас интересует статистика именно владельца GSI.
    steamid = data.get("steamid")

    if steamid != gsi.owner_steamid:
        return

    if event == "match_kill":
        try_trigger_for_stat("kill", data)

    elif event == "assist":
        try_trigger_for_stat("assist", data)

    elif event == "match_death":
        try_trigger_for_stat("death", data)

    elif event == "mvp":
        try_trigger_for_stat("mvp", data)

    elif event == "score_changed":
        try_trigger_for_stat("score", data)


# Присваиваем callback после создания экземпляра.
gsi.event_callback = on_gsi_event


# ============================================================
# GSI ENDPOINT
# ============================================================

@app.route("/", methods=["POST"])
def gsi_endpoint():
    """
    Endpoint для CS2 GSI.
    """

    if not request.is_json:
        return jsonify(
            {
                "error": "Content-Type must be application/json"
            }
        ), 400

    data = request.get_json(silent=True)

    if not isinstance(data, dict):
        return jsonify(
            {
                "error": "Invalid JSON"
            }
        ), 400

    # --------------------------------------------------------
    # Сохраняем оригинальный пакет.
    # --------------------------------------------------------

    try:
        with LOG_FILE.open(
            "a",
            encoding="utf-8"
        ) as f:
            json.dump(
                data,
                f,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            f.write("\n")

    except OSError as exc:
        print(f"[LOG] Не удалось записать log.txt: {exc}")

    # --------------------------------------------------------
    # Передаём пакет в CS2GSI.
    # --------------------------------------------------------

    try:
        with state_lock:
            gsi.process_packet(data)

    except Exception as exc:
        print(f"[GSI] Ошибка обработки пакета: {exc}")

        return jsonify(
            {
                "error": "GSI processing error"
            }
        ), 500

    event_manager.update()

    return jsonify(
        {
            "status": "ok"
        }
    ), 200


# ============================================================
# WEB PAGE
# ============================================================

HTML = r"""
<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">

<meta
    name="viewport"
    content="width=device-width, initial-scale=1.0"
>

<title>CS2 Stats</title>

<style>
* {
    box-sizing: border-box;
}

html,
body {
    margin: 0;
    width: 100%;
    height: 100%;
    overflow: hidden;
    background: transparent;
    color: white;
    font-family: Arial, Helvetica, sans-serif;
}

#panel {
    position: absolute;
    top: 30px;
    left: 30px;

    min-width: 360px;

    padding: 18px 22px;

    border-radius: 16px;

    background: rgba(10, 10, 10, 0.86);

    border: 2px solid rgba(255, 255, 255, 0.12);

    box-shadow:
        0 10px 35px rgba(0, 0, 0, 0.45);

    transition:
        opacity 0.25s ease,
        transform 0.25s ease;
}

.title {
    font-size: 20px;
    font-weight: 800;

    margin-bottom: 4px;
}

.name {
    font-size: 15px;
    opacity: 0.65;

    margin-bottom: 14px;
}

.stats {
    display: grid;
    grid-template-columns:
        repeat(3, 1fr);

    gap: 10px;
}

.stat {
    text-align: center;

    padding: 10px;

    border-radius: 10px;

    background: rgba(255,255,255,0.06);
}

.stat-value {
    font-size: 28px;
    font-weight: 800;
}

.stat-label {
    margin-top: 2px;

    font-size: 11px;
    opacity: 0.55;

    text-transform: uppercase;
}

.extra {
    display: flex;
    justify-content: space-between;

    margin-top: 13px;

    font-size: 13px;
    opacity: 0.7;
}

.event {
    position: fixed;

    left: 50%;
    bottom: 45px;

    transform:
        translateX(-50%)
        translateY(20px);

    min-width: 320px;

    padding: 16px 22px;

    text-align: center;

    border-radius: 13px;

    background: rgba(0,0,0,0.9);

    border: 2px solid rgba(255,255,255,0.18);

    font-size: 18px;
    font-weight: 800;

    opacity: 0;

    transition:
        opacity .25s ease,
        transform .25s ease;
}

.event.visible {
    opacity: 1;

    transform:
        translateX(-50%)
        translateY(0);
}

.timer {
    margin-top: 6px;

    font-size: 13px;
    opacity: .55;
}
</style>
</head>

<body>

<div id="panel">

    <div class="title">
        CS2 — KDA
    </div>

    <div
        id="name"
        class="name"
    >
        Ожидание CS2...
    </div>

    <div class="stats">

        <div class="stat">
            <div
                id="kills"
                class="stat-value"
            >
                0
            </div>

            <div class="stat-label">
                Kills
            </div>
        </div>

        <div class="stat">
            <div
                id="deaths"
                class="stat-value"
            >
                0
            </div>

            <div class="stat-label">
                Deaths
            </div>
        </div>

        <div class="stat">
            <div
                id="assists"
                class="stat-value"
            >
                0
            </div>

            <div class="stat-label">
                Assists
            </div>
        </div>

    </div>

    <div class="extra">

        <span>
            K/D:
            <b id="kd">0.00</b>
        </span>

        <span>
            Score:
            <b id="score">0</b>
        </span>

        <span>
            MVP:
            <b id="mvp">0</b>
        </span>

    </div>

</div>


<div
    id="event"
    class="event"
>

    <div id="eventText">
    </div>

    <div
        id="eventTimer"
        class="timer"
    >
    </div>

</div>


<script>

let lastEventStarted = 0;

function number(value) {
    if (
        value === null ||
        value === undefined
    ) {
        return 0;
    }

    return Number(value);
}


async function update() {

    try {

        const response =
            await fetch(
                "/api/state",
                {
                    cache: "no-store"
                }
            );

        if (!response.ok) {
            return;
        }

        const data =
            await response.json();

        const player =
            data.player;

        if (player) {

            document
                .getElementById("name")
                .textContent =
                    player.name ||
                    player.steamid ||
                    "Игрок";

            const kills =
                number(player.kills);

            const deaths =
                number(player.deaths);

            const assists =
                number(player.assists);

            document
                .getElementById("kills")
                .textContent =
                    kills;

            document
                .getElementById("deaths")
                .textContent =
                    deaths;

            document
                .getElementById("assists")
                .textContent =
                    assists;

            const kd =
                deaths > 0
                    ? kills / deaths
                    : kills;

            document
                .getElementById("kd")
                .textContent =
                    kd.toFixed(2);

            document
                .getElementById("score")
                .textContent =
                    number(player.score);

            document
                .getElementById("mvp")
                .textContent =
                    number(player.mvps);

        } else {

            document
                .getElementById("name")
                .textContent =
                    "Ожидание CS2...";

        }

        // ----------------------------------------------------
        // Event overlay
        // ----------------------------------------------------

        const event =
            data.event;

        const eventElement =
            document.getElementById("event");

        if (
            event &&
            event.name &&
            event.started
        ) {

            if (
                event.started !==
                lastEventStarted
            ) {

                lastEventStarted =
                    event.started;

                document
                    .getElementById("eventText")
                    .textContent =
                        event.text;

                eventElement
                    .classList
                    .add("visible");
            }

            const elapsed =
                Date.now() / 1000 -
                event.started;

            const remaining =
                Math.max(
                    0,
                    event.duration -
                    elapsed
                );

            document
                .getElementById("eventTimer")
                .textContent =
                    remaining > 0
                        ? remaining.toFixed(1) + " сек."
                        : "";

            if (remaining <= 0) {
                eventElement
                    .classList
                    .remove("visible");
            }
        }

    } catch (error) {
        console.error(error);
    }
}


setInterval(
    update,
    200
);

update();

</script>

</body>
</html>
"""


# ============================================================
# WEB API
# ============================================================

@app.route("/", methods=["GET"])
def index():
    return render_template_string(HTML)


@app.route("/api/state", methods=["GET"])
def api_state():

    with state_lock:

        owner = gsi.get_owner()

        player = None

        if owner is not None:

            player = {
                "steamid": owner.steamid,
                "name": owner.name,
                "team": owner.team,

                "health": owner.health,
                "armor": owner.armor,

                "kills": owner.kills,
                "assists": owner.assists,
                "deaths": owner.deaths,
                "mvps": owner.mvps,
                "score": owner.score,

                "round_kills": owner.round_kills,
                "round_killhs": owner.round_killhs,
            }

        event_copy = dict(last_event)

        # Не держим завершённый ивент бесконечно.
        if event_copy["started"]:

            elapsed = (
                time.time()
                - event_copy["started"]
            )

            if (
                elapsed >
                event_copy["duration"] + 1
            ):
                event_copy = {
                    "name": None,
                    "started": 0.0,
                    "duration": 0.0,
                    "text": "",
                }

        return jsonify(
            {
                "connected": gsi.connected,

                "owner_steamid":
                    gsi.owner_steamid,

                "current_player":
                    gsi.current_player_steamid,

                "spectating_other":
                    gsi.is_spectating_other_player(),

                "map": {
                    "name": gsi.map_name,
                    "mode": gsi.map_mode,
                    "phase": gsi.map_phase,
                    "round": gsi.round_number,
                    "round_phase":
                        gsi.round_phase,
                    "bomb":
                        gsi.bomb_state,
                },

                "player": player,

                "event": event_copy,
            }
        )


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":

    print("=" * 60)
    print("CS2 Stream Stats")
    print("=" * 60)

    print(
        f"GSI endpoint: "
        f"http://{HOST}:{PORT}/"
    )

    print(
        f"OBS browser: "
        f"http://{HOST}:{PORT}/"
    )

    print(
        f"Music directory: "
        f"{MUSIC_DIR}"
    )

    print("=" * 60)

    app.run(
        host=HOST,
        port=PORT,
        threaded=True,
        debug=False,
        use_reloader=False,
    )
