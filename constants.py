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

# ─── Sucofindo Color Theme — Professional Dashboard ───────────────────────────
C: dict = {
    # Brand
    "primary":      "#0052CC",   # Sucofindo electric blue
    "primary_dark": "#003087",   # Sucofindo dark blue
    "accent":       "#0091D5",   # Ocean blue accent

    # Sensor card backgrounds (solid color)
    "s_ph":         "#0052CC",   # pH  — electric blue
    "s_tss":        "#0091D5",   # TSS — ocean blue
    "s_debit":      "#00897B",   # Debit — teal emerald

    # Status
    "online":       "#00A878",   # Connected / success
    "offline":      "#E53935",   # Error / disconnected
    "warning":      "#F59E0B",   # Partial / warning

    # Page & components
    "bg":           "#EFF3FB",   # Page background
    "panel":        "#FFFFFF",   # Header / panel
    "card":         "#FFFFFF",   # Card background
    "card_alt":     "#F7FAFF",   # Alternate card (slightly blue-tinted)
    "shadow":       "#D4E0F5",   # Card shadow simulation

    # Text
    "text":         "#0D1F3C",   # Primary text (very dark navy)
    "text_muted":   "#5A6E94",   # Secondary text

    # Misc
    "border":       "#DDE8F5",   # Borders & dividers
    "progress":     "#0052CC",   # Progress bar fill
    "log_bg":       "#081525",   # Terminal background
    "log_fg":       "#4FC3F7",   # Terminal text

    # Aliases kept for backward compat with any remaining references
    "teal":         "#00A878",
    "red":          "#E53935",
    "orange":       "#F59E0B",
}

# ─── Path logo ────────────────────────────────────────────────────────────────
from pathlib import Path
LOGO_FILE = Path(__file__).parent / "PT_Sucofindo.png"
