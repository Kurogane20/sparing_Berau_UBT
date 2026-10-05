"""
datastore.py — Arsip lokal SQLite + satu-satunya jalur kirim-ulang.

Dua tabel, mengikuti dua aliran data yang cadensinya beda:
  • water — pH / TSS / debit, per 2 menit (interval sensor loop).
      sent_s1  = Server 1 logger Internal  |  sent_s1k = Server 1 logger KLHK
      sent_s2  = sudah masuk batch KLH (Server 2)
  • air   — PM / noise / cuaca, per 1 menit (noise loop).
      sent_s1  = Server 1 logger Internal  |  sent_s1k = Server 1 logger KLHK

Nilai penanda: 0 = belum terkirim, 1 = terkirim / ditangani,
  2 = DITOLAK server (4xx permanen) — dilewati kirim-ulang supaya tak menyumbat
      antrean; data tetap di arsip, set kembali ke 0 untuk mencoba lagi.

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

SENT, REJECTED = 1, 2
# Tujuan kirim → kolom penanda. "s1" Internal, "s1k" KLHK (sama-sama Server 1),
# "s2" batch KLH (Server 2, hanya tabel water).
_COL = {"s1": "sent_s1", "s1k": "sent_s1k", "s2": "sent_s2"}


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
            "created_at INTEGER DEFAULT (strftime('%s','now')), "
            "sent_s1k INTEGER DEFAULT 0)")
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS air ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
            f"{acols}, sent_s1 INTEGER DEFAULT 0, "
            "created_at INTEGER DEFAULT (strftime('%s','now')), "
            "sent_s1k INTEGER DEFAULT 0)")
        self._migrate()
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_water_unsent ON water (sent_s1, sent_s2, id)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_water_unsent_k ON water (sent_s1k, id)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_air_unsent ON air (sent_s1, id)")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_air_unsent_k ON air (sent_s1k, id)")
        self._conn.commit()

    def _migrate(self) -> None:
        """DB lama belum punya sent_s1k — dulu sent_s1 mewakili Internal+KLHK
        sekaligus. Tambah kolomnya dan salin nilai sent_s1 supaya status lama
        tetap berlaku untuk kedua logger."""
        for table in ("water", "air"):
            cols = [c[1] for c in self._conn.execute(f"PRAGMA table_info({table})")]
            if "sent_s1k" not in cols:
                self._conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN sent_s1k INTEGER DEFAULT 0")
                self._conn.execute(f"UPDATE {table} SET sent_s1k = sent_s1")
                log.info(f"[DB] Migrasi: kolom sent_s1k ditambahkan ke tabel {table}")

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
    def unsent_water(self, server: str, limit: int = 300, min_age: int = 0
                     ) -> List[Tuple[int, SensorReading, Tuple[str, ...]]]:
        """Baris air yang masih tertunda ke 's1' (Internal/KLHK) atau 's2',
        paling lama dulu → (id, SensorReading, tujuan_tertunda).
        tujuan_tertunda: subset ("s1", "s1k") untuk server 's1', ("s2",) untuk 's2'.
        min_age>0 → hanya baris yang tercatat > min_age detik lalu (hindari
        balapan dengan jalur kirim normal untuk data yang baru masuk)."""
        dests = ("s1", "s1k") if server == "s1" else ("s2",)
        n = len(_WATER_FIELDS)
        out: List[Tuple[int, SensorReading, Tuple[str, ...]]] = []
        for row in self._select("water", _WATER_FIELDS, dests, limit, min_age):
            rid, ts = row[0], row[1]
            kv = dict(zip(_WATER_FIELDS, row[2:2 + n]))
            pending = tuple(d for d, f in zip(dests, row[2 + n:]) if f == 0)
            out.append((rid, SensorReading(timestamp=float(ts), **kv), pending))
        return out

    def unsent_air(self, limit: int = 300, min_age: int = 0
                   ) -> List[Tuple[int, Dict, Tuple[str, ...]]]:
        """Baris udara yang masih tertunda ke Server 1 (Internal/KLHK), paling
        lama dulu → (id, dict nilai, tujuan_tertunda)."""
        dests = ("s1", "s1k")
        n = len(_AIR_FIELDS)
        out: List[Tuple[int, Dict, Tuple[str, ...]]] = []
        for row in self._select("air", _AIR_FIELDS, dests, limit, min_age):
            rid, ts = row[0], row[1]
            d = {"ts": float(ts)}
            d.update(dict(zip(_AIR_FIELDS, row[2:2 + n])))
            pending = tuple(dd for dd, f in zip(dests, row[2 + n:]) if f == 0)
            out.append((rid, d, pending))
        return out

    def _select(self, table: str, fields: list, dests: tuple,
                limit: int, min_age: int = 0) -> list:
        """SELECT id, ts, <fields>, <kolom penanda dests> untuk baris yang
        minimal satu tujuannya masih 0."""
        cols  = [_COL[d] for d in dests]
        where = " OR ".join(f"{c}=0" for c in cols)
        sql   = (f"SELECT id, ts, {', '.join(fields + cols)} FROM {table} "
                 f"WHERE ({where})")
        args: list = []
        if min_age and min_age > 0:
            sql += " AND created_at <= strftime('%s','now') - ?"
            args.append(int(min_age))
        sql += " ORDER BY id LIMIT ?"
        args.append(limit)
        try:
            with self._lock:
                return self._conn.execute(sql, args).fetchall()
        except Exception as e:
            self._err(f"unsent ({table}) gagal: {e}")
            return []

    # ── Tandai terkirim / ditolak ────────────────────────────────────────────
    def mark_water_sent(self, ids, server: str, status: int = SENT) -> None:
        """server: 's1' (Internal) / 's1k' (KLHK) / 's2' (KLH)."""
        self._mark("water", _COL[server], ids, status)

    def mark_air_sent(self, ids, server: str = "s1", status: int = SENT) -> None:
        """server: 's1' (Internal) / 's1k' (KLHK)."""
        self._mark("air", _COL[server], ids, status)

    def _mark(self, table: str, col: str, ids, status: int = SENT) -> None:
        if isinstance(ids, int):
            ids = [ids]
        ids = [i for i in ids if i and i > 0]
        if not ids:
            return
        try:
            with self._lock:
                self._conn.executemany(
                    f"UPDATE {table} SET {col}=? WHERE id=?",
                    [(status, i) for i in ids])
                self._conn.commit()
        except Exception as e:
            self._err(f"mark_sent ({table}) gagal: {e}")

    # ── Statistik ────────────────────────────────────────────────────────────
    def counts(self) -> dict:
        try:
            with self._lock:
                w = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(sent_s1=0),0), "
                    "COALESCE(SUM(sent_s1k=0),0), COALESCE(SUM(sent_s2=0),0), "
                    "COALESCE(SUM((sent_s1=2) + (sent_s1k=2) + (sent_s2=2)),0) "
                    "FROM water").fetchone()
                a = self._conn.execute(
                    "SELECT COUNT(*), COALESCE(SUM(sent_s1=0),0), "
                    "COALESCE(SUM(sent_s1k=0),0), "
                    "COALESCE(SUM((sent_s1=2) + (sent_s1k=2)),0) FROM air").fetchone()
            return {"water_total": w[0] or 0, "water_unsent_s1": w[1] or 0,
                    "water_unsent_s1k": w[2] or 0, "water_unsent_s2": w[3] or 0,
                    "air_total": a[0] or 0, "air_unsent_s1": a[1] or 0,
                    "air_unsent_s1k": a[2] or 0,
                    "rejected": (w[4] or 0) + (a[3] or 0)}
        except Exception as e:
            self._err(f"counts gagal: {e}")
            return {"water_total": 0, "water_unsent_s1": 0, "water_unsent_s1k": 0,
                    "water_unsent_s2": 0, "air_total": 0, "air_unsent_s1": 0,
                    "air_unsent_s1k": 0, "rejected": 0}

    def pending(self) -> int:
        """Total POST yang masih tertunda (indikator buffer di GUI).
        Baris DITOLAK (2) tidak dihitung — tak akan dikirim ulang."""
        c = self.counts()
        return (c["water_unsent_s1"] + c["water_unsent_s1k"] + c["water_unsent_s2"]
                + c["air_unsent_s1"] + c["air_unsent_s1k"])

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.close()
        except Exception:
            pass
