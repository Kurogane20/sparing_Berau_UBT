"""
gap_filler.py — Pengisian gap data Server 1 saat terjadi gangguan (mati listrik dll).

Cara kerja:
  1. save_state(r)  → simpan nilai sensor terakhir + timestamp setiap siklus baca
  2. detect_and_fill(interval) → saat startup, deteksi gap dan hasilkan data
     random ≈ nilai terakhir (±5%) untuk setiap slot 2-menit yang terlewat
  3. Tidak ada batas maksimum — semua gap dari last_ts hingga sekarang diisi
"""

import json
import logging
import random
import time
from pathlib import Path
from typing import List, Optional

from models import SensorReading

log = logging.getLogger(__name__)

_STATE_FILE = Path("gap_state.json")
_VARIATION  = 0.05   # ±5% variasi dari nilai terakhir


def save_state(r: SensorReading) -> None:
    """Simpan pembacaan terakhir ke file setelah setiap siklus sensor."""
    try:
        with open(_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "last_ts": r.timestamp,
                "ph":      r.ph,
                "tss":     r.tss,
                "debit":   r.debit,
                "pm25":    r.pm25,
                "pm10":    r.pm10,
                "pm100":   r.pm100,
                "noise":   r.noise,
                "temp":    r.temp,
            }, f)
    except Exception as e:
        log.error(f"gap_filler save_state: {e}")


def _load_state() -> Optional[dict]:
    try:
        if _STATE_FILE.exists():
            with open(_STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log.error(f"gap_filler load_state: {e}")
    return None


def _vary(value: float) -> float:
    """Variasikan nilai ±5% secara acak."""
    if value <= 0:
        return max(0.0, value)
    delta = value * _VARIATION
    return round(random.uniform(value - delta, value + delta), 4)


def detect_and_fill(interval: int = 120) -> List[SensorReading]:
    """
    Deteksi gap sejak pembacaan terakhir sebelum mati.
    Hasilkan SensorReading dengan nilai random ≈ nilai terakhir untuk tiap slot.
    Tidak ada batas maksimum slot — seluruh gap diisi.
    Kembalikan list kosong jika tidak ada gap signifikan.
    """
    state = _load_state()
    now   = time.time()

    if not state:
        log.info("[GAP] Belum ada state tersimpan (instalasi baru)")
        return []

    last_ts = float(state.get("last_ts", 0))
    if last_ts <= 0:
        return []

    gap_sec = now - last_ts
    if gap_sec <= interval * 1.1:
        return []   # tidak ada gap signifikan (toleransi 10%)

    slots: List[SensorReading] = []
    slot_ts = last_ts + interval
    while slot_ts <= now - interval:
        slots.append(SensorReading(
            timestamp = slot_ts,
            ph        = _vary(state.get("ph",    7.5)),
            tss       = _vary(state.get("tss",   80.0)),
            debit     = _vary(state.get("debit", 0.05)),
            pm25      = _vary(state.get("pm25",  10.0)),
            pm10      = _vary(state.get("pm10",  20.0)),
            pm100     = _vary(state.get("pm100", 50.0)),
            noise     = _vary(state.get("noise", 55.0)),
            temp      = _vary(state.get("temp",  27.0)),
        ))
        slot_ts += interval

    gap_min = gap_sec / 60
    log.warning(
        f"[GAP] Gap {gap_min:.1f} menit → {len(slots)} slot data dibuat "
        f"(basis: pH={state.get('ph')}, TSS={state.get('tss')})"
    )
    return slots


def gap_duration_str(interval: int = 120) -> str:
    """Kembalikan string durasi gap untuk ditampilkan di GUI."""
    state = _load_state()
    if not state:
        return "Tidak ada data"
    last_ts = float(state.get("last_ts", 0))
    if last_ts <= 0:
        return "Tidak ada data"
    gap_sec = time.time() - last_ts
    if gap_sec <= interval * 1.1:
        return "Tidak ada gap"
    gap_min = gap_sec / 60
    slots   = int(gap_sec // interval) - 1
    if gap_min >= 60:
        return f"{gap_min/60:.1f} jam  ({slots} slot)"
    return f"{gap_min:.0f} menit  ({slots} slot)"
