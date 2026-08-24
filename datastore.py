"""
datastore.py — Arsip lokal SQLite + satu-satunya jalur kirim-ulang.

Dua tabel, mengikuti dua aliran data yang cadensinya beda:
  • water — pH / TSS / debit, per 2 menit (interval sensor loop).
      sent_s1 = sudah dikirim ke Server 1 (air)   |  sent_s2 = sudah masuk batch KLH
  • air   — PM / noise / cuaca, per 1 menit (noise loop).
      sent_s1 = sudah dikirim ke Server 1 (udara)

Kenapa arsip ini:
  1. BACKUP permanen semua pembacaan (mode WAL, tahan mati listrik).
  2. JARING PENGAMAN "NO-KEY": reading disimpan MENTAH tanpa perlu secret key;
     di-encode & dikirim saat key + koneksi tersedia — data tak hilang.
  3. SATU JALUR kirim-ulang (menggantikan buffer JSON): baris sent=0 dikirim
     ulang otomatis oleh SparingApp._resend_from_store().
  4. Mendekati syarat penyimpanan lokal SK 3441 §6.2.3.9.
"""

import sqlite3
import threading
import logging
from typing import List, Tuple, Dict

from models import SensorReading

log = logging.getLogger(__name__)

_WATER_FIELDS = ["ph", "tss", "debit", "temp"]
_AIR_FIELDS   = ["pm25", "pm10", "pm100", "noise",
                 "wind_speed", "wind_dir", "air_temp", "humidity", "pressure"]


class DataStore:
    """Arsip SQLite thread-safe: tabel water + air, dengan penanda terkirim."""

    def __init__(self, path: str = "data.db", on_error=None):
        self._path = path
        self._on_error = on_error          # callback(msg) → tampilkan error di GUI
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")      # tahan mati listrik
        self._conn.execute("PRAGMA synchronous=NORMAL")
        wcols = ", ".join(f"{f} REAL" for f in _WATER_FIELDS)
        acols = ", ".join(f"{f} REAL" for f in _AIR_FIELDS)
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS water ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
            f"{wcols}, sent_s1 INTEGER DEFAULT 0, sent_s2 INTEGER DEFAULT 0, "
            "created_at INTEGER DEFAULT (strftime('%s','now')))")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS air ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
            f"{acols}, sent_s1 INTEGER DEFAULT 0, "
            "created_at INTEGER DEFAULT (strftime('%s','now')))")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_water_unsent ON water (sent_s1, sent_s2, id)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_air_unsent ON air (sent_s1, id)")
        self._conn.commit()

    def _err(self, msg: str) -> None:
        """Catat error DB ke logger DAN (kalau ada) ke GUI lewat on_error."""
        log.error(msg)
        if self._on_error:
            try:
                self._on_error(f"[ERROR] DB: {msg}")
            except Exception:
                pass

    # ── Tulis ────────────────────────────────────────────────────────────────
    def log_water(self, r: SensorReading) -> int:
        """Simpan satu pembacaan air (mentah). Kembalikan id baris."""
        vals = [int(r.timestamp)] + [float(getattr(r, f, 0.0) or 0.0) for f in _WATER_FIELDS]
        return self._insert("water", _WATER_FIELDS, vals)

    def log_air(self, ts: float, pm25=0.0, pm10=0.0, pm100=0.0, noise=0.0,
                wind_speed=0.0, wind_dir=0.0, air_temp=0.0, humidity=0.0,
                pressure=0.0) -> int:
        """Simpan satu pembacaan udara+cuaca (mentah). Kembalikan id baris."""
        vals = [int(ts), pm25, pm10, pm100, noise,
                wind_speed, wind_dir, air_temp, humidity, pressure]
        vals = [vals[0]] + [float(v or 0.0) for v in vals[1:]]
        return self._insert("air", _AIR_FIELDS, vals)

    def _insert(self, table: str, fields: list, vals: list) -> int:
        placeholders = ", ".join(["?"] * (1 + len(fields)))
        try:
            with self._lock:
                cur = self._conn.execute(
                    f"INSERT INTO {table} (ts, {', '.join(fields)}) "
                    f"VALUES ({placeholders})", vals)
                self._conn.commit()
                return cur.lastrowid
        except Exception as e:
            self._err(f"log ({table}) gagal: {e}")
            return -1

    # ── Baca yang belum terkirim ─────────────────────────────────────────────
    def unsent_water(self, server: str, limit: int = 300,
                     min_age: int = 0) -> List[Tuple[int, SensorReading]]:
        """Baris air belum terkirim ke 's1'/'s2', paling lama dulu → SensorReading.
        min_age>0 → hanya baris yang tercatat > min_age detik lalu (hindari
        balapan dengan jalur kirim normal untuk data yang baru masuk)."""
        col = "sent_s1" if server == "s1" else "sent_s2"
        rows = self._select("water", _WATER_FIELDS, col, limit, min_age)
        out: List[Tuple[int, SensorReading]] = []
        for row in rows:
            rid, ts = row[0], row[1]
            kv = dict(zip(_WATER_FIELDS, row[2:]))
            out.append((rid, SensorReading(timestamp=float(ts), **kv)))
        return out

    def unsent_air(self, limit: int = 300, min_age: int = 0) -> List[Tuple[int, Dict]]:
        """Baris udara belum terkirim ke Server 1, paling lama dulu → dict nilai."""
        rows = self._select("air", _AIR_FIELDS, "sent_s1", limit, min_age)
        out: List[Tuple[int, Dict]] = []
        for row in rows:
            rid, ts = row[0], row[1]
            d = {"ts": float(ts)}
            d.update(dict(zip(_AIR_FIELDS, row[2:])))
            out.append((rid, d))
        return out

    def _select(self, table: str, fields: list, col: str,
                limit: int, min_age: int = 0) -> list:
        try:
            with self._lock:
                if min_age and min_age > 0:
                    cur = self._conn.execute(
                        f"SELECT id, ts, {', '.join(fields)} FROM {table} "
                        f"WHERE {col}=0 AND created_at <= strftime('%s','now') - ? "
                        f"ORDER BY id LIMIT ?", (int(min_age), limit))
                else:
                    cur = self._conn.execute(
                        f"SELECT id, ts, {', '.join(fields)} FROM {table} "
                        f"WHERE {col}=0 ORDER BY id LIMIT ?", (limit,))
                return cur.fetchall()
        except Exception as e:
            self._err(f"unsent ({table}) gagal: {e}")
            return []

    # ── Tandai terkirim ──────────────────────────────────────────────────────
    def mark_water_sent(self, ids, server: str) -> None:
        col = "sent_s1" if server == "s1" else "sent_s2"
        self._mark("water", col, ids)

    def mark_air_sent(self, ids) -> None:
        self._mark("air", "sent_s1", ids)

    def _mark(self, table: str, col: str, ids) -> None:
        if isinstance(ids, int):
            ids = [ids]
        ids = [i for i in ids if i and i > 0]
        if not ids:
            return
        try:
            with self._lock:
                self._conn.executemany(
                    f"UPDATE {table} SET {col}=1 WHERE id=?", [(i,) for i in ids])
                self._conn.commit()
        except Exception as e:
            self._err(f"mark_sent ({table}) gagal: {e}")

    # ── Statistik ────────────────────────────────────────────────────────────
    def counts(self) -> dict:
        try:
            with self._lock:
                w = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(sent_s1=0),0), "
                    "COALESCE(SUM(sent_s2=0),0) FROM water").fetchone()
                a = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(sent_s1=0),0) FROM air").fetchone()
            return {"water_total": w[0] or 0, "water_unsent_s1": w[1] or 0,
                    "water_unsent_s2": w[2] or 0,
                    "air_total": a[0] or 0, "air_unsent_s1": a[1] or 0}
        except Exception as e:
            self._err(f"counts gagal: {e}")
            return {"water_total": 0, "water_unsent_s1": 0, "water_unsent_s2": 0,
                    "air_total": 0, "air_unsent_s1": 0}

    def pending(self) -> int:
        """Total POST yang masih tertunda (indikator buffer di GUI)."""
        c = self.counts()
        return (c["water_unsent_s1"] + c["water_unsent_s2"] + c["air_unsent_s1"])

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass
