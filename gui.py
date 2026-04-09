"""
gui.py — Antarmuka grafis SPARING Monitor menggunakan tkinter.
Ditampilkan via HDMI pada Raspberry Pi / Orange Pi / Windows.
Semua update dari thread lain harus menggunakan root.after(0, ...).

Layout:
  ┌─ Header ─────────────────────────────────────────────────────┐
  │  Logo | Judul & Subtitel | Status Koneksi (chips) | Jam      │
  ├─ Sensor Row ─────────────────────────────────────────────────┤
  │  [ pH — besar ] [ TSS — besar ] [ DEBIT — besar ]           │
  ├─ Body ───────────────────────────────────────────────────────┤
  │  Log Aktivitas (terminal, lebar)  │  Panel info & kontrol   │
  └──────────────────────────────────────────────────────────────┘
"""

import threading
import tkinter as tk
from tkinter import ttk
from datetime import datetime
from typing import TYPE_CHECKING

from constants import (
    C, LOGO_FILE, SYS_PLATFORM,
    HAS_PIL, HAS_SERIAL_TOOLS,
    Image, ImageTk, list_ports,
)
from config  import save_config, scan_serial_ports
from models  import SensorReading

if TYPE_CHECKING:
    from app import SparingApp


# ── Konstanta visual ──────────────────────────────────────────────────────────
_FONT_UI   = "Segoe UI"
_FONT_MONO = "Consolas"
_R_WIDTH   = 270          # lebar panel kanan (px)


class SparingGUI:
    """Jendela utama SPARING Monitor."""

    def __init__(self, root: tk.Tk, app: "SparingApp"):
        self.root = root
        self.app  = app
        self.cfg  = app.cfg

        self._sensor_vars: dict = {}      # key → StringVar nilai sensor (raw)
        self._proc_vars:   dict = {}      # key → StringVar nilai processed
        self._conn_dots:   dict = {}      # key → Canvas (dot indikator)
        self._conn_chips:  dict = {}      # key → (StringVar, Label)
        self._conn_labels: dict = {}      # alias untuk update_connection()
        self._limit_vars:  dict = {}      # key → StringVar batas S2

        self._unlocked:        bool      = False
        self._lock_btn_var:    tk.StringVar = None   # set saat build footer
        self._limits_wrapper:  tk.Frame  = None      # hidden sampai unlock
        self._limits_pack_ref: tk.Widget = None      # widget sebelum limits card
        self._last_card_shadow: tk.Frame = None      # diset oleh _card()

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
        self.root.bind("<F11>",    self._toggle_fullscreen)
        self.root.bind("<Escape>", self._exit_fullscreen)

    def _toggle_fullscreen(self, event=None) -> None:
        v = not self._is_fullscreen.get()
        self._is_fullscreen.set(v)
        self.root.attributes("-fullscreen", v)

    def _exit_fullscreen(self, event=None) -> None:
        self._is_fullscreen.set(False)
        self.root.attributes("-fullscreen", False)

    def _setup_styles(self) -> None:
        s = ttk.Style(self.root)
        s.theme_use("clam")
        s.configure("TProgressbar",
                    troughcolor=C["border"],
                    background=C["progress"],
                    bordercolor=C["border"],
                    lightcolor=C["progress"],
                    darkcolor=C["progress"],
                    thickness=6)
        s.configure("Vertical.TScrollbar",
                    background=C["bg"],
                    troughcolor=C["bg"],
                    arrowcolor=C["text_muted"],
                    bordercolor=C["bg"],
                    gripcount=0)

    # ── Top-level build ───────────────────────────────────────────────────────
    def _build(self) -> None:
        self._build_header()
        self._build_sensor_row()
        self._build_body()
        self._build_footer()

    # ═══════════════════════════════════════════════════════════════════════════
    # HEADER
    # ═══════════════════════════════════════════════════════════════════════════
    def _build_header(self) -> None:
        # Top accent stripe
        tk.Frame(self.root, bg=C["primary"], height=4).pack(fill="x")

        hdr = tk.Frame(self.root, bg=C["panel"])
        hdr.pack(fill="x")
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill="x")

        row = tk.Frame(hdr, bg=C["panel"])
        row.pack(fill="x", padx=18, pady=10)

        # ── Logo ──────────────────────────────────────────────────────────────
        self._add_logo(row)

        # Divider
        tk.Frame(row, bg=C["border"], width=1).pack(
            side="left", fill="y", padx=18)

        # ── Title ─────────────────────────────────────────────────────────────
        title_col = tk.Frame(row, bg=C["panel"])
        title_col.pack(side="left", fill="y")

        tk.Label(title_col,
                 text="SISTEM PEMANTAUAN KUALITAS AIR",
                 bg=C["panel"], fg=C["text"],
                 font=(_FONT_UI, 15, "bold")).pack(anchor="w")

        sub_row = tk.Frame(title_col, bg=C["panel"])
        sub_row.pack(anchor="w", pady=(4, 0))
        tk.Frame(sub_row, bg=C["accent"],
                 width=22, height=2).pack(side="left",
                                          anchor="center", padx=(0, 8))
        tk.Label(sub_row,
                 text="SPARING  ●  Online Monitoring System",
                 bg=C["panel"], fg=C["accent"],
                 font=(_FONT_UI, 9)).pack(side="left")

        # ── Connection status chips (center-right) ────────────────────────────
        conn_row = tk.Frame(row, bg=C["panel"])
        conn_row.pack(side="left", padx=(30, 0), fill="y")

        for key, label in [
            ("rs485",    "RS485"),
            ("internet", "Internet"),
            ("server1",  "Server 1"),
            ("server2",  "Server 2"),
        ]:
            chip_frame = tk.Frame(conn_row, bg=C["panel"])
            chip_frame.pack(side="left", padx=6)

            dot = tk.Canvas(chip_frame, width=8, height=8,
                            bg=C["panel"], highlightthickness=0)
            dot.pack(side="left", padx=(0, 4), pady=2)
            dot.create_oval(0, 0, 8, 8,
                            fill=C["border"], outline="", tags="dot")

            tk.Label(chip_frame, text=label,
                     bg=C["panel"], fg=C["text_muted"],
                     font=(_FONT_UI, 8)).pack(side="left", pady=(0, 1))

            var = tk.StringVar(value="...")
            status_lbl = tk.Label(chip_frame, textvariable=var,
                                  bg=C["panel"], fg=C["text_muted"],
                                  font=(_FONT_UI, 8, "bold"))
            status_lbl.pack(side="left", padx=(2, 0))

            self._conn_dots[key]  = dot
            self._conn_chips[key] = (var, status_lbl)
            self._conn_labels[key] = (var, status_lbl)   # alias

        # ── Clock ─────────────────────────────────────────────────────────────
        clk_frame = tk.Frame(row, bg=C["primary"],
                             padx=16, pady=8)
        clk_frame.pack(side="right")

        self._date_var  = tk.StringVar()
        self._clock_var = tk.StringVar()

        tk.Label(clk_frame, textvariable=self._date_var,
                 bg=C["primary"], fg="#A8D0FF",
                 font=(_FONT_UI, 8)).pack(anchor="e")
        tk.Label(clk_frame, textvariable=self._clock_var,
                 bg=C["primary"], fg="white",
                 font=(_FONT_MONO, 22, "bold")).pack(anchor="e")

    def _add_logo(self, parent) -> None:
        if HAS_PIL and Image is not None and LOGO_FILE.exists():
            try:
                img = Image.open(LOGO_FILE).resize((118, 54), Image.LANCZOS)
                self._logo_img = ImageTk.PhotoImage(img)
                tk.Label(parent, image=self._logo_img,
                         bg=C["panel"]).pack(side="left")
                return
            except Exception:
                pass
        tk.Label(parent, text="SUCOFINDO",
                 bg=C["panel"], fg=C["primary_dark"],
                 font=(_FONT_UI, 13, "bold")).pack(side="left")

    # ═══════════════════════════════════════════════════════════════════════════
    # SENSOR ROW  — tiga kartu penuh warna, nilai monospace besar
    # ═══════════════════════════════════════════════════════════════════════════
    def _build_sensor_row(self) -> None:
        row = tk.Frame(self.root, bg=C["bg"])
        row.pack(fill="x", padx=14, pady=(10, 0))

        defs = [
            # key     label    unit    bg_color       label_color
            ("ph",   "pH",    "",     C["s_ph"],     "#A8CCFF"),
            ("tss",  "TSS",   "mg/L", C["s_tss"],    "#A0D8F0"),
            ("debit","DEBIT", "m³/s", C["s_debit"],  "#9AECD8"),
        ]
        for col, (key, label, unit, bg, lc) in enumerate(defs):
            card = self._sensor_card(row, key, label, unit, bg, lc)
            card.grid(row=0, column=col, padx=6, sticky="nsew")
            row.columnconfigure(col, weight=1)

    def _sensor_card(self, parent, key: str, label: str,
                     unit: str, bg: str, label_color: str) -> tk.Frame:
        """
        Kartu sensor dengan warna solid.
        Selalu menampilkan raw (besar) dan processed (kecil, dimask saat terkunci).
        """
        shadow = tk.Frame(parent, bg=C["shadow"], padx=1, pady=1)

        card = tk.Frame(shadow, bg=bg)
        card.pack(fill="both", expand=True)

        # Top accent line
        tk.Frame(card, bg=label_color, height=2).pack(fill="x")

        # Parameter label
        tk.Label(card, text=label,
                 bg=bg, fg=label_color,
                 font=(_FONT_UI, 10, "bold")).pack(pady=(12, 0))

        # ── Raw value — large, always visible ────────────────────────────────
        raw_var = tk.StringVar(value="—")
        self._sensor_vars[key] = raw_var
        tk.Label(card, textvariable=raw_var,
                 bg=bg, fg="white",
                 font=(_FONT_MONO, 38, "bold")).pack(pady=(2, 0))

        tk.Label(card, text=unit,
                 bg=bg, fg=label_color,
                 font=(_FONT_UI, 9)).pack()

        # ── Separator ─────────────────────────────────────────────────────────
        tk.Frame(card, bg=label_color, height=1).pack(
            fill="x", padx=20, pady=(8, 4))

        # ── Processed value — smaller, masked when locked ─────────────────────
        proc_row = tk.Frame(card, bg=bg)
        proc_row.pack(pady=(0, 12))

        tk.Label(proc_row, text="PROCESSED",
                 bg=bg, fg=label_color,
                 font=(_FONT_UI, 7, "bold")).pack()

        proc_var = tk.StringVar(value="●  ●  ●")
        self._proc_vars[key] = proc_var
        tk.Label(proc_row, textvariable=proc_var,
                 bg=bg, fg=label_color,
                 font=(_FONT_MONO, 20, "bold")).pack()

        return shadow

    # ═══════════════════════════════════════════════════════════════════════════
    # BODY  — log (kiri lebar) + panel info (kanan)
    # ═══════════════════════════════════════════════════════════════════════════
    def _build_body(self) -> None:
        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill="both", expand=True, padx=14, pady=10)

        self._build_log_panel(body)
        self._build_right_panel(body)

    # ── Log panel ─────────────────────────────────────────────────────────────
    def _build_log_panel(self, parent: tk.Frame) -> None:
        wrapper = tk.Frame(parent, bg=C["shadow"], padx=1, pady=1)
        wrapper.pack(side="left", fill="both", expand=True, padx=(0, 8))

        outer = tk.Frame(wrapper, bg=C["card"])
        outer.pack(fill="both", expand=True)

        # Title bar
        title_bar = tk.Frame(outer, bg=C["card"])
        title_bar.pack(fill="x")

        tk.Frame(title_bar, bg=C["accent"], width=4).pack(
            side="left", fill="y")
        tk.Label(title_bar, text="LOG AKTIVITAS",
                 bg=C["card"], fg=C["accent"],
                 font=(_FONT_UI, 9, "bold"),
                 padx=10, pady=8).pack(side="left")
        tk.Frame(title_bar, bg=C["border"], height=1).pack(
            side="bottom", fill="x")

        # Terminal area
        log_frame = tk.Frame(outer, bg=C["log_bg"])
        log_frame.pack(fill="both", expand=True)

        sb = ttk.Scrollbar(log_frame, orient="vertical")
        self._log_txt = tk.Text(
            log_frame, state="disabled",
            font=(_FONT_MONO, 9),
            bg=C["log_bg"], fg=C["log_fg"],
            relief="flat", padx=12, pady=10,
            wrap="word",
            selectbackground=C["primary"],
            insertbackground=C["log_fg"],
            yscrollcommand=sb.set,
        )
        sb.configure(command=self._log_txt.yview)
        sb.pack(side="right", fill="y")
        self._log_txt.pack(side="left", fill="both", expand=True)

        # Color tags for different log levels
        self._log_txt.tag_configure("error",
                                    foreground="#FF6B6B")
        self._log_txt.tag_configure("ok",
                                    foreground="#4DD9AC")
        self._log_txt.tag_configure("sim",
                                    foreground="#A0A8C0")

    # ── Right panel ───────────────────────────────────────────────────────────
    def _build_right_panel(self, parent: tk.Frame) -> None:
        right = tk.Frame(parent, bg=C["bg"], width=_R_WIDTH)
        right.pack(side="right", fill="y")
        right.pack_propagate(False)

        # ── Info pengiriman ───────────────────────────────────────────────────
        inner = self._card(right, "INFO PENGIRIMAN DATA", C["primary"],
                           fill="x", pady=(0, 8))

        self._count_var   = tk.StringVar(value="0 / 30")
        self._last_tx_var = tk.StringVar(value="—")
        self._buf_var     = tk.StringVar(value="0")
        self._progress    = tk.DoubleVar(value=0)

        self._info_row(inner, "Data Terkumpul",  self._count_var,
                       C["primary"])
        self._info_row(inner, "Kirim Terakhir",  self._last_tx_var,
                       C["text_muted"])
        self._info_row(inner, "Buffer Offline",  self._buf_var,
                       C["warning"], suffix=" batch")

        ttk.Progressbar(inner, variable=self._progress,
                        maximum=30).pack(
            fill="x", pady=(10, 2))

        # ── Status pengiriman ─────────────────────────────────────────────────
        inner2 = self._card(right, "STATUS PENGIRIMAN", C["online"],
                            fill="x", pady=(0, 8))

        self._send_status_var = tk.StringVar(value="— Menunggu batch pertama")
        self._send_status_lbl = tk.Label(
            inner2, textvariable=self._send_status_var,
            bg=C["card"], fg=C["text_muted"],
            font=(_FONT_UI, 11, "bold"),
            wraplength=_R_WIDTH - 28, justify="left",
        )
        self._send_status_lbl.pack(anchor="w", pady=(2, 0))

        self._send_detail_var = tk.StringVar(value="")
        tk.Label(inner2, textvariable=self._send_detail_var,
                 bg=C["card"], fg=C["text_muted"],
                 font=(_FONT_UI, 8),
                 wraplength=_R_WIDTH - 28,
                 justify="left").pack(anchor="w", pady=(3, 2))

        # ── Batas data server 2 — tersembunyi sampai unlock ──────────────────
        # Simpan referensi widget sebelumnya agar bisa di-pack 'after' saat unlock
        self._limits_pack_ref = self._last_card_shadow

        self._limits_wrapper = tk.Frame(right, bg=C["bg"])
        # Tidak di-pack dulu — akan muncul saat _unlock() dipanggil

        inner3 = self._card(self._limits_wrapper, "BATAS DATA  SERVER 2",
                            C["accent"], fill="x")

        hdr = tk.Frame(inner3, bg=C["card"])
        hdr.pack(fill="x")
        for col, (txt, w) in enumerate([("", 6), ("MIN", 8), ("MAX", 8)]):
            tk.Label(hdr, text=txt, bg=C["card"], fg=C["text_muted"],
                     font=(_FONT_UI, 8, "bold"),
                     width=w, anchor="w").grid(
                row=0, column=col, sticky="w")

        tk.Frame(inner3, bg=C["border"], height=1).pack(
            fill="x", pady=(4, 2))

        for param, k_min, k_max in [
            ("pH",    "limit_ph_min",    "limit_ph_max"),
            ("TSS",   "limit_tss_min",   "limit_tss_max"),
            ("Debit", "limit_debit_min", "limit_debit_max"),
        ]:
            lim_row = tk.Frame(inner3, bg=C["card"])
            lim_row.pack(fill="x", pady=2)
            tk.Label(lim_row, text=param,
                     bg=C["card"], fg=C["text_muted"],
                     font=(_FONT_UI, 9),
                     width=6, anchor="w").pack(side="left")
            for key in (k_min, k_max):
                v = tk.StringVar(value=str(self.cfg.get(key, "—")))
                self._limit_vars[key] = v
                tk.Label(lim_row, textvariable=v,
                         bg=C["card_alt"], fg=C["primary"],
                         font=(_FONT_MONO, 9, "bold"),
                         width=8, relief="flat", padx=4).pack(
                    side="left", padx=(4, 0))

        # ── Kontrol RS485 ─────────────────────────────────────────────────────
        ctrl_inner = self._card(right, "KONTROL RS485", C["text_muted"],
                                fill="x", pady=(0, 8))

        port_row = tk.Frame(ctrl_inner, bg=C["card"])
        port_row.pack(fill="x", pady=(0, 6))
        tk.Label(port_row, text="Port aktif :",
                 bg=C["card"], fg=C["text_muted"],
                 font=(_FONT_UI, 9)).pack(side="left")
        self._port_var = tk.StringVar(
            value=self.cfg.get("serial_port", "—"))
        tk.Label(port_row, textvariable=self._port_var,
                 bg=C["card"], fg=C["primary"],
                 font=(_FONT_MONO, 9, "bold")).pack(side="left", padx=(6, 0))

        btn_row = tk.Frame(ctrl_inner, bg=C["card"])
        btn_row.pack(fill="x")
        self._flat_btn(
            btn_row, "↻  Hubungkan Ulang",
            self._reconnect_rs485,
            C["primary"], "white"
        ).pack(side="left", fill="x", expand=True, padx=(0, 4))
        self._flat_btn(
            btn_row, "⌕  Scan Port",
            self._scan_ports_dialog,
            C["card_alt"], C["primary"],
            border=True
        ).pack(side="left", fill="x", expand=True)

        # ── Settings ──────────────────────────────────────────────────────────
        self._flat_btn(
            right, "⚙   Pengaturan Koneksi",
            self._open_settings,
            C["primary_dark"], "white",
            pady=10
        ).pack(fill="x", pady=(4, 0))

    # ═══════════════════════════════════════════════════════════════════════════
    # FOOTER
    # ═══════════════════════════════════════════════════════════════════════════
    def _build_footer(self) -> None:
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill="x")
        bar = tk.Frame(self.root, bg=C["panel"], height=26)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        # Left indicator strip
        tk.Frame(bar, bg=C["primary"], width=3).pack(side="left", fill="y")

        self._statusbar_var = tk.StringVar(value="Siap")
        tk.Label(bar, textvariable=self._statusbar_var,
                 bg=C["panel"], fg=C["text_muted"],
                 font=(_FONT_UI, 9)).pack(side="left", padx=10)

        self._flat_btn(bar, "⛶  F11",
                       self._toggle_fullscreen,
                       C["bg"], C["text_muted"],
                       pady=0).pack(side="right", padx=8, pady=3)

        # Tombol rahasia — terlihat seperti indikator biasa
        self._lock_btn_var = tk.StringVar(value="🔒")
        lck = tk.Label(bar, textvariable=self._lock_btn_var,
                       bg=C["panel"], fg=C["border"],
                       font=(_FONT_UI, 13),
                       cursor="hand2")
        lck.pack(side="right", padx=(0, 2), pady=2)
        lck.bind("<Button-1>", lambda e: self._show_lock_dialog())

        mode = "SIMULASI" if self.cfg.get("simulate_sensors") else "LIVE"
        port = self.cfg.get("serial_port", "—")
        tk.Label(bar,
                 text=f"Mode: {mode}  ·  Port: {port}  ·  {SYS_PLATFORM}  ·  ESC = keluar fullscreen",
                 bg=C["panel"], fg=C["text_muted"],
                 font=(_FONT_UI, 8)).pack(side="right", padx=12)

    # ═══════════════════════════════════════════════════════════════════════════
    # WIDGET HELPERS
    # ═══════════════════════════════════════════════════════════════════════════
    def _card(self, parent, title: str, accent: str,
              **pack_kw) -> tk.Frame:
        """
        Buat kartu putih dengan garis kiri berwarna.
        Dikemas ke parent sesuai pack_kw.
        Kembalikan inner frame tempat konten diletakkan.
        """
        # Shadow wrapper
        shadow = tk.Frame(parent, bg=C["shadow"], padx=1, pady=1)
        if pack_kw:
            shadow.pack(**pack_kw)
        self._last_card_shadow = shadow   # dipakai oleh limits wrapper

        outer = tk.Frame(shadow, bg=C["card"])
        outer.pack(fill="both", expand=True)

        # Left color bar
        tk.Frame(outer, bg=accent, width=4).pack(side="left", fill="y")

        body = tk.Frame(outer, bg=C["card"])
        body.pack(side="left", fill="both", expand=True)

        # Title row
        tk.Label(body, text=title,
                 bg=C["card"], fg=accent,
                 font=(_FONT_UI, 8, "bold"),
                 padx=10, pady=7).pack(anchor="w")

        tk.Frame(body, bg=C["border"], height=1).pack(fill="x")

        # Content frame
        content = tk.Frame(body, bg=C["card"])
        content.pack(fill="both", expand=True, padx=10, pady=8)
        return content

    def _info_row(self, parent, label: str, var: tk.StringVar,
                  fg: str, suffix: str = "") -> None:
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=3)
        tk.Label(row, text=label,
                 bg=C["card"], fg=C["text_muted"],
                 font=(_FONT_UI, 8, "bold"),
                 anchor="w", width=15).pack(side="left")
        tk.Label(row, textvariable=var,
                 bg=C["card"], fg=fg,
                 font=(_FONT_MONO, 10, "bold")).pack(side="left")
        if suffix:
            tk.Label(row, text=suffix,
                     bg=C["card"], fg=C["text_muted"],
                     font=(_FONT_UI, 9)).pack(side="left")

    def _flat_btn(self, parent, text: str, cmd,
                  bg: str, fg: str,
                  pady: int = 5,
                  border: bool = False) -> tk.Button:
        kw = dict(
            text=text, command=cmd,
            bg=bg, fg=fg,
            font=(_FONT_UI, 9, "bold"),
            relief="flat", cursor="hand2", pady=pady,
            activebackground=C["accent"],
            activeforeground="white",
        )
        if border:
            kw.update(highlightthickness=1,
                      highlightbackground=C["border"],
                      highlightcolor=C["primary"])
        return tk.Button(parent, **kw)

    # ── Lock / Unlock ─────────────────────────────────────────────────────────
    def _show_lock_dialog(self) -> None:
        """Tampilkan dialog PIN. Jika sudah terbuka, langsung kunci ulang."""
        if self._unlocked:
            self._lock()
            return

        win = tk.Toplevel(self.root)
        win.title("")
        win.configure(bg=C["panel"])
        win.resizable(False, False)
        win.grab_set()

        # Posisikan di tengah layar
        win.update_idletasks()
        w, h = 320, 200
        sx = self.root.winfo_screenwidth()
        sy = self.root.winfo_screenheight()
        win.geometry(f"{w}x{h}+{(sx-w)//2}+{(sy-h)//2}")

        tk.Frame(win, bg=C["primary"], height=4).pack(fill="x")

        tk.Label(win, text="🔒  Masukkan PIN",
                 bg=C["panel"], fg=C["text"],
                 font=(_FONT_UI, 12, "bold")).pack(pady=(20, 8))

        tk.Label(win, text="PIN diperlukan untuk melihat data processed & batas",
                 bg=C["panel"], fg=C["text_muted"],
                 font=(_FONT_UI, 8)).pack(pady=(0, 12))

        pin_var = tk.StringVar()
        entry = tk.Entry(win, textvariable=pin_var,
                         show="●", font=(_FONT_MONO, 16),
                         width=10, justify="center",
                         relief="flat", bd=0,
                         bg=C["bg"], fg=C["text"],
                         insertbackground=C["primary"],
                         highlightthickness=2,
                         highlightbackground=C["border"],
                         highlightcolor=C["primary"])
        entry.pack(ipady=6)
        entry.focus_set()

        err_var = tk.StringVar(value="")
        tk.Label(win, textvariable=err_var,
                 bg=C["panel"], fg=C["offline"],
                 font=(_FONT_UI, 9)).pack(pady=(6, 0))

        def _try_unlock(event=None):
            correct = str(self.cfg.get("secret_pin", "1234"))
            if pin_var.get() == correct:
                win.destroy()
                self._unlock()
            else:
                err_var.set("PIN salah, coba lagi")
                pin_var.set("")

        entry.bind("<Return>", _try_unlock)

        btn_row = tk.Frame(win, bg=C["panel"])
        btn_row.pack(pady=(8, 0))
        self._flat_btn(btn_row, "Buka", _try_unlock,
                       C["primary"], "white", pady=6).pack(
            side="left", padx=(0, 6), ipadx=12)
        self._flat_btn(btn_row, "Batal", win.destroy,
                       C["bg"], C["text_muted"], pady=6).pack(
            side="left", ipadx=12)

    def _unlock(self) -> None:
        """Tampilkan processed values dan limits card."""
        self._unlocked = True
        if self._lock_btn_var:
            self._lock_btn_var.set("🔓")

        # Isi nilai processed segera dari cache terakhir (jika ada)
        if hasattr(self, "_last_proc"):
            ph, tss, debit = self._last_proc
            self._proc_vars["ph"].set(f"{ph:.2f}")
            self._proc_vars["tss"].set(f"{tss:.2f}")
            self._proc_vars["debit"].set(f"{debit:.4f}")

        # Tampilkan limits wrapper setelah status card
        if self._limits_wrapper and self._limits_pack_ref:
            self._limits_wrapper.pack(
                fill="x", pady=(0, 8),
                after=self._limits_pack_ref)

        self.log("🔓 Tampilan data processed & batas diaktifkan")

    def _lock(self) -> None:
        """Sembunyikan processed values dan limits card."""
        self._unlocked = False
        if self._lock_btn_var:
            self._lock_btn_var.set("🔒")

        # Mask kembali nilai processed
        for key, var in self._proc_vars.items():
            var.set("●  ●  ●")

        # Sembunyikan limits wrapper
        if self._limits_wrapper:
            self._limits_wrapper.pack_forget()

        self.log("🔒 Tampilan data processed & batas disembunyikan")

    # ── Clock ─────────────────────────────────────────────────────────────────
    def _tick_clock(self) -> None:
        now = datetime.now()
        self._clock_var.set(now.strftime("%H:%M:%S"))
        self._date_var.set(now.strftime("%d %B %Y"))
        self.root.after(1000, self._tick_clock)

    # ═══════════════════════════════════════════════════════════════════════════
    # PUBLIC UPDATE METHODS  (dipanggil dari thread via root.after)
    # ═══════════════════════════════════════════════════════════════════════════
    def update_sensors(self, r: SensorReading) -> None:
        self._sensor_vars["ph"].set(f"{r.ph:.2f}")
        self._sensor_vars["tss"].set(f"{r.tss:.2f}")
        self._sensor_vars["debit"].set(f"{r.debit:.4f}")

    def update_sensors_processed(self, ph: float, tss: float,
                                  debit: float) -> None:
        """Perbarui nilai processed — hanya ditampilkan jika sudah di-unlock."""
        self._last_proc = (ph, tss, debit)   # cache untuk keperluan unlock
        if not self._unlocked:
            return   # tetap tampilkan mask sampai dibuka
        self._proc_vars["ph"].set(f"{ph:.2f}")
        self._proc_vars["tss"].set(f"{tss:.2f}")
        self._proc_vars["debit"].set(f"{debit:.4f}")

    def update_count(self, n: int, total: int = 30) -> None:
        self._count_var.set(f"{n} / {total}")
        self._progress.set(n)

    def update_last_tx(self, ts: float) -> None:
        self._last_tx_var.set(
            datetime.fromtimestamp(ts).strftime("%d/%m  %H:%M:%S"))

    def update_buffer(self, n: int) -> None:
        self._buf_var.set(str(n))

    def update_send_status(self, ok1: bool, ok2: bool, ts: float) -> None:
        waktu = datetime.fromtimestamp(ts).strftime("%d/%m/%Y  %H:%M:%S")
        if ok1 and ok2:
            self._send_status_var.set("✓  Berhasil Terkirim")
            self._send_status_lbl.configure(fg=C["online"])
            self._send_detail_var.set(f"Server 1 & 2 OK  ·  {waktu}")
        elif ok1 or ok2:
            self._send_status_var.set("⚠  Sebagian Gagal")
            self._send_status_lbl.configure(fg=C["warning"])
            s1 = "OK" if ok1 else "GAGAL"
            s2 = "OK" if ok2 else "GAGAL"
            self._send_detail_var.set(f"S1:{s1}  S2:{s2}  ·  {waktu}")
        else:
            self._send_status_var.set("✗  Gagal Terkirim")
            self._send_status_lbl.configure(fg=C["offline"])
            self._send_detail_var.set(f"Disimpan buffer  ·  {waktu}")

    def update_send_offline(self, ts: float) -> None:
        waktu = datetime.fromtimestamp(ts).strftime("%d/%m/%Y  %H:%M:%S")
        self._send_status_var.set("⬇  Disimpan Offline")
        self._send_status_lbl.configure(fg=C["accent"])
        self._send_detail_var.set(f"Akan dikirim saat online  ·  {waktu}")

    def update_connection(self, key: str, ok: bool) -> None:
        var, lbl = self._conn_chips[key]
        dot      = self._conn_dots[key]
        if ok:
            var.set("●")
            lbl.configure(fg=C["online"])
            dot.itemconfig("dot", fill=C["online"])
        else:
            var.set("●")
            lbl.configure(fg=C["offline"])
            dot.itemconfig("dot", fill=C["offline"])

    def update_limits(self) -> None:
        for key, var in self._limit_vars.items():
            var.set(str(self.cfg.get(key, "—")))

    def log(self, msg: str) -> None:
        ts   = datetime.now().strftime("%H:%M:%S")
        full = f"[{ts}]  {msg}\n"
        self._log_txt.configure(state="normal")

        # Warnai baris berdasarkan konten
        tag = ""
        lm  = msg.lower()
        if any(k in lm for k in ("error", "gagal", "terputus", "failed")):
            tag = "error"
        elif any(k in lm for k in ("berhasil", "terhubung", "✓", "ok")):
            tag = "ok"
        elif "[sim]" in msg:
            tag = "sim"

        if tag:
            self._log_txt.insert("end", full, tag)
        else:
            self._log_txt.insert("end", full)

        self._log_txt.see("end")
        self._log_txt.configure(state="disabled")
        self._statusbar_var.set(f"[{ts}] {msg}")

    # ═══════════════════════════════════════════════════════════════════════════
    # RECONNECT & DIALOGS
    # ═══════════════════════════════════════════════════════════════════════════
    def _reconnect_rs485(self) -> None:
        self.log("Menghubungkan ulang USB RS485...")
        self.update_connection("rs485", False)

        def _do():
            ok   = (self.app.sensor_rdr.reconnect()
                    if self.app.sensor_rdr else False)
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
        win.geometry("460x360")
        win.grab_set()

        tk.Frame(win, bg=C["primary"], height=4).pack(fill="x")

        title_bar = tk.Frame(win, bg=C["panel"])
        title_bar.pack(fill="x")
        tk.Label(title_bar, text="PORT SERIAL TERSEDIA",
                 bg=C["panel"], fg=C["text"],
                 font=(_FONT_UI, 11, "bold"),
                 padx=16, pady=10).pack(side="left")
        tk.Frame(win, bg=C["border"], height=1).pack(fill="x")

        body = tk.Frame(win, bg=C["bg"], padx=16, pady=12)
        body.pack(fill="both", expand=True)

        tk.Label(body, text="Pilih port USB RS485 Anda:",
                 bg=C["bg"], fg=C["text_muted"],
                 font=(_FONT_UI, 9, "bold")).pack(anchor="w", pady=(0, 6))

        list_shadow = tk.Frame(body, bg=C["shadow"], padx=1, pady=1)
        list_shadow.pack(fill="both", expand=True)

        listbox = tk.Listbox(
            list_shadow,
            font=(_FONT_MONO, 11),
            bg=C["card"], fg=C["text"],
            selectbackground=C["primary"],
            selectforeground="white",
            relief="flat", bd=0, height=7,
            activestyle="none",
        )
        listbox.pack(fill="both", expand=True)

        info_var = tk.StringVar(value="")
        tk.Label(body, textvariable=info_var,
                 bg=C["bg"], fg=C["text_muted"],
                 font=(_FONT_UI, 8)).pack(anchor="w", pady=(6, 0))

        def _refresh():
            listbox.delete(0, "end")
            ports  = scan_serial_ports()
            detail = {}
            if HAS_SERIAL_TOOLS and list_ports is not None:
                detail = {p.device: p.description
                          for p in list_ports.comports()}
            for p in ports:
                listbox.insert("end", f"  {p}   {detail.get(p, '')}")
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

        tk.Frame(win, bg=C["border"], height=1).pack(fill="x")
        btn_bar = tk.Frame(win, bg=C["panel"], padx=12, pady=8)
        btn_bar.pack(fill="x")

        for text, cmd, bg, fg in [
            ("↻  Refresh",          _refresh,    C["bg"],      C["primary"]),
            ("✓  Gunakan Port Ini", _apply,      C["primary"], "white"),
            ("✕  Tutup",            win.destroy, C["bg"],      C["text_muted"]),
        ]:
            self._flat_btn(btn_bar, text, cmd, bg, fg,
                           pady=6).pack(side="left", padx=(0, 6))

    # ── Settings dialog ───────────────────────────────────────────────────────
    def _open_settings(self) -> None:
        win = tk.Toplevel(self.root)
        win.title("Pengaturan")
        win.configure(bg=C["bg"])
        win.geometry("600x540")
        win.grab_set()

        tk.Frame(win, bg=C["primary"], height=4).pack(fill="x")

        title_bar = tk.Frame(win, bg=C["panel"])
        title_bar.pack(fill="x")
        tk.Label(title_bar, text="PENGATURAN KONEKSI & PERANGKAT",
                 bg=C["panel"], fg=C["text"],
                 font=(_FONT_UI, 12, "bold"),
                 padx=16, pady=10).pack(side="left")
        tk.Frame(win, bg=C["border"], height=1).pack(fill="x")

        canvas  = tk.Canvas(win, bg=C["bg"], highlightthickness=0)
        sb      = ttk.Scrollbar(win, orient="vertical",
                                command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)

        form    = tk.Frame(canvas, bg=C["bg"], padx=20, pady=12)
        cwin_id = canvas.create_window((0, 0), window=form, anchor="nw")
        canvas.bind("<Configure>",
                    lambda e: canvas.itemconfig(cwin_id, width=e.width))
        form.bind("<Configure>",
                  lambda e: canvas.configure(
                      scrollregion=canvas.bbox("all")))

        entry_vars: dict = {}
        row_i = [0]   # use list so closure can mutate

        def _section(title: str) -> None:
            if row_i[0] > 0:
                tk.Frame(form, bg=C["border"], height=1).grid(
                    row=row_i[0], column=0, columnspan=3,
                    sticky="ew", pady=(14, 6))
                row_i[0] += 1
            tk.Label(form, text=title,
                     bg=C["bg"], fg=C["primary"],
                     font=(_FONT_UI, 10, "bold")).grid(
                row=row_i[0], column=0, columnspan=3,
                sticky="w", pady=(0, 6))
            row_i[0] += 1

        def _entry(label: str, key: str, width: int = 32) -> None:
            tk.Label(form, text=label,
                     bg=C["bg"], fg=C["text"],
                     font=(_FONT_UI, 10), anchor="w").grid(
                row=row_i[0], column=0, sticky="w", pady=3)
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            entry_vars[key] = v
            e = tk.Entry(form, textvariable=v,
                         font=(_FONT_UI, 10), width=width,
                         relief="flat", bd=0,
                         bg=C["card"], fg=C["text"],
                         insertbackground=C["primary"],
                         highlightthickness=1,
                         highlightbackground=C["border"],
                         highlightcolor=C["primary"])
            e.grid(row=row_i[0], column=1, columnspan=2,
                   sticky="ew", padx=(10, 0), pady=3)
            row_i[0] += 1

        # USB RS485
        _section("USB RS485")

        tk.Label(form, text="Port Serial :",
                 bg=C["bg"], fg=C["text"],
                 font=(_FONT_UI, 10), anchor="w").grid(
            row=row_i[0], column=0, sticky="w", pady=4)
        port_var   = tk.StringVar(value=self.cfg.get("serial_port", ""))
        entry_vars["serial_port"] = port_var
        ports_list = scan_serial_ports() or [self.cfg.get("serial_port", "")]
        port_combo = ttk.Combobox(form, textvariable=port_var,
                                  values=ports_list, width=22,
                                  font=(_FONT_MONO, 10))
        port_combo.grid(row=row_i[0], column=1, sticky="ew",
                        padx=(10, 4), pady=4)

        info_lbl = tk.Label(form, text="",
                            bg=C["bg"], fg=C["online"],
                            font=(_FONT_UI, 8), anchor="w")

        def _refresh_ports():
            nl = scan_serial_ports()
            port_combo["values"] = nl
            info_lbl.configure(
                text=(f"{len(nl)} port: {', '.join(nl)}"
                      if nl else "Tidak ada port"))

        self._flat_btn(form, "⌕", _refresh_ports,
                       C["primary"], "white",
                       pady=3).grid(row=row_i[0], column=2, pady=4)
        row_i[0] += 1
        info_lbl.grid(row=row_i[0], column=1, columnspan=2,
                      sticky="w", padx=(10, 0))
        row_i[0] += 1

        tk.Label(form, text="Baud Rate :",
                 bg=C["bg"], fg=C["text"],
                 font=(_FONT_UI, 10), anchor="w").grid(
            row=row_i[0], column=0, sticky="w", pady=4)
        baud_var = tk.StringVar(
            value=str(self.cfg.get("baud_rate", 9600)))
        entry_vars["baud_rate"] = baud_var
        ttk.Combobox(form, textvariable=baud_var,
                     values=["1200","2400","4800","9600","19200",
                             "38400","57600","115200"],
                     width=10,
                     font=(_FONT_MONO, 10)).grid(
            row=row_i[0], column=1, sticky="w",
            padx=(10, 0), pady=4)
        row_i[0] += 1

        # Slave IDs
        _section("ID SLAVE SENSOR  (MODBUS RTU)")
        for label, key in [
            ("Slave ID pH  :",   "slave_id_ph"),
            ("Slave ID TSS :",   "slave_id_tss"),
            ("Slave ID Debit :", "slave_id_debit"),
        ]:
            _entry(label, key, 8)

        # Server & UID
        _section("SERVER & IDENTITAS")
        for label, key in [
            ("UID 1 (raw) :",       "uid1"),
            ("UID 1 (processed) :", "uid1_processed"),
            ("UID 2 :",             "uid2"),
            ("Server URL 1 :",      "server_url1"),
            ("Secret Key URL 1 :",  "secret_key_url1"),
            ("Server URL 2 :",     "server_url2"),
            ("Secret Key URL 2 :", "secret_key_url2"),
        ]:
            _entry(label, key)

        # Batas Server 2
        _section("BATAS DATA SERVER 2  (KLHK)")
        tk.Label(form,
                 text="Server 1 = data murni sensor.  "
                      "Server 2 = data di-clamp sesuai batas ini.",
                 bg=C["bg"], fg=C["text_muted"],
                 font=(_FONT_UI, 8)).grid(
            row=row_i[0], column=0, columnspan=3,
            sticky="w", pady=(0, 6))
        row_i[0] += 1

        for col, txt in enumerate(["Parameter", "Min", "Max"]):
            tk.Label(form, text=txt,
                     bg=C["bg"], fg=C["text_muted"],
                     font=(_FONT_UI, 9, "bold")).grid(
                row=row_i[0], column=col, sticky="w",
                padx=(0 if col == 0 else 10, 0), pady=2)
        row_i[0] += 1
        tk.Frame(form, bg=C["border"], height=1).grid(
            row=row_i[0], column=0, columnspan=3,
            sticky="ew", pady=(0, 4))
        row_i[0] += 1

        for param, key_min, key_max in [
            ("pH",    "limit_ph_min",    "limit_ph_max"),
            ("TSS",   "limit_tss_min",   "limit_tss_max"),
            ("Debit", "limit_debit_min", "limit_debit_max"),
        ]:
            tk.Label(form, text=param,
                     bg=C["bg"], fg=C["text"],
                     font=(_FONT_UI, 10)).grid(
                row=row_i[0], column=0, sticky="w", pady=4)
            for col, key in enumerate([key_min, key_max], start=1):
                v = tk.StringVar(value=str(self.cfg.get(key, "")))
                entry_vars[key] = v
                tk.Entry(form, textvariable=v,
                         font=(_FONT_MONO, 10), width=10,
                         relief="flat", bd=0,
                         bg=C["card"], fg=C["text"],
                         insertbackground=C["primary"],
                         highlightthickness=1,
                         highlightbackground=C["border"],
                         highlightcolor=C["primary"],
                         justify="center").grid(
                    row=row_i[0], column=col, sticky="w",
                    padx=(10, 0), pady=4)
            row_i[0] += 1

        form.columnconfigure(1, weight=1)

        # Save handler
        def _save():
            int_keys   = {"baud_rate", "slave_id_ph",
                          "slave_id_tss", "slave_id_debit"}
            float_keys = {
                "limit_ph_min",    "limit_ph_max",
                "limit_tss_min",   "limit_tss_max",
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

        # Bottom bar
        tk.Frame(form, bg=C["border"], height=1).grid(
            row=row_i[0], column=0, columnspan=3,
            sticky="ew", pady=(14, 8))
        row_i[0] += 1

        btn_fr = tk.Frame(form, bg=C["bg"])
        btn_fr.grid(row=row_i[0], column=0, columnspan=3, pady=4)

        self._flat_btn(btn_fr, "✓  Simpan & Hubungkan",
                       _save, C["primary"], "white",
                       pady=9).pack(side="left", padx=(0, 8))
        self._flat_btn(btn_fr, "✕  Batal",
                       win.destroy, C["bg"], C["text_muted"],
                       pady=9).pack(side="left")
