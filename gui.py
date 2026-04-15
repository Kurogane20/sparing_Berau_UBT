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
        self._build_sensor_row()
        self._build_dust_row()
        self._build_noise_row()
        self._build_temp_row()
        self._build_footer()   # harus sebelum body (body pakai expand=True)
        self._build_body()

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

        tk.Label(title_col,
                 text="SISTEM PEMANTAUAN KUALITAS AIR",
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
        for key, label in [
            ("rs485",    "RS485"),
            ("internet", "Internet"),
            ("server1",  "Server 1"),
            ("server2",  "Server 2"),
        ]:
            # Sembunyikan indikator RS485 saat mode simulasi
            if key == "rs485" and _simulate:
                # Tetap daftarkan agar update_connection tidak error
                dummy_var = tk.StringVar(value="")
                dummy_lbl = tk.Label(conn_row, bg=C["panel"])
                dummy_dot = tk.Canvas(conn_row, width=0, height=0,
                                      bg=C["panel"], highlightthickness=0)
                dummy_dot.create_oval(0, 0, 0, 0, tags="dot")
                self._conn_dots[key]   = dummy_dot
                self._conn_chips[key]  = (dummy_var, dummy_lbl)
                self._conn_labels[key] = (dummy_var, dummy_lbl)
                continue

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

            self._conn_dots[key]  = dot
            self._conn_chips[key] = (var, status_lbl)
            self._conn_labels[key] = (var, status_lbl)   # alias

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
        self._main_sensor_row = tk.Frame(self.root, bg=C["bg"])
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

        py_top  = self._sp(4 if self._small else 6)
        py_bot  = self._sp(4 if self._small else 6)
        f_label = self._fs(8 if self._small else 9)
        f_raw   = self._fs(20 if self._small else 26)
        f_unit  = self._fs(7 if self._small else 8)
        f_proc  = self._fs(11 if self._small else 14)
        px_sep  = self._sp(14 if self._small else 20)

        tk.Label(inner, text=label,
                 bg=bg, fg=label_color,
                 font=(_FONT_UI, f_label, "bold")).pack(
            pady=(py_top, 0))

        raw_var = tk.StringVar(value="—")
        self._sensor_vars[key] = raw_var
        tk.Label(inner, textvariable=raw_var,
                 bg=bg, fg="white",
                 font=(_FONT_MONO, f_raw, "bold")).pack(
            pady=(self._sp(1), 0))

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
        wrap = tk.Frame(self.root, bg=C["bg"])
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
            ("pm100", "PM 100",  "ug/m³", "#37474F", "#FFCC80"),
        ]
        for col, (key, label, unit, bg, lc) in enumerate(defs):
            card = self._dust_card(row, key, label, unit, bg, lc)
            card.grid(row=0, column=col, padx=self._sp(6), sticky="nsew")
            row.columnconfigure(col, weight=1)
        self._sensor_cards["sensor_dust_enabled"] = (wrap,)

    def _build_noise_row(self) -> None:
        """Baris kartu sensor kebisingan — nilai instan (1 menit) dan Leq (10 menit)."""
        wrap = tk.Frame(self.root, bg=C["bg"])
        wrap.pack(fill="x", padx=self._sp(14), pady=(self._sp(3), 0))
        self._noise_row_frame = wrap
        if not self.cfg.get("sensor_noise_enabled", True):
            wrap.pack_forget()

        tk.Label(wrap, text="KEBISINGAN  (Sound Level Meter)",
                 bg=C["bg"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(7), "bold")).pack(
            anchor="w", pady=(0, self._sp(2)))

        bg, lc = "#4A148C", "#CE93D8"
        canvas, inner = self._rounded_canvas(wrap, bg, radius=self._sp(14))
        canvas.pack(fill="x", padx=self._sp(6))
        self._sensor_cards["sensor_noise_enabled"] = (wrap,)

        py    = self._sp(3 if self._small else 5)
        f_val = self._fs(14 if self._small else 18)

        # ── Baris atas: Instan | Leq ──────────────────────────────────────────
        top_row = tk.Frame(inner, bg=bg)
        top_row.pack(fill="x")

        # Kolom kiri — nilai instan (per 1 menit)
        left = tk.Frame(top_row, bg=bg)
        left.pack(side="left", expand=True, fill="both", padx=(self._sp(8), 0))

        tk.Label(left, text="INSTAN",
                 bg=bg, fg=lc,
                 font=(_FONT_UI, self._fs(7), "bold")).pack(pady=(py, 0))

        instant_var = tk.StringVar(value="—")
        self._sensor_vars["noise_instant"] = instant_var
        tk.Label(left, textvariable=instant_var,
                 bg=bg, fg="white",
                 font=(_FONT_MONO, f_val, "bold")).pack(pady=(self._sp(1), 0))

        tk.Label(left, text="dB",
                 bg=bg, fg=lc,
                 font=(_FONT_UI, self._fs(6))).pack(pady=(0, py))

        # Pemisah vertikal
        tk.Frame(top_row, bg=lc, width=1).pack(
            side="left", fill="y", pady=self._sp(8))

        # Kolom kanan — Leq 10 menit
        right = tk.Frame(top_row, bg=bg)
        right.pack(side="left", expand=True, fill="both", padx=(0, self._sp(8)))

        tk.Label(right, text="Leq  (10 min)",
                 bg=bg, fg=lc,
                 font=(_FONT_UI, self._fs(7), "bold")).pack(pady=(py, 0))

        leq_var = tk.StringVar(value="—")
        self._sensor_vars["noise_leq"] = leq_var
        tk.Label(right, textvariable=leq_var,
                 bg=bg, fg="white",
                 font=(_FONT_MONO, f_val, "bold")).pack(pady=(self._sp(1), 0))

        tk.Label(right, text="dB",
                 bg=bg, fg=lc,
                 font=(_FONT_UI, self._fs(6))).pack(pady=(0, py))

        # ── Baris bawah: Processed Leq (tampil saat unlocked) ────────────────
        tk.Frame(inner, bg=lc, height=1).pack(
            fill="x", padx=self._sp(12),
            pady=(self._sp(2), self._sp(1)))

        proc_row = tk.Frame(inner, bg=bg)
        proc_row.pack(pady=(0, py))

        tk.Label(proc_row, text="PROCESSED  Leq",
                 bg=bg, fg=lc,
                 font=(_FONT_UI, self._fs(6), "bold")).pack()

        proc_var = tk.StringVar(value="●  ●  ●")
        self._proc_vars["noise"] = proc_var
        tk.Label(proc_row, textvariable=proc_var,
                 bg=bg, fg=lc,
                 font=(_FONT_MONO, self._fs(9 if self._small else 11), "bold")).pack()

    def _build_temp_row(self) -> None:
        """Baris kartu sensor suhu air."""
        wrap = tk.Frame(self.root, bg=C["bg"])
        wrap.pack(fill="x", padx=self._sp(14), pady=(self._sp(3), 0))
        self._temp_row_frame = wrap
        if not self.cfg.get("sensor_temp_enabled", True):
            wrap.pack_forget()

        tk.Label(wrap, text="SUHU AIR",
                 bg=C["bg"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(7), "bold")).pack(
            anchor="w", pady=(0, self._sp(2)))

        bg, lc = "#BF360C", "#FFAB91"
        canvas, inner = self._rounded_canvas(wrap, bg, radius=self._sp(14))
        canvas.pack(fill="x", padx=self._sp(6))
        self._sensor_cards["sensor_temp_enabled"] = (wrap,)

        py    = self._sp(3 if self._small else 5)
        f_val = self._fs(14 if self._small else 18)

        tk.Label(inner, text="SUHU",
                 bg=bg, fg=lc,
                 font=(_FONT_UI, self._fs(7), "bold")).pack(pady=(py, 0))

        temp_var = tk.StringVar(value="—")
        self._sensor_vars["temp"] = temp_var
        tk.Label(inner, textvariable=temp_var,
                 bg=bg, fg="white",
                 font=(_FONT_MONO, f_val, "bold")).pack(pady=(self._sp(1), 0))

        tk.Label(inner, text="°C",
                 bg=bg, fg=lc,
                 font=(_FONT_UI, self._fs(6))).pack(pady=(0, py))

    def _dust_card(self, parent, key: str, label: str,
                   unit: str, bg: str, label_color: str) -> tk.Canvas:
        """Kartu kompak untuk sensor debu (PM) — lebih kecil dari sensor utama."""
        canvas, inner = self._rounded_canvas(parent, bg, radius=self._sp(14))

        py_top = self._sp(3 if self._small else 5)
        py_bot = self._sp(3 if self._small else 5)
        f_val  = self._fs(14 if self._small else 18)

        tk.Label(inner, text=label,
                 bg=bg, fg=label_color,
                 font=(_FONT_UI, self._fs(7), "bold")).pack(
            pady=(py_top, 0))

        raw_var = tk.StringVar(value="—")
        self._sensor_vars[key] = raw_var
        tk.Label(inner, textvariable=raw_var,
                 bg=bg, fg="white",
                 font=(_FONT_MONO, f_val, "bold")).pack(
            pady=(self._sp(1), 0))

        tk.Label(inner, text=unit,
                 bg=bg, fg=label_color,
                 font=(_FONT_UI, self._fs(6))).pack()

        tk.Frame(inner, bg=label_color, height=1).pack(
            fill="x", padx=self._sp(12),
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
        body = tk.Frame(self.root, bg=C["bg"])
        body.pack(fill="both", expand=True,
                  padx=self._sp(14), pady=self._sp(6))
        self._body_frame = body

        self._build_log_panel(body)
        self._build_right_panel(body)

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
            fill="x", pady=(10, 2))

        # ── Status pengiriman ─────────────────────────────────────────────────
        inner2 = self._card(right, "STATUS PENGIRIMAN", C["online"],
                            fill="x", pady=(0, 8))

        self._send_status_var = tk.StringVar(value="— Menunggu batch pertama")
        self._send_status_lbl = tk.Label(
            inner2, textvariable=self._send_status_var,
            bg=C["card"], fg=C["text_muted"],
            font=(_FONT_UI, self._fs(11), "bold"),
            wraplength=self._r_width - self._sp(28), justify="left",
        )
        self._send_status_lbl.pack(anchor="w", pady=(self._sp(2), 0))

        self._send_detail_var = tk.StringVar(value="")
        tk.Label(inner2, textvariable=self._send_detail_var,
                 bg=C["card"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(8)),
                 wraplength=self._r_width - self._sp(28),
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
        for col, (txt, w) in enumerate([("", 7), ("MIN", 7), ("MAX", 7), ("±", 6)]):
            tk.Label(hdr, text=txt, bg=C["card"], fg=C["text_muted"],
                     font=(_FONT_UI, 8, "bold"),
                     width=w, anchor="w").grid(
                row=0, column=col, sticky="w")

        tk.Frame(inner3, bg=C["border"], height=1).pack(
            fill="x", pady=(4, 2))

        self._limit_rows: dict = {}   # cfg_key → row frame

        for cfg_key, param, k_min, k_max, k_float in [
            ("sensor_ph_enabled",    "pH",    "limit_ph_min",    "limit_ph_max",    "limit_ph_float"),
            ("sensor_tss_enabled",   "TSS",   "limit_tss_min",   "limit_tss_max",   "limit_tss_float"),
            ("sensor_debit_enabled", "Debit", "limit_debit_min", "limit_debit_max", "limit_debit_float"),
            ("sensor_dust_enabled",  "PM2.5", "limit_pm25_min",  "limit_pm25_max",  "limit_pm25_float"),
            ("sensor_dust_enabled",  "PM10",  "limit_pm10_min",  "limit_pm10_max",  "limit_pm10_float"),
            ("sensor_dust_enabled",  "PM100", "limit_pm100_min", "limit_pm100_max", "limit_pm100_float"),
            ("sensor_noise_enabled", "Noise", "limit_noise_min", "limit_noise_max", "limit_noise_float"),
            ("sensor_temp_enabled",  "Suhu",  "limit_temp_min",  "limit_temp_max",  "limit_temp_float"),
        ]:
            lim_row = tk.Frame(inner3, bg=C["card"])
            tk.Label(lim_row, text=param,
                     bg=C["card"], fg=C["text_muted"],
                     font=(_FONT_UI, 9),
                     width=7, anchor="w").pack(side="left")
            for key in (k_min, k_max, k_float):
                v = tk.StringVar(value=str(self.cfg.get(key, "—")))
                self._limit_vars[key] = v
                lbl = tk.Label(lim_row, textvariable=v,
                               bg=C["card_alt"], fg=C["primary"],
                               font=(_FONT_MONO, 9, "bold"),
                               width=6, relief="flat", padx=3)
                lbl.pack(side="left", padx=(4, 0))
            # Simpan per cfg_key — dust punya 3 baris, simpan sebagai list
            self._limit_rows.setdefault(cfg_key, []).append(lim_row)

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

        mode = "SIMULASI" if self.cfg.get("simulate_sensors") else "LIVE"
        port = self.cfg.get("serial_port", "—")
        tk.Label(bar,
                 text=f"Mode: {mode}  ·  Port: {port}  ·  {SYS_PLATFORM}  ·  ESC = keluar fullscreen",
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
        row.pack(fill="x", pady=self._sp(3))
        tk.Label(row, text=label,
                 bg=C["card"], fg=C["text_muted"],
                 font=(_FONT_UI, self._fs(8), "bold"),
                 anchor="w", width=15).pack(side="left")
        tk.Label(row, textvariable=var,
                 bg=C["card"], fg=fg,
                 font=(_FONT_MONO, self._fs(10), "bold")).pack(side="left")
        if suffix:
            tk.Label(row, text=suffix,
                     bg=C["card"], fg=C["text_muted"],
                     font=(_FONT_UI, self._fs(9))).pack(side="left")

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
        # ── Sensor utama (grid) ───────────────────────────────────────────────
        row_frame = self._main_sensor_row
        ordered = [
            ("sensor_ph_enabled",    "ph"),
            ("sensor_tss_enabled",   "tss"),
            ("sensor_debit_enabled", "debit"),
        ]

        # Sembunyikan semua dulu dan reset bobot kolom
        for i in range(3):
            row_frame.columnconfigure(i, weight=0, minsize=0)
        for cfg_key, _ in ordered:
            card, *_ = self._sensor_cards[cfg_key]
            card.grid_remove()

        # Re-grid kartu aktif ke kolom berurutan (tanpa gap)
        active_col = 0
        for cfg_key, _ in ordered:
            if self.cfg.get(cfg_key, True):
                card, *_ = self._sensor_cards[cfg_key]
                card.grid(row=0, column=active_col,
                          padx=self._sp(6), sticky="nsew")
                row_frame.columnconfigure(active_col, weight=1)
                active_col += 1

        # Sembunyikan/tampilkan baris utama jika tidak ada sensor aktif
        # pack_forget dulu agar re-pack dengan before= selalu bekerja
        any_main = active_col > 0
        self._main_sensor_row.pack_forget()
        if any_main:
            kw = dict(fill="x", padx=self._sp(14),
                      pady=(self._sp(4 if self._small else 6), 0))
            if self._dust_row_frame and self.cfg.get("sensor_dust_enabled", True):
                kw["before"] = self._dust_row_frame
            elif self._noise_row_frame and self.cfg.get("sensor_noise_enabled", True):
                kw["before"] = self._noise_row_frame
            elif self._temp_row_frame and self.cfg.get("sensor_temp_enabled", True):
                kw["before"] = self._temp_row_frame
            elif self._body_frame:
                kw["before"] = self._body_frame
            self._main_sensor_row.pack(**kw)

        # ── Dust row (pack) ───────────────────────────────────────────────────
        if self._dust_row_frame:
            self._dust_row_frame.pack_forget()
            if self.cfg.get("sensor_dust_enabled", True):
                kw_dust = dict(fill="x",
                               padx=self._sp(14),
                               pady=(self._sp(3 if self._small else 4), 0))
                if self._noise_row_frame and self.cfg.get("sensor_noise_enabled", True):
                    kw_dust["before"] = self._noise_row_frame
                elif self._temp_row_frame and self.cfg.get("sensor_temp_enabled", True):
                    kw_dust["before"] = self._temp_row_frame
                else:
                    kw_dust["before"] = self._body_frame
                self._dust_row_frame.pack(**kw_dust)

        # ── Noise row (pack) ──────────────────────────────────────────────────
        if self._noise_row_frame:
            self._noise_row_frame.pack_forget()
            if self.cfg.get("sensor_noise_enabled", True):
                kw_noise = dict(fill="x",
                                padx=self._sp(14),
                                pady=(self._sp(3 if self._small else 4), 0))
                if self._temp_row_frame and self.cfg.get("sensor_temp_enabled", True):
                    kw_noise["before"] = self._temp_row_frame
                else:
                    kw_noise["before"] = self._body_frame
                self._noise_row_frame.pack(**kw_noise)

        # ── Temp row (pack) ───────────────────────────────────────────────────
        if self._temp_row_frame:
            self._temp_row_frame.pack_forget()
            if self.cfg.get("sensor_temp_enabled", True):
                self._temp_row_frame.pack(fill="x",
                                          padx=self._sp(14),
                                          pady=(self._sp(3 if self._small else 4), 0),
                                          before=self._body_frame)

        # ── Batas — ikuti sensor yang aktif ──────────────────────────────────
        self.apply_limits_visibility()

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
        w, h = self._sp(380), self._sp(340)
        win = self._make_dialog(w, h, "Pilihan Sensor")
        win.configure(bg=C["panel"])

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
        for cfg_key, label, bg, lc in sensors:
            var = tk.BooleanVar(value=self.cfg.get(cfg_key, True))
            check_vars[cfg_key] = var

            row = tk.Frame(win, bg=C["panel"],
                           pady=self._sp(6), padx=self._sp(16))
            row.pack(fill="x")

            # Indikator warna
            tk.Frame(row, bg=bg,
                     width=self._sp(10), height=self._sp(10)).pack(
                side="left", padx=(0, self._sp(10)))

            tk.Label(row, text=label,
                     bg=C["panel"], fg=C["text"],
                     font=(_FONT_UI, self._fs(10))).pack(side="left", expand=True,
                                                          anchor="w")

            # Toggle switch (Checkbutton styled)
            cb = tk.Checkbutton(
                row, variable=var,
                bg=C["panel"],
                activebackground=C["panel"],
                selectcolor=C["primary"],
                fg=C["text_muted"],
                font=(_FONT_UI, self._fs(9)),
                relief="flat", cursor="hand2",
            )
            cb.pack(side="right")

            tk.Frame(win, bg=C["border"], height=1).pack(
                fill="x", padx=self._sp(16))

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

        tk.Frame(win, bg=C["border"], height=1).pack(fill="x", pady=(self._sp(8), 0))
        btn_bar = tk.Frame(win, bg=C["panel"],
                           padx=self._sp(16), pady=self._sp(10))
        btn_bar.pack(fill="x")
        self._flat_btn(btn_bar, "✓  Terapkan",
                       _apply, C["primary"], "white",
                       pady=7).pack(side="left", padx=(0, self._sp(8)),
                                    ipadx=self._sp(10))
        self._flat_btn(btn_bar, "✕  Batal",
                       win.destroy, C["bg"], C["text_muted"],
                       pady=7).pack(side="left", ipadx=self._sp(10))

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

        self.log("🔓 Tampilan data processed & batas diaktifkan")

    def _lock(self) -> None:
        """Sembunyikan processed values dan limits card."""
        self._unlocked = False
        if self._lock_btn_var:
            self._lock_btn_var.set("🔒")

        # Mask kembali nilai processed
        for key, var in self._proc_vars.items():
            var.set("●  ●  ●")

        # Sembunyikan kontrol RS485 & pengaturan
        if hasattr(self, "_ctrl_wrapper"):
            self._ctrl_wrapper.pack_forget()

        # Sembunyikan log aktivitas — panel kanan melebar isi seluruh area
        if hasattr(self, "_log_canvas"):
            self._log_canvas.pack_forget()
            if hasattr(self, "_right_outer"):
                self._right_outer.pack_forget()
                self._right_outer.pack(fill="both", expand=True)

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
        for label, key in [
            ("UID 1 :",             "uid1"),
            ("UID 2 :",             "uid2"),
            ("Server URL 1 :",      "server_url1"),
            ("Secret Key URL 1 :",  "secret_key_url1"),
            ("Server URL 2 :",     "server_url2"),
            ("Secret Key URL 2 :", "secret_key_url2"),
            ("Link Video ID :",    "link_video_id"),
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

        for col, txt in enumerate(["Parameter", "Min", "Max", "± Variasi"]):
            tk.Label(form, text=txt,
                     bg=C["bg"], fg=C["text_muted"],
                     font=(_FONT_UI, self._fs(9), "bold")).grid(
                row=row_i[0], column=col, sticky="w",
                padx=(0 if col == 0 else 10, 0), pady=2)
        row_i[0] += 1
        tk.Frame(form, bg=C["border"], height=1).grid(
            row=row_i[0], column=0, columnspan=4,
            sticky="ew", pady=(0, 4))
        row_i[0] += 1

        limit_fields = [
            ("sensor_ph_enabled",    "pH",    "limit_ph_min",    "limit_ph_max",    "limit_ph_float"),
            ("sensor_tss_enabled",   "TSS",   "limit_tss_min",   "limit_tss_max",   "limit_tss_float"),
            ("sensor_debit_enabled", "Debit", "limit_debit_min", "limit_debit_max", "limit_debit_float"),
            ("sensor_dust_enabled",  "PM2.5", "limit_pm25_min",  "limit_pm25_max",  "limit_pm25_float"),
            ("sensor_dust_enabled",  "PM10",  "limit_pm10_min",  "limit_pm10_max",  "limit_pm10_float"),
            ("sensor_dust_enabled",  "PM100", "limit_pm100_min", "limit_pm100_max", "limit_pm100_float"),
            ("sensor_noise_enabled", "Noise", "limit_noise_min", "limit_noise_max", "limit_noise_float"),
            ("sensor_temp_enabled",  "Suhu",  "limit_temp_min",  "limit_temp_max",  "limit_temp_float"),
        ]
        for cfg_key, param, key_min, key_max, key_float in limit_fields:
            if not self.cfg.get(cfg_key, True):
                continue   # sembunyikan jika sensor tidak aktif
            tk.Label(form, text=param,
                     bg=C["bg"], fg=C["text"],
                     font=(_FONT_UI, self._fs(10))).grid(
                row=row_i[0], column=0, sticky="w",
                pady=self._sp(4))
            for col, key in enumerate([key_min, key_max, key_float], start=1):
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

        form.columnconfigure(1, weight=1)

        # Save handler
        def _save():
            int_keys   = {"baud_rate", "slave_id_ph",
                          "slave_id_tss", "slave_id_debit",
                          "slave_id_dust", "slave_id_noise", "slave_id_temp"}
            float_keys = {
                "pm25_factor_min", "pm25_factor_max",
                "pm10_factor_min", "pm10_factor_max",
                "limit_ph_min",    "limit_ph_max",    "limit_ph_float",
                "limit_tss_min",   "limit_tss_max",   "limit_tss_float",
                "limit_debit_min", "limit_debit_max", "limit_debit_float",
                "limit_pm25_min",  "limit_pm25_max",  "limit_pm25_float",
                "limit_pm10_min",  "limit_pm10_max",  "limit_pm10_float",
                "limit_pm100_min", "limit_pm100_max", "limit_pm100_float",
                "limit_noise_min", "limit_noise_max", "limit_noise_float",
                "limit_temp_min",  "limit_temp_max",  "limit_temp_float",
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
            if self.cfg.get("use_rs485_hat", False):
                self._port_var.set(self.cfg.get("rs485_hat_port", "—"))
            else:
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
                       pady=9).pack(side="left", padx=(0, self._sp(8)))
        self._flat_btn(btn_fr, "✕  Batal",
                       win.destroy, C["bg"], C["text_muted"],
                       pady=9).pack(side="left")
