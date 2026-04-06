"""
models.py — Data model yang dipakai bersama oleh semua modul.
"""

from dataclasses import dataclass


@dataclass
class SensorReading:
    """Satu baris pembacaan sensor pada satu waktu tertentu."""
    timestamp: float = 0.0   # Unix epoch (detik)
    ph:        float = 0.0   # pH (0–14)
    tss:       float = 0.0   # Total Suspended Solid (mg/L)
    debit:     float = 0.0   # Debit aliran (m³/s)
