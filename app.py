"""
app.py — SparingApp: orkestrator utama yang menghubungkan semua modul.
Menjalankan dua background thread (sensor & network) dan GUI di main thread.
"""

import json
import math
import queue
import random
import time
import threading
import logging
from typing import List, Optional

import tkinter as tk

from config      import load_config, save_config
from models      import SensorReading
from sensors     import SensorReader
from network     import NetworkManager
from storage     import DataStorage
from gui         import SparingGUI
import gap_filler

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
        self._noise_buf: List[float] = []         # buffer sampel noise 1 menit
        self._noise_buf_lock = threading.Lock()   # proteksi akses antar-thread
        self._sensor_wake = threading.Event()     # set() untuk mempersingkat sleep sensor loop

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
        threading.Thread(target=self._noise_loop,
                         daemon=True, name="noise").start()

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

        # Deteksi dan isi gap otomatis saat startup
        self._fill_gaps(auto=True)

        while self._running:
            try:
                use_hw  = bool(self.sensor_rdr and self.sensor_rdr._port_ok)
                r       = self.sensor_rdr.read_all() if use_hw else self._simulate()
                gap_filler.save_state(r)   # simpan pembacaan terakhir untuk gap fill
                port_ok = bool(self.sensor_rdr and self.sensor_rdr._port_ok)

                self.root.after(0, self.gui.update_connection, "rs485", port_ok)

                # ── Leq noise — dari buffer yang diisi _noise_loop (per 1 menit) ─
                if self.cfg.get("sensor_noise_enabled", True):
                    with self._noise_buf_lock:
                        buf_copy = list(self._noise_buf)
                    leq = self._compute_leq(buf_copy)
                    # Pakai Leq dari buffer jika tersedia; jika buffer masih kosong
                    # (< 1 menit pertama) pertahankan nilai dari _simulate()
                    if leq > 0:
                        r.noise = leq

                self.batch.append(r)
                n        = len(self.batch)
                mode_tag = "" if use_hw else "[SIM] "

                # Hitung nilai processed untuk ditampilkan di GUI
                proc_ph, proc_tss, proc_debit, \
                proc_pm25, proc_pm10, proc_pm100, \
                proc_noise = self.net.get_processed(r)

                self._log(
                    f"{mode_tag}Data {n}/{batch_size} — "
                    f"pH={r.ph:.2f}  TSS={r.tss:.2f} mg/L  "
                    f"Debit={r.debit:.2f} m³/s  "
                    f"PM2.5={r.pm25:.1f}  PM10={r.pm10:.1f}  PM100={r.pm100:.1f} ug/m³  "
                    f"Leq={r.noise:.1f} dB  Temp={r.temp:.1f}°C"
                )
                self.root.after(0, self.gui.update_sensors, r)
                self.root.after(0, self.gui.update_sensors_processed,
                                proc_ph, proc_tss, proc_debit)
                self.root.after(0, self.gui.update_dust_processed,
                                proc_pm25, proc_pm10, proc_pm100)
                self.root.after(0, self.gui.update_noise_processed, proc_noise)
                self.root.after(0, self.gui.update_count, n, batch_size)

                # Server 1: kualitas air (pH, TSS, Debit) — per 2 menit
                self._send_s1_water(r)

                # Server 2: kirim saat batch penuh (jika diaktifkan)
                if n >= batch_size:
                    if self.cfg.get("server2_enabled", True):
                        self._send_s2_batch()
                    else:
                        self._log("[S2] Pengiriman Server 2 dinonaktifkan — batch dibuang")
                    self.batch.clear()
                    self.root.after(0, self.gui.update_count, 0, batch_size)

            except Exception as e:
                self._log(f"[ERROR] sensor loop: {e}")
                self.root.after(0, self.gui.update_connection, "rs485", False)

            self._sensor_wake.wait(timeout=interval)
            self._sensor_wake.clear()

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

    # ── Noise loop — sampling noise setiap 1 menit untuk Leq ─────────────────
    def _noise_loop(self) -> None:
        """
        Sampel noise sensor setiap 60 detik (1 menit).
        Leq 10 menit = rata-rata energi dari 10 sampel terakhir.
        Leq = 10 × log10( (1/N) × Σ 10^(Li/10) )
        """
        _SAMPLE_SEC = 60      # interval sampling (detik)
        _WINDOW_SEC = 600     # jendela Leq (detik) — 10 menit
        _MAX_N      = _WINDOW_SEC // _SAMPLE_SEC   # 10 sampel

        time.sleep(2)   # tunggu GUI & sensor reader siap

        while self._running:
            try:
                now    = time.time()
                use_hw = bool(self.sensor_rdr and self.sensor_rdr._port_ok)

                # Baca noise
                if self.cfg.get("sensor_noise_enabled", True):
                    noise = (self.sensor_rdr.read_noise_safe()
                             if self.sensor_rdr
                             else round(random.uniform(
                                 self.cfg.get("sim_noise_min", 40.0),
                                 self.cfg.get("sim_noise_max", 80.0)), 1))
                    if noise > 0:
                        with self._noise_buf_lock:
                            self._noise_buf.append(noise)
                            if len(self._noise_buf) > _MAX_N:
                                self._noise_buf.pop(0)
                    self.root.after(0, self.gui.update_noise_instant, noise)
                else:
                    noise = 0.0

                # Baca debu (PM)
                if self.cfg.get("sensor_dust_enabled", True):
                    if self.sensor_rdr:
                        pm25, pm10, tsp = self.sensor_rdr.read_dust_safe()
                    else:
                        tsp  = round(random.uniform(
                                self.cfg.get("sim_tsp_min", 30.0),
                                self.cfg.get("sim_tsp_max", 200.0)), 1)
                        pm25 = round(random.uniform(
                            self.cfg.get("pm25_factor_min", 0.1),
                            self.cfg.get("pm25_factor_max", 0.2)) * tsp, 1)
                        pm10 = round(random.uniform(
                            self.cfg.get("pm10_factor_min", 0.3),
                            self.cfg.get("pm10_factor_max", 0.4)) * tsp, 1)
                else:
                    pm25 = pm10 = tsp = 0.0

                # Kirim ke Server 1 (per 1 menit)
                self._send_s1_env(pm25, pm10, tsp, noise, now)

            except Exception as e:
                self._log(f"[ERROR] noise loop: {e}")
            time.sleep(_SAMPLE_SEC)

    # ── Kirim ke Server 1 — kualitas air, per 2 menit ────────────────────────
    def _send_s1_water(self, r: SensorReading) -> None:
        """
        Kirim data kualitas air (pH, TSS, Debit) ke Server 1 setiap 2 menit.
        Format JWT flat: uid, pH, tss, debit, cod, nh3n, datetime, tl.
        """
        int_on  = self.cfg.get("logger_internal", True)
        klhk_on = self.cfg.get("logger_klhk",     False)
        jwts = []
        if int_on:
            j = self.net.create_jwt1_water(r, processed=False)
            if j: jwts.append(("Internal", j))
        if klhk_on:
            j = self.net.create_jwt1_water(r, processed=True)
            if j: jwts.append(("KLHK", j))
        if not jwts:
            return

        online = self.net.check_internet()
        ok_any = False
        for tag, jwt in jwts:
            if not online:
                self.storage_s1.save(jwt_s1=jwt)
                continue
            ok = self.net.post(self.cfg["server_url1"],
                               json.dumps({"token": jwt}))
            self.root.after(0, self.gui.update_connection, "server1", ok)
            if ok:
                ok_any = True
                self._log(f"✓ [S1-W/{tag}] pH={r.ph}  TSS={r.tss}  Debit={r.debit:.2f}")
            else:
                self._log(f"✗ [S1-W/{tag}] Gagal — disimpan ke buffer")
                self.storage_s1.save(jwt_s1=jwt)
        if ok_any:
            self.last_tx = r.timestamp
            self.root.after(0, self.gui.update_last_tx, self.last_tx)
        self.root.after(0, self.gui.update_buffer,
                        self.storage_s1.count() + self.storage_s2.count())

    # ── Kirim ke Server 1 — per 1 menit (pm + noise + link_video_id) ──────────
    def _send_s1_env(self, pm25: float, pm10: float, tsp: float,
                     noise: float, timestamp: float) -> None:
        """
        Kirim data lingkungan (debu + noise) ke Server 1 setiap 1 menit.
        Format: raw JSON langsung (uid, pm_25, pm_10, tsp, noise, temp,
                datetime_unix, link_video_id) — tanpa JWT wrapper.
        Jika offline atau gagal, simpan ke buffer untuk dikirim ulang.
        """
        link_video_id = self.cfg.get("link_video_id", "")
        int_on  = self.cfg.get("logger_internal", True)
        klhk_on = self.cfg.get("logger_klhk",     False)
        jwts = []
        if int_on:
            j = self.net.create_jwt_s1_env(pm25, pm10, tsp, noise,
                                            timestamp, link_video_id,
                                            processed=False)
            if j: jwts.append(("Internal", j))
        if klhk_on:
            j = self.net.create_jwt_s1_env(pm25, pm10, tsp, noise,
                                            timestamp, link_video_id,
                                            processed=True)
            if j: jwts.append(("KLHK", j))
        if not jwts:
            return

        online = self.net.check_internet()
        # Kirim ulang buffer lama (sekali saja)
        if online:
            flushed = self.storage_s1.flush_s1_env(self.net)
            if flushed:
                self._log(f"[S1] {flushed} data lama dari buffer berhasil dikirim ulang")

        ok_any = False
        for tag, jwt in jwts:
            if not online:
                self.storage_s1.save(jwt_s1=jwt)
                continue
            ok = self.net.post(self.cfg["server_url1"],
                               json.dumps({"token": jwt}))
            self.root.after(0, self.gui.update_connection, "server1", ok)
            if ok:
                ok_any = True
                self._log(f"✓ [S1/{tag}] PM+Noise  "
                          f"PM2.5={pm25} PM10={pm10} TSP={tsp} Noise={noise} dB")
            else:
                self._log(f"✗ [S1/{tag}] Gagal — disimpan ke buffer")
                self.storage_s1.save(jwt_s1=jwt)
        if ok_any:
            self.last_tx = timestamp
            self.root.after(0, self.gui.update_last_tx, self.last_tx)
        self.root.after(0, self.gui.update_buffer,
                        self.storage_s1.count() + self.storage_s2.count())

    # ── Kirim batch 30 data ────────────────────────────────────────────────────
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

    # ── Leq — equivalent continuous sound level ───────────────────────────────
    @staticmethod
    def _compute_leq(values: List[float]) -> float:
        """
        Hitung Leq dari daftar nilai dB.
        Leq = 10 × log10( (1/N) × Σ 10^(Li/10) )
        Nilai 0.0 dilewati (data tidak valid / sensor belum siap).
        """
        valid = [v for v in values if v > 0]
        if not valid:
            return 0.0
        mean_energy = sum(10 ** (v / 10) for v in valid) / len(valid)
        return round(10 * math.log10(mean_energy), 1)

    # ── Floating Mode — data acak dalam batas yang dikonfigurasi ─────────────
    def _simulate(self) -> SensorReading:
        c   = self.cfg
        tsp = round(random.uniform(c.get("sim_tsp_min",  30.0),
                                   c.get("sim_tsp_max", 200.0)), 1)
        f25 = random.uniform(c.get("pm25_factor_min", 0.1), c.get("pm25_factor_max", 0.2))
        f10 = random.uniform(c.get("pm10_factor_min", 0.3), c.get("pm10_factor_max", 0.4))
        return SensorReading(
            timestamp = time.time(),
            ph        = round(random.uniform(c.get("sim_ph_min",    7.5),
                                             c.get("sim_ph_max",    7.6)),  2),
            tss       = round(random.uniform(c.get("sim_tss_min",   80.0),
                                             c.get("sim_tss_max",   90.0)), 2),
            debit     = round(random.uniform(c.get("sim_debit_min", 0.01),
                                             c.get("sim_debit_max", 0.10)), 2),
            temp      = round(random.uniform(c.get("sim_temp_min",  25.0),
                                             c.get("sim_temp_max",  30.0)), 1),
            pm100     = tsp,
            pm25      = round(f25 * tsp, 1),
            pm10      = round(f10 * tsp, 1),
            noise     = round(random.uniform(c.get("sim_noise_min", 40.0),
                                             c.get("sim_noise_max", 80.0)), 1),
        )

    def toggle_test_mode(self) -> None:
        """Aktifkan/nonaktifkan floating mode dari tombol GUI."""
        self.cfg["simulate_sensors"] = not self.cfg.get("simulate_sensors", False)
        save_config(self.cfg)
        is_test = self.cfg["simulate_sensors"]
        self.root.after(0, self.gui.update_test_mode_btn, is_test)
        if is_test:
            if self.sensor_rdr:
                self.sensor_rdr._mb      = None
                self.sensor_rdr._port_ok = False
            self._log("[MODE] Floating Mode diaktifkan — data dari sensor dinonaktifkan")
        else:
            self._log("[MODE] Floating Mode dinonaktifkan — mencoba koneksi hardware...")
            def _do_reconnect():
                ok   = self.sensor_rdr.reconnect() if self.sensor_rdr else False
                port = self.cfg.get("serial_port", "—")
                self.root.after(0, self.gui.update_connection, "rs485", ok)
                self.root.after(0, self.gui.log,
                                f"RS485 {'terhubung' if ok else 'GAGAL'} — {port}")
            threading.Thread(target=_do_reconnect,
                             daemon=True, name="reconnect_fm").start()

    # ── Gap fill — isi slot kosong ke Server 1 ────────────────────────────────
    def _fill_gaps(self, auto: bool = False) -> None:
        """
        Deteksi dan kirim data gap ke Server 1.
        auto=True  → dipanggil otomatis saat startup (tidak update tombol GUI)
        auto=False → dipanggil dari tombol GUI
        """
        interval = self.cfg["interval_seconds"]
        slots    = gap_filler.detect_and_fill(interval)

        if not slots:
            msg = "[GAP] Tidak ada gap data yang perlu diisi"
            self._log(msg)
            if not auto:
                self.root.after(0, self.gui.gap_btn_reset)
            return

        gap_min = (slots[-1].timestamp - slots[0].timestamp + interval) / 60
        self._log(
            f"[GAP] Mengisi {len(slots)} slot "
            f"({gap_min:.0f} menit) → Server 1..."
        )

        online = self.net.check_internet()
        sent = saved = 0

        for i, r in enumerate(slots, 1):
            # ── Kualitas air ──────────────────────────────────────────────────
            jwt_w = self.net.create_jwt1_water(r)
            if jwt_w:
                if online and self.net.post(
                        self.cfg["server_url1"],
                        json.dumps({"token": jwt_w})):
                    sent += 1
                else:
                    self.storage_s1.save(jwt_s1=jwt_w)
                    saved += 1

            # ── Kualitas udara ────────────────────────────────────────────────
            link  = self.cfg.get("link_video_id", "")
            jwt_e = self.net.create_jwt_s1_env(
                r.pm25, r.pm10, r.pm100, r.noise, r.timestamp, link)
            if jwt_e:
                if online and self.net.post(
                        self.cfg["server_url1"],
                        json.dumps({"token": jwt_e})):
                    sent += 1
                else:
                    self.storage_s1.save(jwt_s1=jwt_e)
                    saved += 1

            # Log setiap 10 slot
            if i % 10 == 0 or i == len(slots):
                self._log(f"[GAP] Progress {i}/{len(slots)} slot")

        self._log(
            f"[GAP] Selesai — {sent} terkirim langsung, "
            f"{saved} disimpan ke buffer"
        )
        self.root.after(0, self.gui.update_buffer,
                        self.storage_s1.count() + self.storage_s2.count())
        if not auto:
            self.root.after(0, self.gui.gap_btn_reset)

    def trigger_gap_fill(self) -> None:
        """Dipanggil dari tombol GUI — jalankan gap fill di background thread."""
        self.root.after(0, self.gui.gap_btn_busy)
        threading.Thread(
            target=self._fill_gaps,
            kwargs={"auto": False},
            daemon=True,
            name="gap_fill",
        ).start()

    def _quit(self) -> None:
        self._running = False
        if self.sensor_rdr:
            self.sensor_rdr.close()
        self.root.destroy()
