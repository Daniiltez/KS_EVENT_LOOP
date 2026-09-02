# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules


# =========================================================
# Пути
# =========================================================

PROJECT_DIR = Path(SPECPATH)

MAIN_FILE = PROJECT_DIR / "main2.py"

# VLC, установленный в Windows
VLC_DIR = Path(r"C:\Program Files\VideoLAN\VLC")

if not VLC_DIR.exists():
    VLC_DIR = Path(r"C:\Program Files (x86)\VideoLAN\VLC")

if not VLC_DIR.exists():
    raise RuntimeError(
        "Не найден VLC.\n"
        "Проверь путь VLC_DIR в main2.spec."
    )


# =========================================================
# VLC Python module
# =========================================================

hiddenimports = collect_submodules("vlc")


# =========================================================
# VLC DLL
# =========================================================

binaries = [
    (
        str(VLC_DIR / "libvlc.dll"),
        "vlc"
    ),
    (
        str(VLC_DIR / "libvlccore.dll"),
        "vlc"
    ),
]


# =========================================================
# VLC plugins
# =========================================================

datas = [
    (
        str(VLC_DIR / "plugins"),
        "vlc/plugins"
    ),
]


# =========================================================
# Папка с видео
# =========================================================

VID_DIR = PROJECT_DIR / "vid"

if VID_DIR.exists():
    datas.append(
        (
            str(VID_DIR),
            "vid"
        )
    )


# =========================================================
# Flask templates / прочие ресурсы
# =========================================================

# Если в будущем появятся дополнительные папки,
# их можно добавить сюда.
#
# Например:
#
# TEMPLATE_DIR = PROJECT_DIR / "templates"
#
# if TEMPLATE_DIR.exists():
#     datas.append(
#         (
#             str(TEMPLATE_DIR),
#             "templates"
#         )
#     )


# =========================================================
# Analysis
# =========================================================

a = Analysis(
    [str(MAIN_FILE)],

    pathex=[
        str(PROJECT_DIR)
    ],

    binaries=binaries,

    datas=datas,

    hiddenimports=hiddenimports,

    hookspath=[],

    hooksconfig={},

    runtime_hooks=[],

    excludes=[],

    noarchive=False,
)


# =========================================================
# PYZ
# =========================================================

pyz = PYZ(
    a.pure,
    a.zipped_data
)


# =========================================================
# EXE
# =========================================================

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,

    [],

    name="main2",

    debug=False,

    bootloader_ignore_signals=False,

    strip=False,

    upx=False,

    console=True,

    disable_windowed_traceback=False,

    argv_emulation=False,

    target_arch=None,

    codesign_identity=None,

    entitlements_file=None,
)