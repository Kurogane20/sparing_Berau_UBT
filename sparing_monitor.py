#!/usr/bin/env python3
"""
SPARING Monitor - Sistem Monitoring Kualitas Air
PT Sucofindo
Kompatibel: Raspberry Pi, Orange Pi, Windows
"""

import os
import sys
import json
import time
import struct
import random
import threading
import platform
import logging
import queue
from datetime import datetime
from pathlib import Path
from dataclasses import dataclass
from typing import Optional, List

# ─── Setup Logging ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("sparing.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)

# ─── Platform Detection ────────────────────────────────────────────────────────
SYS_PLATFORM = platform.system()
IS_LINUX    = SYS_PLATFORM == "Linux"
IS_WINDOWS  = SYS_PLATFORM == "Windows"

HAS_GPIO = HAS_SPI = HAS_MODBUS = HAS_REQUESTS = HAS_JWT = HAS_PIL = False

if IS_LINUX:
    try:
        import RPi.GPIO as GPIO
        import spidev
        HAS_GPIO = HAS_SPI = True
        log.info("RPi.GPIO + spidev tersedia")
    except ImportError:
        try:
            import OPi.GPIO as GPIO   # type: ignore
            HAS_GPIO = True
            log.info("OPi.GPIO tersedia")
        except ImportError:
            log.warning("GPIO tidak tersedia — mode simulasi ADC aktif")

try:
    from pymodbus.client import ModbusSerialClient   # type: ignore
    HAS_MODBUS = True
    log.info("pymodbus tersedia")
except ImportError:
    log.warning("pymodbus tidak tersedia — data sensor disimulasi")

# pyserial (sudah terinstall bersama pymodbus)
try:
    import serial.tools.list_ports as list_ports   # type: ignore
    HAS_SERIAL_TOOLS = True
except ImportError:
    HAS_SERIAL_TOOLS = False

try:
    import requests as req_lib   # type: ignore
    HAS_REQUESTS = True
except ImportError:
    log.warning("requests tidak tersedia")

try:
    import jwt as pyjwt   # type: ignore
    HAS_JWT = True
except ImportError:
    log.warning("PyJWT tidak tersedia")

try:
    from PIL import Image, ImageTk   # type: ignore
    HAS_PIL = True
except ImportError:
    log.warning("Pillow tidak tersedia — logo tidak ditampilkan")

import tkinter as tk
from tkinter import ttk

# ─── Sucofindo Color Theme ─────────────────────────────────────────────────────
C = {
    "primary":      "#0077C8",   # Biru globe utama
    "primary_dark": "#003087",   # Biru tua
    "light_blue":   "#00AEEF",   # Biru muda
    "teal":         "#009B77",   # Hijau-teal (centang logo)
    "red":          "#DC3545",
    "bg":           "#EBF3FB",   # Latar belakang ringan
    "card":         "#FFFFFF",
    "text":         "#1A1A2E",
    "text_muted":   "#5A6A7A",
    "border":       "#C0D4E8",
    "log_bg":       "#0D1B2A",
    "log_fg":       "#A8D8F0",
}

LOGO_FILE = Path(__file__).parent / "PT_Sucofindo.png"

# ─── Default Configuration ─────────────────────────────────────────────────────
DEFAULT_CONFIG: dict = {
    # Serial / Modbus
    "serial_port":       "/dev/ttyUSB0" if IS_LINUX else "COM3",
    "baud_rate":         9600,
    "slave_id_ph":       2,
    "slave_id_tss":      10,
    "slave_id_debit":    1,
    # Server 1 (Mitra Mutiara — dengan arus & tegangan)
    "server_url1":       "https://sparing.mitramutiara.co.id/api/post-data",
    "secret_key_url1":   "https://sparing.mitramutiara.co.id/api/get-key",
    "uid1":              "AGM03",
    # Server 2 (Kemenlhk — tanpa arus & tegangan)
    "server_url2":       "https://sparing.kemenlh.go.id/api/send-hourly",
    "secret_key_url2":   "https://sparing.kemenlh.go.id/api/secret-sensor",
    "uid2":              "tesuid2",
    # Timing
    "interval_seconds":  120,
    "data_batch_size":   30,
    # Sensor offset
    "offset_ph":         0.0,
    "offset_tss":        0.0,
    "offset_debit":      0.0,
    # ACS712 (30 A model)
    "acs712_sensitivity": 0.066,
    "acs712_vref":        3.3,
    "acs712_offset":      1.65,
    "adc_resolution":     1023,   # MCP3008 = 10-bit
    "adc_channel_current": 0,
    "adc_channel_voltage": 1,
    "voltage_divider_ratio": 5.0,
    # Misc
    "simulate_sensors":  not HAS_MODBUS,
}

CONFIG_FILE      = Path("config.json")
DATA_BUFFER_FILE = Path("data_buffer.json")


# ─── USB RS485 Port Scanner ────────────────────────────────────────────────────
def scan_serial_ports() -> List[str]:
    """Kembalikan daftar port serial yang tersedia di sistem."""
    ports: List[str] = []
    if HAS_SERIAL_TOOLS:
        for p in list_ports.comports():
            ports.append(p.device)
    if not ports:
        # Fallback manual
        if IS_LINUX:
            for pattern in ("/dev/ttyUSB*", "/dev/ttyACM*", "/dev/ttyS*"):
                import glob
                ports.extend(sorted(glob.glob(pattern)))
        else:
            ports = [f"COM{i}" for i in range(1, 17)]
    return ports


def detect_usb_rs485() -> Optional[str]:
    """Coba deteksi otomatis port USB RS485 (VID umum CH340/CP210x/FT232)."""
    if not HAS_SERIAL_TOOLS:
        return None
    USB_RS485_VIDS = {0x1A86, 0x10C4, 0x0403, 0x067B, 0x04D8}  # CH340, CP210x, FTDI, Prolific, MCP
    for p in list_ports.comports():
        if p.vid in USB_RS485_VIDS:
            log.info(f"USB RS485 terdeteksi: {p.device}  [{p.description}]")
            return p.device
    return None


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            return {**DEFAULT_CONFIG, **saved}
        except Exception as e:
            log.error(f"Gagal membaca config.json: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Gagal menyimpan config: {e}")


# ─── Data Model ───────────────────────────────────────────────────────────────
@dataclass
class SensorReading:
    timestamp: float = 0.0
    ph:        float = 0.0
    tss:       float = 0.0
    debit:     float = 0.0
    current:   float = 0.0
    voltage:   float = 0.0


# ─── ADC Reader via MCP3008 (SPI) ─────────────────────────────────────────────
class ADCReader:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._spi = None
        if HAS_SPI:
            try:
                self._spi = spidev.SpiDev()   # type: ignore
                self._spi.open(0, 0)
                self._spi.max_speed_hz = 1_350_000
                log.info("MCP3008 SPI terinisialisasi")
            except Exception as e:
                log.warning(f"SPI gagal: {e}")

    def _read_channel(self, ch: int) -> int:
        if self._spi is None:
            return random.randint(1750, 2100)
        try:
            resp = self._spi.xfer2([1, (8 + ch) << 4, 0])
            return ((resp[1] & 3) << 8) | resp[2]
        except Exception:
            return 0

    def read_current(self) -> float:
        raw   = self._read_channel(self.cfg["adc_channel_current"])
        volt  = (raw * self.cfg["acs712_vref"]) / self.cfg["adc_resolution"]
        amps  = (volt - self.cfg["acs712_offset"]) / self.cfg["acs712_sensitivity"]
        return round(amps if abs(amps) >= 0.1 else 0.0, 3)

    def read_voltage(self) -> float:
        raw   = self._read_channel(self.cfg["adc_channel_voltage"])
        volt  = (raw * self.cfg["acs712_vref"]) / self.cfg["adc_resolution"]
        return round(volt * self.cfg["voltage_divider_ratio"], 3)

    def close(self) -> None:
        if self._spi:
            self._spi.close()


# ─── Modbus / Sensor Reader ────────────────────────────────────────────────────
class SensorReader:
    def __init__(self, cfg: dict):
        self.cfg      = cfg
        self._mb      = None
        self._port_ok = False
        self._adc     = ADCReader(cfg)
        self._connect()

    def _connect(self) -> None:
        if not HAS_MODBUS or self.cfg.get("simulate_sensors"):
            return

        port = self.cfg["serial_port"]

        # Jika port belum dikonfigurasi / tidak ada, coba auto-deteksi USB RS485
        if not port or port in ("COM3", "/dev/ttyUSB0"):
            detected = detect_usb_rs485()
            if detected:
                port = detected
                self.cfg["serial_port"] = port
                save_config(self.cfg)

        try:
            self._mb = ModbusSerialClient(
                port=port,
                baudrate=self.cfg["baud_rate"],
                stopbits=1,
                bytesize=8,
                parity="N",
                timeout=1,
                # USB RS485: DE/RE dikelola otomatis oleh adapter, tidak butuh GPIO
                rtscts=False,
                dsrdtr=False,
                xonxoff=False,
            )
            if self._mb.connect():
                log.info(f"USB RS485 terhubung — port: {port}  baud: {self.cfg['baud_rate']}")
                self._port_ok = True
            else:
                log.warning(f"Gagal membuka port {port}")
                self._mb = None
                self._port_ok = False
        except Exception as e:
            log.error(f"USB RS485 init error: {e}")
            self._mb = None
            self._port_ok = False

    def reconnect(self) -> bool:
        """Tutup dan buka ulang koneksi Modbus (dipanggil dari GUI)."""
        if self._mb:
            try:
                self._mb.close()
            except Exception:
                pass
            self._mb = None
        self._connect()
        return self._port_ok

    # ── pH (slave 2, register 0-1, nilai = reg[1] / 100) ──────────────────
    def _read_ph(self) -> float:
        if self._mb is None:
            return round(random.uniform(6.0, 8.0), 2)
        try:
            r = self._mb.read_holding_registers(0, 2, slave=self.cfg["slave_id_ph"])
            if not r.isError():
                raw = r.registers[1] / 100.0
                return self._offset_ph(raw, self.cfg["offset_ph"])
        except Exception as e:
            log.error(f"Baca pH gagal: {e}")
        return round(random.uniform(6.0, 8.0), 2)

    # ── TSS (slave 10, register 0-4, float format CDAB dari reg[3],reg[2]) ─
    def _read_tss(self) -> float:
        if self._mb is None:
            return round(random.uniform(60.0, 90.0), 2)
        try:
            r = self._mb.read_holding_registers(0, 5, slave=self.cfg["slave_id_tss"])
            if not r.isError():
                c = r.registers[3]
                d = r.registers[2]
                combined = (c << 16) | d
                tss = struct.unpack("f", struct.pack("I", combined))[0]
                return round(tss - self.cfg["offset_tss"], 3)
        except Exception as e:
            log.error(f"Baca TSS gagal: {e}")
        return round(random.uniform(60.0, 90.0), 2)

    # ── Debit (slave 1, 30 register, double ABCD dari reg[15-18]) ─────────
    def _read_debit(self) -> float:
        if self._mb is None:
            return round(random.uniform(0.010, 0.030), 5)
        try:
            r = self._mb.read_holding_registers(0, 30, slave=self.cfg["slave_id_debit"])
            if not r.isError():
                a, b, c, d = (r.registers[15], r.registers[16],
                              r.registers[17], r.registers[18])
                combined = (a << 48) | (b << 32) | (c << 16) | d
                debit = struct.unpack("d", struct.pack("Q", combined))[0]
                return round(debit - self.cfg["offset_debit"], 5)
        except Exception as e:
            log.error(f"Baca Debit gagal: {e}")
        return round(random.uniform(0.010, 0.030), 5)

    def read_all(self) -> SensorReading:
        reading = SensorReading(timestamp=time.time())
        reading.ph      = self._read_ph();    time.sleep(0.1)
        reading.tss     = self._read_tss();   time.sleep(0.1)
        reading.debit   = self._read_debit()
        reading.current = self._adc.read_current()
        reading.voltage = self._adc.read_voltage()
        return reading

    @staticmethod
    def _offset_ph(value: float, offset: float) -> float:
        return min(round(value + offset, 2), 14.0)

    def close(self) -> None:
        if self._mb:
            self._mb.close()
        self._adc.close()


# ─── Network Manager ──────────────────────────────────────────────────────────
class NetworkManager:
    def __init__(self, cfg: dict):
        self.cfg         = cfg
        self.secret_key1 = ""
        self.secret_key2 = ""
        self.keys_fetched = False

    def check_internet(self) -> bool:
        if not HAS_REQUESTS:
            return False
        try:
            r = req_lib.get("http://www.google.com", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def _fetch_key(self, url: str) -> Optional[str]:
        if not HAS_REQUESTS:
            return None
        try:
            r = req_lib.get(url, timeout=10)
            if r.status_code == 200:
                return r.text.strip()
        except Exception as e:
            log.error(f"Fetch key gagal ({url}): {e}")
        return None

    def fetch_all_keys(self) -> None:
        k1 = self._fetch_key(self.cfg["secret_key_url1"])
        self.secret_key1 = k1 if k1 else "sparing1"
        if not k1:
            log.warning("Secret key 1 default digunakan")

        k2 = self._fetch_key(self.cfg["secret_key_url2"])
        self.secret_key2 = k2 if k2 else "sparing2"
        if not k2:
            log.warning("Secret key 2 default digunakan")

        self.keys_fetched = True

    def _make_jwt(self, uid: str, key: str, batch: List[SensorReading],
                  include_power: bool) -> str:
        if not key or not HAS_JWT:
            return ""
        rows = []
        for r in batch:
            entry: dict = {
                "datetime": int(r.timestamp),
                "pH":       round(r.ph,    3),
                "tss":      round(r.tss,   3),
                "debit":    round(r.debit, 5),
                "cod":      0,
                "nh3n":     0,
            }
            if include_power:
                entry["current"] = round(r.current, 3)
                entry["voltage"] = round(r.voltage, 3)
            rows.append(entry)
        payload = {"uid": uid, "data": rows}
        try:
            return pyjwt.encode(payload, key, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT encode error: {e}")
            return ""

    def create_jwt1(self, batch: List[SensorReading]) -> str:
        return self._make_jwt(self.cfg["uid1"], self.secret_key1, batch, True)

    def create_jwt2(self, batch: List[SensorReading]) -> str:
        return self._make_jwt(self.cfg["uid2"], self.secret_key2, batch, False)

    def post(self, url: str, body: str) -> bool:
        if not HAS_REQUESTS:
            return False
        try:
            r = req_lib.post(url, data=body,
                             headers={"Content-Type": "application/json"},
                             timeout=30)
            log.info(f"POST {url} → HTTP {r.status_code}")
            return r.status_code in (200, 201)
        except Exception as e:
            log.error(f"POST gagal {url}: {e}")
            return False


# ─── Offline Data Storage ──────────────────────────────────────────────────────
class DataStorage:
    def save(self, jwt1: str, jwt2: str) -> None:
        entries = self._load()
        entries.append({"jwt1": jwt1, "jwt2": jwt2, "ts": time.time()})
        self._write(entries)
        log.info(f"Buffer offline: {len(entries)} batch tersimpan")

    def flush(self, net: NetworkManager) -> int:
        entries = self._load()
        if not entries:
            return 0
        remaining = []
        sent = 0
        for e in entries:
            ok1 = net.post(net.cfg["server_url1"],
                           json.dumps({"token": e.get("jwt1", "")}))
            ok2 = net.post(net.cfg["server_url2"],
                           json.dumps({"token": e.get("jwt2", "")}))
            if ok1 and ok2:
                sent += 1
            else:
                remaining.append(e)
        self._write(remaining)
        if sent:
            log.info(f"{sent} batch dari buffer berhasil dikirim ulang")
        return sent

    def count(self) -> int:
        return len(self._load())

    def _load(self) -> list:
        if DATA_BUFFER_FILE.exists():
            try:
                with open(DATA_BUFFER_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _write(self, entries: list) -> None:
        try:
            with open(DATA_BUFFER_FILE, "w", encoding="utf-8") as f:
                json.dump(entries, f)
        except Exception as e:
            log.error(f"Buffer write error: {e}")


# ─── GUI ───────────────────────────────────────────────────────────────────────
class SparingGUI:
    def __init__(self, root: tk.Tk, app: "SparingApp"):
        self.root = root
        self.app  = app
        self.cfg  = app.cfg
        self._sensor_vars: dict  = {}
        self._conn_labels: dict  = {}
        self._setup_window()
        self._setup_styles()
        self._build()
        self._tick_clock()

    # ── Window setup ──────────────────────────────────────────────────────────
    def _setup_window(self) -> None:
        self.root.title("SPARING Monitor — PT Sucofindo")
        self.root.configure(bg=C["bg"])
        if IS_WINDOWS:
            try:
                self.root.state("zoomed")
            except Exception:
                self.root.geometry("1280x720")
        else:
            try:
                self.root.attributes("-zoomed", True)
            except Exception:
                self.root.geometry("1280x720")

    def _setup_styles(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure("TProgressbar",
                    troughcolor=C["border"],
                    background=C["primary"],
                    thickness=12)

    # ── Build all widgets ─────────────────────────────────────────────────────
    def _build(self) -> None:
        self._build_header()
        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=14, pady=8)
        self._build_left(body)
        self._build_right(body)
        self._build_footer()

    # ── Header ────────────────────────────────────────────────────────────────
    def _build_header(self) -> None:
        hdr = tk.Frame(self.root, bg=C["primary_dark"], height=68)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        # Logo
        if HAS_PIL and LOGO_FILE.exists():
            try:
                img = Image.open(LOGO_FILE).resize((118, 54), Image.LANCZOS)
                self._logo = ImageTk.PhotoImage(img)
                tk.Label(hdr, image=self._logo,
                         bg=C["primary_dark"]).pack(side="left", padx=14, pady=7)
            except Exception:
                pass

        tk.Label(hdr, text="SISTEM PEMANTAUAN KUALITAS AIR (SPARING)",
                 bg=C["primary_dark"], fg="white",
                 font=("Segoe UI", 15, "bold")).pack(side="left", padx=6)

        # Clock — right side
        right_box = tk.Frame(hdr, bg=C["primary_dark"])
        right_box.pack(side="right", padx=18)

        self._date_var  = tk.StringVar()
        self._clock_var = tk.StringVar()
        tk.Label(right_box, textvariable=self._date_var,
                 bg=C["primary_dark"], fg="#A8C8E8",
                 font=("Segoe UI", 10)).pack(anchor="e")
        tk.Label(right_box, textvariable=self._clock_var,
                 bg=C["primary_dark"], fg=C["light_blue"],
                 font=("Segoe UI", 16, "bold")).pack(anchor="e")

    # ── Left panel ────────────────────────────────────────────────────────────
    def _build_left(self, parent: tk.Frame) -> None:
        left = tk.Frame(parent, bg=C["bg"])
        left.pack(side="left", fill="both", expand=True)

        # Sensor cards row
        cards = tk.Frame(left, bg=C["bg"])
        cards.pack(fill="x", pady=(0, 8))

        defs = [
            ("pH",        "ph",     "",      C["primary_dark"]),
            ("TSS",       "tss",    "mg/L",  C["primary"]),
            ("DEBIT",     "debit",  "m³/s",  C["light_blue"]),
            ("ARUS",      "arus",   "A",     C["teal"]),
            ("TEGANGAN",  "volt",   "V",     C["primary"]),
        ]
        for col, (lbl, key, unit, color) in enumerate(defs):
            self._sensor_card(cards, lbl, key, unit, color).grid(
                row=0, column=col, padx=5, pady=4, sticky="nsew")
            cards.columnconfigure(col, weight=1)

        # Info row
        info_card = self._card(left, "INFO PENGIRIMAN DATA", C["primary"])
        info_card.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(info_card, bg=C["card"])
        inner.pack(fill="x", padx=14, pady=8)

        self._count_var   = tk.StringVar(value="0 / 30")
        self._last_tx_var = tk.StringVar(value="—")
        self._buf_var     = tk.StringVar(value="0")
        self._progress    = tk.DoubleVar(value=0)

        self._info_row(inner, "Data Terkumpul :", self._count_var, C["primary_dark"])
        self._info_row(inner, "Kirim Terakhir :", self._last_tx_var, C["primary"])
        self._info_row(inner, "Buffer Offline  :", self._buf_var, C["teal"], " batch")
        ttk.Progressbar(inner, variable=self._progress,
                        maximum=30, style="TProgressbar").pack(
            fill="x", pady=(8, 0))

        # Log
        log_card = self._card(left, "LOG AKTIVITAS", C["primary_dark"])
        log_card.pack(fill="both", expand=True)
        self._log_txt = tk.Text(
            log_card, height=9, state="disabled",
            font=("Consolas", 9), bg=C["log_bg"], fg=C["log_fg"],
            relief="flat", padx=8, pady=6, wrap="word",
        )
        self._log_txt.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        sb = ttk.Scrollbar(self._log_txt, command=self._log_txt.yview)
        self._log_txt.configure(yscrollcommand=sb.set)

    # ── Right panel ───────────────────────────────────────────────────────────
    def _build_right(self, parent: tk.Frame) -> None:
        right = tk.Frame(parent, bg=C["bg"], width=270)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)

        # Connection status
        conn_card = self._card(right, "STATUS KONEKSI", C["primary"])
        conn_card.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(conn_card, bg=C["card"])
        inner.pack(fill="x", padx=14, pady=8)

        for key, label in [
            ("rs485",    "RS485 USB"),
            ("internet", "Internet"),
            ("server1",  "Server 1"),
            ("server2",  "Server 2"),
        ]:
            var = tk.StringVar(value="Mengecek...")
            lbl = self._status_row(inner, label, var)
            self._conn_labels[key] = (var, lbl)

        # Port info + reconnect button
        self._port_var = tk.StringVar(value=self.cfg.get("serial_port", "—"))
        port_row = tk.Frame(inner, bg=C["card"])
        port_row.pack(fill="x", pady=(6, 0))
        tk.Label(port_row, text="Port :", bg=C["card"], fg=C["text_muted"],
                 font=("Segoe UI", 9), width=10, anchor="w").pack(side="left")
        tk.Label(port_row, textvariable=self._port_var, bg=C["card"],
                 fg=C["primary_dark"], font=("Consolas", 9, "bold")).pack(side="left")

        btn_row = tk.Frame(inner, bg=C["card"])
        btn_row.pack(fill="x", pady=(6, 2))
        tk.Button(btn_row, text="↻ Hubungkan Ulang",
                  command=self._reconnect_rs485,
                  bg=C["primary"], fg="white",
                  font=("Segoe UI", 9, "bold"),
                  relief="flat", cursor="hand2", pady=4).pack(side="left", fill="x", expand=True, padx=(0, 3))
        tk.Button(btn_row, text="⌕ Scan Port",
                  command=self._scan_ports_dialog,
                  bg=C["primary_dark"], fg="white",
                  font=("Segoe UI", 9, "bold"),
                  relief="flat", cursor="hand2", pady=4).pack(side="left", fill="x", expand=True)

        # Offset calibration
        cal_card = self._card(right, "OFFSET KALIBRASI", C["primary_dark"])
        cal_card.pack(fill="x", pady=(0, 8))
        inner2 = tk.Frame(cal_card, bg=C["card"])
        inner2.pack(fill="x", padx=14, pady=8)

        self._off_ph    = tk.DoubleVar(value=self.cfg["offset_ph"])
        self._off_tss   = tk.DoubleVar(value=self.cfg["offset_tss"])
        self._off_debit = tk.DoubleVar(value=self.cfg["offset_debit"])

        self._offset_row(inner2, "Offset pH   :", self._off_ph,    "offset_ph")
        self._offset_row(inner2, "Offset TSS  :", self._off_tss,   "offset_tss")
        self._offset_row(inner2, "Offset Debit:", self._off_debit, "offset_debit")

        # Settings button
        tk.Button(right, text="⚙  Pengaturan Koneksi",
                  command=self._open_settings,
                  bg=C["primary_dark"], fg="white",
                  font=("Segoe UI", 10, "bold"),
                  relief="flat", cursor="hand2", pady=7).pack(
            fill="x", pady=(8, 0))

    # ── Footer / status bar ───────────────────────────────────────────────────
    def _build_footer(self) -> None:
        bar = tk.Frame(self.root, bg=C["primary_dark"], height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)
        self._statusbar_var = tk.StringVar(value="Siap")
        tk.Label(bar, textvariable=self._statusbar_var,
                 bg=C["primary_dark"], fg="#A8C8E8",
                 font=("Segoe UI", 9)).pack(side="left", padx=12, pady=4)
        mode = "SIMULASI" if self.cfg.get("simulate_sensors") else "LIVE"
        port = self.cfg.get("serial_port", "—")
        tk.Label(bar, text=f"Mode: {mode}  |  Port: {port}  |  Platform: {SYS_PLATFORM}",
                 bg=C["primary_dark"], fg=C["light_blue"],
                 font=("Segoe UI", 9)).pack(side="right", padx=12, pady=4)

    # ── Widget helpers ────────────────────────────────────────────────────────
    def _card(self, parent, title: str, accent: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=C["card"],
                         highlightbackground=C["border"], highlightthickness=1)
        tk.Frame(outer, bg=accent, height=4).pack(fill="x")
        tk.Label(outer, text=title, bg=C["card"], fg=accent,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=12, pady=(5, 1))
        return outer

    def _sensor_card(self, parent, label: str, key: str,
                     unit: str, color: str) -> tk.Frame:
        card = tk.Frame(parent, bg=C["card"],
                        highlightbackground=C["border"], highlightthickness=1)
        tk.Frame(card, bg=color, height=5).pack(fill="x")
        tk.Label(card, text=label, bg=C["card"], fg=C["text_muted"],
                 font=("Segoe UI", 9, "bold")).pack(pady=(8, 0))
        var = tk.StringVar(value="—")
        self._sensor_vars[key] = var
        tk.Label(card, textvariable=var, bg=C["card"], fg=color,
                 font=("Segoe UI", 28, "bold")).pack(pady=(0, 0))
        tk.Label(card, text=unit, bg=C["card"], fg=C["text_muted"],
                 font=("Segoe UI", 9)).pack(pady=(0, 10))
        return card

    def _info_row(self, parent, label: str, var: tk.StringVar,
                  fg: str, suffix: str = "") -> None:
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, bg=C["card"], fg=C["text_muted"],
                 font=("Segoe UI", 9), anchor="w", width=16).pack(side="left")
        tk.Label(row, textvariable=var, bg=C["card"], fg=fg,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        if suffix:
            tk.Label(row, text=suffix, bg=C["card"], fg=C["text_muted"],
                     font=("Segoe UI", 9)).pack(side="left")

    def _status_row(self, parent, label: str, var: tk.StringVar) -> tk.Label:
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=3)
        tk.Label(row, text=f"{label} :", bg=C["card"], fg=C["text_muted"],
                 font=("Segoe UI", 10), width=10, anchor="w").pack(side="left")
        lbl = tk.Label(row, textvariable=var, bg=C["card"],
                       fg=C["text_muted"], font=("Segoe UI", 10, "bold"))
        lbl.pack(side="left")
        return lbl

    def _offset_row(self, parent, label: str, var: tk.DoubleVar,
                    cfg_key: str) -> None:
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label, bg=C["card"], fg=C["text_muted"],
                 font=("Segoe UI", 10), anchor="w", width=14).pack(side="left")
        spin = tk.Spinbox(row, textvariable=var, from_=-999, to=999,
                          increment=0.1, format="%.2f", width=8,
                          font=("Segoe UI", 10), relief="flat",
                          bg=C["bg"])
        spin.pack(side="left", padx=(4, 0))

        def _apply(event=None, k=cfg_key, v=var):
            self.cfg[k] = round(v.get(), 3)
            save_config(self.cfg)

        spin.bind("<Return>",   _apply)
        spin.bind("<FocusOut>", _apply)

    # ── Clock ─────────────────────────────────────────────────────────────────
    def _tick_clock(self) -> None:
        now = datetime.now()
        self._clock_var.set(now.strftime("%H:%M:%S"))
        self._date_var.set(now.strftime("%d %B %Y"))
        self.root.after(1000, self._tick_clock)

    # ── Public update methods (called from main thread via root.after) ─────────
    def update_sensors(self, r: SensorReading) -> None:
        self._sensor_vars["ph"].set(f"{r.ph:.2f}")
        self._sensor_vars["tss"].set(f"{r.tss:.2f}")
        self._sensor_vars["debit"].set(f"{r.debit:.4f}")
        self._sensor_vars["arus"].set(f"{r.current:.2f}")
        self._sensor_vars["volt"].set(f"{r.voltage:.2f}")

    def update_count(self, n: int, total: int = 30) -> None:
        self._count_var.set(f"{n} / {total}")
        self._progress.set(n)

    def update_last_tx(self, ts: float) -> None:
        self._last_tx_var.set(datetime.fromtimestamp(ts).strftime("%d/%m %H:%M:%S"))

    def update_buffer(self, n: int) -> None:
        self._buf_var.set(str(n))

    def update_connection(self, key: str, ok: bool) -> None:
        var, lbl = self._conn_labels[key]
        if ok:
            var.set("● Terhubung")
            lbl.configure(fg=C["teal"])
        else:
            var.set("● Terputus")
            lbl.configure(fg=C["red"])

    def log(self, msg: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        full = f"[{ts}]  {msg}\n"
        self._log_txt.configure(state="normal")
        self._log_txt.insert("end", full)
        self._log_txt.see("end")
        self._log_txt.configure(state="disabled")
        self._statusbar_var.set(f"[{ts}] {msg}")

    # ── Reconnect RS485 ───────────────────────────────────────────────────────
    def _reconnect_rs485(self) -> None:
        self.log("Menghubungkan ulang USB RS485...")
        self.update_connection("rs485", False)

        def _do():
            ok = self.app.sensor_rdr.reconnect() if self.app.sensor_rdr else False
            port = self.cfg.get("serial_port", "—")
            self.root.after(0, self.update_connection, "rs485", ok)
            self.root.after(0, self._port_var.set, port)
            msg = f"RS485 {'terhubung' if ok else 'GAGAL'} — {port}"
            self.root.after(0, self.log, msg)

        threading.Thread(target=_do, daemon=True).start()

    # ── Scan port dialog ──────────────────────────────────────────────────────
    def _scan_ports_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Scan Port USB RS485")
        win.configure(bg=C["bg"])
        win.geometry("420x320")
        win.grab_set()

        tk.Frame(win, bg=C["primary_dark"], height=4).pack(fill="x")
        tk.Label(win, text="PORT SERIAL YANG TERSEDIA",
                 bg=C["primary_dark"], fg="white",
                 font=("Segoe UI", 11, "bold")).pack(fill="x", ipady=7)

        frame = tk.Frame(win, bg=C["bg"], padx=16, pady=10)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text="Pilih port USB RS485 Anda:",
                 bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 10)).pack(anchor="w")

        listbox = tk.Listbox(frame, font=("Consolas", 11),
                             bg=C["card"], fg=C["primary_dark"],
                             selectbackground=C["primary"],
                             selectforeground="white",
                             relief="solid", bd=1, height=8)
        listbox.pack(fill="both", expand=True, pady=8)

        info_var = tk.StringVar(value="")
        tk.Label(frame, textvariable=info_var, bg=C["bg"],
                 fg=C["text_muted"], font=("Segoe UI", 9),
                 wraplength=380, justify="left").pack(anchor="w")

        def _refresh():
            listbox.delete(0, "end")
            ports = scan_serial_ports()
            if HAS_SERIAL_TOOLS:
                detail = {p.device: p.description for p in list_ports.comports()}
            else:
                detail = {}
            for port in ports:
                desc = detail.get(port, "")
                listbox.insert("end", f"  {port}   {desc}")
            if not ports:
                listbox.insert("end", "  (tidak ada port terdeteksi)")
            info_var.set(f"{len(ports)} port ditemukan")

        def _apply():
            sel = listbox.curselection()
            if not sel:
                return
            line = listbox.get(sel[0]).strip()
            port = line.split()[0]
            self.cfg["serial_port"] = port
            save_config(self.cfg)
            self._port_var.set(port)
            self.log(f"Port diubah ke: {port}")
            win.destroy()
            self._reconnect_rs485()

        _refresh()

        btn_row = tk.Frame(win, bg=C["bg"])
        btn_row.pack(pady=(0, 10))
        tk.Button(btn_row, text="↻ Refresh", command=_refresh,
                  bg=C["primary_dark"], fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  pady=5, padx=12, cursor="hand2").pack(side="left", padx=4)
        tk.Button(btn_row, text="✓ Gunakan Port Ini", command=_apply,
                  bg=C["teal"], fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  pady=5, padx=12, cursor="hand2").pack(side="left", padx=4)
        tk.Button(btn_row, text="✕ Tutup", command=win.destroy,
                  bg=C["red"], fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  pady=5, padx=12, cursor="hand2").pack(side="left", padx=4)

    # ── Settings dialog ───────────────────────────────────────────────────────
    def _open_settings(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Pengaturan")
        win.configure(bg=C["bg"])
        win.geometry("560x500")
        win.grab_set()

        tk.Frame(win, bg=C["primary_dark"], height=5).pack(fill="x")
        tk.Label(win, text="PENGATURAN KONEKSI & PERANGKAT",
                 bg=C["primary_dark"], fg="white",
                 font=("Segoe UI", 12, "bold")).pack(fill="x", ipady=8)

        canvas = tk.Canvas(win, bg=C["bg"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)

        form = tk.Frame(canvas, bg=C["bg"], padx=20, pady=10)
        canvas_win = canvas.create_window((0, 0), window=form, anchor="nw")

        def _on_resize(event):
            canvas.itemconfig(canvas_win, width=event.width)
        canvas.bind("<Configure>", _on_resize)
        form.bind("<Configure>", lambda e: canvas.configure(
            scrollregion=canvas.bbox("all")))

        entry_vars: dict = {}
        row_i = 0

        # ── Section: USB RS485 ────────────────────────────────────────────────
        tk.Label(form, text="USB RS485", bg=C["bg"], fg=C["primary_dark"],
                 font=("Segoe UI", 10, "bold")).grid(
            row=row_i, column=0, columnspan=3, sticky="w", pady=(8, 2))
        row_i += 1

        # Port — dropdown + scan button
        tk.Label(form, text="Port Serial :", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 10), anchor="w").grid(
            row=row_i, column=0, sticky="w", pady=4)
        port_var = tk.StringVar(value=self.cfg.get("serial_port", ""))
        entry_vars["serial_port"] = port_var
        ports_list = scan_serial_ports() or [self.cfg.get("serial_port", "")]
        port_combo = ttk.Combobox(form, textvariable=port_var,
                                  values=ports_list, width=22,
                                  font=("Consolas", 10))
        port_combo.grid(row=row_i, column=1, sticky="ew", padx=(10, 4), pady=4)

        def _refresh_ports():
            new_list = scan_serial_ports()
            port_combo["values"] = new_list
            if new_list:
                info_lbl.configure(
                    text=f"{len(new_list)} port ditemukan: {', '.join(new_list)}")
        tk.Button(form, text="⌕ Scan", command=_refresh_ports,
                  bg=C["primary_dark"], fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  cursor="hand2", pady=2, padx=6).grid(
            row=row_i, column=2, padx=(0, 0), pady=4)
        row_i += 1

        info_lbl = tk.Label(form, text="", bg=C["bg"], fg=C["teal"],
                            font=("Segoe UI", 8), anchor="w")
        info_lbl.grid(row=row_i, column=1, columnspan=2, sticky="w", padx=(10, 0))
        row_i += 1

        # Baud Rate
        tk.Label(form, text="Baud Rate :", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 10), anchor="w").grid(
            row=row_i, column=0, sticky="w", pady=4)
        baud_var = tk.StringVar(value=str(self.cfg.get("baud_rate", 9600)))
        entry_vars["baud_rate"] = baud_var
        baud_combo = ttk.Combobox(form, textvariable=baud_var,
                                  values=["1200","2400","4800","9600","19200","38400","57600","115200"],
                                  width=10, font=("Consolas", 10))
        baud_combo.grid(row=row_i, column=1, sticky="w", padx=(10, 0), pady=4)
        row_i += 1

        # ── Section: ID Sensor ────────────────────────────────────────────────
        tk.Label(form, text="ID Slave Sensor (Modbus)", bg=C["bg"], fg=C["primary_dark"],
                 font=("Segoe UI", 10, "bold")).grid(
            row=row_i, column=0, columnspan=3, sticky="w", pady=(10, 2))
        row_i += 1

        for label, key in [("Slave ID pH  :", "slave_id_ph"),
                            ("Slave ID TSS :", "slave_id_tss"),
                            ("Slave ID Debit:", "slave_id_debit")]:
            tk.Label(form, text=label, bg=C["bg"], fg=C["text"],
                     font=("Segoe UI", 10), anchor="w").grid(
                row=row_i, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            entry_vars[key] = v
            tk.Entry(form, textvariable=v, font=("Segoe UI", 10),
                     width=8, relief="solid", bd=1).grid(
                row=row_i, column=1, sticky="w", padx=(10, 0), pady=3)
            row_i += 1

        # ── Section: Server ───────────────────────────────────────────────────
        tk.Label(form, text="Server & Identitas", bg=C["bg"], fg=C["primary_dark"],
                 font=("Segoe UI", 10, "bold")).grid(
            row=row_i, column=0, columnspan=3, sticky="w", pady=(10, 2))
        row_i += 1

        for label, key in [
            ("UID 1 :",           "uid1"),
            ("UID 2 :",           "uid2"),
            ("Server URL 1 :",    "server_url1"),
            ("Secret Key URL 1:", "secret_key_url1"),
            ("Server URL 2 :",    "server_url2"),
            ("Secret Key URL 2:", "secret_key_url2"),
        ]:
            tk.Label(form, text=label, bg=C["bg"], fg=C["text"],
                     font=("Segoe UI", 10), anchor="w").grid(
                row=row_i, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            entry_vars[key] = v
            tk.Entry(form, textvariable=v, font=("Segoe UI", 10),
                     width=36, relief="solid", bd=1).grid(
                row=row_i, column=1, columnspan=2, sticky="ew", padx=(10, 0), pady=3)
            row_i += 1

        form.columnconfigure(1, weight=1)

        def _save():
            int_keys = {"baud_rate", "slave_id_ph", "slave_id_tss", "slave_id_debit"}
            for key, v in entry_vars.items():
                raw = v.get().strip()
                try:
                    self.cfg[key] = int(raw) if key in int_keys else raw
                except (ValueError, TypeError):
                    self.cfg[key] = raw
            save_config(self.cfg)
            self._port_var.set(self.cfg.get("serial_port", "—"))
            self.log("Pengaturan disimpan")
            win.destroy()
            # Reconnect otomatis setelah simpan
            self._reconnect_rs485()

        btn = tk.Frame(win, bg=C["bg"])
        btn.pack(pady=8)
        tk.Button(btn, text="  Simpan & Hubungkan  ", command=_save,
                  bg=C["teal"], fg="white", font=("Segoe UI", 10, "bold"),
                  relief="flat", pady=6, cursor="hand2").pack(side="left", padx=6)
        tk.Button(btn, text="  Batal  ", command=win.destroy,
                  bg=C["red"], fg="white", font=("Segoe UI", 10, "bold"),
                  relief="flat", pady=6, cursor="hand2").pack(side="left", padx=6)


# ─── Main Application ──────────────────────────────────────────────────────────
class SparingApp:
    def __init__(self) -> None:
        self.cfg         = load_config()
        self.sensor_rdr  = None
        self.net         = NetworkManager(self.cfg)
        self.storage     = DataStorage()
        self.batch: List[SensorReading] = []
        self.last_tx     = 0.0
        self._running    = True
        self._q: queue.Queue = queue.Queue()

    def start(self) -> None:
        # Init sensors
        try:
            self.sensor_rdr = SensorReader(self.cfg)
        except Exception as e:
            log.warning(f"SensorReader init gagal (simulasi aktif): {e}")
            self.sensor_rdr = None

        # Build GUI
        self.root = tk.Tk()
        self.gui  = SparingGUI(self.root, self)

        # Tampilkan status RS485 awal setelah GUI siap
        def _post_init():
            ok = bool(self.sensor_rdr and self.sensor_rdr._port_ok)
            self.gui.update_connection("rs485", ok)
            port = self.cfg.get("serial_port", "—")
            msg = (f"USB RS485 terhubung pada {port}" if ok
                   else f"USB RS485 tidak terdeteksi — port: {port}")
            self.gui.log(msg)
            if not ok and not self.cfg.get("simulate_sensors"):
                self.gui.log("→ Klik  ⌕ Scan Port  untuk mencari port USB RS485 Anda")

        self.root.after(500, _post_init)

        # Background threads
        threading.Thread(target=self._sensor_loop,  daemon=True, name="sensor").start()
        threading.Thread(target=self._network_loop, daemon=True, name="network").start()

        # Pump log queue into GUI
        self._pump_log()

        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self.root.mainloop()

    # ── Log pump (runs on GUI thread via after) ────────────────────────────────
    def _pump_log(self) -> None:
        while not self._q.empty():
            try:
                self.gui.log(self._q.get_nowait())
            except queue.Empty:
                break
        self.root.after(150, self._pump_log)

    def _log(self, msg: str) -> None:
        log.info(msg)
        self._q.put(msg)

    # ── Sensor reading loop ────────────────────────────────────────────────────
    def _sensor_loop(self) -> None:
        batch_size = self.cfg["data_batch_size"]
        interval   = self.cfg["interval_seconds"]
        time.sleep(2)  # beri waktu GUI load

        while self._running:
            try:
                use_hw = bool(self.sensor_rdr and self.sensor_rdr._port_ok)
                r = (self.sensor_rdr.read_all() if use_hw else self._simulate())

                # Update status RS485 berdasarkan hasil baca
                port_ok = bool(self.sensor_rdr and self.sensor_rdr._port_ok)
                self.root.after(0, self.gui.update_connection, "rs485", port_ok)

                self.batch.append(r)
                n = len(self.batch)
                mode_tag = "" if use_hw else "[SIM] "

                self._log(
                    f"{mode_tag}Data {n}/{batch_size} — "
                    f"pH={r.ph:.2f}  TSS={r.tss:.2f} mg/L  "
                    f"Debit={r.debit:.4f} m³/s  "
                    f"I={r.current:.2f} A  V={r.voltage:.2f} V"
                )
                self.root.after(0, self.gui.update_sensors, r)
                self.root.after(0, self.gui.update_count, n, batch_size)

                if n >= batch_size:
                    self._send_batch()
                    self.batch.clear()
                    self.root.after(0, self.gui.update_count, 0, batch_size)

            except Exception as e:
                self._log(f"[ERROR] sensor loop: {e}")
                self.root.after(0, self.gui.update_connection, "rs485", False)

            time.sleep(interval)

    # ── Network monitoring loop ────────────────────────────────────────────────
    def _network_loop(self) -> None:
        time.sleep(3)
        while self._running:
            try:
                ok = self.net.check_internet()
                self.root.after(0, self.gui.update_connection, "internet", ok)

                if ok and not self.net.keys_fetched:
                    self._log("Mengambil secret key dari server...")
                    self.net.fetch_all_keys()
                    self._log("Secret key berhasil diperoleh")
            except Exception as e:
                self._log(f"[ERROR] network loop: {e}")
            time.sleep(30)

    # ── Send a complete 30-reading batch ──────────────────────────────────────
    def _send_batch(self) -> None:
        batch   = list(self.batch)
        online  = self.net.check_internet()

        jwt1 = self.net.create_jwt1(batch)
        jwt2 = self.net.create_jwt2(batch)

        if not jwt1 or not jwt2:
            self._log("JWT gagal dibuat — secret key belum tersedia, data disimpan offline")
            if jwt1 or jwt2:
                self.storage.save(jwt1, jwt2)
            self.root.after(0, self.gui.update_buffer, self.storage.count())
            return

        if not online:
            self._log("Offline — data batch disimpan ke buffer")
            self.storage.save(jwt1, jwt2)
            self.root.after(0, self.gui.update_connection, "internet", False)
            self.root.after(0, self.gui.update_buffer, self.storage.count())
            return

        # Flush offline buffer first
        flushed = self.storage.flush(self.net)
        if flushed:
            self._log(f"{flushed} batch lama dari buffer berhasil dikirim ulang")

        # Send current batch
        body1 = json.dumps({"token": jwt1})
        body2 = json.dumps({"token": jwt2})
        ok1   = self.net.post(self.cfg["server_url1"], body1)
        ok2   = self.net.post(self.cfg["server_url2"], body2)

        self.root.after(0, self.gui.update_connection, "server1", ok1)
        self.root.after(0, self.gui.update_connection, "server2", ok2)

        if ok1 and ok2:
            self.last_tx = time.time()
            self.root.after(0, self.gui.update_last_tx, self.last_tx)
            self._log("✓ Data batch berhasil dikirim ke Server 1 & Server 2")
        else:
            status = f"S1={'OK' if ok1 else 'GAGAL'}  S2={'OK' if ok2 else 'GAGAL'}"
            self._log(f"Pengiriman sebagian gagal ({status}) — disimpan ke buffer")
            self.storage.save(jwt1, jwt2)

        self.root.after(0, self.gui.update_buffer, self.storage.count())

        # Refresh secret keys after each send cycle
        threading.Thread(target=self.net.fetch_all_keys, daemon=True).start()

    # ── Simulated sensor data (for testing without hardware) ──────────────────
    @staticmethod
    def _simulate() -> SensorReading:
        return SensorReading(
            timestamp = time.time(),
            ph        = round(random.uniform(6.5, 8.5), 2),
            tss       = round(random.uniform(50.0, 110.0), 2),
            debit     = round(random.uniform(0.010, 0.035), 5),
            current   = round(random.uniform(0.0, 3.5), 2),
            voltage   = round(random.uniform(11.5, 13.8), 2),
        )

    def _quit(self) -> None:
        self._running = False
        if self.sensor_rdr:
            self.sensor_rdr.close()
        self.root.destroy()


# ─── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    SparingApp().start()
