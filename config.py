"""
config.py — Konfigurasi default, load/save JSON, dan utilitas scan port USB RS485.
"""

import json
import glob
import logging
from pathlib import Path
from typing import List, Optional

from constants import IS_LINUX, HAS_MODBUS, HAS_SERIAL_TOOLS, list_ports

log = logging.getLogger(__name__)

# ─── Path file ────────────────────────────────────────────────────────────────
CONFIG_FILE      = Path("config.json")
DATA_BUFFER_FILE = Path("data_buffer.json")

# ─── Nilai default ────────────────────────────────────────────────────────────
DEFAULT_CONFIG: dict = {
    # Serial / Modbus USB RS485
    "serial_port":            "/dev/ttyUSB0" if IS_LINUX else "COM3",
    "baud_rate":              9600,
    "slave_id_ph":            2,
    "slave_id_tss":           10,
    "slave_id_debit":         1,

    # Server 1 — Mitra Mutiara
    # uid1          : UID untuk data MURNI (tanpa batas min/max)
    # uid1_processed: UID untuk data yang sudah di-filter min/max
    "server_url1":            "http://127.0.0.1:8000/api/post-data",
    "secret_key_url1":        "http://127.0.0.1:8000/api/get-key",
    "uid1":                   "test",  # ganti jika server pakai UID berbeda
    "uid1_processed":         "test_processed",   # ganti jika server pakai UID berbeda

    # Log server
    "log_url":                "http://127.0.0.1:8000/api/log",
    "log_key":                "sparing",

    # Server 2 — Kemenlhk
    "server_url2":            "https://sparing.kemenlh.go.id/api/send-hourly",
    "secret_key_url2":        "https://sparing.kemenlh.go.id/api/secret-sensor",
    "uid2":                   "tesuid2",

    # Timing
    "interval_seconds":       120,
    "data_batch_size":        30,

    # Offset kalibrasi sensor
    "offset_ph":              0.0,
    "offset_tss":             0.0,
    "offset_debit":           0.0,

    # Mode simulasi aktif jika pymodbus tidak tersedia
    "simulate_sensors":       not HAS_MODBUS,

    # PIN untuk membuka tampilan data processed & batas Server 2
    "secret_pin":             "1234",

    # ── Batas min/max — berlaku untuk data processed (Server 1 processed & Server 2) ──
    # Server 1 (uid1)            : data MURNI, tidak ada batas.
    # Server 1 (uid1_processed)  : data difilter — nilai diluar batas → 0.
    # Server 2 (uid2)            : data difilter — nilai diluar batas → 0.
    # Nilai di luar [min, max] TIDAK dipaksa ke batas (tidak statis),
    # melainkan diganti 0 agar server mengetahui data tidak valid.
    "limit_ph_min":           0.0,
    "limit_ph_max":           14.0,
    "limit_tss_min":          0.0,
    "limit_tss_max":          500.0,   # mg/L
    "limit_debit_min":        0.0,
    "limit_debit_max":        100.0,   # m³/s
}


def load_config() -> dict:
    """Baca config.json dan gabungkan dengan default."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            return {**DEFAULT_CONFIG, **saved}
        except Exception as e:
            log.error(f"Gagal membaca config.json: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    """Tulis konfigurasi ke config.json."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Gagal menyimpan config: {e}")


# ─── USB RS485 Port Utilities ─────────────────────────────────────────────────

def scan_serial_ports() -> List[str]:
    """Kembalikan semua port serial yang tersedia di sistem."""
    ports: List[str] = []
    if HAS_SERIAL_TOOLS and list_ports is not None:
        ports = [p.device for p in list_ports.comports()]
    if not ports:
        if IS_LINUX:
            for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyS*"):
                ports.extend(sorted(glob.glob(pattern)))
        else:
            ports = [f"COM{i}" for i in range(1, 17)]
    return ports


# VID chip USB-Serial yang umum dipakai pada konverter RS485
_USB_RS485_VIDS = {
    0x1A86,  # CH340 / CH341
    0x10C4,  # CP210x (Silicon Labs)
    0x0403,  # FT232 (FTDI)
    0x067B,  # PL2303 (Prolific)
    0x04D8,  # MCP2200 (Microchip)
}


def detect_usb_rs485() -> Optional[str]:
    """
    Deteksi otomatis port USB RS485 berdasarkan Vendor ID chip.
    Kembalikan device path (misal '/dev/ttyUSB0' atau 'COM5'), atau None.
    """
    if not HAS_SERIAL_TOOLS or list_ports is None:
        return None
    for p in list_ports.comports():
        if p.vid in _USB_RS485_VIDS:
            log.info(f"USB RS485 terdeteksi: {p.device}  [{p.description}]")
            return p.device
    return None
