"""
constants.py — Platform detection, optional library imports, warna GUI, path.
Semua modul lain import dari sini agar tidak ada duplikasi try/except import.
"""

import sys
import platform
import logging

log = logging.getLogger(__name__)

# ─── Platform ─────────────────────────────────────────────────────────────────
SYS_PLATFORM = platform.system()
IS_LINUX     = SYS_PLATFORM == "Linux"
IS_WINDOWS   = SYS_PLATFORM == "Windows"

# ─── Optional hardware libraries ──────────────────────────────────────────────
HAS_MODBUS = False
HAS_REQUESTS = HAS_JWT = HAS_PIL = HAS_SERIAL_TOOLS = False

# Modbus RTU via USB RS485
ModbusSerialClient = None
try:
    from pymodbus.client import ModbusSerialClient   # type: ignore
    HAS_MODBUS = True
    log.info("pymodbus tersedia")
except ImportError:
    log.warning("pymodbus tidak tersedia — data sensor disimulasi")

# pyserial port listing
list_ports = None
try:
    import serial.tools.list_ports as list_ports     # type: ignore
    HAS_SERIAL_TOOLS = True
except ImportError:
    pass

# HTTP
req_lib = None
try:
    import requests as req_lib                       # type: ignore
    HAS_REQUESTS = True
except ImportError:
    log.warning("requests tidak tersedia")

# JWT
pyjwt = None
try:
    import jwt as pyjwt                              # type: ignore
    HAS_JWT = True
except ImportError:
    log.warning("PyJWT tidak tersedia")

# Pillow (logo image)
Image = ImageTk = None
try:
    from PIL import Image, ImageTk                   # type: ignore
    HAS_PIL = True
except ImportError:
    log.warning("Pillow tidak tersedia — logo tidak ditampilkan")

# ─── Sucofindo Color Theme ─────────────────────────────────────────────────────
# Biru primary    #0077C8 — warna globe utama logo
# Biru tua        #003087 — latar header
# Biru muda       #00AEEF — aksen ringan
# Hijau-teal      #009B77 — warna centang logo (status "terhubung")
C: dict = {
    "primary":      "#0077C8",
    "primary_dark": "#003087",
    "light_blue":   "#00AEEF",
    "teal":         "#009B77",
    "red":          "#DC3545",
    "bg":           "#EBF3FB",
    "card":         "#FFFFFF",
    "text":         "#1A1A2E",
    "text_muted":   "#5A6A7A",
    "border":       "#C0D4E8",
    "log_bg":       "#0D1B2A",
    "log_fg":       "#A8D8F0",
}

# ─── Path logo ────────────────────────────────────────────────────────────────
from pathlib import Path
LOGO_FILE = Path(__file__).parent / "PT_Sucofindo.png"
