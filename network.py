"""
network.py — Manajemen koneksi internet, pengambilan secret key, pembuatan JWT,
             dan pengiriman data ke dua server SPARING.
"""

import json
import logging
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

    def __init__(self, cfg: dict):
        self.cfg          = cfg
        self.secret_key1  = ""
        self.secret_key2  = ""
        self.keys_fetched = False

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
    def _clamp(value: float, lo: float, hi: float) -> float:
        return max(lo, min(hi, value))

    def _apply_limits(self, ph: float, tss: float, debit: float):
        """
        Terapkan batas min/max dari konfigurasi untuk data Server 2.
        Kembalikan (ph, tss, debit) yang sudah di-clamp.
        """
        ph_out    = self._clamp(ph,    self.cfg["limit_ph_min"],    self.cfg["limit_ph_max"])
        tss_out   = self._clamp(tss,   self.cfg["limit_tss_min"],   self.cfg["limit_tss_max"])
        debit_out = self._clamp(debit, self.cfg["limit_debit_min"], self.cfg["limit_debit_max"])
        return ph_out, tss_out, debit_out

    def _make_jwt_server1(self, uid: str, key: str, batch: List[SensorReading]) -> str:
        """
        JWT Server 1 — data MURNI dari sensor, tanpa clamping.
        """
        if not key or not HAS_JWT or pyjwt is None:
            return ""
        rows = []
        for r in batch:
            rows.append({
                "datetime": int(r.timestamp),
                "pH":       round(r.ph,    3),
                "tss":      round(r.tss,   3),
                "debit":    round(r.debit, 5),
                "cod":      0,
                "nh3n":     0,
            })
        try:
            return pyjwt.encode({"uid": uid, "data": rows}, key, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT1 encode error: {e}")
            return ""

    def _make_jwt_server2(self, uid: str, key: str, batch: List[SensorReading]) -> str:
        """
        JWT Server 2 — data dengan batas min/max (KLHK).
        Nilai di luar rentang valid di-clamp ke batas terdekat.
        Tidak menyertakan arus & tegangan.
        """
        if not key or not HAS_JWT or pyjwt is None:
            return ""
        rows = []
        for r in batch:
            ph, tss, debit = self._apply_limits(r.ph, r.tss, r.debit)
            # Catat jika ada nilai yang di-clamp
            if ph != r.ph or tss != r.tss or debit != r.debit:
                log.debug(
                    f"[S2 clamp] pH {r.ph:.3f}→{ph:.3f}  "
                    f"TSS {r.tss:.3f}→{tss:.3f}  "
                    f"Debit {r.debit:.5f}→{debit:.5f}"
                )
            rows.append({
                "datetime": int(r.timestamp),
                "pH":       round(ph,    3),
                "tss":      round(tss,   3),
                "debit":    round(debit, 5),
                "cod":      0,
                "nh3n":     0,
            })
        try:
            return pyjwt.encode({"uid": uid, "data": rows}, key, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT2 encode error: {e}")
            return ""

    def create_jwt1(self, batch: List[SensorReading]) -> str:
        """JWT untuk Server 1 — data murni sensor."""
        return self._make_jwt_server1(self.cfg["uid1"], self.secret_key1, batch)

    def create_jwt2(self, batch: List[SensorReading]) -> str:
        """JWT untuk Server 2 — data di-clamp sesuai batas min/max (KLHK)."""
        return self._make_jwt_server2(self.cfg["uid2"], self.secret_key2, batch)

    # ── HTTP POST ─────────────────────────────────────────────────────────────
    def post(self, url: str, body: str) -> bool:
        if not HAS_REQUESTS or req_lib is None:
            return False
        try:
            r = req_lib.post(
                url, data=body,
                headers={"Content-Type": "application/json"},
                timeout=30,
            )
            log.info(f"POST {url} → HTTP {r.status_code}")
            return r.status_code in (200, 201)
        except Exception as e:
            log.error(f"POST gagal {url}: {e}")
            return False
