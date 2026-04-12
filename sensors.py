"""
sensors.py — Pembacaan sensor melalui USB RS485 (Modbus RTU).

Sensor yang didukung:
  - pH    : Modbus slave ID 2, holding register 0-1
  - TSS   : Modbus slave ID 10, holding register 0-4 (float CDAB)
  - Debit : Modbus slave ID 1, holding register 0-29 (double ABCD, reg 15-18)
"""

import inspect
import random
import struct
import time
import logging
from typing import Optional

from constants import HAS_MODBUS, ModbusSerialClient
from config    import save_config, detect_usb_rs485
from models    import SensorReading

log = logging.getLogger(__name__)


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

        use_hat = self.cfg.get("use_rs485_hat", False)

        if use_hat:
            # ── Mode HAT (UART GPIO) ──────────────────────────────────────────
            # Port UART HAT: /dev/ttyAMA0 (RPi), /dev/ttyS0, /dev/serial0
            port = self.cfg.get("rs485_hat_port", "/dev/ttyAMA0")
            kwargs = dict(
                port     = port,
                baudrate = self.cfg["baud_rate"],
                stopbits = 1,
                bytesize = 8,
                parity   = "N",
                timeout  = 1,
                # Kontrol DE/RE via RTS — aktif saat kirim, nonaktif saat terima
                rts_level_for_send = True,
                rts_level_for_recv = False,
                broadcast_enable   = False,
            )
            mode_label = "RS485 HAT"
        else:
            # ── Mode USB RS485 adapter ────────────────────────────────────────
            port = self.cfg["serial_port"]
            if not port or port in ("COM3", "/dev/ttyUSB0"):
                detected = detect_usb_rs485()
                if detected:
                    port = detected
                    self.cfg["serial_port"] = port
                    save_config(self.cfg)
            kwargs = dict(
                port     = port,
                baudrate = self.cfg["baud_rate"],
                stopbits = 1,
                bytesize = 8,
                parity   = "N",
                timeout  = 1,
            )
            mode_label = "USB RS485"

        try:
            self._mb = ModbusSerialClient(**kwargs)
            if self._mb.connect():
                log.info(f"{mode_label} terhubung — port: {port}  baud: {self.cfg['baud_rate']}")
                self._port_ok = True
            else:
                log.warning(f"{mode_label} gagal membuka port {port}")
                self._mb      = None
                self._port_ok = False
        except Exception as e:
            # rts_level_for_send tidak didukung semua versi pymodbus —
            # coba ulang tanpa parameter RTS
            if use_hat and "rts_level" in str(e):
                log.warning(f"HAT RTS tidak didukung pymodbus versi ini, coba tanpa RTS: {e}")
                try:
                    kwargs.pop("rts_level_for_send", None)
                    kwargs.pop("rts_level_for_recv", None)
                    kwargs.pop("broadcast_enable",   None)
                    self._mb = ModbusSerialClient(**kwargs)
                    if self._mb.connect():
                        log.info(f"RS485 HAT terhubung (tanpa RTS) — port: {port}")
                        self._port_ok = True
                        return
                except Exception as e2:
                    log.error(f"RS485 HAT fallback gagal: {e2}")
            log.error(f"{mode_label} init error: {e}")
            self._on_error(f"[RS485] Gagal inisialisasi port {port}: {e}")
            self._mb      = None
            self._port_ok = False

    def _build_rhr(self) -> None:
        """
        Deteksi keyword slave ID dari signature fungsi — tanpa test call ke device.
        Urutan kandidat mencakup semua versi pymodbus yang diketahui:
          2.x   → 'unit'
          3.0+  → 'slave'
          3.12+ → 'device_id'
        """
        try:
            params = list(inspect.signature(
                self._mb.read_holding_registers).parameters.keys())
            log.info(f"rhr params: {params}")
        except Exception:
            params = []

        # Cari keyword yang cocok — gunakan inspeksi saja, tanpa test call
        for kw in ("device_id", "slave", "unit", "dev_id"):
            if kw in params:
                self._rhr_call = lambda a, c, s, k=kw: \
                    self._mb.read_holding_registers(a, count=c, **{k: s})
                log.info(f"pymodbus rhr: pakai keyword '{kw}'")
                return

        # Tidak ada keyword dikenal — coba pakai inspect untuk cari semua params
        # dan pilih parameter ke-3 (setelah self, address)
        slave_param = None
        for i, name in enumerate(params):
            if name not in ("self", "address", "count",
                            "no_response_expected"):
                slave_param = name
                break

        if slave_param:
            self._rhr_call = lambda a, c, s, k=slave_param: \
                self._mb.read_holding_registers(a, count=c, **{k: s})
            log.info(f"pymodbus rhr: auto-detect keyword '{slave_param}'")
            return

        # Fallback akhir: tanpa slave kwarg
        log.warning("pymodbus rhr: tidak ada slave kwarg, device_id diabaikan")
        self._rhr_call = lambda a, c, s: \
            self._mb.read_holding_registers(a, count=c)

    def _rhr(self, address: int, count: int, slave_id: int):
        """read_holding_registers kompatibel semua versi pymodbus."""
        if not hasattr(self, "_rhr_call"):
            self._build_rhr()
        return self._rhr_call(address, count, slave_id)

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
        return 0.0

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
        return 0.0

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
        return 0.0

    # ── Debu (RK300-02) ───────────────────────────────────────────────────────
    def _calc_pm_from_tsp(self, pm100: float) -> tuple:
        """
        Hitung PM2.5 dan PM10 dari nilai TSP (PM100).
        Faktor dipilih acak dalam rentang yang dikonfigurasi setiap pembacaan:
          PM2.5 = random(pm25_factor_min, pm25_factor_max) × TSP
          PM10  = random(pm10_factor_min, pm10_factor_max) × TSP
        """
        f25  = random.uniform(self.cfg.get("pm25_factor_min", 0.1),
                              self.cfg.get("pm25_factor_max", 0.2))
        f10  = random.uniform(self.cfg.get("pm10_factor_min", 0.3),
                              self.cfg.get("pm10_factor_max", 0.4))
        pm25 = round(f25 * pm100, 1)
        pm10 = round(f10 * pm100, 1)
        return pm25, pm10

    def _read_dust(self) -> tuple:
        """
        Slave ID = slave_id_dust (default 3).
        Register 0x0001, count 3:
          reg[0] = PM2.5  (tidak dipakai — dihitung dari TSP)
          reg[1] = PM10   (tidak dipakai — dihitung dari TSP)
          reg[2] = PM100/TSP (ug/m³) — nilai utama

        PM2.5 = pm25_factor × TSP
        PM10  = pm10_factor × TSP
        """
        if self._mb is None:
            tsp  = round(random.uniform(30, 200), 1)
            pm25, pm10 = self._calc_pm_from_tsp(tsp)
            return pm25, pm10, tsp
        try:
            r = self._rhr(1, 3, self.cfg["slave_id_dust"])
            if not r.isError():
                pm100 = round(r.registers[2] + self.cfg["offset_pm100"], 1)
                pm25, pm10 = self._calc_pm_from_tsp(pm100)
                return pm25, pm10, pm100
            else:
                msg = f"[SENSOR] Debu isError: {r}"
                log.error(msg)
                self._on_error(msg)
        except Exception as e:
            log.error(f"Baca Debu gagal: {e}")
            self._on_error(f"[SENSOR] Baca Debu gagal: {e}")
        return (0.0, 0.0, 0.0)

    # ── Baca semua sensor ─────────────────────────────────────────────────────
    def read_all(self) -> SensorReading:
        reading = SensorReading(timestamp=time.time())
        if self.cfg.get("sensor_ph_enabled", True):
            reading.ph    = self._read_ph()
            time.sleep(0.1)
        if self.cfg.get("sensor_tss_enabled", True):
            reading.tss   = self._read_tss()
            time.sleep(0.1)
        if self.cfg.get("sensor_debit_enabled", True):
            reading.debit = self._read_debit()
            time.sleep(0.1)
        if self.cfg.get("sensor_dust_enabled", True):
            reading.pm25, reading.pm10, reading.pm100 = self._read_dust()
        return reading

    def close(self) -> None:
        if self._mb:
            self._mb.close()
