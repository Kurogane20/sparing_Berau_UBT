"""
main.py — Entry point SPARING Monitor.
Jalankan: python main.py
"""

import sys
import logging

# Setup logging global sebelum modul lain di-import
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.FileHandler("sparing.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

from app import SparingApp

if __name__ == "__main__":
    SparingApp().start()
