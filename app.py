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
        self.net        = NetworkManager(self.cfg, on_log=self._log)
        # Server 1: dikirim setiap pembacaan (2 menit), buffer terpisah
        self.storage_s1 = DataStorage("data_buffer_s1.json")
        # Server 2: dikirim setiap batch penuh (30 data), buffer terpisah
        self.storage_s2 = DataStorage("data_buffer_s2.json")
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
        # Kirim ke server log secara async
        if self.cfg.get("log_url"):
            lvl = ("ERROR"   if "[ERROR]"   in msg else
                   "WARNING" if "[WARN]"    in msg else
                   "DEBUG"   if "[DEBUG]"   in msg else "INFO")
            threading.Thread(
                target=self.net.post_log,
                args=(msg, lvl),
                daemon=True,
                name="log_send",
            ).start()

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

                # Hitung nilai processed untuk ditampilkan di GUI
                proc_ph, proc_tss, proc_debit, \
                proc_pm25, proc_pm10, proc_pm100 = self.net.get_processed(r)

                self._log(
                    f"{mode_tag}Data {n}/{batch_size} — "
                    f"pH={r.ph:.2f}  TSS={r.tss:.2f} mg/L  "
                    f"Debit={r.debit:.4f} m³/s  "
                    f"PM2.5={r.pm25:.1f}  PM10={r.pm10:.1f}  PM100={r.pm100:.1f} ug/m³"
                )
                self.root.after(0, self.gui.update_sensors, r)
                self.root.after(0, self.gui.update_sensors_processed,
                                proc_ph, proc_tss, proc_debit)
                self.root.after(0, self.gui.update_dust_processed,
                                proc_pm25, proc_pm10, proc_pm100)
                self.root.after(0, self.gui.update_count, n, batch_size)

                # Server 1: kirim setiap pembacaan (per 2 menit)
                self._send_s1([r])

                # Server 2: kirim saat batch penuh
                if n >= batch_size:
                    self._send_s2_batch()
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
    # ── Kirim ke Server 1 — setiap pembacaan (per 2 menit) ───────────────────
    def _send_s1(self, readings: List[SensorReading]) -> None:
        """
        Kirim 1 data terbaru ke Server 1:
          • jwt1_raw  — data murni sensor
          • jwt1_proc — data setelah filter min/max
        Jika offline atau gagal, simpan ke buffer_s1 untuk dikirim ulang nanti.
        """
        jwt1_raw  = self.net.create_jwt1_raw(readings)
        jwt1_proc = self.net.create_jwt1_processed(readings)
        now       = time.time()

        if not jwt1_raw:
            self._log("[S1] JWT gagal — secret key belum ada, data dibuang")
            return

        online = self.net.check_internet()
        if not online:
            self.storage_s1.save(jwt1_raw=jwt1_raw, jwt1_proc=jwt1_proc)
            return   # tidak log setiap 2 menit agar log bersih

        # Kirim ulang buffer lama
        flushed = self.storage_s1.flush_s1(self.net)
        if flushed:
            self._log(f"[S1] {flushed} data lama dari buffer berhasil dikirim ulang")

        url1 = self.cfg["server_url1"]
        ok1r = self.net.post(url1, json.dumps({"token": jwt1_raw}))
        ok1p = self.net.post(url1, json.dumps({"token": jwt1_proc}))
        ok1  = ok1r and ok1p
        now  = time.time()

        self.root.after(0, self.gui.update_connection, "server1", ok1)

        if ok1:
            self.last_tx = now
            self.root.after(0, self.gui.update_last_tx, self.last_tx)
            self._log(f"✓ [S1] Data terkirim (raw + processed)")
        else:
            parts = []
            if not ok1r: parts.append("raw GAGAL")
            if not ok1p: parts.append("processed GAGAL")
            self._log(f"✗ [S1] {', '.join(parts)} — disimpan ke buffer")
            self.storage_s1.save(jwt1_raw=jwt1_raw, jwt1_proc=jwt1_proc)

        self.root.after(0, self.gui.update_buffer,
                        self.storage_s1.count() + self.storage_s2.count())

    # ── Kirim ke Server 2 — setiap batch penuh (30 data × 2 menit = 60 menit) ─
    def _send_s2_batch(self) -> None:
        """
        Kirim batch 30 data ke Server 2 (data processed dengan filter min/max).
        Jika offline atau gagal, simpan ke buffer_s2.
        """
        batch  = list(self.batch)
        jwt2   = self.net.create_jwt2(batch)
        now    = time.time()

        if not jwt2:
            self._log("[S2] JWT gagal — secret key belum ada, data dibuang")
            self.root.after(0, self.gui.update_send_offline, now)
            self.root.after(0, self.gui.update_buffer,
                            self.storage_s1.count() + self.storage_s2.count())
            return

        online = self.net.check_internet()
        if not online:
            self._log("[S2] Offline — batch disimpan ke buffer")
            self.storage_s2.save(jwt2=jwt2)
            self.root.after(0, self.gui.update_connection, "internet", False)
            self.root.after(0, self.gui.update_send_offline, now)
            self.root.after(0, self.gui.update_buffer,
                            self.storage_s1.count() + self.storage_s2.count())
            return

        # Kirim ulang buffer lama
        flushed = self.storage_s2.flush_s2(self.net)
        if flushed:
            self._log(f"[S2] {flushed} batch lama dari buffer berhasil dikirim ulang")

        ok2 = self.net.post(self.cfg["server_url2"],
                            json.dumps({"token": jwt2}))
        now = time.time()

        self.root.after(0, self.gui.update_connection, "server2", ok2)
        self.root.after(0, self.gui.update_send_status,
                        True, ok2, now)   # S1 selalu True di titik ini

        if ok2:
            self._log(f"✓ [S2] Batch {len(batch)} data berhasil dikirim ke Server 2")
        else:
            self._log(f"✗ [S2] Gagal — batch disimpan ke buffer")
            self.storage_s2.save(jwt2=jwt2)

        self.root.after(0, self.gui.update_buffer,
                        self.storage_s1.count() + self.storage_s2.count())

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
