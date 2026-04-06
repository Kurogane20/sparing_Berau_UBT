"""
gui.py — Antarmuka grafis SPARING Monitor menggunakan tkinter.
Ditampilkan via HDMI pada Raspberry Pi / Orange Pi / Windows.
Semua update dari thread lain harus menggunakan root.after(0, ...).
"""

import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from typing import TYPE_CHECKING

from constants import (
    C, LOGO_FILE, SYS_PLATFORM, IS_WINDOWS,
    HAS_PIL, HAS_SERIAL_TOOLS,
    Image, ImageTk, list_ports,
)
from config  import save_config, scan_serial_ports, DEFAULT_CONFIG
from models  import SensorReading

if TYPE_CHECKING:
    from app import SparingApp


class SparingGUI:
    """
    Jendela utama aplikasi SPARING Monitor.

    Layout:
      Header  — logo, judul, jam digital
      Left    — 5 kartu sensor, info pengiriman, log aktivitas
      Right   — status koneksi RS485/internet/server, batas data, tombol
      Footer  — status bar
    """

    def __init__(self, root: tk.Tk, app: "SparingApp"):
        self.root = root
        self.app  = app
        self.cfg  = app.cfg
        self._sensor_vars: dict = {}
        self._conn_labels: dict = {}
        self._setup_window()
        self._setup_styles()
        self._build()
        self._tick_clock()

    # ── Window ────────────────────────────────────────────────────────────────
    def _setup_window(self) -> None:
        self.root.title("SPARING Monitor — PT Sucofindo")
        self.root.configure(bg=C["bg"])
        self._is_fullscreen = tk.BooleanVar(value=True)
        self.root.attributes("-fullscreen", True)
        # F11 toggle fullscreen, ESC keluar fullscreen
        self.root.bind("<F11>", self._toggle_fullscreen)
        self.root.bind("<Escape>", self._exit_fullscreen)

    def _toggle_fullscreen(self, event=None) -> None:
        val = not self._is_fullscreen.get()
        self._is_fullscreen.set(val)
        self.root.attributes("-fullscreen", val)

    def _exit_fullscreen(self, event=None) -> None:
        self._is_fullscreen.set(False)
        self.root.attributes("-fullscreen", False)

    def _setup_styles(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure("TProgressbar",
                    troughcolor=C["border"],
                    background=C["primary"],
                    thickness=12)

    # ── Build ─────────────────────────────────────────────────────────────────
    def _build(self) -> None:
        self._build_header()
        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=14, pady=8)
        self._build_left(body)
        self._build_right(body)
        self._build_footer()

    # ── Header ────────────────────────────────────────────────────────────────
    def _build_header(self) -> None:
        # Garis aksen teal tipis di paling atas
        tk.Frame(self.root, bg=C["teal"], height=4).pack(fill="x")

        # Header utama — putih agar logo biru Sucofindo terlihat jelas
        hdr = tk.Frame(self.root, bg="white", height=70)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        # Logo di background putih — globe biru langsung terlihat kontras
        if HAS_PIL and Image is not None and LOGO_FILE.exists():
            try:
                img = Image.open(LOGO_FILE).resize((130, 60), Image.LANCZOS)
                self._logo = ImageTk.PhotoImage(img)
                tk.Label(hdr, image=self._logo,
                         bg="white").pack(side="left", padx=16, pady=5)
            except Exception:
                pass

        # Separator vertikal antara logo dan judul
        tk.Frame(hdr, bg=C["border"], width=1).pack(side="left", fill="y",
                                                      pady=10, padx=(0, 16))

        # Judul aplikasi — teks gelap di background putih
        title_box = tk.Frame(hdr, bg="white")
        title_box.pack(side="left", fill="y", pady=10)
        tk.Label(title_box, text="SISTEM PEMANTAUAN KUALITAS AIR",
                 bg="white", fg=C["primary_dark"],
                 font=("Segoe UI", 14, "bold")).pack(anchor="w")
        tk.Label(title_box, text="SPARING  —  Online Monitoring System",
                 bg="white", fg=C["primary"],
                 font=("Segoe UI", 9)).pack(anchor="w")

        # Jam & tanggal (kanan) — kotak biru sebagai aksen
        right_box = tk.Frame(hdr, bg=C["primary_dark"], padx=18)
        right_box.pack(side="right", fill="y")
        self._date_var  = tk.StringVar()
        self._clock_var = tk.StringVar()
        tk.Label(right_box, textvariable=self._date_var,
                 bg=C["primary_dark"], fg="#A8C8E8",
                 font=("Segoe UI", 9)).pack(anchor="e", pady=(12, 0))
        tk.Label(right_box, textvariable=self._clock_var,
                 bg=C["primary_dark"], fg=C["teal"],
                 font=("Segoe UI", 18, "bold")).pack(anchor="e")

    # ── Left panel ────────────────────────────────────────────────────────────
    def _build_left(self, parent: tk.Frame) -> None:
        left = tk.Frame(parent, bg=C["bg"])
        left.pack(side="left", fill="both", expand=True)

        # Sensor cards
        cards = tk.Frame(left, bg=C["bg"])
        cards.pack(fill="x", pady=(0, 8))
        defs = [
            ("pH",    "ph",    "",      C["primary_dark"]),
            ("TSS",   "tss",   "mg/L",  C["primary"]),
            ("DEBIT", "debit", "m³/s",  C["light_blue"]),
        ]
        for col, (lbl, key, unit, color) in enumerate(defs):
            self._sensor_card(cards, lbl, key, unit, color).grid(
                row=0, column=col, padx=5, pady=4, sticky="nsew")
            cards.columnconfigure(col, weight=1)

        # Info pengiriman
        info_card = self._card(left, "INFO PENGIRIMAN DATA", C["primary"])
        info_card.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(info_card, bg=C["card"])
        inner.pack(fill="x", padx=14, pady=8)

        self._count_var   = tk.StringVar(value="0 / 30")
        self._last_tx_var = tk.StringVar(value="—")
        self._buf_var     = tk.StringVar(value="0")
        self._progress    = tk.DoubleVar(value=0)

        self._info_row(inner, "Data Terkumpul :", self._count_var, C["primary_dark"])
        self._info_row(inner, "Kirim Terakhir :", self._last_tx_var, C["primary"])
        self._info_row(inner, "Buffer Offline  :", self._buf_var, C["teal"], " batch")
        ttk.Progressbar(inner, variable=self._progress,
                        maximum=30, style="TProgressbar").pack(fill="x", pady=(8, 0))

        # Log aktivitas
        log_card = self._card(left, "LOG AKTIVITAS", C["primary_dark"])
        log_card.pack(fill="both", expand=True)
        self._log_txt = tk.Text(
            log_card, height=9, state="disabled",
            font=("Consolas", 9), bg=C["log_bg"], fg=C["log_fg"],
            relief="flat", padx=8, pady=6, wrap="word",
        )
        self._log_txt.pack(fill="both", expand=True, padx=10, pady=(2, 10))
        sb = ttk.Scrollbar(self._log_txt, command=self._log_txt.yview)
        self._log_txt.configure(yscrollcommand=sb.set)

    # ── Right panel ───────────────────────────────────────────────────────────
    def _build_right(self, parent: tk.Frame) -> None:
        right = tk.Frame(parent, bg=C["bg"], width=270)
        right.pack(side="right", fill="y", padx=(10, 0))
        right.pack_propagate(False)

        # Status koneksi
        conn_card = self._card(right, "STATUS KONEKSI", C["primary"])
        conn_card.pack(fill="x", pady=(0, 8))
        inner = tk.Frame(conn_card, bg=C["card"])
        inner.pack(fill="x", padx=14, pady=8)

        for key, label in [
            ("rs485",    "RS485 USB"),
            ("internet", "Internet"),
            ("server1",  "Server 1"),
            ("server2",  "Server 2"),
        ]:
            var = tk.StringVar(value="Mengecek...")
            lbl = self._status_row(inner, label, var)
            self._conn_labels[key] = (var, lbl)

        # Info port aktif
        self._port_var = tk.StringVar(value=self.cfg.get("serial_port", "—"))
        port_row = tk.Frame(inner, bg=C["card"])
        port_row.pack(fill="x", pady=(6, 0))
        tk.Label(port_row, text="Port :", bg=C["card"], fg=C["text_muted"],
                 font=("Segoe UI", 9), width=10, anchor="w").pack(side="left")
        tk.Label(port_row, textvariable=self._port_var, bg=C["card"],
                 fg=C["primary_dark"], font=("Consolas", 9, "bold")).pack(side="left")

        # Tombol reconnect & scan
        btn_row = tk.Frame(inner, bg=C["card"])
        btn_row.pack(fill="x", pady=(6, 2))
        tk.Button(btn_row, text="↻ Hubungkan Ulang",
                  command=self._reconnect_rs485,
                  bg=C["primary"], fg="white",
                  font=("Segoe UI", 9, "bold"),
                  relief="flat", cursor="hand2", pady=4).pack(
            side="left", fill="x", expand=True, padx=(0, 3))
        tk.Button(btn_row, text="⌕ Scan Port",
                  command=self._scan_ports_dialog,
                  bg=C["primary_dark"], fg="white",
                  font=("Segoe UI", 9, "bold"),
                  relief="flat", cursor="hand2", pady=4).pack(
            side="left", fill="x", expand=True)

        # Status pengiriman data
        send_card = self._card(right, "STATUS PENGIRIMAN", C["primary"])
        send_card.pack(fill="x", pady=(0, 8))
        inner3 = tk.Frame(send_card, bg=C["card"])
        inner3.pack(fill="x", padx=14, pady=8)

        self._send_status_var = tk.StringVar(value="— Menunggu batch pertama")
        self._send_status_lbl = tk.Label(
            inner3, textvariable=self._send_status_var,
            bg=C["card"], fg=C["text_muted"],
            font=("Segoe UI", 10, "bold"),
            wraplength=220, justify="left",
        )
        self._send_status_lbl.pack(anchor="w")

        self._send_detail_var = tk.StringVar(value="")
        tk.Label(inner3, textvariable=self._send_detail_var,
                 bg=C["card"], fg=C["text_muted"],
                 font=("Segoe UI", 8),
                 wraplength=220, justify="left").pack(anchor="w", pady=(2, 0))

        # Ringkasan batas Server 2
        lim_card = self._card(right, "BATAS DATA  SERVER 2", C["light_blue"])
        lim_card.pack(fill="x", pady=(0, 8))
        lim_inner = tk.Frame(lim_card, bg=C["card"])
        lim_inner.pack(fill="x", padx=14, pady=(4, 8))

        # Header
        for col, txt in enumerate(["", "Min", "Max"]):
            tk.Label(lim_inner, text=txt, bg=C["card"], fg=C["primary_dark"],
                     font=("Segoe UI", 8, "bold"),
                     width=6 if col > 0 else 7).grid(
                row=0, column=col, sticky="w")

        # Nilai batas — diupdate via update_limits()
        self._limit_vars: dict = {}
        for i, (param, k_min, k_max) in enumerate([
            ("pH",    "limit_ph_min",    "limit_ph_max"),
            ("TSS",   "limit_tss_min",   "limit_tss_max"),
            ("Debit", "limit_debit_min", "limit_debit_max"),
        ], start=1):
            tk.Label(lim_inner, text=param, bg=C["card"], fg=C["text_muted"],
                     font=("Segoe UI", 9), width=7, anchor="w").grid(
                row=i, column=0, sticky="w", pady=1)
            for col, key in enumerate([k_min, k_max], start=1):
                v = tk.StringVar(value=str(self.cfg.get(key, "—")))
                self._limit_vars[key] = v
                tk.Label(lim_inner, textvariable=v, bg=C["card"],
                         fg=C["primary_dark"],
                         font=("Consolas", 9, "bold"),
                         width=6).grid(row=i, column=col, sticky="w", padx=(4, 0))

        # Tombol pengaturan
        tk.Button(right, text="⚙  Pengaturan Koneksi",
                  command=self._open_settings,
                  bg=C["primary_dark"], fg="white",
                  font=("Segoe UI", 10, "bold"),
                  relief="flat", cursor="hand2", pady=7).pack(
            fill="x", pady=(8, 0))

    # ── Footer ────────────────────────────────────────────────────────────────
    def _build_footer(self) -> None:
        bar = tk.Frame(self.root, bg=C["primary_dark"], height=28)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self._statusbar_var = tk.StringVar(value="Siap")
        tk.Label(bar, textvariable=self._statusbar_var,
                 bg=C["primary_dark"], fg="#A8C8E8",
                 font=("Segoe UI", 9)).pack(side="left", padx=12, pady=4)

        # Tombol fullscreen toggle (kanan footer)
        tk.Button(bar, text="⛶  Fullscreen  F11",
                  command=self._toggle_fullscreen,
                  bg=C["primary"], fg="white",
                  font=("Segoe UI", 8, "bold"),
                  relief="flat", cursor="hand2",
                  padx=8, pady=0).pack(side="right", padx=6, pady=4)

        mode = "SIMULASI" if self.cfg.get("simulate_sensors") else "LIVE"
        port = self.cfg.get("serial_port", "—")
        tk.Label(bar, text=f"Mode: {mode}  |  Port: {port}  |  Platform: {SYS_PLATFORM}  |  ESC = keluar fullscreen",
                 bg=C["primary_dark"], fg=C["light_blue"],
                 font=("Segoe UI", 9)).pack(side="right", padx=6, pady=4)

    # ── Widget helpers ─────────────────────────────────────────────────────────
    def _card(self, parent, title: str, accent: str) -> tk.Frame:
        outer = tk.Frame(parent, bg=C["card"],
                         highlightbackground=C["border"], highlightthickness=1)
        tk.Frame(outer, bg=accent, height=4).pack(fill="x")
        tk.Label(outer, text=title, bg=C["card"], fg=accent,
                 font=("Segoe UI", 9, "bold")).pack(anchor="w", padx=12, pady=(5, 1))
        return outer

    def _sensor_card(self, parent, label: str, key: str,
                     unit: str, color: str) -> tk.Frame:
        card = tk.Frame(parent, bg=C["card"],
                        highlightbackground=C["border"], highlightthickness=1)
        tk.Frame(card, bg=color, height=5).pack(fill="x")
        tk.Label(card, text=label, bg=C["card"], fg=C["text_muted"],
                 font=("Segoe UI", 9, "bold")).pack(pady=(8, 0))
        var = tk.StringVar(value="—")
        self._sensor_vars[key] = var
        tk.Label(card, textvariable=var, bg=C["card"], fg=color,
                 font=("Segoe UI", 28, "bold")).pack()
        tk.Label(card, text=unit, bg=C["card"], fg=C["text_muted"],
                 font=("Segoe UI", 9)).pack(pady=(0, 10))
        return card

    def _info_row(self, parent, label: str, var: tk.StringVar,
                  fg: str, suffix: str = "") -> None:
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text=label, bg=C["card"], fg=C["text_muted"],
                 font=("Segoe UI", 9), anchor="w", width=16).pack(side="left")
        tk.Label(row, textvariable=var, bg=C["card"], fg=fg,
                 font=("Segoe UI", 9, "bold")).pack(side="left")
        if suffix:
            tk.Label(row, text=suffix, bg=C["card"], fg=C["text_muted"],
                     font=("Segoe UI", 9)).pack(side="left")

    def _status_row(self, parent, label: str, var: tk.StringVar) -> tk.Label:
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=3)
        tk.Label(row, text=f"{label} :", bg=C["card"], fg=C["text_muted"],
                 font=("Segoe UI", 10), width=10, anchor="w").pack(side="left")
        lbl = tk.Label(row, textvariable=var, bg=C["card"],
                       fg=C["text_muted"], font=("Segoe UI", 10, "bold"))
        lbl.pack(side="left")
        return lbl

    # ── Clock ─────────────────────────────────────────────────────────────────
    def _tick_clock(self) -> None:
        now = datetime.now()
        self._clock_var.set(now.strftime("%H:%M:%S"))
        self._date_var.set(now.strftime("%d %B %Y"))
        self.root.after(1000, self._tick_clock)

    # ── Public update methods (dipanggil dari thread via root.after) ───────────
    def update_sensors(self, r: SensorReading) -> None:
        self._sensor_vars["ph"].set(f"{r.ph:.2f}")
        self._sensor_vars["tss"].set(f"{r.tss:.2f}")
        self._sensor_vars["debit"].set(f"{r.debit:.4f}")

    def update_count(self, n: int, total: int = 30) -> None:
        self._count_var.set(f"{n} / {total}")
        self._progress.set(n)

    def update_last_tx(self, ts: float) -> None:
        self._last_tx_var.set(datetime.fromtimestamp(ts).strftime("%d/%m %H:%M:%S"))

    def update_buffer(self, n: int) -> None:
        self._buf_var.set(str(n))

    def update_send_status(self, ok1: bool, ok2: bool, ts: float) -> None:
        """Perbarui kartu Status Pengiriman setelah satu batch dikirim."""
        waktu = datetime.fromtimestamp(ts).strftime("%d/%m/%Y  %H:%M:%S")
        if ok1 and ok2:
            self._send_status_var.set("✓  Berhasil Terkirim")
            self._send_status_lbl.configure(fg=C["teal"])
            self._send_detail_var.set(f"Server 1 & 2 OK\n{waktu}")
        elif ok1 or ok2:
            self._send_status_var.set("⚠  Sebagian Gagal")
            self._send_status_lbl.configure(fg="#FFA500")
            s1 = "OK" if ok1 else "GAGAL"
            s2 = "OK" if ok2 else "GAGAL"
            self._send_detail_var.set(f"S1: {s1}  |  S2: {s2}\n{waktu}")
        else:
            self._send_status_var.set("✗  Gagal Terkirim")
            self._send_status_lbl.configure(fg=C["red"])
            self._send_detail_var.set(f"Disimpan ke buffer offline\n{waktu}")

    def update_limits(self) -> None:
        """Perbarui tampilan kartu Batas Data Server 2 dari cfg terkini."""
        for key, var in self._limit_vars.items():
            var.set(str(self.cfg.get(key, "—")))

    def update_send_offline(self, ts: float) -> None:
        """Tampilkan status saat data disimpan offline (tidak ada internet)."""
        waktu = datetime.fromtimestamp(ts).strftime("%d/%m/%Y  %H:%M:%S")
        self._send_status_var.set("⬇  Disimpan Offline")
        self._send_status_lbl.configure(fg=C["primary"])
        self._send_detail_var.set(f"Akan dikirim saat online\n{waktu}")

    def update_connection(self, key: str, ok: bool) -> None:
        var, lbl = self._conn_labels[key]
        if ok:
            var.set("● Terhubung")
            lbl.configure(fg=C["teal"])
        else:
            var.set("● Terputus")
            lbl.configure(fg=C["red"])

    def log(self, msg: str) -> None:
        ts   = datetime.now().strftime("%H:%M:%S")
        full = f"[{ts}]  {msg}\n"
        self._log_txt.configure(state="normal")
        self._log_txt.insert("end", full)
        self._log_txt.see("end")
        self._log_txt.configure(state="disabled")
        self._statusbar_var.set(f"[{ts}] {msg}")

    # ── Reconnect RS485 ───────────────────────────────────────────────────────
    def _reconnect_rs485(self) -> None:
        self.log("Menghubungkan ulang USB RS485...")
        self.update_connection("rs485", False)

        def _do():
            ok   = self.app.sensor_rdr.reconnect() if self.app.sensor_rdr else False
            port = self.cfg.get("serial_port", "—")
            self.root.after(0, self.update_connection, "rs485", ok)
            self.root.after(0, self._port_var.set, port)
            self.root.after(0, self.log,
                            f"RS485 {'terhubung' if ok else 'GAGAL'} — {port}")

        threading.Thread(target=_do, daemon=True).start()

    # ── Scan port dialog ──────────────────────────────────────────────────────
    def _scan_ports_dialog(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Scan Port USB RS485")
        win.configure(bg=C["bg"])
        win.geometry("420x320")
        win.grab_set()

        tk.Frame(win, bg=C["primary_dark"], height=4).pack(fill="x")
        tk.Label(win, text="PORT SERIAL YANG TERSEDIA",
                 bg=C["primary_dark"], fg="white",
                 font=("Segoe UI", 11, "bold")).pack(fill="x", ipady=7)

        frame = tk.Frame(win, bg=C["bg"], padx=16, pady=10)
        frame.pack(fill="both", expand=True)

        tk.Label(frame, text="Pilih port USB RS485 Anda:",
                 bg=C["bg"], fg=C["text"], font=("Segoe UI", 10)).pack(anchor="w")

        listbox = tk.Listbox(frame, font=("Consolas", 11),
                             bg=C["card"], fg=C["primary_dark"],
                             selectbackground=C["primary"],
                             selectforeground="white",
                             relief="solid", bd=1, height=8)
        listbox.pack(fill="both", expand=True, pady=8)

        info_var = tk.StringVar(value="")
        tk.Label(frame, textvariable=info_var, bg=C["bg"],
                 fg=C["text_muted"], font=("Segoe UI", 9),
                 wraplength=380, justify="left").pack(anchor="w")

        def _refresh():
            listbox.delete(0, "end")
            ports  = scan_serial_ports()
            detail = {}
            if HAS_SERIAL_TOOLS and list_ports is not None:
                detail = {p.device: p.description for p in list_ports.comports()}
            for port in ports:
                listbox.insert("end", f"  {port}   {detail.get(port, '')}")
            if not ports:
                listbox.insert("end", "  (tidak ada port terdeteksi)")
            info_var.set(f"{len(ports)} port ditemukan")

        def _apply():
            sel = listbox.curselection()
            if not sel:
                return
            port = listbox.get(sel[0]).strip().split()[0]
            self.cfg["serial_port"] = port
            save_config(self.cfg)
            self._port_var.set(port)
            self.log(f"Port diubah ke: {port}")
            win.destroy()
            self._reconnect_rs485()

        _refresh()

        btn_row = tk.Frame(win, bg=C["bg"])
        btn_row.pack(pady=(0, 10))
        for text, cmd, bg in [
            ("↻ Refresh",          _refresh,      C["primary_dark"]),
            ("✓ Gunakan Port Ini", _apply,        C["teal"]),
            ("✕ Tutup",            win.destroy,   C["red"]),
        ]:
            tk.Button(btn_row, text=text, command=cmd, bg=bg, fg="white",
                      font=("Segoe UI", 9, "bold"), relief="flat",
                      pady=5, padx=12, cursor="hand2").pack(side="left", padx=4)

    # ── Settings dialog ───────────────────────────────────────────────────────
    def _open_settings(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Pengaturan")
        win.configure(bg=C["bg"])
        win.geometry("560x500")
        win.grab_set()

        tk.Frame(win, bg=C["primary_dark"], height=5).pack(fill="x")
        tk.Label(win, text="PENGATURAN KONEKSI & PERANGKAT",
                 bg=C["primary_dark"], fg="white",
                 font=("Segoe UI", 12, "bold")).pack(fill="x", ipady=8)

        # Canvas dengan scrollbar agar muat di layar kecil
        canvas = tk.Canvas(win, bg=C["bg"], highlightthickness=0)
        sb     = ttk.Scrollbar(win, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)

        form     = tk.Frame(canvas, bg=C["bg"], padx=20, pady=10)
        cwin_id  = canvas.create_window((0, 0), window=form, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(cwin_id, width=e.width))
        form.bind("<Configure>",
                  lambda e: canvas.configure(scrollregion=canvas.bbox("all")))

        entry_vars: dict = {}
        row_i = 0

        def _section(title: str):
            nonlocal row_i
            tk.Label(form, text=title, bg=C["bg"], fg=C["primary_dark"],
                     font=("Segoe UI", 10, "bold")).grid(
                row=row_i, column=0, columnspan=3, sticky="w", pady=(10, 2))
            row_i += 1

        def _field(label: str, key: str, width: int = 30):
            nonlocal row_i
            tk.Label(form, text=label, bg=C["bg"], fg=C["text"],
                     font=("Segoe UI", 10), anchor="w").grid(
                row=row_i, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            entry_vars[key] = v
            tk.Entry(form, textvariable=v, font=("Segoe UI", 10),
                     width=width, relief="solid", bd=1).grid(
                row=row_i, column=1, columnspan=2, sticky="ew",
                padx=(10, 0), pady=3)
            row_i += 1

        # ── USB RS485 ─────────────────────────────────────────────────────────
        _section("USB RS485")

        tk.Label(form, text="Port Serial :", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 10), anchor="w").grid(
            row=row_i, column=0, sticky="w", pady=4)
        port_var = tk.StringVar(value=self.cfg.get("serial_port", ""))
        entry_vars["serial_port"] = port_var
        ports_list  = scan_serial_ports() or [self.cfg.get("serial_port", "")]
        port_combo  = ttk.Combobox(form, textvariable=port_var,
                                   values=ports_list, width=22,
                                   font=("Consolas", 10))
        port_combo.grid(row=row_i, column=1, sticky="ew", padx=(10, 4), pady=4)
        info_lbl = tk.Label(form, text="", bg=C["bg"], fg=C["teal"],
                            font=("Segoe UI", 8), anchor="w")

        def _refresh_ports():
            new_list = scan_serial_ports()
            port_combo["values"] = new_list
            info_lbl.configure(
                text=f"{len(new_list)} port: {', '.join(new_list)}" if new_list
                else "Tidak ada port terdeteksi")

        tk.Button(form, text="⌕ Scan", command=_refresh_ports,
                  bg=C["primary_dark"], fg="white",
                  font=("Segoe UI", 9, "bold"), relief="flat",
                  cursor="hand2", pady=2, padx=6).grid(
            row=row_i, column=2, pady=4)
        row_i += 1
        info_lbl.grid(row=row_i, column=1, columnspan=2, sticky="w", padx=(10, 0))
        row_i += 1

        tk.Label(form, text="Baud Rate :", bg=C["bg"], fg=C["text"],
                 font=("Segoe UI", 10), anchor="w").grid(
            row=row_i, column=0, sticky="w", pady=4)
        baud_var = tk.StringVar(value=str(self.cfg.get("baud_rate", 9600)))
        entry_vars["baud_rate"] = baud_var
        ttk.Combobox(form, textvariable=baud_var,
                     values=["1200","2400","4800","9600","19200","38400","57600","115200"],
                     width=10, font=("Consolas", 10)).grid(
            row=row_i, column=1, sticky="w", padx=(10, 0), pady=4)
        row_i += 1

        # ── Slave ID ──────────────────────────────────────────────────────────
        _section("ID Slave Sensor (Modbus)")
        for label, key in [("Slave ID pH  :", "slave_id_ph"),
                            ("Slave ID TSS :", "slave_id_tss"),
                            ("Slave ID Debit:", "slave_id_debit")]:
            tk.Label(form, text=label, bg=C["bg"], fg=C["text"],
                     font=("Segoe UI", 10), anchor="w").grid(
                row=row_i, column=0, sticky="w", pady=3)
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            entry_vars[key] = v
            tk.Entry(form, textvariable=v, font=("Segoe UI", 10),
                     width=8, relief="solid", bd=1).grid(
                row=row_i, column=1, sticky="w", padx=(10, 0), pady=3)
            row_i += 1

        # ── Server & UID ──────────────────────────────────────────────────────
        _section("Server & Identitas")
        for label, key in [
            ("UID 1 :",            "uid1"),
            ("UID 2 :",            "uid2"),
            ("Server URL 1 :",     "server_url1"),
            ("Secret Key URL 1 :", "secret_key_url1"),
            ("Server URL 2 :",     "server_url2"),
            ("Secret Key URL 2 :", "secret_key_url2"),
        ]:
            _field(label, key)

        # ── Batas Min/Max Server 2 ────────────────────────────────────────────
        _section("Batas Data Server 2 (KLHK)")

        # Keterangan singkat
        tk.Label(form,
                 text="Server 1 menerima data MURNI sensor.\n"
                      "Server 2 menerima data yang di-clamp sesuai batas ini.",
                 bg=C["bg"], fg=C["text_muted"],
                 font=("Segoe UI", 8), justify="left").grid(
            row=row_i, column=0, columnspan=3, sticky="w", pady=(0, 6))
        row_i += 1

        # Header kolom
        for col, txt in enumerate(["Parameter", "Min", "Max"]):
            tk.Label(form, text=txt, bg=C["bg"], fg=C["primary_dark"],
                     font=("Segoe UI", 9, "bold")).grid(
                row=row_i, column=col, sticky="w",
                padx=(0 if col == 0 else 10, 0), pady=2)
        row_i += 1

        # Separator tipis
        tk.Frame(form, bg=C["border"], height=1).grid(
            row=row_i, column=0, columnspan=3, sticky="ew", pady=(0, 4))
        row_i += 1

        # Baris min/max per parameter
        limit_rows = [
            ("pH",    "limit_ph_min",    "limit_ph_max"),
            ("TSS",   "limit_tss_min",   "limit_tss_max"),
            ("Debit", "limit_debit_min", "limit_debit_max"),
        ]
        for param, key_min, key_max in limit_rows:
            tk.Label(form, text=param, bg=C["bg"], fg=C["text"],
                     font=("Segoe UI", 10)).grid(
                row=row_i, column=0, sticky="w", pady=4)
            for col, key in enumerate([key_min, key_max], start=1):
                v = tk.StringVar(value=str(self.cfg.get(key, "")))
                entry_vars[key] = v
                tk.Entry(form, textvariable=v, font=("Consolas", 10),
                         width=10, relief="solid", bd=1,
                         justify="center").grid(
                    row=row_i, column=col, sticky="w", padx=(10, 0), pady=4)
            row_i += 1

        form.columnconfigure(1, weight=1)

        def _save():
            int_keys   = {"baud_rate", "slave_id_ph", "slave_id_tss", "slave_id_debit"}
            float_keys = {
                "limit_ph_min", "limit_ph_max",
                "limit_tss_min", "limit_tss_max",
                "limit_debit_min", "limit_debit_max",
            }
            for key, v in entry_vars.items():
                raw = v.get().strip()
                try:
                    if key in int_keys:
                        self.cfg[key] = int(raw)
                    elif key in float_keys:
                        self.cfg[key] = float(raw)
                    else:
                        self.cfg[key] = raw
                except (ValueError, TypeError):
                    self.cfg[key] = raw
            save_config(self.cfg)
            self._port_var.set(self.cfg.get("serial_port", "—"))
            self.update_limits()
            self.log("Pengaturan disimpan")
            win.destroy()
            self._reconnect_rs485()

        btn = tk.Frame(win, bg=C["bg"])
        btn.pack(pady=8)
        tk.Button(btn, text="  Simpan & Hubungkan  ", command=_save,
                  bg=C["teal"], fg="white", font=("Segoe UI", 10, "bold"),
                  relief="flat", pady=6, cursor="hand2").pack(side="left", padx=6)
        tk.Button(btn, text="  Batal  ", command=win.destroy,
                  bg=C["red"], fg="white", font=("Segoe UI", 10, "bold"),
                  relief="flat", pady=6, cursor="hand2").pack(side="left", padx=6)
