"""
network.py — Manajemen koneksi internet, pengambilan secret key, pembuatan JWT,
             dan pengiriman data ke dua server SPARING.
"""

import json
import logging
import random
from datetime import datetime
from typing import List, Optional

from constants import HAS_REQUESTS, HAS_JWT, req_lib, pyjwt
from models    import SensorReading

log = logging.getLogger(__name__)


class NetworkManager:
    """
    Mengurusi semua operasi jaringan:
      - Cek koneksi internet
      - Ambil secret key dari server
      - Buat JWT payload untuk dua server (server 1 dengan arus+tegangan,
        server 2 tanpa arus+tegangan)
      - POST data ke server
    """

    def __init__(self, cfg: dict, on_log=None):
        self.cfg          = cfg
        self.secret_key1  = ""
        self.secret_key2  = ""
        self.keys_fetched = False
        self._on_log      = on_log or (lambda msg: None)

    # ── Internet check ────────────────────────────────────────────────────────
    def check_internet(self) -> bool:
        if not HAS_REQUESTS or req_lib is None:
            return False
        try:
            r = req_lib.get("http://www.google.com", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    def check_server(self, secret_key_url: str) -> bool:
        """
        Cek apakah server dapat dijangkau menggunakan endpoint secret key
        sebagai health check (HTTP 200 = server aktif).
        """
        if not HAS_REQUESTS or req_lib is None:
            return False
        try:
            r = req_lib.get(secret_key_url, timeout=8)
            return r.status_code in (200, 201, 401, 403, 405)
            # 401/403/405 = server aktif tapi butuh auth — tetap "terhubung"
        except Exception:
            return False

    # ── Secret key ────────────────────────────────────────────────────────────
    def _fetch_key(self, url: str) -> Optional[str]:
        if not HAS_REQUESTS or req_lib is None:
            return None
        try:
            r = req_lib.get(url, timeout=10)
            if r.status_code == 200:
                return r.text.strip()
        except Exception as e:
            log.error(f"Fetch key gagal ({url}): {e}")
        return None

    def fetch_all_keys(self) -> None:
        """Ambil secret key untuk kedua server. Gunakan default jika gagal."""
        k1 = self._fetch_key(self.cfg["secret_key_url1"])
        self.secret_key1 = k1 if k1 else "sparing1"
        if not k1:
            log.warning("Secret key 1 default digunakan")

        k2 = self._fetch_key(self.cfg["secret_key_url2"])
        self.secret_key2 = k2 if k2 else "sparing2"
        if not k2:
            log.warning("Secret key 2 default digunakan")

        self.keys_fetched = True

    # ── JWT ───────────────────────────────────────────────────────────────────
    @staticmethod
    def _cap_fluctuate(value: float, lo: float, hi: float,
                       variation: float = 0.0) -> float:
        """
        Jika nilai dalam [lo, hi] → kembalikan apa adanya.
        Jika nilai di luar batas → kembalikan nilai acak di zona floating:
          • value > hi  → acak dalam [hi - variation, hi]
          • value < lo  → acak dalam [lo, lo + variation]
        variation = lebar zona floating (dikonfigurasi per sensor).
        """
        if lo <= value <= hi:
            return value
        var = max(0.001, variation)
        if value > hi:
            return hi - random.uniform(0, var)
        else:  # value < lo
            return lo + random.uniform(0, var)

    def _apply_limits(self, ph: float, tss: float, debit: float,
                      pm25: float = 0.0, pm10: float = 0.0,
                      pm100: float = 0.0, noise: float = 0.0):
        """Terapkan batas min/max dengan variasi fluktuatif ke semua parameter."""
        c = self.cfg
        ph_out    = self._cap_fluctuate(ph,    c["limit_ph_min"],    c["limit_ph_max"],    c.get("limit_ph_float",    0.3))
        tss_out   = self._cap_fluctuate(tss,   c["limit_tss_min"],   c["limit_tss_max"],   c.get("limit_tss_float",   10.0))
        debit_out = self._cap_fluctuate(debit, c["limit_debit_min"], c["limit_debit_max"], c.get("limit_debit_float",  1.0))
        pm25_out  = self._cap_fluctuate(pm25,  c["limit_pm25_min"],  c["limit_pm25_max"],  c.get("limit_pm25_float",  10.0))
        pm10_out  = self._cap_fluctuate(pm10,  c["limit_pm10_min"],  c["limit_pm10_max"],  c.get("limit_pm10_float",  10.0))
        pm100_out = self._cap_fluctuate(pm100, c["limit_pm100_min"], c["limit_pm100_max"], c.get("limit_pm100_float", 10.0))
        noise_out = self._cap_fluctuate(noise, c.get("limit_noise_min", 0.0), c.get("limit_noise_max", 120.0), c.get("limit_noise_float", 2.0))
        return ph_out, tss_out, debit_out, pm25_out, pm10_out, pm100_out, noise_out

    def _build_row(self, r: SensorReading, processed: bool = False) -> dict:
        """
        Bangun satu baris data untuk JWT.
        Hanya sertakan field sensor yang diaktifkan di config.
        Jika processed=True, terapkan filter min/max.
        """
        row: dict = {"datetime": int(r.timestamp), "cod": 0, "nh3n": 0}
        cfg = self.cfg

        if processed:
            ph, tss, debit, pm25, pm10, pm100, noise = self._apply_limits(
                r.ph, r.tss, r.debit, r.pm25, r.pm10, r.pm100, r.noise)
        else:
            ph, tss, debit = r.ph, r.tss, r.debit
            pm25, pm10, pm100 = r.pm25, r.pm10, r.pm100
            noise = r.noise

        if cfg.get("sensor_ph_enabled",    True):
            row["pH"]    = round(ph,    2)
        if cfg.get("sensor_tss_enabled",   True):
            row["tss"]   = round(tss,   2)
        if cfg.get("sensor_debit_enabled", True):
            row["debit"] = round(debit, 2)
        if cfg.get("sensor_dust_enabled",  True):
            row["pm25"]  = round(pm25,  1)
            row["pm10"]  = round(pm10,  1)
            row["pm100"] = round(pm100, 1)
        if cfg.get("sensor_noise_enabled", True):
            row["noise"] = round(noise, 1)
        return row

    def _make_jwt_raw(self, uid: str, key: str,
                      batch: List[SensorReading]) -> str:
        """JWT data MURNI — nilai sensor tanpa filter min/max."""
        if not key or not HAS_JWT or pyjwt is None:
            return ""
        rows = [self._build_row(r, processed=False) for r in batch]
        try:
            return pyjwt.encode({"uid": uid, "data": rows}, key, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT raw encode error: {e}")
            return ""

    def _make_jwt_processed(self, uid: str, key: str,
                            batch: List[SensorReading]) -> str:
        """JWT data PROCESSED — nilai di luar batas difluktuasikan ke batas."""
        if not key or not HAS_JWT or pyjwt is None:
            return ""
        rows = [self._build_row(r, processed=True) for r in batch]
        try:
            return pyjwt.encode({"uid": uid, "data": rows}, key, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT processed encode error: {e}")
            return ""

    def create_jwt1_raw(self, batch: List[SensorReading]) -> str:
        """Server 1 — data murni sensor (uid1, secret_key1)."""
        return self._make_jwt_raw(
            self.cfg["uid1"], self.secret_key1, batch)

    def create_jwt1_processed(self, batch: List[SensorReading]) -> str:
        """Server 1 — data processed/filtered (uid1_processed, secret_key1)."""
        return self._make_jwt_processed(
            self.cfg.get("uid1_processed", self.cfg["uid1"]),
            self.secret_key1, batch)

    def create_jwt2(self, batch: List[SensorReading]) -> str:
        """Server 2 — data processed/filtered (uid2, secret_key2)."""
        return self._make_jwt_processed(
            self.cfg["uid2"], self.secret_key2, batch)

    def create_jwt_s1_env(self, pm25: float, pm10: float, tsp: float,
                          noise: float, timestamp: float,
                          link_video_id: str = "") -> str:
        """
        JWT Server 1 — format per-1-menit:
          uid, pm_25, pm_10, tsp, noise (instan), datetime_unix, link_video_id
        """
        if not self.secret_key1 or not HAS_JWT or pyjwt is None:
            return ""
        payload = {
            "uid": self.cfg["uid1"],
            "data": [{
                "pm_25":         round(pm25,  1),
                "pm_10":         round(pm10,  1),
                "tsp":           round(tsp,   1),
                "noise":         round(noise, 1),
                "datetime_unix": int(timestamp),
                "link_video_id": link_video_id,
            }],
        }
        try:
            return pyjwt.encode(payload, self.secret_key1, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT s1_env encode error: {e}")
            return ""

    # Alias lama agar tidak ada error jika masih dipanggil
    def get_processed(self, r: SensorReading) -> tuple:
        """Kembalikan (ph, tss, debit, pm25, pm10, pm100, noise) setelah filter — untuk GUI."""
        return self._apply_limits(r.ph, r.tss, r.debit, r.pm25, r.pm10, r.pm100, r.noise)

    def create_jwt1(self, batch: List[SensorReading]) -> str:
        return self.create_jwt1_raw(batch)

    # ── HTTP POST ─────────────────────────────────────────────────────────────
    def post(self, url: str, body: str) -> bool:
        if not HAS_REQUESTS or req_lib is None:
            return False
        # Tampilkan nama host saja agar log tidak terlalu panjang
        host = url.split("/")[2] if "/" in url else url
        try:
            r    = req_lib.post(
                url, data=body,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            ok   = r.status_code in (200, 201)
            resp = r.text.strip()[:120] or "(no body)"
            msg  = f"[POST] {host} → HTTP {r.status_code}  {resp}"
            log.info(msg)
            self._on_log(msg)
            return ok
        except Exception as e:
            msg = f"[POST] {host} → ERROR: {e}"
            log.error(msg)
            self._on_log(msg)
            return False

    # ── Log ke server ─────────────────────────────────────────────────────────
    def post_log(self, message: str, level: str = "INFO") -> bool:
        """
        Kirim satu baris log ke endpoint POST /api/log.
        Tidak memanggil _on_log untuk menghindari rekursi.
        """
        if not HAS_REQUESTS or req_lib is None:
            return False
        url = self.cfg.get("log_url", "")
        if not url:
            return False
        payload = {
            "uid":       self.cfg.get("uid1", ""),
            "key":       self.cfg.get("log_key", "sparing"),
            "level":     level,
            "message":   message,
            "logged_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        try:
            req_lib.post(url, json=payload, timeout=10)
            return True
        except Exception as e:
            log.debug(f"post_log gagal: {e}")
            return False
