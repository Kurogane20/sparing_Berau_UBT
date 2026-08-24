#!/usr/bin/env bash
#
# backup.sh — Backup arsip data SPARING (data.db) ke luar device.
#
# Kenapa: SD card Orange Pi rapuh (korup/overheat/pernah kena malware). Arsip
# lokal `data.db` HARUS disalin keluar device secara berkala supaya data ukur
# tidak hilang kalau SD card mati. Ini juga mendukung syarat penyimpanan
# lokal/backup SK 3441.
#
# Cara pakai:
#   ./backup.sh                     # backup lokal ke ./backups
#   ./backup.sh /media/usb          # backup lokal + salin ke USB/NAS
#   REMOTE=user@server:/path ./backup.sh   # backup lokal + scp ke server
#
# Jadwalkan lewat cron, mis. tiap jam:
#   crontab -e
#   0 * * * * cd /home/orangepi/sparing && ./backup.sh >> backup.log 2>&1
#
set -euo pipefail

# ── Konfigurasi ──────────────────────────────────────────────────────────────
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DB="$APP_DIR/data.db"
BACKUP_DIR="${1:-$APP_DIR/backups}"        # arg1 atau default ./backups
REMOTE="${REMOTE:-}"                        # env REMOTE=user@host:/path (opsional)
KEEP_DAYS="${KEEP_DAYS:-30}"               # simpan backup N hari terakhir
STAMP="$(date +%Y%m%d_%H%M%S)"
OUT="$BACKUP_DIR/data_${STAMP}.db"

log() { echo "[$(date '+%F %T')] $*"; }

# ── Validasi ─────────────────────────────────────────────────────────────────
if [[ ! -f "$DB" ]]; then
    log "ERROR: $DB tidak ditemukan — belum ada data untuk di-backup."
    exit 1
fi
mkdir -p "$BACKUP_DIR"

# ── Backup KONSISTEN via sqlite3 .backup ────────────────────────────────────
# Pakai perintah .backup (bukan cp) agar aman walau DB sedang ditulis (WAL).
if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "$DB" ".backup '$OUT'"
else
    # Fallback: checkpoint WAL lalu salin file (kurang ideal tapi tetap jalan)
    log "WARN: sqlite3 tidak ada — fallback cp (pastikan install: sudo apt install sqlite3)"
    cp -f "$DB" "$OUT"
    [[ -f "$DB-wal" ]] && cp -f "$DB-wal" "$OUT-wal" || true
fi

# Kompres untuk hemat ruang
gzip -f "$OUT"
OUT="${OUT}.gz"
SIZE="$(du -h "$OUT" | cut -f1)"
log "Backup lokal OK: $OUT ($SIZE)"

# ── Salin ke tujuan luar (opsional) ─────────────────────────────────────────
if [[ -n "$REMOTE" ]]; then
    if scp -q "$OUT" "$REMOTE/" 2>/dev/null; then
        log "Salin ke REMOTE OK: $REMOTE"
    else
        log "ERROR: scp ke $REMOTE gagal — backup lokal tetap aman."
    fi
elif [[ "$BACKUP_DIR" != "$APP_DIR/backups" ]]; then
    # Arg1 diberikan (mis. path USB) — file sudah ditulis langsung ke sana
    log "Backup tersimpan di tujuan eksternal: $BACKUP_DIR"
fi

# ── Bersihkan backup lama ────────────────────────────────────────────────────
find "$BACKUP_DIR" -name 'data_*.db.gz' -type f -mtime +"$KEEP_DAYS" -delete 2>/dev/null || true
COUNT="$(find "$BACKUP_DIR" -name 'data_*.db.gz' -type f | wc -l)"
log "Selesai. Total backup tersimpan: $COUNT (retensi ${KEEP_DAYS} hari)."
