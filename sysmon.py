"""
sysmon.py — Monitor resource sistem (CPU, RAM, disk, suhu, undervoltage)
            untuk diagnosa penyebab device mati mendadak.

Khusus Linux (Orange Pi / Raspberry Pi). Di Windows hanya sebagian data.
Tanpa dependency tambahan — baca langsung dari /proc, /sys, dan os.statvfs.

Tujuan utama: menulis snapshot resource secara berkala ke file yang
di-flush + fsync ke disk, sehingga saat device mati mendadak, baris
TERAKHIR di resource.log menunjukkan kondisi sesaat sebelum mati:
  - suhu tinggi   → kemungkinan thermal shutdown (overheat)
  - undervoltage  → power supply / kabel USB jelek (penyebab #1 di Pi)
  - RAM penuh     → proses dibunuh OOM killer
  - disk penuh    → SD card penuh / korup, gagal tulis
  - uptime kecil  → device reboot (bukan hang)
"""

import os
import time
import shutil
import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

IS_LINUX = os.name == "posix" and os.path.exists("/proc")

# Bit makna dari vcgencmd get_throttled (Raspberry Pi)
_THROTTLE_BITS = {
    0:  "UNDERVOLTAGE-SEKARANG",
    1:  "freq-capped-sekarang",
    2:  "throttled-sekarang",
    3:  "suhu-limit-sekarang",
    16: "pernah-undervoltage",
    17: "pernah-freq-capped",
    18: "pernah-throttled",
    19: "pernah-suhu-limit",
}


class SystemMonitor:
    """Pembaca resource sistem + penulis resource.log yang tahan-mati."""

    def __init__(self, resource_log: str = "resource.log",
                 max_bytes: int = 2_000_000):
        self._prev_cpu = None             # (idle, total) untuk %CPU
        self._res_path = Path(resource_log)
        self._max_bytes = max_bytes
        self._vcgencmd = None             # "" = sudah dicek & tidak ada

    # ── CPU temperature (°C) ──────────────────────────────────────────────────
    def cpu_temp(self):
        for path in (
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/hwmon/hwmon0/temp1_input",
        ):
            try:
                with open(path) as f:
                    v = int(f.read().strip())
                # Nilai biasanya dalam milidegree (mis. 47000 = 47°C)
                return round(v / 1000.0, 1) if v > 1000 else float(v)
            except Exception:
                continue
        return None

    # ── Memory ────────────────────────────────────────────────────────────────
    def mem(self) -> dict:
        try:
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        info[parts[0].strip()] = int(parts[1].strip().split()[0])
            total = info.get("MemTotal", 0)
            avail = info.get("MemAvailable", info.get("MemFree", 0))
            used_pct = round((total - avail) / total * 100, 1) if total else 0.0
            return {
                "mem_total_mb": round(total / 1024, 1),
                "mem_avail_mb": round(avail / 1024, 1),
                "mem_used_pct": used_pct,
            }
        except Exception:
            return {}

    # ── Disk ──────────────────────────────────────────────────────────────────
    def disk(self, path: str = "/") -> dict:
        try:
            st = os.statvfs(path)
            total = st.f_blocks * st.f_frsize
            free = st.f_bavail * st.f_frsize
            used_pct = round((total - free) / total * 100, 1) if total else 0.0
            return {
                "disk_total_gb": round(total / 1e9, 2),
                "disk_free_gb": round(free / 1e9, 2),
                "disk_used_pct": used_pct,
            }
        except Exception:
            return {}

    # ── CPU usage % (butuh 2 sampel berturut) ─────────────────────────────────
    def cpu_pct(self):
        try:
            with open("/proc/stat") as f:
                fields = [int(x) for x in f.readline().split()[1:]]
            idle = fields[3] + (fields[4] if len(fields) > 4 else 0)
            total = sum(fields)
            if self._prev_cpu is None:
                self._prev_cpu = (idle, total)
                return None
            pidle, ptotal = self._prev_cpu
            self._prev_cpu = (idle, total)
            dt = total - ptotal
            if dt <= 0:
                return None
            return round((1 - (idle - pidle) / dt) * 100, 1)
        except Exception:
            return None

    # ── Load average (1 menit) ────────────────────────────────────────────────
    def loadavg(self):
        try:
            return round(os.getloadavg()[0], 2)
        except Exception:
            return None

    # ── Uptime (detik) ────────────────────────────────────────────────────────
    def uptime(self):
        try:
            with open("/proc/uptime") as f:
                return int(float(f.read().split()[0]))
        except Exception:
            return None

    # ── Throttle / undervoltage (Raspberry Pi via vcgencmd) ───────────────────
    def throttle_code(self):
        """Kembalikan bitmask int dari vcgencmd, atau None jika tidak ada."""
        if self._vcgencmd is None:
            self._vcgencmd = shutil.which("vcgencmd") or ""
        if not self._vcgencmd:
            return None
        try:
            out = subprocess.run([self._vcgencmd, "get_throttled"],
                                 capture_output=True, text=True, timeout=3)
            return int(out.stdout.strip().split("=")[-1], 16)
        except Exception:
            return None

    @staticmethod
    def decode_throttle(code: int) -> list:
        if not code:
            return []
        return [name for bit, name in _THROTTLE_BITS.items() if code & (1 << bit)]

    # ── Snapshot lengkap ──────────────────────────────────────────────────────
    def snapshot(self) -> dict:
        snap = {
            "ts": int(time.time()),
            "cpu_pct": self.cpu_pct(),
            "cpu_temp": self.cpu_temp(),
            "load1": self.loadavg(),
            "uptime_s": self.uptime(),
        }
        snap.update(self.mem())
        snap.update(self.disk())
        code = self.throttle_code()
        if code is not None:
            snap["throttle"] = code
            flags = self.decode_throttle(code)
            if flags:
                snap["throttle_flags"] = flags
        return snap

    # ── Format ringkas untuk satu baris log ───────────────────────────────────
    @staticmethod
    def format_line(snap: dict) -> str:
        t = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(snap.get("ts", 0)))
        def g(k, fmt="{}"):
            v = snap.get(k)
            return fmt.format(v) if v is not None else "—"
        parts = [
            t,
            f"cpu={g('cpu_pct','{}')}%",
            f"temp={g('cpu_temp','{}')}C",
            f"mem={g('mem_used_pct','{}')}%(free {g('mem_avail_mb','{}')}MB)",
            f"disk={g('disk_used_pct','{}')}%(free {g('disk_free_gb','{}')}GB)",
            f"load={g('load1','{}')}",
            f"up={g('uptime_s','{}')}s",
        ]
        flags = snap.get("throttle_flags")
        if flags:
            parts.append("THROTTLE=" + ",".join(flags))
        return "  ".join(parts)

    # ── Tulis ke resource.log dengan fsync (tahan mati mendadak) ──────────────
    def write_line(self, line: str) -> None:
        try:
            # Rotasi sederhana: bila file > max_bytes, geser ke .1
            if self._res_path.exists() and self._res_path.stat().st_size > self._max_bytes:
                try:
                    self._res_path.replace(self._res_path.with_suffix(
                        self._res_path.suffix + ".1"))
                except Exception:
                    pass
            with open(self._res_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())   # paksa tulis ke disk sekarang juga
        except Exception as e:
            log.debug(f"write resource.log gagal: {e}")
