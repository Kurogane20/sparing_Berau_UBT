"""
app.py — SparingApp: orkestrator utama yang menghubungkan semua modul.
Menjalankan dua background thread (sensor & network) dan GUI di main thread.
"""

import json
import queue
import random
import time
import threading
import logging
from typing import List, Optional

import tkinter as tk

from config   import load_config, save_config
from models   import SensorReading
from sensors  import SensorReader
from network  import NetworkManager
from storage  import DataStorage
from gui      import SparingGUI

log = logging.getLogger(__name__)


class SparingApp:
    """
    Orkestrator aplikasi SPARING Monitor.

    Thread model:
      main thread  → GUI (tkinter mainloop)
      thread sensor  → baca sensor setiap interval, kirim data saat batch penuh
      thread network → cek internet & ambil secret key setiap 30 detik

    Komunikasi thread → GUI melalui root.after(0, callback).
    Log dari thread dikirim lewat queue dan di-pump ke GUI tiap 150 ms.
    """

    def __init__(self) -> None:
        self.cfg        = load_config()
        self.sensor_rdr: Optional[SensorReader] = None
        self.net        = NetworkManager(self.cfg)
        self.storage    = DataStorage()
        self.batch: List[SensorReading] = []
        self.last_tx    = 0.0
        self._running   = True
        self._q: queue.Queue = queue.Queue()

    def start(self) -> None:
        # Inisialisasi sensor reader (gagal graceful → simulasi aktif)
        try:
            self.sensor_rdr = SensorReader(self.cfg, on_error=self._log)
        except Exception as e:
            log.warning(f"SensorReader init gagal (simulasi aktif): {e}")
            self.sensor_rdr = None

        # Bangun GUI
        self.root = tk.Tk()
        self.gui  = SparingGUI(self.root, self)

        # Tampilkan status RS485 setelah GUI selesai dirender
        self.root.after(500, self._post_init)

        # Background threads
        threading.Thread(target=self._sensor_loop,
                         daemon=True, name="sensor").start()
        threading.Thread(target=self._network_loop,
                         daemon=True, name="network").start()

        # Pompa antrian log ke GUI
        self._pump_log()

        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        self.root.mainloop()

    # ── Post-init: status awal setelah GUI siap ────────────────────────────────
    def _post_init(self) -> None:
        ok   = bool(self.sensor_rdr and self.sensor_rdr._port_ok)
        port = self.cfg.get("serial_port", "—")
        self.gui.update_connection("rs485", ok)
        if ok:
            self.gui.log(f"USB RS485 terhubung pada {port}")
        else:
            self.gui.log(f"USB RS485 tidak terdeteksi — port: {port}")
            if not self.cfg.get("simulate_sensors"):
                self.gui.log("→ Klik  ⌕ Scan Port  untuk mencari port USB RS485 Anda")

    # ── Log pump (main thread, via root.after) ─────────────────────────────────
    def _pump_log(self) -> None:
        while not self._q.empty():
            try:
                self.gui.log(self._q.get_nowait())
            except queue.Empty:
                break
        self.root.after(150, self._pump_log)

    def _log(self, msg: str) -> None:
        log.info(msg)
        self._q.put(msg)

    # ── Sensor loop (background thread) ───────────────────────────────────────
    def _sensor_loop(self) -> None:
        batch_size = self.cfg["data_batch_size"]
        interval   = self.cfg["interval_seconds"]
        time.sleep(2)   # beri waktu GUI load

        while self._running:
            try:
                use_hw  = bool(self.sensor_rdr and self.sensor_rdr._port_ok)
                r       = self.sensor_rdr.read_all() if use_hw else self._simulate()
                port_ok = bool(self.sensor_rdr and self.sensor_rdr._port_ok)

                self.root.after(0, self.gui.update_connection, "rs485", port_ok)
                self.batch.append(r)
                n        = len(self.batch)
                mode_tag = "" if use_hw else "[SIM] "

                self._log(
                    f"{mode_tag}Data {n}/{batch_size} — "
                    f"pH={r.ph:.2f}  TSS={r.tss:.2f} mg/L  "
                    f"Debit={r.debit:.4f} m³/s"
                )
                self.root.after(0, self.gui.update_sensors, r)
                self.root.after(0, self.gui.update_count, n, batch_size)

                if n >= batch_size:
                    self._send_batch()
                    self.batch.clear()
                    self.root.after(0, self.gui.update_count, 0, batch_size)

            except Exception as e:
                self._log(f"[ERROR] sensor loop: {e}")
                self.root.after(0, self.gui.update_connection, "rs485", False)

            time.sleep(interval)

    # ── Network loop (background thread) ──────────────────────────────────────
    def _network_loop(self) -> None:
        time.sleep(3)
        while self._running:
            try:
                # 1. Cek koneksi internet
                internet_ok = self.net.check_internet()
                self.root.after(0, self.gui.update_connection, "internet", internet_ok)

                if internet_ok:
                    # 2. Ambil secret key (sekali saja saat pertama kali online)
                    if not self.net.keys_fetched:
                        self._log("Mengambil secret key dari server...")
                        self.net.fetch_all_keys()
                        self._log("Secret key berhasil diperoleh")

                    # 3. Cek keterjangkauan kedua server secara independen
                    s1_ok = self.net.check_server(self.cfg["secret_key_url1"])
                    s2_ok = self.net.check_server(self.cfg["secret_key_url2"])
                else:
                    # Tidak perlu cek server jika internet sudah mati
                    s1_ok = False
                    s2_ok = False

                self.root.after(0, self.gui.update_connection, "server1", s1_ok)
                self.root.after(0, self.gui.update_connection, "server2", s2_ok)

            except Exception as e:
                self._log(f"[ERROR] network loop: {e}")
            time.sleep(30)

    # ── Kirim batch 30 data ────────────────────────────────────────────────────
    def _send_batch(self) -> None:
        batch  = list(self.batch)
        online = self.net.check_internet()
        jwt1   = self.net.create_jwt1(batch)
        jwt2   = self.net.create_jwt2(batch)

        now = time.time()

        if not jwt1 or not jwt2:
            self._log("JWT gagal dibuat — secret key belum ada, data disimpan offline")
            if jwt1 or jwt2:
                self.storage.save(jwt1, jwt2)
            self.root.after(0, self.gui.update_send_offline, now)
            self.root.after(0, self.gui.update_buffer, self.storage.count())
            return

        if not online:
            self._log("Offline — data batch disimpan ke buffer")
            self.storage.save(jwt1, jwt2)
            self.root.after(0, self.gui.update_connection, "internet", False)
            self.root.after(0, self.gui.update_send_offline, now)
            self.root.after(0, self.gui.update_buffer, self.storage.count())
            return

        # Kirim ulang buffer lama terlebih dahulu
        flushed = self.storage.flush(self.net)
        if flushed:
            self._log(f"{flushed} batch lama dari buffer berhasil dikirim ulang")

        # Kirim batch saat ini
        body1 = json.dumps({"token": jwt1})
        body2 = json.dumps({"token": jwt2})
        ok1   = self.net.post(self.cfg["server_url1"], body1)
        ok2   = self.net.post(self.cfg["server_url2"], body2)
        now   = time.time()

        self.root.after(0, self.gui.update_connection, "server1", ok1)
        self.root.after(0, self.gui.update_connection, "server2", ok2)
        self.root.after(0, self.gui.update_send_status, ok1, ok2, now)

        if ok1 and ok2:
            self.last_tx = now
            self.root.after(0, self.gui.update_last_tx, self.last_tx)
            self._log("✓ Data batch berhasil dikirim ke Server 1 & Server 2")
        else:
            status = f"S1={'OK' if ok1 else 'GAGAL'}  S2={'OK' if ok2 else 'GAGAL'}"
            self._log(f"Pengiriman sebagian gagal ({status}) — disimpan ke buffer")
            self.storage.save(jwt1, jwt2)

        self.root.after(0, self.gui.update_buffer, self.storage.count())

        # Perbarui secret key setelah setiap siklus kirim
        threading.Thread(target=self.net.fetch_all_keys, daemon=True).start()

    # ── Simulasi data sensor (tanpa hardware) ──────────────────────────────────
    @staticmethod
    def _simulate() -> SensorReading:
        return SensorReading(
            timestamp = time.time(),
            ph        = round(random.uniform(6.5, 8.5), 2),
            tss       = round(random.uniform(50.0, 110.0), 2),
            debit     = round(random.uniform(0.010, 0.035), 5),
        )

    def _quit(self) -> None:
        self._running = False
        if self.sensor_rdr:
            self.sensor_rdr.close()
        self.root.destroy()
