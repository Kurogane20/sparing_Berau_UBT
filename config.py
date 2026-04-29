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
    # Serial / Modbus
    # use_rs485_hat = True  → pakai UART HAT via GPIO (kontrol DE/RE via RTS)
    # use_rs485_hat = False → pakai USB RS485 adapter (CH340/CP210x/FT232/PL2303)
    #
    # Port UART Orange Pi (tergantung model):
    #   3B (digunakan): /dev/ttyS1 (40-pin GPIO)  atau  /dev/ttyS7
    #   Zero / Zero 2 : /dev/ttyS1  atau  /dev/ttyS3
    #   5 / 5 Plus    : /dev/ttyS0  atau  /dev/ttyS3  atau  /dev/ttyS5
    "serial_port":            "/dev/ttyUSB0" if IS_LINUX else "COM3",
    "baud_rate":              9600,
    "use_rs485_hat":          False,   # True = RS485 HAT via UART GPIO
    "rs485_hat_port":         "/dev/ttyS1",   # Orange Pi 3B — pin 8/10 (TX/RX)
    "slave_id_ph":            2,
    "slave_id_tss":           10,
    "slave_id_debit":         1,
    "slave_id_dust":          3,     # RK300-02 dust sensor
    "slave_id_noise":         4,     # Sound level meter — address 0, reg[0]/10 = dB
    "slave_id_temp":          5,     # Sensor suhu air

    # Server 1 — Mitra Mutiara
    # uid1          : UID untuk data MURNI (tanpa batas min/max)
    # uid1_processed: UID untuk data yang sudah di-filter min/max
    "server_url1":            "http://127.0.0.1:8000/api/post-data",
    "secret_key_url1":        "http://127.0.0.1:8000/api/get-key",
    "uid1":                   "test",   # UID tipe logger Internal (data raw)
    "uid1_klhk":             "",       # UID tipe logger KLHK (data processed), kosong = pakai uid1
    "tl_water":               1,       # tipe_logger Internal (data raw)
    "tl_klhk":                2,       # tipe_logger KLHK (data processed)
    "logger_internal":        True,    # aktifkan pengiriman data raw (Internal)
    "logger_klhk":            False,   # aktifkan pengiriman data processed (KLHK)

    # Log server
    "log_url":                "http://13.215.182.25/api/log",
    "log_key":                "sparing",

    # ID rekaman video/CCTV — disertakan di setiap pengiriman Server 1
    "link_video_id":          "",

    # Server 2 — Kemenlhk
    "server2_enabled":        True,    # False = nonaktifkan pengiriman ke Server 2
    "server_url2":            "https://sparing.kemenlh.go.id/api/send-hourly",
    "secret_key_url2":        "https://sparing.kemenlh.go.id/api/secret-sensor",
    "uid2":                   "tesuid2",

    # Timing
    "interval_seconds":       120,
    "data_batch_size":        30,

    # Pilihan sensor aktif (True = tampil + kirim ke server)
    "sensor_ph_enabled":      True,
    "sensor_tss_enabled":     True,
    "sensor_debit_enabled":   True,
    "sensor_dust_enabled":    True,   # PM2.5, PM10, PM100 (RK300-02)
    "sensor_noise_enabled":   True,   # Kebisingan dB (Sound Level Meter)
    "sensor_temp_enabled":    True,   # Suhu air (°C)
    "offset_ph":              0.0,
    "offset_tss":             0.0,
    "offset_debit":           0.0,
    "offset_pm100":           0.0,
    "offset_noise":           0.0,
    "offset_temp":            0.0,

    # Faktor perhitungan PM2.5 dan PM10 dari TSP (PM100)
    # PM2.5 = random(pm25_factor_min, pm25_factor_max) × TSP
    # PM10  = random(pm10_factor_min, pm10_factor_max) × TSP
    "pm25_factor_min":        0.1,
    "pm25_factor_max":        0.2,
    "pm10_factor_min":        0.3,
    "pm10_factor_max":        0.4,

    # Floating Mode (tanpa hardware) — aktif otomatis jika pymodbus tidak tersedia
    "simulate_sensors":       not HAS_MODBUS,

    # Batas nilai acak saat floating mode aktif
    "sim_ph_min":             7.5,
    "sim_ph_max":             7.6,
    "sim_tss_min":            80.0,
    "sim_tss_max":            90.0,
    "sim_debit_min":          0.01,
    "sim_debit_max":          0.10,
    "sim_tsp_min":            30.0,
    "sim_tsp_max":            200.0,
    "sim_noise_min":          40.0,
    "sim_noise_max":          80.0,
    "sim_temp_min":           25.0,
    "sim_temp_max":           30.0,

    # PIN untuk membuka tampilan data processed & batas Server 2
    "secret_pin":             "1234",

    # ── Batas min/max — berlaku untuk data processed (Server 1 processed & Server 2) ──
    # Server 1 (uid1)            : data MURNI, tidak ada batas.
    # Server 1 (uid1_processed)  : data difilter — nilai diluar batas → 0.
    # Server 2 (uid2)            : data difilter — nilai diluar batas → 0.
    # Nilai di luar [min, max] tapi dalam [float_min, float_max] → random di zona ambang.
    # float_min harus ≤ limit_min;  float_max harus ≥ limit_max.
    # Zona float berada DI DALAM [min, max].
    # Saat value < min → random dalam [float_lo_min, float_lo_max].
    # Saat value > max → random dalam [float_hi_min, float_hi_max].
    # float_lo_* harus ≥ min;  float_hi_* harus ≤ max.
    "limit_ph_min":              0.0,
    "limit_ph_max":              14.0,
    "limit_ph_float_lo_min":     0.3,
    "limit_ph_float_lo_max":     0.6,
    "limit_ph_float_hi_min":     13.4,
    "limit_ph_float_hi_max":     13.7,
    "limit_tss_min":             0.0,
    "limit_tss_max":             500.0,   # mg/L
    "limit_tss_float_lo_min":    1.0,
    "limit_tss_float_lo_max":    5.0,
    "limit_tss_float_hi_min":    495.0,
    "limit_tss_float_hi_max":    499.0,
    "limit_debit_min":           0.0,
    "limit_debit_max":           100.0,   # m³/s
    "limit_debit_float_lo_min":  0.1,
    "limit_debit_float_lo_max":  1.0,
    "limit_debit_float_hi_min":  99.0,
    "limit_debit_float_hi_max":  99.9,
    "limit_pm25_min":            0.0,
    "limit_pm25_max":            1000.0,  # ug/m³
    "limit_pm25_float_lo_min":   1.0,
    "limit_pm25_float_lo_max":   5.0,
    "limit_pm25_float_hi_min":   995.0,
    "limit_pm25_float_hi_max":   999.0,
    "limit_pm10_min":            0.0,
    "limit_pm10_max":            1000.0,  # ug/m³
    "limit_pm10_float_lo_min":   1.0,
    "limit_pm10_float_lo_max":   5.0,
    "limit_pm10_float_hi_min":   995.0,
    "limit_pm10_float_hi_max":   999.0,
    "limit_pm100_min":           0.0,
    "limit_pm100_max":           1000.0,  # ug/m³
    "limit_pm100_float_lo_min":  1.0,
    "limit_pm100_float_lo_max":  5.0,
    "limit_pm100_float_hi_min":  995.0,
    "limit_pm100_float_hi_max":  999.0,
    "limit_noise_min":           0.0,
    "limit_noise_max":           120.0,   # dB
    "limit_noise_float_lo_min":  1.0,
    "limit_noise_float_lo_max":  3.0,
    "limit_noise_float_hi_min":  117.0,
    "limit_noise_float_hi_max":  119.0,
    "limit_temp_min":            0.0,
    "limit_temp_max":            50.0,    # °C
    "limit_temp_float_lo_min":   0.5,
    "limit_temp_float_lo_max":   1.0,
    "limit_temp_float_hi_min":   49.0,
    "limit_temp_float_hi_max":   49.5,
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
            for pattern in (
                "/dev/ttyUSB*",   # USB RS485 adapter
                "/dev/ttyACM*",   # USB CDC
                "/dev/ttyS*",     # UART onboard (ttyS0-ttyS9, Orange Pi)
                "/dev/ttyAMA*",   # UART ARM (Raspberry Pi)
                "/dev/serial*",   # symlink serial
            ):
                ports.extend(sorted(glob.glob(pattern)))
            # Hapus duplikat, pertahankan urutan
            seen: set = set()
            ports = [p for p in ports if not (p in seen or seen.add(p))]
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
