"""
network.py — Manajemen koneksi internet, pengambilan secret key, pembuatan JWT,
             dan pengiriman data ke dua server SPARING.
"""

import json
import logging
import random
import socket
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
        """
        Cek koneksi jaringan ke server.
        1. Coba internet publik via 8.8.8.8:53 (Google DNS).
        2. Fallback: coba TCP langsung ke host server (skenario LAN tanpa internet).
        """
        # Coba internet publik
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect(("8.8.8.8", 53))
            s.close()
            return True
        except Exception:
            pass
        # Fallback: coba TCP ke host server (LAN)
        try:
            url = self.cfg.get("secret_key_url1", "")
            if not url:
                return False
            # Ekstrak host dan port dari URL (http://host:port/path atau https://host/path)
            hostpart = url.split("//")[-1].split("/")[0]  # "host:port" atau "host"
            tokens   = hostpart.split(":")
            host     = tokens[0]
            port     = int(tokens[1]) if len(tokens) > 1 else (443 if url.startswith("https") else 80)
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(5)
            s.connect((host, port))
            s.close()
            return True
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

    def fetch_all_keys(self) -> bool:
        """
        Ambil secret key dari server.
        Hanya update key & set keys_fetched=True jika KEDUA key berhasil diambil.
        Kembalikan True jika berhasil, False jika salah satu gagal.
        Tidak menggunakan key default — JWT kosong lebih aman dari JWT salah-tanda.
        """
        k1 = self._fetch_key(self.cfg["secret_key_url1"])
        k2 = self._fetch_key(self.cfg["secret_key_url2"])

        if k1:
            self.secret_key1 = k1
            log.info(f"Secret key 1 OK ({len(k1)} chars)")
        else:
            log.warning("Secret key 1 gagal diambil dari server")

        if k2:
            self.secret_key2 = k2
            log.info(f"Secret key 2 OK ({len(k2)} chars)")
        else:
            log.warning("Secret key 2 gagal diambil dari server")

        if k1 and k2:
            self.keys_fetched = True

        return bool(k1 and k2)

    # ── JWT ───────────────────────────────────────────────────────────────────
    @staticmethod
    def _cap_fluctuate(value: float, lo: float, hi: float,
                       flo_min: float = None, flo_max: float = None,
                       fhi_min: float = None, fhi_max: float = None) -> float:
        """
        Nilai dalam [lo, hi]  → kembalikan apa adanya.
        Nilai < lo            → random dalam [flo_min, flo_max]  (zona float bawah, di dalam range).
        Nilai > hi            → random dalam [fhi_min, fhi_max]  (zona float atas, di dalam range).
        """
        if lo <= value <= hi:
            return value
        if value < lo:
            a = lo  if flo_min is None else flo_min
            b = lo  if flo_max is None else flo_max
            return round(random.uniform(min(a, b), max(a, b)), 4)
        # value > hi
        a = hi  if fhi_min is None else fhi_min
        b = hi  if fhi_max is None else fhi_max
        return round(random.uniform(min(a, b), max(a, b)), 4)

    def _apply_limits(self, ph: float, tss: float, debit: float,
                      pm25: float = 0.0, pm10: float = 0.0,
                      pm100: float = 0.0, noise: float = 0.0):
        """Terapkan batas min/max dengan variasi fluktuatif ke semua parameter."""
        c = self.cfg
        def _f(key): return (
            c.get(f"limit_{key}_min"),   c.get(f"limit_{key}_max"),
            c.get(f"limit_{key}_float_lo_min"), c.get(f"limit_{key}_float_lo_max"),
            c.get(f"limit_{key}_float_hi_min"), c.get(f"limit_{key}_float_hi_max"),
        )
        ph_out    = self._cap_fluctuate(ph,    *_f("ph"))
        tss_out   = self._cap_fluctuate(tss,   *_f("tss"))
        debit_out = self._cap_fluctuate(debit, *_f("debit"))
        pm25_out  = self._cap_fluctuate(pm25,  *_f("pm25"))
        pm10_out  = self._cap_fluctuate(pm10,  *_f("pm10"))
        pm100_out = self._cap_fluctuate(pm100, *_f("pm100"))
        noise_out = self._cap_fluctuate(noise, *_f("noise"))
        return ph_out, tss_out, debit_out, pm25_out, pm10_out, pm100_out, noise_out

    def _build_row(self, r: SensorReading, processed: bool = False,
                   include_env: bool = True) -> dict:
        """
        Bangun satu baris data untuk JWT.
        Hanya sertakan field sensor yang diaktifkan di config.
        Jika processed=True, terapkan filter min/max.
        include_env=False → tidak sertakan PM dan noise (untuk Server 2 KLHK).
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
        if include_env:
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
        """Server 2 — data processed/filtered (uid2, secret_key2), tanpa data udara."""
        if not self.secret_key2 or not HAS_JWT or pyjwt is None:
            return ""
        rows = [self._build_row(r, processed=True, include_env=False) for r in batch]
        try:
            return pyjwt.encode(
                {"uid": self.cfg["uid2"], "data": rows},
                self.secret_key2, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT2 encode error: {e}")
            return ""

    def create_jwt1_water(self, r: SensorReading,
                          processed: bool = False) -> str:
        """
        JWT Server 1 — kualitas air (pH, TSS, Debit).
        processed=False → data raw (Internal).
        processed=True  → data setelah apply_limits (KLHK).
        Kembalikan "" jika tidak ada sensor air yang aktif.
        """
        if not self.secret_key1 or not HAS_JWT or pyjwt is None:
            return ""
        cfg = self.cfg
        ph_on    = cfg.get("sensor_ph_enabled",    True)
        tss_on   = cfg.get("sensor_tss_enabled",   True)
        debit_on = cfg.get("sensor_debit_enabled", True)
        temp_on  = cfg.get("sensor_temp_enabled",  True)

        if not (ph_on or tss_on or debit_on or temp_on):
            return ""

        if processed:
            ph_v, tss_v, debit_v, *_ = self._apply_limits(
                r.ph, r.tss, r.debit, 0, 0, 0, 0)
            uid = cfg.get("uid1_klhk") or cfg["uid1"]
        else:
            ph_v, tss_v, debit_v = r.ph, r.tss, r.debit
            uid = cfg["uid1"]

        tl = cfg.get("tl_klhk", 2) if processed else cfg.get("tl_water", 1)
        payload: dict = {
            "uid":      uid,
            "cod":      0,
            "nh3n":     0,
            "datetime": int(r.timestamp),
            "tl":       tl,
        }
        if ph_on:    payload["pH"]    = round(ph_v,    2)
        if tss_on:   payload["tss"]   = round(tss_v,   2)
        if debit_on: payload["debit"] = round(debit_v, 2)
        if temp_on:  payload["temp"]  = round(r.temp,  1)
        try:
            return pyjwt.encode(payload, self.secret_key1, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT water encode error: {e}")
            return ""

    def create_jwt_s1_env(self, pm25: float, pm10: float, tsp: float,
                          noise: float, timestamp: float,
                          link_video_id: str = "",
                          processed: bool = False,
                          wind_speed: float = 0.0, wind_dir: float = 0.0,
                          air_temp: float = 0.0, humidity: float = 0.0,
                          pressure: float = 0.0) -> str:
        """
        JWT Server 1 — kualitas udara (PM + noise + cuaca YGC-CSM), per 1 menit.
        processed=False → data raw (Internal).
        processed=True  → data setelah apply_limits (KLHK).
        Kembalikan "" jika tidak ada sensor udara yang aktif.
        """
        if not self.secret_key1 or not HAS_JWT or pyjwt is None:
            return ""
        cfg        = self.cfg
        dust_on    = cfg.get("sensor_dust_enabled",    True)
        noise_on   = cfg.get("sensor_noise_enabled",   True)
        weather_on = cfg.get("sensor_weather_enabled", True)

        if not (dust_on or noise_on or weather_on):
            return ""

        if processed:
            _, _, _, pm25_v, pm10_v, tsp_v, noise_v = self._apply_limits(
                0, 0, 0, pm25, pm10, tsp, noise)
            uid = cfg.get("uid1_klhk") or cfg["uid1"]
        else:
            pm25_v, pm10_v, tsp_v, noise_v = pm25, pm10, tsp, noise
            uid = cfg["uid1"]

        tl = cfg.get("tl_klhk", 2) if processed else cfg.get("tl_water", 1)
        payload: dict = {
            "uid":      uid,
            "tl":       tl,
            "datetime": int(timestamp),
        }
        if dust_on:
            payload["pm2.5"] = round(pm25_v, 1)
            payload["pm10"]  = round(pm10_v, 1)
            payload["tsp"]   = round(tsp_v,  1)
        if noise_on:
            payload["noise"] = round(noise_v, 1)
        if weather_on:
            payload["wind_speed"] = round(wind_speed, 2)
            payload["wind_dir"]   = int(wind_dir)
            payload["air_temp"]   = round(air_temp,   1)
            payload["humidity"]   = round(humidity,   1)
            payload["pressure"]   = round(pressure,   1)
        if link_video_id:
            payload["link_video_id"] = link_video_id
        try:
            return pyjwt.encode(payload, self.secret_key1, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT s1_env encode error: {e}")
            return ""

    def create_jwt_s1_weather(self, r: SensorReading,
                              processed: bool = False) -> str:
        """
        JWT Server 1 — data cuaca YGC-CSM (angin, suhu udara, RH, tekanan).
        Dikirim per 2 menit bersama data air.
        Kembalikan "" jika sensor cuaca tidak aktif.
        """
        if not self.cfg.get("sensor_weather_enabled", True):
            return ""
        if not self.secret_key1 or not HAS_JWT or pyjwt is None:
            return ""
        cfg = self.cfg
        uid = (cfg.get("uid1_klhk") or cfg["uid1"]) if processed else cfg["uid1"]
        tl  = cfg.get("tl_klhk", 2) if processed else cfg.get("tl_water", 1)
        payload = {
            "uid":        uid,
            "tl":         tl,
            "datetime":   int(r.timestamp),
            "wind_speed": round(r.wind_speed, 2),
            "wind_dir":   int(r.wind_dir),
            "air_temp":   round(r.air_temp,   1),
            "humidity":   round(r.humidity,   1),
            "pressure":   round(r.pressure,   1),
        }
        try:
            return pyjwt.encode(payload, self.secret_key1, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT s1_weather encode error: {e}")
            return ""

    def create_jwt1_water_status(self, status_code: int, timestamp: float,
                                 processed: bool = False) -> str:
        """JWT Server 1 kualitas air — kondisi tidak normal sesuai SK 3441/2025 §6.2.6.6g.
        status_code: -1 berhenti, -2 kalibrasi, -3 gangguan peralatan."""
        if not self.secret_key1 or not HAS_JWT or pyjwt is None:
            return ""
        cfg = self.cfg
        uid = (cfg.get("uid1_klhk") or cfg["uid1"]) if processed else cfg["uid1"]
        tl  = cfg.get("tl_klhk", 2) if processed else cfg.get("tl_water", 1)
        v   = status_code
        payload: dict = {"uid": uid, "tl": tl, "datetime": int(timestamp),
                         "cod": v, "nh3n": v}
        if cfg.get("sensor_ph_enabled",    True): payload["pH"]    = v
        if cfg.get("sensor_tss_enabled",   True): payload["tss"]   = v
        if cfg.get("sensor_debit_enabled", True): payload["debit"] = v
        if cfg.get("sensor_temp_enabled",  True): payload["temp"]  = v
        try:
            return pyjwt.encode(payload, self.secret_key1, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT water status encode error: {e}")
            return ""

    def create_jwt_s1_env_status(self, status_code: int, timestamp: float,
                                  link_video_id: str = "",
                                  processed: bool = False) -> str:
        """JWT Server 1 kualitas udara — kondisi tidak normal sesuai SK 3441/2025 §6.2.6.6g."""
        if not self.secret_key1 or not HAS_JWT or pyjwt is None:
            return ""
        cfg        = self.cfg
        dust_on    = cfg.get("sensor_dust_enabled",    True)
        noise_on   = cfg.get("sensor_noise_enabled",   True)
        weather_on = cfg.get("sensor_weather_enabled", True)
        if not (dust_on or noise_on or weather_on):
            return ""
        uid = (cfg.get("uid1_klhk") or cfg["uid1"]) if processed else cfg["uid1"]
        tl  = cfg.get("tl_klhk", 2) if processed else cfg.get("tl_water", 1)
        v   = status_code
        payload: dict = {"uid": uid, "tl": tl, "datetime": int(timestamp)}
        if dust_on:
            payload["pm2.5"] = v; payload["pm10"] = v; payload["tsp"] = v
        if noise_on:
            payload["noise"] = v
        if weather_on:
            payload["wind_speed"] = v; payload["wind_dir"]  = v
            payload["air_temp"]   = v; payload["humidity"]  = v
            payload["pressure"]   = v
        if link_video_id:
            payload["link_video_id"] = link_video_id
        try:
            return pyjwt.encode(payload, self.secret_key1, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT s1_env status encode error: {e}")
            return ""

    def create_jwt2_status(self, status_code: int, batch_size: int) -> str:
        """JWT Server 2 batch — kondisi tidak normal sesuai SK 3441/2025 §6.2.6.6g."""
        import time as _time
        if not self.secret_key2 or not HAS_JWT or pyjwt is None:
            return ""
        cfg      = self.cfg
        interval = cfg.get("interval_seconds", 120)
        now      = int(_time.time())
        v        = status_code
        rows = []
        for i in range(batch_size):
            row: dict = {"datetime": now - (batch_size - 1 - i) * interval,
                         "cod": v, "nh3n": v}
            if cfg.get("sensor_ph_enabled",    True): row["pH"]    = v
            if cfg.get("sensor_tss_enabled",   True): row["tss"]   = v
            if cfg.get("sensor_debit_enabled", True): row["debit"] = v
            rows.append(row)
        try:
            return pyjwt.encode(
                {"uid": cfg["uid2"], "data": rows},
                self.secret_key2, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT2 status encode error: {e}")
            return ""

    # Alias lama agar tidak ada error jika masih dipanggil
    def get_processed(self, r: SensorReading) -> tuple:
        """Kembalikan (ph, tss, debit, pm25, pm10, pm100, noise) setelah filter — untuk GUI."""
        return self._apply_limits(r.ph, r.tss, r.debit, r.pm25, r.pm10, r.pm100, r.noise)

    def create_jwt1(self, batch: List[SensorReading]) -> str:
        return self.create_jwt1_raw(batch)

    # ── HTTP POST ─────────────────────────────────────────────────────────────
    def _do_post(self, url: str, body: str) -> int:
        """Kirim POST, kembalikan HTTP status code atau -1 jika error jaringan."""
        if not HAS_REQUESTS or req_lib is None:
            return -1
        host = url.split("/")[2] if "/" in url else url
        try:
            r    = req_lib.post(
                url, data=body,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            resp = r.text.strip()[:120] or "(no body)"
            msg  = f"[POST] {host} → HTTP {r.status_code}  {resp}"
            log.info(msg)
            self._on_log(msg)
            return r.status_code
        except Exception as e:
            msg = f"[POST] {host} → ERROR: {e}"
            log.error(msg)
            self._on_log(msg)
            return -1

    def post(self, url: str, body: str) -> bool:
        return self._do_post(url, body) in (200, 201)

    def post_status(self, url: str, body: str) -> int:
        """Versi post() yang mengembalikan HTTP status code (untuk deteksi 401)."""
        return self._do_post(url, body)

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
