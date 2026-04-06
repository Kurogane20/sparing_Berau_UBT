"""
storage.py — Buffer offline untuk menyimpan JWT batch ketika tidak ada internet.
Data disimpan di data_buffer.json dan dikirim ulang saat koneksi kembali.
"""

import json
import time
import logging

from config  import DATA_BUFFER_FILE
from network import NetworkManager

log = logging.getLogger(__name__)


class DataStorage:
    """
    Menyimpan batch JWT ke file lokal saat offline,
    dan mengirim ulang semua batch yang tersimpan saat online.
    """

    def save(self, jwt1: str, jwt2: str) -> None:
        """Tambahkan satu batch ke buffer file."""
        entries = self._load()
        entries.append({"jwt1": jwt1, "jwt2": jwt2, "ts": time.time()})
        self._write(entries)
        log.info(f"Buffer offline: {len(entries)} batch tersimpan")

    def flush(self, net: NetworkManager) -> int:
        """
        Coba kirim semua batch yang tersimpan.
        Kembalikan jumlah batch yang berhasil dikirim.
        """
        entries = self._load()
        if not entries:
            return 0

        remaining = []
        sent = 0
        for e in entries:
            body1 = json.dumps({"token": e.get("jwt1", "")})
            body2 = json.dumps({"token": e.get("jwt2", "")})
            ok1 = net.post(net.cfg["server_url1"], body1)
            ok2 = net.post(net.cfg["server_url2"], body2)
            if ok1 and ok2:
                sent += 1
            else:
                remaining.append(e)

        self._write(remaining)
        if sent:
            log.info(f"{sent} batch dari buffer berhasil dikirim ulang")
        return sent

    def count(self) -> int:
        """Jumlah batch yang masih tersimpan di buffer."""
        return len(self._load())

    # ── Internal ──────────────────────────────────────────────────────────────
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
