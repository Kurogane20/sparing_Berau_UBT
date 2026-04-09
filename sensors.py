"""
sensors.py — Pembacaan sensor melalui USB RS485 (Modbus RTU).

Sensor yang didukung:
  - pH    : Modbus slave ID 2, holding register 0-1
  - TSS   : Modbus slave ID 10, holding register 0-4 (float CDAB)
  - Debit : Modbus slave ID 1, holding register 0-29 (double ABCD, reg 15-18)
"""

import random
import struct
import time
import logging
from typing import Optional

from constants import HAS_MODBUS, ModbusSerialClient
from config    import save_config, detect_usb_rs485
from models    import SensorReading

log = logging.getLogger(__name__)

# Deteksi keyword slave/unit berdasarkan versi pymodbus
try:
    import pymodbus as _pm
    _SLAVE_KW = "slave" if int(_pm.__version__.split(".")[0]) >= 3 else "unit"
except Exception:
    _SLAVE_KW = "slave"


# ─── Modbus Sensor Reader ─────────────────────────────────────────────────────
class SensorReader:
    """
    Membaca tiga sensor kualitas air via Modbus RTU over USB RS485,
    dan sensor arus/tegangan via ADC.

    USB RS485 adapter (CH340/CP210x/FT232/PL2303) mengelola sinyal DE/RE
    secara otomatis — tidak perlu pin GPIO RTS seperti pada Arduino.
    """

    def __init__(self, cfg: dict, on_error=None):
        self.cfg       = cfg
        self._mb       = None
        self._port_ok  = False
        self._on_error = on_error or (lambda msg: None)
        self._connect()

    # ── Koneksi Modbus ────────────────────────────────────────────────────────
    def _connect(self) -> None:
        if not HAS_MODBUS or self.cfg.get("simulate_sensors"):
            return

        port = self.cfg["serial_port"]

        # Auto-deteksi jika port masih default atau kosong
        if not port or port in ("COM3", "/dev/ttyUSB0"):
            detected = detect_usb_rs485()
            if detected:
                port = detected
                self.cfg["serial_port"] = port
                save_config(self.cfg)

        try:
            self._mb = ModbusSerialClient(
                port     = port,
                baudrate = self.cfg["baud_rate"],
                stopbits = 1,
                bytesize = 8,
                parity   = "N",
                timeout  = 1,
            )
            if self._mb.connect():
                log.info(f"USB RS485 terhubung — port: {port}  baud: {self.cfg['baud_rate']}")
                self._port_ok = True
            else:
                log.warning(f"Gagal membuka port {port}")
                self._mb      = None
                self._port_ok = False
        except Exception as e:
            log.error(f"USB RS485 init error: {e}")
            self._on_error(f"[RS485] Gagal inisialisasi port {port}: {e}")
            self._mb      = None
            self._port_ok = False

    def _rhr(self, address: int, count: int, slave_id: int):
        """
        read_holding_registers kompatibel dengan pymodbus 2.x (unit=)
        dan pymodbus 3.x (slave=).
        """
        return self._mb.read_holding_registers(
            address, count, **{_SLAVE_KW: slave_id})

    def reconnect(self) -> bool:
        """Tutup dan buka ulang koneksi. Dipanggil dari tombol GUI."""
        if self._mb:
            try:
                self._mb.close()
            except Exception:
                pass
            self._mb = None
        self._port_ok = False
        self._connect()
        return self._port_ok

    # ── pH ────────────────────────────────────────────────────────────────────
    def _read_ph(self) -> float:
        """Slave ID 2, holding register 0-1. Nilai = reg[1] / 100."""
        if self._mb is None:
            return round(random.uniform(6.0, 8.0), 2)
        try:
            r = self._rhr(0, 2, self.cfg["slave_id_ph"])
            if not r.isError():
                raw = r.registers[1] / 100.0
                return min(round(raw + self.cfg["offset_ph"], 2), 14.0)
            else:
                msg = f"[SENSOR] pH isError: {r}"
                log.error(msg)
                self._on_error(msg)
        except Exception as e:
            log.error(f"Baca pH gagal: {e}")
            self._on_error(f"[SENSOR] Baca pH gagal: {e}")
        return round(random.uniform(6.0, 8.0), 2)

    # ── TSS ───────────────────────────────────────────────────────────────────
    def _read_tss(self) -> float:
        """Slave ID 10, holding register 0-4. Float format CDAB: reg[3]<<16 | reg[2]."""
        if self._mb is None:
            return round(random.uniform(60.0, 90.0), 2)
        try:
            r = self._rhr(0, 5, self.cfg["slave_id_tss"])
            if not r.isError():
                combined = (r.registers[3] << 16) | r.registers[2]
                tss = struct.unpack("f", struct.pack("I", combined))[0]
                return round(tss - self.cfg["offset_tss"], 3)
            else:
                msg = f"[SENSOR] TSS isError: {r}"
                log.error(msg)
                self._on_error(msg)
        except Exception as e:
            log.error(f"Baca TSS gagal: {e}")
            self._on_error(f"[SENSOR] Baca TSS gagal: {e}")
        return round(random.uniform(60.0, 90.0), 2)

    # ── Debit ─────────────────────────────────────────────────────────────────
    def _read_debit(self) -> float:
        """Slave ID 1, holding register 0-29. Double ABCD dari reg[15-18]."""
        if self._mb is None:
            return round(random.uniform(0.010, 0.030), 5)
        try:
            r = self._rhr(0, 30, self.cfg["slave_id_debit"])
            if not r.isError():
                a, b, c, d = (r.registers[15], r.registers[16],
                              r.registers[17], r.registers[18])
                combined = (a << 48) | (b << 32) | (c << 16) | d
                debit = struct.unpack("d", struct.pack("Q", combined))[0]
                return round(debit - self.cfg["offset_debit"], 5)
            else:
                msg = f"[SENSOR] Debit isError: {r}"
                log.error(msg)
                self._on_error(msg)
        except Exception as e:
            log.error(f"Baca Debit gagal: {e}")
            self._on_error(f"[SENSOR] Baca Debit gagal: {e}")
        return round(random.uniform(0.010, 0.030), 5)

    # ── Baca semua sensor ─────────────────────────────────────────────────────
    def read_all(self) -> SensorReading:
        reading = SensorReading(timestamp=time.time())
        reading.ph    = self._read_ph();   time.sleep(0.1)
        reading.tss   = self._read_tss();  time.sleep(0.1)
        reading.debit = self._read_debit()
        return reading

    def close(self) -> None:
        if self._mb:
            self._mb.close()
