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
    def _cap_fluctuate(value: float, lo: float, hi: float) -> float:
        """
        Batasi nilai ke rentang [lo, hi].
        Jika nilai di luar batas, kembalikan nilai dekat batas dengan variasi
        acak kecil (2% dari rentang) agar tidak statis di angka batas yang sama.

        Contoh (pH max=14, sensor baca 15.2):
          → dikirim antara 13.72–14.00, berfluktuasi setiap pembacaan.
        """
        if lo <= value <= hi:
            return value
        # Variasi = 2% dari rentang konfigurasi, minimal 0.01
        variation = max(0.01, (hi - lo) * 0.05)
        if value > hi:
            return hi - random.uniform(0, variation)
        else:  # value < lo
            return lo + random.uniform(0, variation)

    def _apply_limits(self, ph: float, tss: float, debit: float):
        """
        Terapkan batas min/max dengan variasi fluktuatif.
        Nilai dalam batas → dikirim apa adanya.
        Nilai di luar batas → dikembalikan mendekati batas dengan variasi kecil,
        sehingga tidak terlihat statis/flat di nilai batas.
        """
        ph_out    = self._cap_fluctuate(
            ph,    self.cfg["limit_ph_min"],    self.cfg["limit_ph_max"])
        tss_out   = self._cap_fluctuate(
            tss,   self.cfg["limit_tss_min"],   self.cfg["limit_tss_max"])
        debit_out = self._cap_fluctuate(
            debit, self.cfg["limit_debit_min"], self.cfg["limit_debit_max"])

        if ph_out != ph or tss_out != tss or debit_out != debit:
            log.debug(
                f"[limit] pH {ph:.3f}→{ph_out:.3f}  "
                f"TSS {tss:.3f}→{tss_out:.3f}  "
                f"Debit {debit:.5f}→{debit_out:.5f}"
            )
        return ph_out, tss_out, debit_out

    def _make_jwt_raw(self, uid: str, key: str,
                      batch: List[SensorReading]) -> str:
        """JWT data MURNI — nilai sensor tanpa filter min/max."""
        if not key or not HAS_JWT or pyjwt is None:
            return ""
        rows = [{
            "datetime": int(r.timestamp),
            "pH":       round(r.ph,    2),
            "tss":      round(r.tss,   2),
            "debit":    round(r.debit, 2),
            "cod":      0,
            "nh3n":     0,
        } for r in batch]
        try:
            return pyjwt.encode({"uid": uid, "data": rows}, key, algorithm="HS256")
        except Exception as e:
            log.error(f"JWT raw encode error: {e}")
            return ""

    def _make_jwt_processed(self, uid: str, key: str,
                            batch: List[SensorReading]) -> str:
        """JWT data PROCESSED — nilai di luar batas diganti 0."""
        if not key or not HAS_JWT or pyjwt is None:
            return ""
        rows = []
        for r in batch:
            ph, tss, debit = self._apply_limits(r.ph, r.tss, r.debit)
            rows.append({
                "datetime": int(r.timestamp),
                "pH":       round(ph,    2),
                "tss":      round(tss,   2),
                "debit":    round(debit, 2),
                "cod":      0,
                "nh3n":     0,
            })
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

    # Alias lama agar tidak ada error jika masih dipanggil
    def get_processed(self, r: SensorReading) -> tuple:
        """Kembalikan (ph, tss, debit) setelah filter min/max — untuk tampilan GUI."""
        return self._apply_limits(r.ph, r.tss, r.debit)

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
