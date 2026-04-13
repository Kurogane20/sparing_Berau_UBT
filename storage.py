"""
storage.py — Buffer offline untuk menyimpan JWT batch ketika tidak ada internet.
Data disimpan di file JSON dan dikirim ulang saat koneksi kembali.

Digunakan oleh dua jalur pengiriman berbeda:
  DataStorage("data_buffer_s1.json") → buffer Server 1 (per-reading, tiap 2 menit)
  DataStorage("data_buffer_s2.json") → buffer Server 2 (per-batch, tiap 30 data)
"""

import json
import time
import logging
from pathlib import Path

log = logging.getLogger(__name__)


class DataStorage:
    """
    Buffer offline generik — menyimpan dict payload ke file JSON.
    Setiap entri adalah dict bebas (jwt1_raw, jwt1_proc, jwt2, dst.)
    beserta timestamp.
    """

    def __init__(self, filepath: str = "data_buffer.json"):
        self._file = Path(filepath)

    def save(self, **tokens) -> None:
        """Tambahkan satu entri ke buffer."""
        entries = self._load()
        entries.append({"ts": time.time(), **tokens})
        self._write(entries)
        log.info(f"[{self._file.name}] buffer: {len(entries)} entri")

    def flush_s1(self, net) -> int:
        """
        Kirim ulang semua entri Server 1 yang tersimpan.
        Setiap entri berisi jwt1_raw dan jwt1_proc.
        Kembalikan jumlah entri yang berhasil dikirim.
        """
        entries = self._load()
        if not entries:
            return 0
        remaining, sent = [], 0
        url = net.cfg["server_url1"]
        for e in entries:
            raw  = e.get("jwt1_raw",  "")
            proc = e.get("jwt1_proc", "")
            # Buang entri dengan token kosong — tidak perlu dikirim ulang
            if not raw:
                sent += 1   # anggap selesai, hapus dari buffer
                continue
            ok_r = net.post(url, json.dumps({"token": raw}))
            ok_p = net.post(url, json.dumps({"token": proc})) if proc else True
            if ok_r and ok_p:
                sent += 1
            else:
                remaining.append(e)
        self._write(remaining)
        if sent:
            log.info(f"[S1 buffer] {sent} entri berhasil dikirim ulang")
        return sent

    def flush_s1_env(self, net) -> int:
        """
        Kirim ulang entri Server 1 format baru (raw JSON per 1 menit).
        Setiap entri berisi body_s1 (raw JSON string).
        Entri format lama (jwt_env / jwt1_raw) dibuang otomatis.
        """
        entries = self._load()
        if not entries:
            return 0
        remaining, sent = [], 0
        url = net.cfg["server_url1"]
        for e in entries:
            jwt = e.get("jwt_s1", "")
            if not jwt:
                sent += 1   # buang entri format lama / kosong
                continue
            if net.post(url, json.dumps({"token": jwt})):
                sent += 1
            else:
                remaining.append(e)
        self._write(remaining)
        if sent:
            log.info(f"[S1 env buffer] {sent} entri berhasil dikirim ulang")
        return sent

    def flush_s2(self, net) -> int:
        """
        Kirim ulang semua entri Server 2 yang tersimpan.
        Setiap entri berisi jwt2.
        Kembalikan jumlah entri yang berhasil dikirim.
        """
        entries = self._load()
        if not entries:
            return 0
        remaining, sent = [], 0
        url = net.cfg["server_url2"]
        for e in entries:
            jwt2 = e.get("jwt2", "")
            if not jwt2:
                sent += 1   # buang entri dengan token kosong
                continue
            if net.post(url, json.dumps({"token": jwt2})):
                sent += 1
            else:
                remaining.append(e)
        self._write(remaining)
        if sent:
            log.info(f"[S2 buffer] {sent} entri berhasil dikirim ulang")
        return sent

    def count(self) -> int:
        """Jumlah entri yang masih tersimpan di buffer."""
        return len(self._load())

    # ── Internal ──────────────────────────────────────────────────────────────
    def _load(self) -> list:
        if self._file.exists():
            try:
                with open(self._file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return []

    def _write(self, entries: list) -> None:
        try:
            with open(self._file, "w", encoding="utf-8") as f:
                json.dump(entries, f)
        except Exception as e:
            log.error(f"Buffer write error ({self._file.name}): {e}")
