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
from collections import deque
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
_FONT_UI    = "Segoe UI"
_FONT_MONO  = "Consolas"
_REF_W      = 1280        # resolusi referensi (lebar)
_REF_H      = 720         # resolusi referensi (tinggi)


class SparingGUI:
    """Jendela utama SPARING Monitor."""

    def __init__(self, root: tk.Tk, app: "SparingApp"):
        self.root = root
        self.app  = app
        self.cfg  = app.cfg

        self._sensor_vars:  dict = {}      # key → StringVar nilai sensor (raw)
        self._proc_vars:    dict = {}      # key → StringVar nilai processed
        self._conn_dots:    dict = {}      # key → Canvas (dot indikator)
        self._conn_chips:   dict = {}      # key → (StringVar, Label)
        self._conn_labels:  dict = {}      # alias untuk update_connection()
        self._limit_vars:   dict = {}      # key → StringVar batas S2
        self._sensor_cards: dict = {}      # cfg_key → (canvas, col, row_frame)
        self._dust_row_frame:  tk.Frame = None  # baris kartu debu
        self._noise_row_frame: tk.Frame = None  # baris kartu noise
        self._temp_row_frame:  tk.Frame = None  # baris kartu suhu air

        self._unlocked:        bool      = False
        self._chart_data:         dict      = {}        # key → deque(maxlen=30)
        self._chart_canvases:     dict      = {}        # key → Canvas (normal view)
        self._locked_chart_canvases: dict   = {}        # key → Canvas (locked overlay)
        self._test_mode_btn:   tk.Button    = None   # tombol floating mode
        self._test_mode_var:   tk.StringVar = None
        self._gap_btn:         tk.Button    = None   # tombol isi gap data
        self._gap_btn_var:     tk.StringVar = None
        self._gap_info_var:    tk.StringVar = None
        self._lock_btn_var:    tk.StringVar = None   # set saat build footer
        self._limits_wrapper:  tk.Frame  = None      # hidden sampai unlock
        self._limits_pack_ref: tk.Widget = None      # widget sebelum limits card
        self._last_card_shadow: tk.Frame = None      # diset oleh _card()
        self._right_canvas:    tk.Canvas = None      # canvas scroll panel kanan

        self._setup_window()
        self._calc_scale()
        self._setup_styles()
        self._build()
        self._tick_clock()

    # ── Scaling ───────────────────────────────────────────────────────────────
    def _calc_scale(self) -> None:
        """
        Hitung faktor skala dari resolusi layar aktual vs referensi 1280×720.
        sc < 1 → layar kecil (7-inch 800×480), sc > 1 → layar besar (1920×1080).
        Layar ≤ 600px tinggi dianggap layar kecil — gunakan layout kompak.
        """
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self._sc       = max(0.50, min(sw / _REF_W, sh / _REF_H, 1.8))
        self._small    = sh <= 600          # True untuk layar 7-inch
        self._r_width  = self._sp(240) if self._small else self._sp(270)

    def _fs(self, n: int) -> int:
        """Skala font size — minimal 7pt."""
        return max(7, round(n * self._sc))

    def _sp(self, n: int) -> int:
        """Skala pixel (padding, width, height) — minimal 1px."""
        return max(1, round(n * self._sc))

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
        self._build_footer()        # side="bottom" — harus dipasang sebelum content (expand)
        # Content frame fills space between header and footer
        self._content_frame = tk.Frame(self.root, bg=C["bg"])
        self._content_frame.pack(fill="both", expand=True)
        self._build_sensor_row()
        self._build_dust_row()
        self._build_noise_row()
        self._build_body()
        # Mulai dalam mode terkunci — overlay dipasang setelah widget selesai render
        self.root.after(200, self._lock)

    # ═══════════════════════════════════════════════════════════════════════════
    # HEADER
    # ═══════════════════════════════════════════════════════════════════════════
    def _build_header(self) -> None:
        # Top accent stripe
        tk.Frame(self.root, bg=C["primary"],
                 height=self._sp(4)).pack(fill="x")

        hdr = tk.Frame(self.root, bg=C["panel"])
        hdr.pack(fill="x")
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill="x")

        row = tk.Frame(hdr, bg=C["panel"])
        row.pack(fill="x", padx=self._sp(18),
                 pady=(self._sp(3) if self._small else self._sp(5)))

        # ── Logo ──────────────────────────────────────────────────────────────
        self._add_logo(row)

        # Divider
        tk.Frame(row, bg=C["border"], width=1).pack(
            side="left", fill="y", padx=self._sp(18))

        # ── Title ─────────────────────────────────────────────────────────────
        title_col = tk.Frame(row, bg=C["panel"])
        title_col.pack(side="left", fill="y")

        self._app_title_var = tk.StringVar(value=self._get_app_title())
        tk.Label(title_col,
                 textvariable=self._app_title_var,
                 bg=C["panel"], fg=C["text"],
                 font=(_FONT_UI, self._fs(13), "bold")).pack(anchor="w")

        sub_row = tk.Frame(title_col, bg=C["panel"])
        sub_row.pack(anchor="w", pady=(self._sp(4), 0))
        tk.Frame(sub_row, bg=C["accent"],
                 width=self._sp(22), height=2).pack(
            side="left", anchor="center", padx=(0, self._sp(8)))
        tk.Label(sub_row,
                 text="SPARING  ●  Online Monitoring System",
                 bg=C["panel"], fg=C["accent"],
                 font=(_FONT_UI, self._fs(9))).pack(side="left")

        # ── Connection status chips (center-right) ────────────────────────────
        conn_row = tk.Frame(row, bg=C["panel"])
        conn_row.pack(side="left", padx=(self._sp(30), 0), fill="y")

        _dot_sz   = self._sp(8)
        _simulate = self.cfg.get("simulate_sensors", False)
        self._rs485_chip_frame    = None
        self._internet_chip_frame = None
        for key, label in [
            ("rs485",    "RS485"),
            ("internet", "Internet"),
            ("server1",  "Internal"),
            ("server2",  "KLHK"),
        ]:
            chip_frame = tk.Frame(conn_row, bg=C["panel"])
            chip_frame.pack(side="left", padx=self._sp(6))

            dot = tk.Canvas(chip_frame, width=_dot_sz, height=_dot_sz,
                            bg=C["panel"], highlightthickness=0)
            dot.pack(side="left", padx=(0, self._sp(4)), pady=2)
            dot.create_oval(0, 0, _dot_sz, _dot_sz,
                            fill=C["border"], outline="", tags="dot")

            tk.Label(chip_frame, text=label,
                     bg=C["panel"], fg=C["text_muted"],
                     font=(_FONT_UI, self._fs(8))).pack(
                side="left", pady=(0, 1))

            var = tk.StringVar(value="...")
            status_lbl = tk.Label(chip_frame, textvariable=var,
                                  bg=C["panel"], fg=C["text_muted"],
                                  font=(_FONT_UI, self._fs(8), "bold"))
            status_lbl.pack(side="left", padx=(self._sp(2), 0))

            self._conn_dots[key]   = dot
            self._conn_chips[key]  = (var, status_lbl)
            self._conn_labels[key] = (var, status_lbl)

            if key == "rs485":
                self._rs485_chip_frame = chip_frame
                if _simulate:
                    chip_frame.pack_forget()   # sembunyikan saat floating mode
            if key == "internet":
                self._internet_chip_frame = chip_frame

        # ── Clock ─────────────────────────────────────────────────────────────
        clk_frame = tk.Frame(row, bg=C["primary"],
                             padx=self._sp(14), pady=self._sp(5))
        clk_frame.pack(side="right")

        self._date_var  = tk.StringVar()
        self._clock_var = tk.StringVar()

        tk.Label(clk_frame, textvariable=self._date_var,
                 bg=C["primary"], fg="#A8D0FF",
                 font=(_FONT_UI, self._fs(8))).pack(anchor="e")
        tk.Label(clk_frame, textvariable=self._clock_var,
                 bg=C["primary"], fg="white",
                 font=(_FONT_MONO, self._fs(16), "bold")).pack(anchor="e")

    def _add_logo(self, parent) -> None:
        if HAS_PIL and Image is not None and LOGO_FILE.exists():
            try:
                img = Image.open(LOGO_FILE).convert("RGBA")

                # Pertahankan aspek rasio — tinggi max sesuai header
                max_h = self._sp(48)
                max_w = self._sp(120)
                orig_w, orig_h = img.size
                ratio = min(max_w / orig_w, max_h / orig_h)
                new_w = max(1, round(orig_w * ratio))
                new_h = max(1, round(orig_h * ratio))

                # Resize high-quality dengan antialiasing
                img = img.resize((new_w * 2, new_h * 2), Image.LANCZOS)
                img = img.resize((new_w, new_h), Image.LANCZOS)

                # Tempel ke background panel (hilangkan artefak transparan)
                bg_img = Image.new("RGBA", (new_w, new_h), C["panel"])
                bg_img.paste(img, mask=img.split()[3])
                img = bg_img.convert("RGB")

                self._logo_img = ImageTk.PhotoImage(img)
                tk.Label(parent, image=self._logo_img,
                         bg=C["panel"]).pack(side="left")
                return
            except Exception:
                pass
        tk.Label(parent, text="SUCOFINDO",
                 bg=C["panel"], fg=C["primary_dark"],
                 font=(_FONT_UI, self._fs(13), "bold")).pack(side="left")

    # ═══════════════════════════════════════════════════════════════════════════
    # SENSOR ROW  — tiga kartu penuh warna, nilai monospace besar
    # ═══════════════════════════════════════════════════════════════════════════
    def _build_sensor_row(self) -> None:
        self._main_sensor_row = tk.Frame(self._content_frame, bg=C["bg"])
        self._main_sensor_row.pack(fill="x", padx=self._sp(14),
                                   pady=(self._sp(6), 0))
        row = self._main_sensor_row

        defs = [
            # cfg_key                  key     label    unit    bg           label_color
            ("sensor_ph_enabled",    "ph",    "pH",    "",     C["s_ph"],    "#A8CCFF"),
            ("sensor_tss_enabled",   "tss",   "TSS",   "mg/L", C["s_tss"],   "#A0D8F0"),
            ("sensor_debit_enabled", "debit", "DEBIT", "m³/s", C["s_debit"], "#9AECD8"),
        ]
        for col, (cfg_key, key, label, unit, bg, lc) in enumerate(defs):
            card = self._sensor_card(row, key, label, unit, bg, lc)
            card.grid(row=0, column=col, padx=self._sp(6), sticky="nsew")
            row.columnconfigure(col, weight=1)
            self._sensor_cards[cfg_key] = (card,)   # simpan hanya widget

        # Terapkan visibilitas awal
        self.root.after(100, self.apply_sensor_visibility)

    def _sensor_card(self, parent, key: str, label: str,
                     unit: str, bg: str, label_color: str) -> tk.Canvas:
        """
        Kartu sensor dengan sudut melengkung menggunakan Canvas.
        Selalu menampilkan raw (besar) dan processed (kecil, dimask saat terkunci).
        """
        canvas, inner = self._rounded_canvas(
            parent, bg, radius=self._sp(18))

        py_top  = self._sp(3 if self._small else 4)
        py_bot  = self._sp(2 if self._small else 3)
        f_label = self._fs(9  if self._small else 11)
        f_raw   = self._fs(24 if self._small else 30)
        f_unit  = self._fs(8  if self._small else 10)
        f_proc  = self._fs(9  if self._small else 11)
        px_sep  = self._sp(10 if self._small else 14)

        tk.Label(inner, text=label,
                 bg=bg, fg=label_color,
                 font=(_FONT_UI, f_label, "bold")).pack(pady=(py_top, 0))

        raw_var = tk.StringVar(value="0.00")
        self._sensor_vars[key] = raw_var
        tk.Label(inner, textvariable=raw_var,
                 bg=bg, fg="white",
                 font=(_FONT_MONO, f_raw, "bold")).pack(pady=(self._sp(1), 0))

        tk.Label(inner, text=unit,
                 bg=bg, fg=label_color,
                 font=(_FONT_UI, f_unit)).pack()

        tk.Frame(inner, bg=label_color, height=1).pack(
            fill="x", padx=px_sep,
            pady=(self._sp(3), self._sp(2)))

        proc_row = tk.Frame(inner, bg=bg)
        proc_row.pack(pady=(0, py_bot))

        tk.Label(proc_row, text="PROCESSED",
                 bg=bg, fg=label_color,
                 font=(_FONT_UI, self._fs(6), "bold")).pack()

        proc_var = tk.StringVar(value="●  ●  ●")
        self._proc_vars[key] = proc_var
        tk.Label(proc_row, textvariable=proc_var,
                 bg=bg, fg=label_color,
                 font=(_FONT_MONO, f_proc, "bold")).pack()

        return canvas

    # ═══════════════════════════════════════════════════════════════════════════
    # DUST ROW  — tiga kartu kompak PM2.5 / PM10 / PM100
    # ═══════════════════════════════════════════════════════════════════════════
    def _build_dust_row(self) -> None:
        # Wrapper baris dengan label judul di kiri
        wrap = tk.Frame(self._content_frame, bg=C["bg"])
        wrap.pack(fill="x", padx=self._sp(14), pady=(self._sp(4), 0))
        self._dust_row_frame = wrap
        if not self.cfg.get("sensor_dust_enabled", True):
            wrap.pack_forget()

        # Label "KUALITAS UDARA" sebagai sub-header
        tk.Label(wrap, text="KUALITAS UDARA  (RK300-02)",
                 bg=C["bg"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(7), "bold")).pack(
            anchor="w", pady=(0, self._sp(2 if self._small else 3)))

        row = tk.Frame(wrap, bg=C["bg"])
        row.pack(fill="x")

        # Warna kartu debu — lebih gelap/abu agar berbeda dari sensor utama
        _DUST_COLOR = "#455A64"   # biru-abu gelap
        _DUST_LC    = "#B0BEC5"   # abu terang untuk label

        defs = [
            ("pm25",  "PM 2.5",  "ug/m³", "#37474F", "#90A4AE"),
            ("pm10",  "PM 10",   "ug/m³", "#37474F", "#80CBC4"),
            ("pm100", "TSP",  "ug/m³", "#37474F", "#FFCC80"),
        ]
        for col, (key, label, unit, bg, lc) in enumerate(defs):
            card = self._dust_card(row, key, label, unit, bg, lc)
            card.grid(row=0, column=col, padx=self._sp(6), sticky="nsew")
            row.columnconfigure(col, weight=1)
        self._sensor_cards["sensor_dust_enabled"] = (wrap,)

    def _build_noise_row(self) -> None:
        """Baris kartu sensor kebisingan + suhu air — side by side (noise 2/3, suhu 1/3)."""
        wrap = tk.Frame(self._content_frame, bg=C["bg"])
        wrap.pack(fill="x", padx=self._sp(14), pady=(self._sp(3), 0))
        self._noise_row_frame = wrap
        self._temp_row_frame  = wrap  # shared frame

        noise_on = self.cfg.get("sensor_noise_enabled", True)
        temp_on  = self.cfg.get("sensor_temp_enabled",  True)
        if not noise_on and not temp_on:
            wrap.pack_forget()

        # Grid row yang menampung kedua kartu
        card_row = tk.Frame(wrap, bg=C["bg"])
        card_row.pack(fill="x")
        self._noise_card_row = card_row
        card_row.columnconfigure(0, weight=2)
        card_row.columnconfigure(1, weight=1)

        # ── Kartu Kebisingan (kolom 0) ────────────────────────────────────────
        bg_n, lc_n = "#4A148C", "#CE93D8"
        noise_canvas, noise_inner = self._rounded_canvas(card_row, bg_n, radius=self._sp(14))
        noise_canvas.grid(row=0, column=0, sticky="nsew",
                          padx=(self._sp(6), self._sp(3)), pady=self._sp(2))
        self._noise_canvas = noise_canvas
        self._sensor_cards["sensor_noise_enabled"] = (wrap,)

        py    = self._sp(3 if self._small else 4)
        f_val = self._fs(22 if self._small else 28)
        f_lbl = self._fs(10 if self._small else 12)

        # Instan | Leq
        top_row = tk.Frame(noise_inner, bg=bg_n)
        top_row.pack(fill="x")

        left = tk.Frame(top_row, bg=bg_n)
        left.pack(side="left", expand=True, fill="both", padx=(self._sp(8), 0))

        tk.Label(left, text="INSTAN",
                 bg=bg_n, fg=lc_n, font=(_FONT_UI, f_lbl, "bold")).pack(pady=(py, 0))

        instant_var = tk.StringVar(value="0.0")
        self._sensor_vars["noise_instant"] = instant_var
        tk.Label(left, textvariable=instant_var,
                 bg=bg_n, fg="white", font=(_FONT_MONO, f_val, "bold")).pack(
            pady=(self._sp(1), 0))
        tk.Label(left, text="dB",
                 bg=bg_n, fg=lc_n, font=(_FONT_UI, self._fs(9))).pack(pady=(0, py))

        tk.Frame(top_row, bg=lc_n, width=1).pack(side="left", fill="y", pady=self._sp(8))

        right = tk.Frame(top_row, bg=bg_n)
        right.pack(side="left", expand=True, fill="both", padx=(0, self._sp(8)))

        tk.Label(right, text="Leq  (10 min)",
                 bg=bg_n, fg=lc_n, font=(_FONT_UI, f_lbl, "bold")).pack(pady=(py, 0))

        leq_var = tk.StringVar(value="0.0")
        self._sensor_vars["noise_leq"] = leq_var
        tk.Label(right, textvariable=leq_var,
                 bg=bg_n, fg="white", font=(_FONT_MONO, f_val, "bold")).pack(
            pady=(self._sp(1), 0))
        tk.Label(right, text="dB",
                 bg=bg_n, fg=lc_n, font=(_FONT_UI, self._fs(9))).pack(pady=(0, py))

        # Processed Leq
        tk.Frame(noise_inner, bg=lc_n, height=1).pack(
            fill="x", padx=self._sp(12), pady=(self._sp(2), self._sp(1)))
        proc_row = tk.Frame(noise_inner, bg=bg_n)
        proc_row.pack(pady=(0, py))
        tk.Label(proc_row, text="PROCESSED  Leq",
                 bg=bg_n, fg=lc_n, font=(_FONT_UI, self._fs(6), "bold")).pack()
        proc_var = tk.StringVar(value="●  ●  ●")
        self._proc_vars["noise"] = proc_var
        tk.Label(proc_row, textvariable=proc_var, bg=bg_n, fg=lc_n,
                 font=(_FONT_MONO, self._fs(9 if self._small else 11), "bold")).pack()

        # ── Kartu Suhu Air (kolom 1) ──────────────────────────────────────────
        bg_t, lc_t = "#BF360C", "#FFAB91"
        temp_canvas, temp_inner = self._rounded_canvas(card_row, bg_t, radius=self._sp(14))
        temp_canvas.grid(row=0, column=1, sticky="nsew",
                         padx=(self._sp(3), self._sp(6)), pady=self._sp(2))
        self._temp_canvas = temp_canvas
        self._sensor_cards["sensor_temp_enabled"] = (wrap,)

        py_t  = self._sp(2 if self._small else 3)
        f_val_t = self._fs(18 if self._small else 22)

        tk.Label(temp_inner, text="SUHU AIR",
                 bg=bg_t, fg=lc_t,
                 font=(_FONT_UI, self._fs(10 if self._small else 12), "bold")).pack(
            pady=(py_t, 0))

        temp_var = tk.StringVar(value="0.0")
        self._sensor_vars["temp"] = temp_var
        tk.Label(temp_inner, textvariable=temp_var,
                 bg=bg_t, fg="white", font=(_FONT_MONO, f_val_t, "bold")).pack(
            pady=(self._sp(1), 0))
        tk.Label(temp_inner, text="°C",
                 bg=bg_t, fg=lc_t, font=(_FONT_UI, self._fs(9))).pack(pady=(0, py_t))

        # Atur layout awal sesuai sensor aktif
        self._update_noise_temp_layout(noise_on, temp_on)

    def _update_noise_temp_layout(self, noise_on: bool, temp_on: bool) -> None:
        """Atur grid noise/suhu: side-by-side, atau full-width jika salah satu nonaktif."""
        if not hasattr(self, "_noise_canvas") or not hasattr(self, "_temp_canvas"):
            return
        nc, tc = self._noise_canvas, self._temp_canvas
        cr = self._noise_card_row
        nc.grid_remove()
        tc.grid_remove()
        px6 = self._sp(6)
        px3 = self._sp(3)
        if noise_on and temp_on:
            nc.grid(row=0, column=0, sticky="nsew", padx=(px6, px3), pady=self._sp(2))
            tc.grid(row=0, column=1, sticky="nsew", padx=(px3, px6), pady=self._sp(2))
            cr.columnconfigure(0, weight=2)
            cr.columnconfigure(1, weight=1)
        elif noise_on:
            nc.grid(row=0, column=0, columnspan=2, sticky="nsew",
                    padx=px6, pady=self._sp(2))
            cr.columnconfigure(0, weight=1)
            cr.columnconfigure(1, weight=0)
        elif temp_on:
            tc.grid(row=0, column=0, columnspan=2, sticky="nsew",
                    padx=px6, pady=self._sp(2))
            cr.columnconfigure(0, weight=1)
            cr.columnconfigure(1, weight=0)

    def _dust_card(self, parent, key: str, label: str,
                   unit: str, bg: str, label_color: str) -> tk.Canvas:
        """Kartu kompak untuk sensor debu (PM)."""
        canvas, inner = self._rounded_canvas(parent, bg, radius=self._sp(14))

        py_top = self._sp(3 if self._small else 4)
        py_bot = self._sp(2 if self._small else 3)
        f_title = self._fs(10 if self._small else 12)
        f_val   = self._fs(22 if self._small else 28)

        tk.Label(inner, text=label,
                 bg=bg, fg=label_color,
                 font=(_FONT_UI, f_title, "bold")).pack(pady=(py_top, 0))

        raw_var = tk.StringVar(value="0.0")
        self._sensor_vars[key] = raw_var
        tk.Label(inner, textvariable=raw_var,
                 bg=bg, fg="white",
                 font=(_FONT_MONO, f_val, "bold")).pack(pady=(self._sp(1), 0))

        tk.Label(inner, text=unit,
                 bg=bg, fg=label_color,
                 font=(_FONT_UI, self._fs(9))).pack()

        tk.Frame(inner, bg=label_color, height=1).pack(
            fill="x", padx=self._sp(10),
            pady=(self._sp(2), self._sp(1)))

        proc_row = tk.Frame(inner, bg=bg)
        proc_row.pack(pady=(0, py_bot))

        tk.Label(proc_row, text="PROCESSED",
                 bg=bg, fg=label_color,
                 font=(_FONT_UI, self._fs(6), "bold")).pack()

        proc_var = tk.StringVar(value="●  ●  ●")
        self._proc_vars[key] = proc_var
        tk.Label(proc_row, textvariable=proc_var,
                 bg=bg, fg=label_color,
                 font=(_FONT_MONO, self._fs(9 if self._small else 11), "bold")).pack()

        return canvas

    # ═══════════════════════════════════════════════════════════════════════════
    # BODY  — log (kiri lebar) + panel info (kanan)
    # ═══════════════════════════════════════════════════════════════════════════
    def _build_body(self) -> None:
        body = tk.Frame(self._content_frame, bg=C["bg"])
        body.pack(fill="both", expand=True,
                  padx=self._sp(14), pady=self._sp(6))
        self._body_frame = body

        self._build_log_panel(body)
        self._build_right_panel(body)

    # ═══════════════════════════════════════════════════════════════════════════
    # LOCKED OVERLAY  — full-screen sensor grid saat mode kunci
    # ═══════════════════════════════════════════════════════════════════════════
    def _locked_rounded_card(self, parent, bg: str,
                             row: int, col: int,
                             colspan: int = 1, px: int = 5) -> tuple:
        """
        Canvas rounded yang mengembang mengisi sel grid.
        Sudut melengkung: create_window diletakkan offset r dari tepi sehingga
        pojok canvas (C["bg"]) terlihat sebagai rounded corner.
        """
        r = self._sp(16)
        canvas = tk.Canvas(parent, bg=C["bg"], highlightthickness=0, bd=0)
        canvas.grid(row=row, column=col, columnspan=colspan,
                    sticky="nsew", padx=px, pady=px)
        inner = tk.Frame(canvas, bg=bg)
        win_id = canvas.create_window(r, r, window=inner, anchor="nw")

        def _draw():
            w, h = canvas.winfo_width(), canvas.winfo_height()
            if w < 4 or h < 4:
                return
            re = min(r, w // 4, h // 3)
            # Polygon mengisi seluruh canvas dengan sudut melengkung
            canvas.delete("bg")
            pts = [re, 0,   w-re, 0,   w, 0,   w, re,
                   w, h-re, w, h,   w-re, h,  re, h,
                   0, h,   0, h-re,  0, re,   0, 0]
            canvas.create_polygon(pts, smooth=True,
                                  fill=bg, outline="", tags="bg")
            canvas.tag_lower("bg")
            # Inner frame mengisi area dalam radius — pojok dibiarkan kosong
            canvas.itemconfig(win_id,
                              width=max(1, w - 2 * r),
                              height=max(1, h - 2 * r))

        canvas.bind("<Configure>", lambda e: canvas.after_idle(_draw))
        return canvas, inner

    def _build_locked_overlay(self) -> None:
        """Full-screen overlay: sensor grid atas + status/info bar bawah."""
        ol = tk.Frame(self._content_frame, bg=C["bg"])
        self._locked_overlay = ol

        px  = self._sp(7 if self._small else 9)
        pad = self._sp(10 if self._small else 13)

        # ── Bottom bar: STATUS (kiri) | INFO (kanan) ──────────────────────────
        bot_h = self._sp(88 if self._small else 110)
        bot = tk.Frame(ol, bg=C["bg"], height=bot_h)
        bot.pack(side="bottom", fill="x", padx=pad, pady=(0, pad))
        bot.pack_propagate(False)

        st_frame = tk.Frame(bot, bg=C["bg"])
        st_frame.pack(side="left", fill="both", expand=True,
                      padx=(0, self._sp(4)))
        self._build_locked_status_panel(st_frame)

        info_frame = tk.Frame(bot, bg=C["bg"])
        info_frame.pack(side="right", fill="both", expand=True,
                        padx=(self._sp(4), 0))
        self._build_locked_info_panel(info_frame)

        # ── Sensor grid (atas, mengisi sisa ruang) ────────────────────────────
        sg = tk.Frame(ol, bg=C["bg"])
        sg.pack(fill="both", expand=True, padx=pad, pady=(pad, px))

        cfg = self.cfg
        dust_on      = cfg.get("sensor_dust_enabled",  True)
        noise_on_cfg = cfg.get("sensor_noise_enabled", True)
        temp_on_cfg  = cfg.get("sensor_temp_enabled",  True)

        # Ketika hanya sensor air + suhu aktif (tanpa debu/noise):
        # pakai layout 4-kolom — SUHU masuk baris yang sama dengan pH/TSS/DEBIT
        four_col = temp_on_cfg and not dust_on and not noise_on_cfg

        if four_col:
            for c in range(4):
                sg.columnconfigure(c, weight=1)
            sg.rowconfigure(0, weight=1)
            sg.rowconfigure(1, weight=0)
            sg.rowconfigure(2, weight=0)
        else:
            for c in range(3):
                sg.columnconfigure(c, weight=1)
            sg.rowconfigure(0, weight=4)
            sg.rowconfigure(1, weight=3 if dust_on else 0)
            sg.rowconfigure(2, weight=2 if (noise_on_cfg or temp_on_cfg) else 0)

        f_main_lbl  = self._fs(11 if self._small else 14)
        f_main_val  = self._fs(38 if self._small else 50)
        f_main_unit = self._fs(10 if self._small else 12)
        f_sub_lbl   = self._fs(9  if self._small else 12)
        f_sub_val   = self._fs(26 if self._small else 36)
        f_sub_unit  = self._fs(8  if self._small else 10)
        chart_h     = self._sp(20 if self._small else 26)

        # Font lebih kecil untuk 4 kolom agar muat di layar
        if four_col:
            f_main_lbl  = self._fs(9  if self._small else 12)
            f_main_val  = self._fs(28 if self._small else 38)
            f_main_unit = self._fs(8  if self._small else 10)

        def _value_card(key, label, unit, bg, lc, row, col, colspan=1,
                        f_lbl=f_main_lbl, f_val=f_main_val, f_unit=f_main_unit):
            _cv, inner = self._locked_rounded_card(sg, bg, row, col,
                                                    colspan=colspan, px=px)
            content = tk.Frame(inner, bg=bg)
            content.pack(expand=True, fill="both")
            vf = tk.Frame(content, bg=bg)
            vf.place(relx=0.5, rely=0.46, anchor="center")
            tk.Label(vf, text=label, bg=bg, fg=lc,
                     font=(_FONT_UI, f_lbl, "bold")).pack()
            tk.Label(vf, textvariable=self._sensor_vars[key],
                     bg=bg, fg="white",
                     font=(_FONT_MONO, f_val, "bold")).pack(
                pady=(self._sp(1), 0))
            if unit:
                tk.Label(vf, text=unit, bg=bg, fg=lc,
                         font=(_FONT_UI, f_unit)).pack()
            return _cv

        # Baris 0 — kualitas air
        water_sensors = [
            ("sensor_ph_enabled",    "ph",    "pH",    "",     C["s_ph"],    "#A8CCFF"),
            ("sensor_tss_enabled",   "tss",   "TSS",   "mg/L", C["s_tss"],   "#A0D8F0"),
            ("sensor_debit_enabled", "debit", "DEBIT", "m³/s", C["s_debit"], "#9AECD8"),
        ]
        for col, (cfg_key, key, label, unit, bg, lc) in enumerate(water_sensors):
            card = _value_card(key, label, unit, bg, lc, row=0, col=col)
            if not cfg.get(cfg_key, True):
                card.grid_remove()

        # SUHU masuk baris 0 sebagai kolom ke-4 (layout 4-kolom)
        if four_col:
            _value_card("temp", "SUHU AIR", "°C", "#BF360C", "#FFAB91",
                        row=0, col=3, colspan=1)

        # Baris 1 — kualitas udara / PM
        for col, (key, label, unit, bg, lc) in enumerate([
            ("pm25",  "PM 2.5", "ug/m³", "#37474F", "#90A4AE"),
            ("pm10",  "PM 10",  "ug/m³", "#37474F", "#80CBC4"),
            ("pm100", "TSP", "ug/m³", "#37474F", "#FFCC80"),
        ]):
            card = _value_card(key, label, unit, bg, lc, row=1, col=col,
                               f_lbl=f_sub_lbl, f_val=f_sub_val,
                               f_unit=f_sub_unit)
            if not cfg.get("sensor_dust_enabled", True):
                card.grid_remove()

        # Baris 2 — noise + suhu (hanya jika bukan layout 4-kolom)
        if not four_col:
            noise_on = noise_on_cfg
            temp_on  = temp_on_cfg

            if noise_on:
                nc = 2 if temp_on else 3
                _cv, ni = self._locked_rounded_card(sg, "#4A148C", row=2,
                                                     col=0, colspan=nc, px=px)
                nbg, nlc = "#4A148C", "#CE93D8"
                lf = tk.Frame(ni, bg=nbg)
                lf.place(relx=0.25, rely=0.5, anchor="center")
                tk.Label(lf, text="INSTAN", bg=nbg, fg=nlc,
                         font=(_FONT_UI, f_sub_lbl, "bold")).pack()
                tk.Label(lf, textvariable=self._sensor_vars["noise_instant"],
                         bg=nbg, fg="white",
                         font=(_FONT_MONO, f_sub_val, "bold")).pack(
                    pady=(self._sp(1), 0))
                tk.Label(lf, text="dB", bg=nbg, fg=nlc,
                         font=(_FONT_UI, f_sub_unit)).pack()
                tk.Frame(ni, bg=nlc, width=1).place(relx=0.5, rely=0.1, relheight=0.8)
                rf = tk.Frame(ni, bg=nbg)
                rf.place(relx=0.75, rely=0.5, anchor="center")
                tk.Label(rf, text="Leq  (10 min)", bg=nbg, fg=nlc,
                         font=(_FONT_UI, f_sub_lbl, "bold")).pack()
                tk.Label(rf, textvariable=self._sensor_vars["noise_leq"],
                         bg=nbg, fg="white",
                         font=(_FONT_MONO, f_sub_val, "bold")).pack(
                    pady=(self._sp(1), 0))
                tk.Label(rf, text="dB", bg=nbg, fg=nlc,
                         font=(_FONT_UI, f_sub_unit)).pack()

            if temp_on:
                # noise aktif: temp di col 2 (1/3 kanan)
                # noise nonaktif: temp di col 0 colspan=2 (2/3 kiri)
                _value_card("temp", "SUHU AIR", "°C", "#BF360C", "#FFAB91",
                            row=2, col=(2 if noise_on else 0),
                            colspan=(1 if noise_on else 2),
                            f_lbl=f_sub_lbl, f_val=f_sub_val, f_unit=f_sub_unit)

    def _build_locked_status_panel(self, parent: tk.Frame) -> None:
        """Panel kiri bawah — STATUS PENGIRIMAN."""
        bg    = C["card"]
        muted = C["text_muted"]
        f_ttl = self._fs(7 if self._small else 8)
        f_big = self._fs(10 if self._small else 12)
        f_sm  = self._fs(6 if self._small else 7)
        sp    = self._sp

        _, inner_c = self._rounded_canvas(parent, bg, radius=sp(14),
                                          fill="both", expand=True)
        tk.Frame(inner_c, bg=C["warning"], height=sp(3)).pack(fill="x")
        inner = tk.Frame(inner_c, bg=bg)
        inner.pack(fill="both", expand=True, padx=sp(10), pady=sp(6))

        tk.Label(inner, text="STATUS PENGIRIMAN", bg=bg, fg=C["warning"],
                 font=(_FONT_UI, f_ttl, "bold")).pack(anchor="w")

        self._locked_status_lbl = tk.Label(
            inner, textvariable=self._send_status_var,
            bg=bg, fg=muted,
            font=(_FONT_UI, f_big, "bold"),
            wraplength=sp(140 if self._small else 180), justify="left")
        self._locked_status_lbl.pack(anchor="w", pady=(sp(2), 0))

        tk.Label(inner, textvariable=self._send_detail_var,
                 bg=bg, fg=muted, font=(_FONT_UI, f_sm),
                 wraplength=sp(140 if self._small else 180),
                 justify="left").pack(anchor="w", pady=(sp(1), 0))

    def _build_locked_info_panel(self, parent: tk.Frame) -> None:
        """Panel kanan bawah — INFO PENGIRIMAN."""
        bg    = C["card"]
        muted = C["text_muted"]
        f_ttl = self._fs(7 if self._small else 8)
        f_val = self._fs(9 if self._small else 10)
        f_sm  = self._fs(6 if self._small else 7)
        sp    = self._sp

        _, inner_c = self._rounded_canvas(parent, bg, radius=sp(14),
                                          fill="both", expand=True)
        tk.Frame(inner_c, bg=C["primary"], height=sp(3)).pack(fill="x")
        inner = tk.Frame(inner_c, bg=bg)
        inner.pack(fill="both", expand=True, padx=sp(10), pady=sp(6))

        tk.Label(inner, text="INFO PENGIRIMAN", bg=bg, fg=C["primary"],
                 font=(_FONT_UI, f_ttl, "bold")).pack(anchor="w")

        def _irow(lbl, var, fg=None):
            r = tk.Frame(inner, bg=bg)
            r.pack(fill="x", pady=(sp(1), 0))
            tk.Label(r, text=lbl + ":", bg=bg, fg=muted,
                     font=(_FONT_UI, f_sm)).pack(side="left")
            tk.Label(r, textvariable=var, bg=bg,
                     fg=(fg or C["primary"]),
                     font=(_FONT_MONO, f_val, "bold")).pack(
                side="left", padx=(sp(4), 0))

        _irow("Terkumpul", self._count_var, C["primary"])
        ttk.Progressbar(inner, variable=self._progress,
                        maximum=self.cfg.get("data_batch_size", 30),
                        length=80).pack(fill="x", pady=(sp(2), sp(1)))
        _irow("Kirim", self._last_tx_var, muted)
        _irow("Buffer", self._buf_var, C["warning"])

    # ── Log panel ─────────────────────────────────────────────────────────────
    def _build_log_panel(self, parent: tk.Frame) -> None:
        canvas, outer = self._rounded_canvas(
            parent, C["card"], radius=self._sp(14),
            side="left", fill="both", expand=True,
            padx=(0, self._sp(8)))
        self._log_canvas = canvas   # simpan referensi untuk show/hide
        canvas.pack_forget()        # sembunyikan sampai di-unlock

        # Accent stripe + title
        tk.Frame(outer, bg=C["accent"],
                 height=self._sp(3)).pack(fill="x")

        title_bar = tk.Frame(outer, bg=C["card"])
        title_bar.pack(fill="x")
        tk.Label(title_bar, text="LOG AKTIVITAS",
                 bg=C["card"], fg=C["accent"],
                 font=(_FONT_UI, self._fs(9), "bold"),
                 padx=self._sp(10), pady=self._sp(7)).pack(side="left")
        tk.Frame(outer, bg=C["border"], height=1).pack(fill="x")

        # Terminal area (rounded bottom corners mengikuti canvas)
        log_frame = tk.Frame(outer, bg=C["log_bg"])
        log_frame.pack(fill="both", expand=True)

        sb = ttk.Scrollbar(log_frame, orient="vertical")
        self._log_txt = tk.Text(
            log_frame, state="disabled",
            font=(_FONT_MONO, self._fs(8)),
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
        # Container — lebar tetap saat terkunci (isi penuh), kanan saat terbuka
        right_outer = tk.Frame(parent, bg=C["bg"], width=self._r_width)
        right_outer.pack(fill="both", expand=True)   # isi penuh saat log hidden
        right_outer.pack_propagate(False)
        self._right_outer = right_outer   # simpan untuk resize saat unlock/lock

        # Canvas + scrollbar agar konten bisa di-scroll jika tidak muat
        r_canvas = tk.Canvas(right_outer, bg=C["bg"], highlightthickness=0,
                             bd=0)
        r_sb = ttk.Scrollbar(right_outer, orient="vertical",
                             command=r_canvas.yview)
        r_canvas.configure(yscrollcommand=r_sb.set)
        r_sb.pack(side="right", fill="y")
        r_canvas.pack(side="left", fill="both", expand=True)

        right = tk.Frame(r_canvas, bg=C["bg"])
        r_win = r_canvas.create_window((0, 0), window=right, anchor="nw")

        def _on_right_configure(e):
            r_canvas.configure(scrollregion=r_canvas.bbox("all"))
        def _on_canvas_resize(e):
            r_canvas.itemconfig(r_win, width=e.width)

        right.bind("<Configure>", _on_right_configure)
        r_canvas.bind("<Configure>", _on_canvas_resize)

        # Scroll dengan mouse wheel
        def _on_mousewheel(e):
            r_canvas.yview_scroll(int(-1 * (e.delta / 120)), "units")
        r_canvas.bind_all("<MouseWheel>", _on_mousewheel)

        self._right_canvas = r_canvas   # simpan untuk auto-scroll saat unlock

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
            fill="x", pady=(6, 2))

        # ── Status pengiriman ─────────────────────────────────────────────────
        inner2 = self._card(right, "STATUS PENGIRIMAN", C["online"],
                            fill="x", pady=(0, 8))

        self._send_status_var = tk.StringVar(value="— Menunggu batch pertama")
        self._send_status_lbl = tk.Label(
            inner2, textvariable=self._send_status_var,
            bg=C["card"], fg=C["text_muted"],
            font=(_FONT_UI, self._fs(8), "bold"),
            wraplength=self._r_width - self._sp(28), justify="left",
        )
        self._send_status_lbl.pack(anchor="w", pady=(self._sp(1), 0))

        self._send_detail_var = tk.StringVar(value="")
        tk.Label(inner2, textvariable=self._send_detail_var,
                 bg=C["card"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(7)),
                 wraplength=self._r_width - self._sp(28),
                 justify="left").pack(anchor="w", pady=(2, 2))

        # ── Batas data server 2 — tersembunyi sampai unlock ──────────────────
        # Simpan referensi widget sebelumnya agar bisa di-pack 'after' saat unlock
        self._limits_pack_ref = self._last_card_shadow

        self._limits_wrapper = tk.Frame(right, bg=C["bg"])
        # Tidak di-pack dulu — akan muncul saat _unlock() dipanggil

        inner3 = self._card(self._limits_wrapper, "BATAS DATA  SERVER 2",
                            C["accent"], fill="x")

        self._limit_rows: dict = {}

        _lim_defs = [
            ("sensor_ph_enabled",    "pH",    "limit_ph_min",    "limit_ph_max",    "limit_ph_float_lo_min",    "limit_ph_float_lo_max",    "limit_ph_float_hi_min",    "limit_ph_float_hi_max"),
            ("sensor_tss_enabled",   "TSS",   "limit_tss_min",   "limit_tss_max",   "limit_tss_float_lo_min",   "limit_tss_float_lo_max",   "limit_tss_float_hi_min",   "limit_tss_float_hi_max"),
            ("sensor_debit_enabled", "Debit", "limit_debit_min", "limit_debit_max", "limit_debit_float_lo_min", "limit_debit_float_lo_max", "limit_debit_float_hi_min", "limit_debit_float_hi_max"),
            ("sensor_dust_enabled",  "PM2.5", "limit_pm25_min",  "limit_pm25_max",  "limit_pm25_float_lo_min",  "limit_pm25_float_lo_max",  "limit_pm25_float_hi_min",  "limit_pm25_float_hi_max"),
            ("sensor_dust_enabled",  "PM10",  "limit_pm10_min",  "limit_pm10_max",  "limit_pm10_float_lo_min",  "limit_pm10_float_lo_max",  "limit_pm10_float_hi_min",  "limit_pm10_float_hi_max"),
            ("sensor_dust_enabled",  "PM100", "limit_pm100_min", "limit_pm100_max", "limit_pm100_float_lo_min", "limit_pm100_float_lo_max", "limit_pm100_float_hi_min", "limit_pm100_float_hi_max"),
            ("sensor_noise_enabled", "Noise", "limit_noise_min", "limit_noise_max", "limit_noise_float_lo_min", "limit_noise_float_lo_max", "limit_noise_float_hi_min", "limit_noise_float_hi_max"),
            ("sensor_temp_enabled",  "Suhu",  "limit_temp_min",  "limit_temp_max",  "limit_temp_float_lo_min",  "limit_temp_float_lo_max",  "limit_temp_float_hi_min",  "limit_temp_float_hi_max"),
        ]

        def _lv(key):
            v = tk.StringVar(value=str(self.cfg.get(key, "—")))
            self._limit_vars[key] = v
            return v

        for cfg_key, param, k_min, k_max, k_flo_min, k_flo_max, k_fhi_min, k_fhi_max in _lim_defs:
            # Daftarkan semua key ke _limit_vars meski tidak ditampilkan
            for k in (k_flo_min, k_flo_max, k_fhi_min, k_fhi_max):
                _lv(k)

            wrap = tk.Frame(inner3, bg=C["card"])

            # Baris 1: Nama  MIN  MAX
            row1 = tk.Frame(wrap, bg=C["card"])
            row1.pack(fill="x", pady=(self._sp(4), 0))
            tk.Label(row1, text=param,
                     bg=C["card"], fg=C["text"],
                     font=(_FONT_UI, self._fs(8), "bold"),
                     width=5, anchor="w").pack(side="left")
            for prefix, key in [("Min", k_min), ("Max", k_max)]:
                tk.Label(row1, text=prefix + ":",
                         bg=C["card"], fg=C["text_muted"],
                         font=(_FONT_UI, self._fs(7))).pack(side="left",
                                                             padx=(self._sp(6), 1))
                tk.Label(row1, textvariable=_lv(key),
                         bg=C["card_alt"], fg=C["primary"],
                         font=(_FONT_MONO, self._fs(8), "bold"),
                         padx=self._sp(3), relief="flat").pack(side="left")

            # Baris 2: Lo: lo_min – lo_max  Hi: hi_min – hi_max
            row2 = tk.Frame(wrap, bg=C["card"])
            row2.pack(fill="x", pady=(1, self._sp(3)))
            tk.Label(row2, text="",
                     bg=C["card"], width=5).pack(side="left")
            for lbl_txt, k_a, k_b in [
                ("Lo", k_flo_min, k_flo_max),
                ("Hi", k_fhi_min, k_fhi_max),
            ]:
                tk.Label(row2, text=lbl_txt + ":",
                         bg=C["card"], fg=C["text_muted"],
                         font=(_FONT_UI, self._fs(6))).pack(side="left",
                                                             padx=(self._sp(5), 1))
                tk.Label(row2, textvariable=self._limit_vars[k_a],
                         bg=C["card"], fg=C["accent"],
                         font=(_FONT_MONO, self._fs(7))).pack(side="left")
                tk.Label(row2, text="–",
                         bg=C["card"], fg=C["text_muted"],
                         font=(_FONT_UI, self._fs(6))).pack(side="left", padx=1)
                tk.Label(row2, textvariable=self._limit_vars[k_b],
                         bg=C["card"], fg=C["accent"],
                         font=(_FONT_MONO, self._fs(7))).pack(side="left")

            tk.Frame(wrap, bg=C["border"], height=1).pack(
                fill="x", pady=(0, self._sp(1)))
            self._limit_rows.setdefault(cfg_key, []).append(wrap)

        # ── Kontrol RS485 + Settings — hanya tampil saat unlocked ───────────────
        self._ctrl_wrapper = tk.Frame(right, bg=C["bg"])
        self._ctrl_wrapper.pack(fill="x")
        self._ctrl_wrapper.pack_forget()   # sembunyikan sampai unlock

        ctrl_inner = self._card(self._ctrl_wrapper, "KONTROL RS485",
                                C["text_muted"], fill="x", pady=(0, 8))

        use_hat = self.cfg.get("use_rs485_hat", False)

        # Baris mode — USB atau HAT
        mode_row = tk.Frame(ctrl_inner, bg=C["card"])
        mode_row.pack(fill="x", pady=(0, self._sp(4)))
        mode_label = "RS485 HAT" if use_hat else "USB Adapter"
        mode_color = C["accent"] if use_hat else C["primary"]
        tk.Label(mode_row, text="Mode :",
                 bg=C["card"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(8))).pack(side="left")
        tk.Label(mode_row, text=mode_label,
                 bg=C["card"], fg=mode_color,
                 font=(_FONT_UI, self._fs(8), "bold")).pack(
            side="left", padx=(self._sp(6), 0))

        # Baris port aktif
        port_row = tk.Frame(ctrl_inner, bg=C["card"])
        port_row.pack(fill="x", pady=(0, self._sp(6)))

        port_label = "Port HAT :" if use_hat else "Port aktif :"
        port_value = (self.cfg.get("rs485_hat_port", "/dev/ttyS1")
                      if use_hat else self.cfg.get("serial_port", "—"))
        tk.Label(port_row, text=port_label,
                 bg=C["card"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(9))).pack(side="left")
        self._port_var = tk.StringVar(value=port_value)
        tk.Label(port_row, textvariable=self._port_var,
                 bg=C["card"], fg=mode_color,
                 font=(_FONT_MONO, self._fs(9), "bold")).pack(
            side="left", padx=(self._sp(6), 0))

        # Tombol — HAT: hanya Hubungkan Ulang | USB: + Scan Port
        btn_row = tk.Frame(ctrl_inner, bg=C["card"])
        btn_row.pack(fill="x")
        self._flat_btn(
            btn_row, "↻  Hubungkan Ulang",
            self._reconnect_rs485,
            C["primary"], "white"
        ).pack(side="left", fill="x", expand=True,
               padx=(0, 0 if use_hat else 4))

        if not use_hat:
            self._flat_btn(
                btn_row, "⌕  Scan Port",
                self._scan_ports_dialog,
                C["card_alt"], C["primary"],
                border=True
            ).pack(side="left", fill="x", expand=True)

        # ── Settings ──────────────────────────────────────────────────────────
        self._flat_btn(
            self._ctrl_wrapper, "⚙   Pengaturan Koneksi",
            self._open_settings,
            C["primary_dark"], "white",
            pady=10
        ).pack(fill="x", pady=(4, 0))

        # ── Toggle Floating Mode ──────────────────────────────────────────────
        is_test = self.cfg.get("simulate_sensors", False)
        self._test_mode_var = tk.StringVar(
            value="⚠  Floating Mode: AKTIF" if is_test else "⚠  Floating Mode: NONAKTIF")
        self._test_mode_btn = tk.Button(
            self._ctrl_wrapper,
            textvariable   = self._test_mode_var,
            command        = self._on_toggle_test_mode,
            bg             = "#F57F17" if is_test else C["card_alt"],
            fg             = "white"   if is_test else C["text"],
            font           = (_FONT_UI, self._fs(9), "bold"),
            relief         = "flat", cursor="hand2",
            activebackground = "#E65100",
            activeforeground = "white",
            pady           = self._sp(8),
        )
        self._test_mode_btn.pack(fill="x", pady=(4, 0))

        # ── Gap Fill ──────────────────────────────────────────────────────────
        gap_wrap = tk.Frame(self._ctrl_wrapper, bg=C["bg"])
        gap_wrap.pack(fill="x", pady=(4, 0))

        self._gap_btn_var = tk.StringVar(value="⏱  Isi Gap Data Server 1")
        self._gap_btn = tk.Button(
            gap_wrap,
            textvariable = self._gap_btn_var,
            command      = self._on_gap_fill,
            bg="#E65100", fg="white",
            font=(_FONT_UI, self._fs(9), "bold"),
            relief="flat", cursor="hand2",
            activebackground="#BF360C",
            activeforeground="white",
            pady=self._sp(8),
        )
        self._gap_btn.pack(fill="x")

        # Info durasi gap
        self._gap_info_var = tk.StringVar(value="")
        tk.Label(gap_wrap, textvariable=self._gap_info_var,
                 bg=C["bg"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(7))).pack(anchor="w", pady=(2, 0))
        self._refresh_gap_info()

    def _get_app_title(self) -> str:
        water_on = any(self.cfg.get(k, True)
                       for k in ("sensor_ph_enabled", "sensor_tss_enabled",
                                 "sensor_debit_enabled", "sensor_temp_enabled"))
        env_on   = self.cfg.get("sensor_dust_enabled",  True) or \
                   self.cfg.get("sensor_noise_enabled", True)
        if water_on:
            return "SISTEM PEMANTAUAN KUALITAS AIR"
        if env_on:
            return "SISTEM PEMANTAUAN KUALITAS UDARA"
        return "SISTEM PEMANTAUAN KUALITAS AIR"

    def _update_app_title(self) -> None:
        if hasattr(self, "_app_title_var"):
            self._app_title_var.set(self._get_app_title())

    # ═══════════════════════════════════════════════════════════════════════════
    # FOOTER
    # ═══════════════════════════════════════════════════════════════════════════
    def _build_footer(self) -> None:
        tk.Frame(self.root, bg=C["border"], height=1).pack(fill="x")
        bar = tk.Frame(self.root, bg=C["panel"], height=self._sp(22))
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        # Left indicator strip
        tk.Frame(bar, bg=C["primary"],
                 width=self._sp(3)).pack(side="left", fill="y")

        self._statusbar_var = tk.StringVar(value="Siap")
        tk.Label(bar, textvariable=self._statusbar_var,
                 bg=C["panel"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(9))).pack(
            side="left", padx=self._sp(10))

        self._flat_btn(bar, "⛶  F11",
                       self._toggle_fullscreen,
                       C["bg"], C["text_muted"],
                       pady=0).pack(side="right", padx=self._sp(6), pady=3)

        self._flat_btn(bar, "⚙  Sensor",
                       self._open_sensor_select,
                       C["bg"], C["accent"],
                       pady=0).pack(side="right", padx=(0, self._sp(2)), pady=3)

        # Tombol rahasia — terlihat seperti indikator biasa
        self._lock_btn_var = tk.StringVar(value="🔒")
        lck = tk.Label(bar, textvariable=self._lock_btn_var,
                       bg=C["panel"], fg=C["border"],
                       font=(_FONT_UI, self._fs(13)),
                       cursor="hand2")
        lck.pack(side="right", padx=(0, 2), pady=2)
        lck.bind("<Button-1>", lambda e: self._show_lock_dialog())

        is_test = self.cfg.get("simulate_sensors", False)
        port = self.cfg.get("serial_port", "—")
        self._mode_label_var = tk.StringVar(
            value=f"Mode: {'FLOAT' if is_test else 'LIVE'}  ·  Port: {port}  ·  "
                  f"{SYS_PLATFORM}  ·  ESC = keluar fullscreen")
        tk.Label(bar,
                 textvariable=self._mode_label_var,
                 bg=C["panel"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(8))).pack(
            side="right", padx=self._sp(12))

    # ═══════════════════════════════════════════════════════════════════════════
    # WIDGET HELPERS
    # ═══════════════════════════════════════════════════════════════════════════
    def _make_dialog(self, w: int, h: int, title: str = "") -> tk.Toplevel:
        """
        Buat Toplevel yang selalu muncul di atas window utama,
        termasuk saat fullscreen di embedded display (Orange Pi / RPi).
        """
        win = tk.Toplevel(self.root)
        win.title(title)
        win.configure(bg=C["bg"])
        win.resizable(False, False)
        win.transient(self.root)

        sx = self.root.winfo_screenwidth()
        sy = self.root.winfo_screenheight()
        x  = (sx - w) // 2
        y  = (sy - h) // 2
        win.geometry(f"{w}x{h}+{x}+{y}")

        win.attributes("-topmost", True)   # selalu di atas fullscreen
        win.update_idletasks()
        win.lift()
        win.focus_force()
        win.grab_set()
        return win

    # ═══════════════════════════════════════════════════════════════════════════
    def _rounded_canvas(self, parent, card_bg: str,
                        radius: int = None,
                        outer_bg: str = None,
                        **pack_kw) -> tuple:
        """
        Buat Canvas dengan latar sudut melengkung (smooth polygon).
        Kembalikan (canvas, inner_frame).
        canvas  — dipasang ke parent sesuai pack_kw
        inner   — Frame tempat konten diletakkan
        """
        r        = radius   if radius   is not None else self._sp(16)
        outer_bg = outer_bg if outer_bg is not None else C["bg"]
        pad      = self._sp(2)

        canvas = tk.Canvas(parent, bg=outer_bg,
                           highlightthickness=0, bd=0)
        if pack_kw:
            canvas.pack(**pack_kw)

        inner  = tk.Frame(canvas, bg=card_bg)
        win_id = canvas.create_window(pad, pad, window=inner, anchor="nw")

        def _redraw():
            w = canvas.winfo_width()
            h = canvas.winfo_height()
            if w < 4 or h < 4:
                return
            canvas.delete("rr")
            pts = [
                r, 0,   w-r, 0,   w, 0,   w, r,
                w, h-r, w, h,     w-r, h, r, h,
                0, h,   0, h-r,   0, r,   0, 0,
            ]
            canvas.create_polygon(pts, smooth=True,
                                  fill=card_bg, outline="", tags="rr")
            canvas.tag_lower("rr")
            canvas.itemconfig(win_id,
                              width=w - pad * 2,
                              height=h - pad * 2)

        def _on_canvas_resize(event=None):
            # Lebar canvas berubah → sesuaikan lebar inner frame
            canvas.itemconfig(win_id, width=canvas.winfo_width() - pad * 2)
            canvas.after_idle(_redraw)

        def _on_inner_resize(event=None):
            # Konten inner frame berubah → sesuaikan tinggi canvas
            req_h = inner.winfo_reqheight() + pad * 2
            if req_h > 4 and abs(canvas.winfo_height() - req_h) > 1:
                canvas.configure(height=req_h)
            canvas.after_idle(_redraw)

        canvas.bind("<Configure>", _on_canvas_resize)
        inner.bind("<Configure>",  _on_inner_resize)
        return canvas, inner

    def _card(self, parent, title: str, accent: str,
              **pack_kw) -> tk.Frame:
        """
        Kartu putih sudut melengkung dengan accent bar atas dan judul.
        Kembalikan inner frame tempat konten diletakkan.
        """
        canvas, outer = self._rounded_canvas(
            parent, C["card"], radius=self._sp(12), **pack_kw)
        self._last_card_shadow = canvas   # dipakai oleh limits wrapper

        # Accent stripe tipis di atas
        tk.Frame(outer, bg=accent,
                 height=self._sp(3)).pack(fill="x")

        # Title row
        title_row = tk.Frame(outer, bg=C["card"])
        title_row.pack(fill="x")
        tk.Label(title_row, text=title,
                 bg=C["card"], fg=accent,
                 font=(_FONT_UI, self._fs(8), "bold"),
                 padx=self._sp(10), pady=self._sp(6)).pack(side="left")

        tk.Frame(outer, bg=C["border"], height=1).pack(fill="x")

        # Content frame
        content = tk.Frame(outer, bg=C["card"])
        content.pack(fill="both", expand=True,
                     padx=self._sp(10), pady=self._sp(8))
        return content

    def _info_row(self, parent, label: str, var: tk.StringVar,
                  fg: str, suffix: str = "") -> None:
        row = tk.Frame(parent, bg=C["card"])
        row.pack(fill="x", pady=self._sp(2))
        tk.Label(row, text=label,
                 bg=C["card"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(7)),
                 anchor="w", width=15).pack(side="left")
        tk.Label(row, textvariable=var,
                 bg=C["card"], fg=fg,
                 font=(_FONT_MONO, self._fs(8), "bold")).pack(side="left")
        if suffix:
            tk.Label(row, text=suffix,
                     bg=C["card"], fg=C["text_muted"],
                     font=(_FONT_UI, self._fs(7))).pack(side="left")

    def _flat_btn(self, parent, text: str, cmd,
                  bg: str, fg: str,
                  pady: int = 5,
                  border: bool = False) -> tk.Button:
        kw = dict(
            text=text, command=cmd,
            bg=bg, fg=fg,
            font=(_FONT_UI, self._fs(9), "bold"),
            relief="flat", cursor="hand2", pady=self._sp(pady),
            activebackground=C["accent"],
            activeforeground="white",
        )
        if border:
            kw.update(highlightthickness=1,
                      highlightbackground=C["border"],
                      highlightcolor=C["primary"])
        return tk.Button(parent, **kw)

    # ── Sensor selection ──────────────────────────────────────────────────────
    def apply_sensor_visibility(self) -> None:
        """
        Tampilkan/sembunyikan kartu sensor dan re-layout kolom agar tidak ada gap.
        Kartu aktif selalu diisi berurutan dari kolom 0.
        """
        # Saat locked overlay aktif, semua row sudah disembunyikan oleh _lock().
        # Jangan pack ulang — cukup update limits; _unlock() yang akan restore layout.
        if hasattr(self, "_locked_overlay") and not self._unlocked:
            self.apply_limits_visibility()
            return

        # ── Sensor utama (grid) ───────────────────────────────────────────────
        row_frame = self._main_sensor_row
        ordered = [
            ("sensor_ph_enabled",    "ph"),
            ("sensor_tss_enabled",   "tss"),
            ("sensor_debit_enabled", "debit"),
        ]

        for i in range(3):
            row_frame.columnconfigure(i, weight=0, minsize=0)
        for cfg_key, _ in ordered:
            card, *_ = self._sensor_cards[cfg_key]
            card.grid_remove()

        active_col = 0
        for cfg_key, _ in ordered:
            if self.cfg.get(cfg_key, True):
                card, *_ = self._sensor_cards[cfg_key]
                card.grid(row=0, column=active_col,
                          padx=self._sp(6), sticky="nsew")
                row_frame.columnconfigure(active_col, weight=1)
                active_col += 1

        any_main = active_col > 0

        # ── Re-pack semua baris dalam urutan tampil — tanpa before= ──────────
        # (before= gagal bila target belum di-pack; pack ulang berurutan lebih aman)
        px = self._sp(14)
        sp_main  = (self._sp(4 if self._small else 6), 0)
        sp_sub   = (self._sp(3 if self._small else 4), 0)

        all_rows = [self._main_sensor_row, self._dust_row_frame,
                    self._noise_row_frame]
        body = getattr(self, "_body_frame", None)
        if body:
            all_rows.append(body)

        for fr in all_rows:
            if fr:
                try:
                    fr.pack_forget()
                except Exception:
                    pass

        noise_on = self.cfg.get("sensor_noise_enabled", True)
        temp_on  = self.cfg.get("sensor_temp_enabled",  True)

        if any_main:
            self._main_sensor_row.pack(fill="x", padx=px, pady=sp_main)
        if self._dust_row_frame and self.cfg.get("sensor_dust_enabled", True):
            self._dust_row_frame.pack(fill="x", padx=px, pady=sp_sub)
        if self._noise_row_frame and (noise_on or temp_on):
            self._noise_row_frame.pack(fill="x", padx=px, pady=sp_sub)
            self._update_noise_temp_layout(noise_on, temp_on)
        if body:
            body.pack(fill="both", expand=True, padx=px, pady=self._sp(6))

        # ── Batas — ikuti sensor yang aktif ──────────────────────────────────
        self.apply_limits_visibility()
        self._update_app_title()

    def apply_limits_visibility(self) -> None:
        """Tampilkan baris batas hanya untuk sensor yang aktif."""
        if not hasattr(self, "_limit_rows"):
            return
        for cfg_key, rows in self._limit_rows.items():
            enabled = self.cfg.get(cfg_key, True)
            for row in rows:
                if enabled:
                    row.pack(fill="x", pady=2)
                else:
                    row.pack_forget()

    def _open_sensor_select(self) -> None:
        """Dialog pilih sensor yang aktif — ditampilkan dan dikirim ke server."""
        w, h = self._sp(380), self._sp(420)
        win = self._make_dialog(w, h, "Pilihan Sensor")
        win.configure(bg=C["panel"])

        sensors = [
            ("sensor_ph_enabled",    "pH",              C["s_ph"],    "#A8CCFF"),
            ("sensor_tss_enabled",   "TSS (mg/L)",      C["s_tss"],   "#A0D8F0"),
            ("sensor_debit_enabled", "Debit (m³/s)",    C["s_debit"], "#9AECD8"),
            ("sensor_dust_enabled",  "Debu — PM2.5 / PM10 / PM100 (RK300-02)",
             "#37474F", "#90A4AE"),
            ("sensor_noise_enabled", "Kebisingan — Noise dB (Sound Level Meter)",
             "#4A148C", "#CE93D8"),
            ("sensor_temp_enabled",  "Suhu Air (°C)",
             "#BF360C", "#FFAB91"),
        ]

        check_vars = {}

        def _apply():
            changed = False
            for cfg_key, var in check_vars.items():
                new_val = var.get()
                if self.cfg.get(cfg_key, True) != new_val:
                    self.cfg[cfg_key] = new_val
                    changed = True
            if changed:
                save_config(self.cfg)
                self.apply_sensor_visibility()
                active = [s[1] for s in sensors
                          if self.cfg.get(s[0], True)]
                self.log(f"Sensor aktif: {', '.join(active) if active else '(tidak ada)'}")
            win.destroy()

        # ── Tombol bar — pack PERTAMA ke bawah agar selalu terlihat ──────────
        tk.Frame(win, bg=C["border"], height=1).pack(
            side="bottom", fill="x")
        btn_bar = tk.Frame(win, bg=C["panel"],
                           padx=self._sp(16), pady=self._sp(10))
        btn_bar.pack(side="bottom", fill="x")
        self._flat_btn(btn_bar, "✓  Terapkan",
                       _apply, C["primary"], "white",
                       pady=7).pack(side="left", padx=(0, self._sp(8)),
                                    ipadx=self._sp(10))
        self._flat_btn(btn_bar, "✕  Batal",
                       win.destroy, C["bg"], C["text_muted"],
                       pady=7).pack(side="left", ipadx=self._sp(10))

        # ── Header ────────────────────────────────────────────────────────────
        tk.Frame(win, bg=C["primary"],
                 height=self._sp(4)).pack(fill="x")
        tk.Label(win, text="PILIH SENSOR AKTIF",
                 bg=C["panel"], fg=C["text"],
                 font=(_FONT_UI, self._fs(12), "bold"),
                 padx=self._sp(16), pady=self._sp(12)).pack(anchor="w")
        tk.Frame(win, bg=C["border"], height=1).pack(fill="x")

        tk.Label(win,
                 text="Sensor yang dinonaktifkan tidak akan\nditampilkan dan tidak dikirim ke server.",
                 bg=C["panel"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(8)),
                 justify="left").pack(anchor="w",
                                      padx=self._sp(16), pady=(self._sp(10), self._sp(4)))

        # ── Daftar sensor ─────────────────────────────────────────────────────
        for cfg_key, label, bg, lc in sensors:
            var = tk.BooleanVar(value=self.cfg.get(cfg_key, True))
            check_vars[cfg_key] = var

            row = tk.Frame(win, bg=C["panel"],
                           pady=self._sp(6), padx=self._sp(16))
            row.pack(fill="x")

            tk.Frame(row, bg=bg,
                     width=self._sp(10), height=self._sp(10)).pack(
                side="left", padx=(0, self._sp(10)))

            # Toggle dipak side="right" SEBELUM label teks agar selalu dapat tempat
            _lbl = tk.Label(
                row,
                text="✓" if var.get() else "",
                bg=C["primary"] if var.get() else C["border"],
                fg="white",
                font=(_FONT_UI, self._fs(10), "bold"),
                width=2,
                padx=self._sp(3),
                pady=self._sp(3),
                cursor="hand2",
            )
            _lbl.pack(side="right", padx=(self._sp(6), 0))

            def _bind_toggle(_v=var, _l=_lbl):
                def _tog(e=None):
                    _v.set(not _v.get())
                    col = C["primary"] if _v.get() else C["border"]
                    _l.config(text="✓" if _v.get() else "", bg=col)
                _l.bind("<Button-1>", _tog)
            _bind_toggle()

            tk.Label(row, text=label,
                     bg=C["panel"], fg=C["text"],
                     font=(_FONT_UI, self._fs(10))).pack(side="left", expand=True,
                                                          anchor="w")

            tk.Frame(win, bg=C["border"], height=1).pack(
                fill="x", padx=self._sp(16))

    # ── Lock / Unlock ─────────────────────────────────────────────────────────
    def _show_lock_dialog(self) -> None:
        """Tampilkan dialog PIN. Jika sudah terbuka, langsung kunci ulang."""
        if self._unlocked:
            self._lock()
            return

        w, h = self._sp(300), self._sp(210)
        win = self._make_dialog(w, h)
        win.configure(bg=C["panel"])
        inner_win = win   # alias agar kode di bawah tidak perlu diubah

        # Accent bar atas
        tk.Frame(win, bg=C["primary"],
                 height=self._sp(4)).pack(fill="x")

        tk.Label(inner_win, text="Masukkan PIN",
                 bg=C["panel"], fg=C["text"],
                 font=(_FONT_UI, self._fs(11), "bold")).pack(
            pady=(self._sp(14), self._sp(6)))

        tk.Label(inner_win, text="Diperlukan untuk melihat data processed",
                 bg=C["panel"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(8))).pack(
            pady=(0, self._sp(10)))

        pin_var = tk.StringVar()
        entry = tk.Entry(inner_win, textvariable=pin_var,
                         show="●", font=(_FONT_MONO, self._fs(16)),
                         width=10, justify="center",
                         relief="flat", bd=0,
                         bg=C["bg"], fg=C["text"],
                         insertbackground=C["primary"],
                         highlightthickness=2,
                         highlightbackground=C["border"],
                         highlightcolor=C["primary"])
        entry.pack(ipady=self._sp(6))
        entry.focus_set()

        err_var = tk.StringVar(value="")
        tk.Label(inner_win, textvariable=err_var,
                 bg=C["panel"], fg=C["offline"],
                 font=(_FONT_UI, self._fs(9))).pack(pady=(self._sp(4), 0))

        def _try_unlock(event=None):
            correct = str(self.cfg.get("secret_pin", "1234"))
            if pin_var.get() == correct:
                win.destroy()
                self._unlock()
            else:
                err_var.set("PIN salah, coba lagi")
                pin_var.set("")

        entry.bind("<Return>", _try_unlock)

        btn_row = tk.Frame(inner_win, bg=C["panel"])
        btn_row.pack(pady=(self._sp(8), 0))
        self._flat_btn(btn_row, "Buka", _try_unlock,
                       C["primary"], "white", pady=6).pack(
            side="left", padx=(0, self._sp(6)),
            ipadx=self._sp(12))
        self._flat_btn(btn_row, "Batal", win.destroy,
                       C["bg"], C["text_muted"], pady=6).pack(
            side="left", ipadx=self._sp(12))

    def _unlock(self) -> None:
        """Tampilkan log aktivitas, processed values, dan limits card."""
        self._unlocked = True
        if self._lock_btn_var:
            self._lock_btn_var.set("🔓")

        # Sembunyikan locked overlay, tampilkan kembali sensor rows + body
        if hasattr(self, "_locked_overlay"):
            self._locked_overlay.pack_forget()
        # Restore sensor rows dalam urutan yang benar
        px = self._sp(14)
        if self._main_sensor_row:
            self._main_sensor_row.pack(fill="x", padx=px, pady=(self._sp(6), 0))
        if self._dust_row_frame and self.cfg.get("sensor_dust_enabled", True):
            self._dust_row_frame.pack(fill="x", padx=px, pady=(self._sp(4), 0))
        noise_on = self.cfg.get("sensor_noise_enabled", True)
        temp_on  = self.cfg.get("sensor_temp_enabled",  True)
        if self._noise_row_frame and (noise_on or temp_on):
            self._noise_row_frame.pack(fill="x", padx=px, pady=(self._sp(3), 0))
            self._update_noise_temp_layout(noise_on, temp_on)
        if hasattr(self, "_body_frame"):
            self._body_frame.pack(fill="both", expand=True,
                                  padx=px, pady=self._sp(6))

        # Tampilkan kontrol RS485 & pengaturan
        if hasattr(self, "_ctrl_wrapper"):
            self._ctrl_wrapper.pack(fill="x")

        # Tampilkan log aktivitas — panel kanan kembali ke lebar tetap di kanan
        if hasattr(self, "_log_canvas"):
            if hasattr(self, "_right_outer"):
                self._right_outer.pack_forget()
                self._right_outer.configure(width=self._r_width)
                self._right_outer.pack(side="right", fill="y")
            self._log_canvas.pack(side="left", fill="both", expand=True,
                                  padx=(0, self._sp(8)))

        # Isi nilai processed segera dari cache terakhir (jika ada)
        if hasattr(self, "_last_proc"):
            ph, tss, debit = self._last_proc
            self._proc_vars["ph"].set(f"{ph:.2f}")
            self._proc_vars["tss"].set(f"{tss:.2f}")
            self._proc_vars["debit"].set(f"{debit:.2f}")
        if hasattr(self, "_last_proc_dust"):
            pm25, pm10, pm100 = self._last_proc_dust
            self._proc_vars["pm25"].set(f"{pm25:.1f}")
            self._proc_vars["pm10"].set(f"{pm10:.1f}")
            self._proc_vars["pm100"].set(f"{pm100:.1f}")
        if hasattr(self, "_last_proc_noise"):
            self._proc_vars["noise"].set(f"{self._last_proc_noise:.1f}")

        # Tampilkan limits wrapper setelah status card
        if self._limits_wrapper and self._limits_pack_ref:
            self._limits_wrapper.pack(
                fill="x", pady=(0, 8),
                after=self._limits_pack_ref)
            # Scroll ke card BATAS agar langsung terlihat
            if self._right_canvas:
                def _scroll_to_limits():
                    try:
                        c = self._right_canvas
                        c.update_idletasks()
                        total = c.bbox("all")
                        if total and total[3] > 0:
                            y_pos = self._limits_wrapper.winfo_y()
                            fraction = y_pos / total[3]
                            c.yview_moveto(max(0.0, fraction - 0.05))
                    except Exception:
                        pass
                self._right_canvas.after(100, _scroll_to_limits)

        # Terapkan visibilitas sensor (termasuk perubahan yang dibuat saat terkunci)
        self.apply_sensor_visibility()

        self.log("🔓 Tampilan data processed & batas diaktifkan")

    def _lock(self) -> None:
        """Sembunyikan processed values dan tampilkan full-screen sensor overlay."""
        self._unlocked = False
        if self._lock_btn_var:
            self._lock_btn_var.set("🔒")

        # Mask nilai processed
        for key, var in self._proc_vars.items():
            var.set("●  ●  ●")

        # Sembunyikan kontrol RS485 & pengaturan
        if hasattr(self, "_ctrl_wrapper"):
            self._ctrl_wrapper.pack_forget()

        # Sembunyikan limits wrapper
        if self._limits_wrapper:
            self._limits_wrapper.pack_forget()

        # Sembunyikan sensor rows dan body
        for fr in [self._main_sensor_row, self._dust_row_frame,
                   self._noise_row_frame]:
            if fr:
                try: fr.pack_forget()
                except Exception: pass
        if hasattr(self, "_body_frame"):
            self._body_frame.pack_forget()

        # Bangun ulang dan tampilkan locked overlay (ikut config sensor terkini)
        self._locked_chart_canvases = {}
        if hasattr(self, "_locked_overlay"):
            try: self._locked_overlay.destroy()
            except Exception: pass
        self._build_locked_overlay()
        self._locked_overlay.pack(fill="both", expand=True)

        self.log("🔒 Tampilan data processed & batas disembunyikan")

    # ── Clock ─────────────────────────────────────────────────────────────────
    def _tick_clock(self) -> None:
        now = datetime.now()
        self._clock_var.set(now.strftime("%H:%M:%S"))
        self._date_var.set(now.strftime("%d %B %Y"))
        self.root.after(1000, self._tick_clock)

    # ═══════════════════════════════════════════════════════════════════════════
    # ── Floating Mode helpers ─────────────────────────────────────────────────
    def _on_toggle_test_mode(self) -> None:
        self.app.toggle_test_mode()

    def update_test_mode_btn(self, is_test: bool) -> None:
        """Perbarui tampilan tombol floating mode, indikator RS485, dan label footer."""
        if self._test_mode_btn:
            self._test_mode_var.set(
                "⚠  Floating Mode: AKTIF" if is_test else "⚠  Floating Mode: NONAKTIF")
            self._test_mode_btn.configure(
                bg = "#F57F17" if is_test else C["card_alt"],
                fg = "white"   if is_test else C["text"],
            )
        # Tampilkan/sembunyikan chip RS485 di header
        rs485_frame = getattr(self, "_rs485_chip_frame", None)
        if rs485_frame:
            if is_test:
                rs485_frame.pack_forget()
            else:
                kw = dict(side="left", padx=self._sp(6))
                inet_frame = getattr(self, "_internet_chip_frame", None)
                if inet_frame:
                    kw["before"] = inet_frame
                rs485_frame.pack(**kw)
                self.update_connection("rs485", False)   # reset ke merah sampai reconnect selesai
        if hasattr(self, "_mode_label_var"):
            port = self.cfg.get("serial_port", "—")
            self._mode_label_var.set(
                f"Mode: FLOAT  ·  Port: {port}  ·  {SYS_PLATFORM}  ·  ESC = keluar fullscreen"
                if is_test else
                f"Mode: LIVE  ·  Port: {port}  ·  {SYS_PLATFORM}  ·  ESC = keluar fullscreen"
            )

    # ── Gap fill helpers ──────────────────────────────────────────────────────
    def _on_gap_fill(self) -> None:
        """Tombol ISI GAP diklik."""
        self.app.trigger_gap_fill()

    def _refresh_gap_info(self) -> None:
        """Update label info durasi gap."""
        try:
            import gap_filler
            interval = self.cfg.get("interval_seconds", 120)
            info = gap_filler.gap_duration_str(interval)
            self._gap_info_var.set(f"Gap saat ini: {info}")
        except Exception:
            pass

    def gap_btn_busy(self) -> None:
        """Ubah tombol ke mode 'sedang berjalan' (dipanggil dari main thread)."""
        self._gap_btn.configure(state="disabled", bg="#9E9E9E")
        self._gap_btn_var.set("⏳  Mengisi gap...")

    def gap_btn_reset(self) -> None:
        """Kembalikan tombol ke keadaan normal dan refresh info gap."""
        self._gap_btn.configure(state="normal", bg="#E65100")
        self._gap_btn_var.set("⏱  Isi Gap Data Server 1")
        self._refresh_gap_info()

    # ── Mini chart ────────────────────────────────────────────────────────────
    def _draw_chart_on(self, cv, vals: list) -> None:
        """Gambar polyline pada canvas yang diberikan."""
        cv.delete("all")
        W, H = cv.winfo_width(), cv.winfo_height()
        if W < 4 or H < 4:
            return
        lo, hi = min(vals), max(vals)
        rng = hi - lo
        if rng < 1e-6:
            lo -= 1; hi += 1; rng = 2
        pad = 2
        step = (W - pad * 2) / max(len(vals) - 1, 1)
        pts = []
        for i, v in enumerate(vals):
            pts.extend([pad + i * step,
                        (H - pad) - (v - lo) / rng * (H - pad * 2)])
        cv.create_line(pts, fill="white", width=1, smooth=True)
        if len(pts) >= 2:
            lx, ly = pts[-2], pts[-1]
            cv.create_oval(lx - 2, ly - 2, lx + 2, ly + 2,
                           fill="white", outline="")

    def _draw_chart(self, key: str) -> None:
        data = self._chart_data.get(key)
        if not data or len(data) < 2:
            return
        vals = list(data)
        for cv in [self._chart_canvases.get(key),
                   self._locked_chart_canvases.get(key)]:
            if cv:
                try:
                    self._draw_chart_on(cv, vals)
                except Exception:
                    pass

    # ═══════════════════════════════════════════════════════════════════════════
    # PUBLIC UPDATE METHODS  (dipanggil dari thread via root.after)
    # ═══════════════════════════════════════════════════════════════════════════
    def update_sensors(self, r: SensorReading) -> None:
        self._sensor_vars["ph"].set(f"{r.ph:.2f}")
        self._sensor_vars["tss"].set(f"{r.tss:.2f}")
        self._sensor_vars["debit"].set(f"{r.debit:.2f}")
        self._sensor_vars["pm25"].set(f"{r.pm25:.1f}")
        self._sensor_vars["pm10"].set(f"{r.pm10:.1f}")
        self._sensor_vars["pm100"].set(f"{r.pm100:.1f}")
        self._sensor_vars["noise_leq"].set(f"{r.noise:.1f}")
        self._sensor_vars["temp"].set(f"{r.temp:.1f}")

    def update_sensors_processed(self, ph: float, tss: float,
                                  debit: float) -> None:
        """Perbarui nilai processed — hanya ditampilkan jika sudah di-unlock."""
        self._last_proc = (ph, tss, debit)   # cache untuk keperluan unlock
        if not self._unlocked:
            return   # tetap tampilkan mask sampai dibuka
        self._proc_vars["ph"].set(f"{ph:.2f}")
        self._proc_vars["tss"].set(f"{tss:.2f}")
        self._proc_vars["debit"].set(f"{debit:.2f}")

    def update_dust_processed(self, pm25: float, pm10: float,
                               pm100: float) -> None:
        """Perbarui nilai processed debu — hanya ditampilkan jika unlocked."""
        self._last_proc_dust = (pm25, pm10, pm100)
        if not self._unlocked:
            return
        self._proc_vars["pm25"].set(f"{pm25:.1f}")
        self._proc_vars["pm10"].set(f"{pm10:.1f}")
        self._proc_vars["pm100"].set(f"{pm100:.1f}")

    def update_noise_instant(self, value: float) -> None:
        """Perbarui nilai noise instan (per 1 menit) — selalu ditampilkan."""
        self._sensor_vars["noise_instant"].set(f"{value:.1f}")

    def update_noise_processed(self, noise: float) -> None:
        """Perbarui nilai processed Leq — hanya ditampilkan jika unlocked."""
        self._last_proc_noise = noise
        if not self._unlocked:
            return
        self._proc_vars["noise"].set(f"{noise:.1f}")

    def update_count(self, n: int, total: int = 30) -> None:
        self._count_var.set(f"{n} / {total}")
        self._progress.set(n)

    def update_last_tx(self, ts: float) -> None:
        self._last_tx_var.set(
            datetime.fromtimestamp(ts).strftime("%d/%m  %H:%M:%S"))

    def update_buffer(self, n: int) -> None:
        self._buf_var.set(str(n))

    def _set_send_status_color(self, color: str) -> None:
        self._send_status_lbl.configure(fg=color)
        lbl = getattr(self, "_locked_status_lbl", None)
        if lbl:
            try:
                lbl.configure(fg=color)
            except Exception:
                pass

    def update_send_status(self, ok1: bool, ok2: bool, ts: float) -> None:
        waktu = datetime.fromtimestamp(ts).strftime("%d/%m/%Y  %H:%M:%S")
        if ok1 and ok2:
            self._send_status_var.set("✓  Berhasil Terkirim")
            self._set_send_status_color(C["online"])
            self._send_detail_var.set(f"Server 1 & 2 OK  ·  {waktu}")
        elif ok1 or ok2:
            self._send_status_var.set("⚠  Sebagian Gagal")
            self._set_send_status_color(C["warning"])
            s1 = "OK" if ok1 else "GAGAL"
            s2 = "OK" if ok2 else "GAGAL"
            self._send_detail_var.set(f"S1:{s1}  S2:{s2}  ·  {waktu}")
        else:
            self._send_status_var.set("✗  Gagal Terkirim")
            self._set_send_status_color(C["offline"])
            self._send_detail_var.set(f"Disimpan buffer  ·  {waktu}")

    def update_send_offline(self, ts: float) -> None:
        waktu = datetime.fromtimestamp(ts).strftime("%d/%m/%Y  %H:%M:%S")
        self._send_status_var.set("⬇  Disimpan Offline")
        self._set_send_status_color(C["accent"])
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
            use_hat = self.cfg.get("use_rs485_hat", False)
            port = (self.cfg.get("rs485_hat_port", "—")
                    if use_hat else self.cfg.get("serial_port", "—"))
            self.root.after(0, self.update_connection, "rs485", ok)
            self.root.after(0, self._port_var.set, port)
            self.root.after(0, self.log,
                            f"RS485 {'terhubung' if ok else 'GAGAL'} — {port}")

        threading.Thread(target=_do, daemon=True).start()

    # ── Scan port dialog ──────────────────────────────────────────────────────
    def _scan_ports_dialog(self) -> None:
        win = self._make_dialog(self._sp(460), self._sp(360), "Scan Port USB RS485")
        win.configure(bg=C["bg"])

        tk.Frame(win, bg=C["primary"],
                 height=self._sp(4)).pack(fill="x")

        title_bar = tk.Frame(win, bg=C["panel"])
        title_bar.pack(fill="x")
        tk.Label(title_bar, text="PORT SERIAL TERSEDIA",
                 bg=C["panel"], fg=C["text"],
                 font=(_FONT_UI, self._fs(11), "bold"),
                 padx=self._sp(16), pady=self._sp(10)).pack(side="left")
        tk.Frame(win, bg=C["border"], height=1).pack(fill="x")

        body = tk.Frame(win, bg=C["bg"],
                        padx=self._sp(16), pady=self._sp(12))
        body.pack(fill="both", expand=True)

        tk.Label(body, text="Pilih port USB RS485 Anda:",
                 bg=C["bg"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(9), "bold")).pack(
            anchor="w", pady=(0, self._sp(6)))

        list_shadow = tk.Frame(body, bg=C["shadow"], padx=1, pady=1)
        list_shadow.pack(fill="both", expand=True)

        listbox = tk.Listbox(
            list_shadow,
            font=(_FONT_MONO, self._fs(11)),
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
                 font=(_FONT_UI, self._fs(8))).pack(
            anchor="w", pady=(self._sp(6), 0))

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
        btn_bar = tk.Frame(win, bg=C["panel"],
                           padx=self._sp(12), pady=self._sp(8))
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
        win = self._make_dialog(self._sp(600), self._sp(540), "Pengaturan")
        win.configure(bg=C["bg"])

        tk.Frame(win, bg=C["primary"],
                 height=self._sp(4)).pack(fill="x")

        title_bar = tk.Frame(win, bg=C["panel"])
        title_bar.pack(fill="x")
        tk.Label(title_bar, text="PENGATURAN KONEKSI & PERANGKAT",
                 bg=C["panel"], fg=C["text"],
                 font=(_FONT_UI, self._fs(12), "bold"),
                 padx=self._sp(16), pady=self._sp(10)).pack(side="left")
        tk.Frame(win, bg=C["border"], height=1).pack(fill="x")

        canvas  = tk.Canvas(win, bg=C["bg"], highlightthickness=0)
        sb      = ttk.Scrollbar(win, orient="vertical",
                                command=canvas.yview)
        canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        canvas.pack(fill="both", expand=True)

        form    = tk.Frame(canvas, bg=C["bg"],
                           padx=self._sp(20), pady=self._sp(12))
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
                    sticky="ew", pady=(self._sp(14), self._sp(6)))
                row_i[0] += 1
            tk.Label(form, text=title,
                     bg=C["bg"], fg=C["primary"],
                     font=(_FONT_UI, self._fs(10), "bold")).grid(
                row=row_i[0], column=0, columnspan=3,
                sticky="w", pady=(0, self._sp(6)))
            row_i[0] += 1

        def _entry(label: str, key: str, width: int = 32) -> None:
            tk.Label(form, text=label,
                     bg=C["bg"], fg=C["text"],
                     font=(_FONT_UI, self._fs(10)), anchor="w").grid(
                row=row_i[0], column=0, sticky="w", pady=self._sp(3))
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            entry_vars[key] = v
            e = tk.Entry(form, textvariable=v,
                         font=(_FONT_UI, self._fs(10)), width=width,
                         relief="flat", bd=0,
                         bg=C["card"], fg=C["text"],
                         insertbackground=C["primary"],
                         highlightthickness=1,
                         highlightbackground=C["border"],
                         highlightcolor=C["primary"])
            e.grid(row=row_i[0], column=1, columnspan=2,
                   sticky="ew", padx=(10, 0), pady=3)
            row_i[0] += 1

        # RS485 — USB adapter atau HAT
        _section("KONEKSI RS485")

        # Toggle USB / HAT
        hat_var = tk.BooleanVar(value=self.cfg.get("use_rs485_hat", False))
        hat_frame = tk.Frame(form, bg=C["bg"])
        hat_frame.grid(row=row_i[0], column=0, columnspan=3,
                       sticky="w", pady=(0, self._sp(6)))
        tk.Radiobutton(hat_frame, text="USB RS485 Adapter  (CH340/CP210x/FT232)",
                       variable=hat_var, value=False,
                       bg=C["bg"], fg=C["text"], selectcolor=C["card"],
                       activebackground=C["bg"],
                       font=(_FONT_UI, self._fs(9))).pack(anchor="w")
        tk.Radiobutton(hat_frame, text="RS485 HAT  (Waveshare/UART GPIO)",
                       variable=hat_var, value=True,
                       bg=C["bg"], fg=C["text"], selectcolor=C["card"],
                       activebackground=C["bg"],
                       font=(_FONT_UI, self._fs(9))).pack(anchor="w")
        entry_vars["use_rs485_hat"] = hat_var
        row_i[0] += 1

        # Port USB
        tk.Label(form, text="Port USB :",
                 bg=C["bg"], fg=C["text"],
                 font=(_FONT_UI, 10), anchor="w").grid(
            row=row_i[0], column=0, sticky="w", pady=4)
        port_var   = tk.StringVar(value=self.cfg.get("serial_port", ""))
        entry_vars["serial_port"] = port_var
        ports_list = scan_serial_ports() or [self.cfg.get("serial_port", "")]
        port_combo = ttk.Combobox(form, textvariable=port_var,
                                  values=ports_list, width=22,
                                  font=(_FONT_MONO, self._fs(10)))
        port_combo.grid(row=row_i[0], column=1, sticky="ew",
                        padx=(10, 4), pady=4)

        info_lbl = tk.Label(form, text="",
                            bg=C["bg"], fg=C["online"],
                            font=(_FONT_UI, self._fs(8)), anchor="w")

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
                     font=(_FONT_MONO, self._fs(10))).grid(
            row=row_i[0], column=1, sticky="w",
            padx=(10, 0), pady=4)
        row_i[0] += 1

        # Port HAT
        _entry("Port HAT :", "rs485_hat_port", 18)
        tk.Label(form,
                 text="Orange Pi 3B: /dev/ttyS1  atau  /dev/ttyS7\n"
                      "Orange Pi lain: /dev/ttyS3  /dev/ttyS0",
                 bg=C["bg"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(7))).grid(
            row=row_i[0], column=1, columnspan=2,
            sticky="w", padx=(10, 0), pady=(0, self._sp(4)))
        row_i[0] += 1

        # Slave IDs
        _section("ID SLAVE SENSOR  (MODBUS RTU)")
        for label, key in [
            ("Slave ID pH  :",    "slave_id_ph"),
            ("Slave ID TSS :",    "slave_id_tss"),
            ("Slave ID Debit :",  "slave_id_debit"),
            ("Slave ID Debu :",   "slave_id_dust"),
            ("Slave ID Noise :",  "slave_id_noise"),
            ("Slave ID Suhu :",   "slave_id_temp"),
        ]:
            _entry(label, key, 8)

        # Faktor PM (hanya tampil jika sensor debu aktif)
        if self.cfg.get("sensor_dust_enabled", True):
            _section("FAKTOR PM DARI TSP")
            tk.Label(form,
                     text="PM2.5 = random(min, max) × TSP\n"
                          "PM10  = random(min, max) × TSP",
                     bg=C["bg"], fg=C["text_muted"],
                     font=(_FONT_UI, self._fs(8))).grid(
                row=row_i[0], column=0, columnspan=3,
                sticky="w", pady=(0, 6))
            row_i[0] += 1

            # Header Min / Max
            for col, txt in enumerate(["", "Min", "Max"]):
                tk.Label(form, text=txt,
                         bg=C["bg"], fg=C["text_muted"],
                         font=(_FONT_UI, self._fs(9), "bold")).grid(
                    row=row_i[0], column=col, sticky="w",
                    padx=(0 if col == 0 else 10, 0), pady=2)
            row_i[0] += 1

            for label, key_min, key_max in [
                ("Faktor PM2.5 :", "pm25_factor_min", "pm25_factor_max"),
                ("Faktor PM10  :", "pm10_factor_min", "pm10_factor_max"),
            ]:
                tk.Label(form, text=label,
                         bg=C["bg"], fg=C["text"],
                         font=(_FONT_UI, self._fs(10))).grid(
                    row=row_i[0], column=0, sticky="w", pady=self._sp(4))
                for col, key in enumerate([key_min, key_max], start=1):
                    v = tk.StringVar(value=str(self.cfg.get(key, "")))
                    entry_vars[key] = v
                    tk.Entry(form, textvariable=v,
                             font=(_FONT_MONO, self._fs(10)), width=8,
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

        # Server & UID
        _section("SERVER & IDENTITAS")

        # Toggle Server 2
        s2_var = tk.BooleanVar(value=self.cfg.get("server2_enabled", True))
        entry_vars["server2_enabled"] = s2_var
        s2_row = tk.Frame(form, bg=C["bg"])
        s2_row.grid(row=row_i[0], column=0, columnspan=3,
                    sticky="w", pady=(0, self._sp(6)))
        tk.Checkbutton(
            s2_row, text="Aktifkan pengiriman ke Server 2  (KLH/BPLH)",
            variable=s2_var,
            bg=C["bg"], fg=C["text"],
            activebackground=C["bg"],
            selectcolor=C["primary"],
            font=(_FONT_UI, self._fs(10)),
        ).pack(side="left")
        row_i[0] += 1

        for label, key in [
            ("UID 1  (Internal) :", "uid1"),
            ("TL   (Internal) :",   "tl_water"),
            ("UID 1  (KLHK) :",     "uid1_klhk"),
            ("TL   (KLHK) :",       "tl_klhk"),
            ("Server URL 1 :",      "server_url1"),
            ("Secret Key URL 1 :",  "secret_key_url1"),
        ]:
            _entry(label, key)

        # Tipe Logger Server 1
        int_var  = tk.BooleanVar(value=self.cfg.get("logger_internal", True))
        klhk_var = tk.BooleanVar(value=self.cfg.get("logger_klhk",    False))
        entry_vars["logger_internal"] = int_var
        entry_vars["logger_klhk"]     = klhk_var
        lt_outer = tk.Frame(form, bg=C["bg"])
        lt_outer.grid(row=row_i[0], column=0, columnspan=3,
                      sticky="w", pady=(self._sp(4), self._sp(8)))
        tk.Label(lt_outer, text="Tipe Logger :",
                 bg=C["bg"], fg=C["text"],
                 font=(_FONT_UI, self._fs(10))).pack(anchor="w")
        cb_frame = tk.Frame(lt_outer, bg=C["bg"])
        cb_frame.pack(anchor="w", padx=(self._sp(16), 0))
        tk.Checkbutton(
            cb_frame,
            text="Internal  (data raw sensor)",
            variable=int_var,
            bg=C["bg"], fg=C["text"],
            activebackground=C["bg"],
            selectcolor=C["primary"],
            font=(_FONT_UI, self._fs(9)),
        ).pack(anchor="w")
        tk.Checkbutton(
            cb_frame,
            text="KLHK  (data processed / batas KLHK)",
            variable=klhk_var,
            bg=C["bg"], fg=C["text"],
            activebackground=C["bg"],
            selectcolor=C["primary"],
            font=(_FONT_UI, self._fs(9)),
        ).pack(anchor="w")
        row_i[0] += 1

        for label, key in [
            ("UID 2 :",             "uid2"),
            ("Server URL 2 :",     "server_url2"),
            ("Secret Key URL 2 :", "secret_key_url2"),
            ("Link Video ID :",    "link_video_id"),
        ]:
            _entry(label, key)

        # ── Batas clamp Server 2 ─────────────────────────────────────────────
        _section("BATAS DATA SERVER 2  (KLHK)")
        tk.Label(form,
                 text="Nilai terkirim ke Server 2 akan di-clamp dalam rentang Min–Max ini.",
                 bg=C["bg"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(8))).grid(
            row=row_i[0], column=0, columnspan=3,
            sticky="w", pady=(0, self._sp(6)))
        row_i[0] += 1

        def _entry(key: str, col: int) -> None:
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            entry_vars[key] = v
            tk.Entry(form, textvariable=v,
                     font=(_FONT_MONO, self._fs(10)), width=9,
                     relief="flat", bd=0,
                     bg=C["card"], fg=C["text"],
                     insertbackground=C["primary"],
                     highlightthickness=1,
                     highlightbackground=C["border"],
                     highlightcolor=C["primary"],
                     justify="center").grid(
                row=row_i[0], column=col, sticky="w",
                padx=(self._sp(8), 0), pady=self._sp(3))

        # Header batas clamp
        for col, txt in enumerate(["Parameter", "Min", "Max"]):
            tk.Label(form, text=txt,
                     bg=C["bg"], fg=C["text_muted"],
                     font=(_FONT_UI, self._fs(9), "bold")).grid(
                row=row_i[0], column=col, sticky="w",
                padx=(0 if col == 0 else self._sp(8), 0), pady=2)
        row_i[0] += 1
        tk.Frame(form, bg=C["border"], height=1).grid(
            row=row_i[0], column=0, columnspan=3,
            sticky="ew", pady=(0, self._sp(4)))
        row_i[0] += 1

        limit_fields = [
            ("sensor_ph_enabled",    "pH",    "limit_ph_min",    "limit_ph_max",    "limit_ph_float_lo_min",    "limit_ph_float_lo_max",    "limit_ph_float_hi_min",    "limit_ph_float_hi_max"),
            ("sensor_tss_enabled",   "TSS",   "limit_tss_min",   "limit_tss_max",   "limit_tss_float_lo_min",   "limit_tss_float_lo_max",   "limit_tss_float_hi_min",   "limit_tss_float_hi_max"),
            ("sensor_debit_enabled", "Debit", "limit_debit_min", "limit_debit_max", "limit_debit_float_lo_min", "limit_debit_float_lo_max", "limit_debit_float_hi_min", "limit_debit_float_hi_max"),
            ("sensor_dust_enabled",  "PM2.5", "limit_pm25_min",  "limit_pm25_max",  "limit_pm25_float_lo_min",  "limit_pm25_float_lo_max",  "limit_pm25_float_hi_min",  "limit_pm25_float_hi_max"),
            ("sensor_dust_enabled",  "PM10",  "limit_pm10_min",  "limit_pm10_max",  "limit_pm10_float_lo_min",  "limit_pm10_float_lo_max",  "limit_pm10_float_hi_min",  "limit_pm10_float_hi_max"),
            ("sensor_dust_enabled",  "PM100", "limit_pm100_min", "limit_pm100_max", "limit_pm100_float_lo_min", "limit_pm100_float_lo_max", "limit_pm100_float_hi_min", "limit_pm100_float_hi_max"),
            ("sensor_noise_enabled", "Noise", "limit_noise_min", "limit_noise_max", "limit_noise_float_lo_min", "limit_noise_float_lo_max", "limit_noise_float_hi_min", "limit_noise_float_hi_max"),
            ("sensor_temp_enabled",  "Suhu",  "limit_temp_min",  "limit_temp_max",  "limit_temp_float_lo_min",  "limit_temp_float_lo_max",  "limit_temp_float_hi_min",  "limit_temp_float_hi_max"),
        ]
        for cfg_key, param, k_min, k_max, *_ in limit_fields:
            if not self.cfg.get(cfg_key, True):
                continue
            tk.Label(form, text=param,
                     bg=C["bg"], fg=C["text"],
                     font=(_FONT_UI, self._fs(10))).grid(
                row=row_i[0], column=0, sticky="w", pady=self._sp(2))
            _entry(k_min, 1)
            _entry(k_max, 2)
            row_i[0] += 1

        # ── Ambang batas tampilan (Lo / Hi warna) ────────────────────────────
        _section("AMBANG BATAS TAMPILAN")
        tk.Label(form,
                 text="Tentukan zona warna nilai processed:\n"
                      "  Lo: batas bawah normal  ·  Hi: batas atas normal",
                 bg=C["bg"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(8)),
                 justify="left").grid(
            row=row_i[0], column=0, columnspan=5,
            sticky="w", pady=(0, self._sp(6)))
        row_i[0] += 1

        # Header ambang — 5 kolom
        for col, txt in enumerate(["Parameter", "Lo Min", "Lo Max", "Hi Min", "Hi Max"]):
            tk.Label(form, text=txt,
                     bg=C["bg"], fg=C["text_muted"],
                     font=(_FONT_UI, self._fs(9), "bold")).grid(
                row=row_i[0], column=col, sticky="w",
                padx=(0 if col == 0 else self._sp(6), 0), pady=2)
        row_i[0] += 1
        tk.Frame(form, bg=C["border"], height=1).grid(
            row=row_i[0], column=0, columnspan=5,
            sticky="ew", pady=(0, self._sp(4)))
        row_i[0] += 1

        def _entry5(key: str, col: int) -> None:
            v = tk.StringVar(value=str(self.cfg.get(key, "")))
            entry_vars[key] = v
            tk.Entry(form, textvariable=v,
                     font=(_FONT_MONO, self._fs(10)), width=7,
                     relief="flat", bd=0,
                     bg=C["card"], fg=C["text"],
                     insertbackground=C["primary"],
                     highlightthickness=1,
                     highlightbackground=C["border"],
                     highlightcolor=C["primary"],
                     justify="center").grid(
                row=row_i[0], column=col, sticky="w",
                padx=(self._sp(6), 0), pady=self._sp(3))

        for cfg_key, param, k_min, k_max, k_flo_min, k_flo_max, k_fhi_min, k_fhi_max in limit_fields:
            if not self.cfg.get(cfg_key, True):
                continue
            tk.Label(form, text=param,
                     bg=C["bg"], fg=C["text"],
                     font=(_FONT_UI, self._fs(10))).grid(
                row=row_i[0], column=0, sticky="w", pady=self._sp(2))
            _entry5(k_flo_min, 1)
            _entry5(k_flo_max, 2)
            _entry5(k_fhi_min, 3)
            _entry5(k_fhi_max, 4)
            row_i[0] += 1

        form.columnconfigure(1, weight=1)
        form.columnconfigure(2, weight=1)
        form.columnconfigure(3, weight=1)
        form.columnconfigure(4, weight=1)

        # Batas nilai floating mode
        _section("BATAS NILAI FLOATING MODE")
        tk.Label(form,
                 text="Nilai acak yang digunakan saat floating mode aktif (simulate_sensors).",
                 bg=C["bg"], fg=C["text_muted"],
                 font=(_FONT_UI, 8)).grid(
            row=row_i[0], column=0, columnspan=3,
            sticky="w", pady=(0, 6))
        row_i[0] += 1

        for col, txt in enumerate(["Parameter", "Min", "Max"]):
            tk.Label(form, text=txt,
                     bg=C["bg"], fg=C["text_muted"],
                     font=(_FONT_UI, self._fs(9), "bold")).grid(
                row=row_i[0], column=col, sticky="w",
                padx=(0 if col == 0 else 10, 0), pady=2)
        row_i[0] += 1

        for cfg_key, param, key_min, key_max in [
            ("sensor_ph_enabled",    "pH",    "sim_ph_min",    "sim_ph_max"),
            ("sensor_tss_enabled",   "TSS",   "sim_tss_min",   "sim_tss_max"),
            ("sensor_debit_enabled", "Debit", "sim_debit_min", "sim_debit_max"),
            ("sensor_dust_enabled",  "TSP",   "sim_tsp_min",   "sim_tsp_max"),
            ("sensor_noise_enabled", "Noise", "sim_noise_min", "sim_noise_max"),
            ("sensor_temp_enabled",  "Suhu",  "sim_temp_min",  "sim_temp_max"),
        ]:
            if not self.cfg.get(cfg_key, True):
                continue
            tk.Label(form, text=param,
                     bg=C["bg"], fg=C["text"],
                     font=(_FONT_UI, self._fs(10))).grid(
                row=row_i[0], column=0, sticky="w", pady=self._sp(4))
            for col, key in enumerate([key_min, key_max], start=1):
                v = tk.StringVar(value=str(self.cfg.get(key, "")))
                entry_vars[key] = v
                tk.Entry(form, textvariable=v,
                         font=(_FONT_MONO, self._fs(10)), width=8,
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

        # Save handler
        def _save():
            int_keys   = {"baud_rate", "slave_id_ph",
                          "slave_id_tss", "slave_id_debit",
                          "slave_id_dust", "slave_id_noise", "slave_id_temp",
                          "tl_water", "tl_klhk"}
            float_keys = {
                "pm25_factor_min", "pm25_factor_max",
                "pm10_factor_min", "pm10_factor_max",
                "limit_ph_min",    "limit_ph_max",
                "limit_ph_float_lo_min",    "limit_ph_float_lo_max",    "limit_ph_float_hi_min",    "limit_ph_float_hi_max",
                "limit_tss_min",   "limit_tss_max",
                "limit_tss_float_lo_min",   "limit_tss_float_lo_max",   "limit_tss_float_hi_min",   "limit_tss_float_hi_max",
                "limit_debit_min", "limit_debit_max",
                "limit_debit_float_lo_min", "limit_debit_float_lo_max", "limit_debit_float_hi_min", "limit_debit_float_hi_max",
                "limit_pm25_min",  "limit_pm25_max",
                "limit_pm25_float_lo_min",  "limit_pm25_float_lo_max",  "limit_pm25_float_hi_min",  "limit_pm25_float_hi_max",
                "limit_pm10_min",  "limit_pm10_max",
                "limit_pm10_float_lo_min",  "limit_pm10_float_lo_max",  "limit_pm10_float_hi_min",  "limit_pm10_float_hi_max",
                "limit_pm100_min", "limit_pm100_max",
                "limit_pm100_float_lo_min", "limit_pm100_float_lo_max", "limit_pm100_float_hi_min", "limit_pm100_float_hi_max",
                "limit_noise_min", "limit_noise_max",
                "limit_noise_float_lo_min", "limit_noise_float_lo_max", "limit_noise_float_hi_min", "limit_noise_float_hi_max",
                "limit_temp_min",  "limit_temp_max",
                "limit_temp_float_lo_min",  "limit_temp_float_lo_max",  "limit_temp_float_hi_min",  "limit_temp_float_hi_max",
                "sim_ph_min",    "sim_ph_max",
                "sim_tss_min",   "sim_tss_max",
                "sim_debit_min", "sim_debit_max",
                "sim_tsp_min",   "sim_tsp_max",
                "sim_noise_min", "sim_noise_max",
                "sim_temp_min",  "sim_temp_max",
            }
            for key, v in entry_vars.items():
                # BooleanVar (use_rs485_hat) — simpan langsung
                if isinstance(v, tk.BooleanVar):
                    self.cfg[key] = v.get()
                    continue
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
            # Refresh port label sesuai mode aktif
            use_hat = self.cfg.get("use_rs485_hat", False)
            port = (self.cfg.get("rs485_hat_port", "—") if use_hat
                    else self.cfg.get("serial_port", "—"))
            self._port_var.set(port)
            self.update_limits()
            self.apply_limits_visibility()
            self._update_app_title()
            # Hitung ulang nilai processed segera dengan batas baru
            if self.app.batch:
                last_r = self.app.batch[-1]
                p = self.app.net.get_processed(last_r)
                self.update_sensors_processed(p[0], p[1], p[2])
                self.update_dust_processed(p[3], p[4], p[5])
                self.update_noise_processed(p[6])
            # Update label footer dengan port baru
            if hasattr(self, "_mode_label_var"):
                is_test = self.cfg.get("simulate_sensors", False)
                self._mode_label_var.set(
                    f"Mode: {'FLOAT' if is_test else 'LIVE'}  ·  Port: {port}  ·  "
                    f"{SYS_PLATFORM}  ·  ESC = keluar fullscreen"
                )
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
                       pady=9).pack(side="left", padx=(0, self._sp(8)))
        self._flat_btn(btn_fr, "✕  Batal",
                       win.destroy, C["bg"], C["text_muted"],
                       pady=9).pack(side="left")
