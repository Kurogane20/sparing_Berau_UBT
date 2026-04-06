# SPARING Monitor — PT Sucofindo

Sistem pemantauan kualitas air (SPARING) berbasis Python untuk Raspberry Pi, Orange Pi, dan Windows.
Data sensor dibaca via **USB TTL RS485 (Modbus RTU)** dan dikirim ke dua server SPARING menggunakan JWT.

---

## Daftar Isi

1. [Fitur](#fitur)
2. [Struktur Proyek](#struktur-proyek)
3. [Hardware yang Dibutuhkan](#hardware-yang-dibutuhkan)
4. [Instalasi](#instalasi)
5. [Konfigurasi](#konfigurasi)
6. [Cara Menjalankan](#cara-menjalankan)
7. [Tampilan GUI](#tampilan-gui)
8. [Alamat Register Modbus](#alamat-register-modbus)
9. [Troubleshooting](#troubleshooting)

---

## Fitur

- Baca sensor **pH**, **TSS**, dan **Debit** via Modbus RTU over USB RS485
- Baca sensor **arus** (ACS712) dan **tegangan** via ADC MCP3008 SPI
- Kirim data batch 30 menit ke **2 server SPARING** menggunakan **JWT HS256**
- **Buffer offline** — data tersimpan ke file saat tidak ada internet dan dikirim ulang otomatis
- **Auto-deteksi port** USB RS485 (CH340, CP210x, FT232, PL2303)
- **Mode simulasi** otomatis jika hardware tidak terpasang
- GUI fullscreen via **HDMI** dengan tema warna PT Sucofindo
- Kompatibel: **Raspberry Pi**, **Orange Pi**, **Windows**

---

## Struktur Proyek

```
project UBT/
├── main.py          # Entry point — jalankan file ini
├── app.py           # Orkestrator utama (thread sensor & network)
├── gui.py           # Antarmuka grafis tkinter
├── sensors.py       # Pembacaan sensor Modbus + ADC
├── network.py       # HTTP, JWT, secret key
├── storage.py       # Buffer data offline
├── config.py        # Konfigurasi + scan port USB
├── constants.py     # Platform flags, warna tema, import library
├── models.py        # SensorReading dataclass
├── requirements.txt # Dependensi Python
├── config.json      # Dibuat otomatis saat pertama kali disimpan
├── data_buffer.json # Dibuat otomatis saat offline
├── sparing.log      # Log aktivitas
└── PT_Sucofindo.png # Logo (wajib ada di folder yang sama)
```

### Tanggung Jawab Tiap File

| File | Ubah jika ingin... |
|---|---|
| `constants.py` | Menambah library baru atau mengubah warna tema |
| `models.py` | Menambah field data sensor (misal: COD, NH3N) |
| `config.py` | Mengubah nilai default konfigurasi atau logika scan port |
| `sensors.py` | Mengubah cara baca sensor atau alamat register Modbus |
| `network.py` | Mengubah format JWT, menambah server, atau mengubah endpoint |
| `storage.py` | Mengubah mekanisme buffer data offline |
| `gui.py` | Mengubah tampilan, menambah widget, atau dialog baru |
| `app.py` | Mengubah alur utama, interval pengiriman, atau logika batch |

---

## Hardware yang Dibutuhkan

### Wajib
| Komponen | Keterangan |
|---|---|
| Raspberry Pi / Orange Pi / PC | Minimal RPi 3B+ |
| Konverter **USB TTL RS485** | Chip CH340, CP210x, FT232, atau PL2303 |
| Sensor **pH** | Modbus RTU, slave ID 2 |
| Sensor **TSS** | Modbus RTU, slave ID 10 |
| Flow meter **Debit** | Modbus RTU, slave ID 1 |

### Opsional (sensor arus & tegangan)
| Komponen | Keterangan |
|---|---|
| **ACS712 30A** | Sensor arus, terhubung ke MCP3008 channel 0 |
| **Pembagi tegangan** | Rasio 1:5, terhubung ke MCP3008 channel 1 |
| **MCP3008** | ADC 10-bit, komunikasi SPI dengan RPi/OPi |

### Diagram Koneksi USB RS485

```
Sensor pH (slave 2)  ─┐
Sensor TSS (slave 10) ─┤─ Kabel RS485 (A/B) ──► USB RS485 Adapter ──► USB Port RPi/PC
Flow meter (slave 1) ─┘
```

> **Catatan:** Adapter USB RS485 mengelola sinyal DE/RE secara otomatis.
> Tidak perlu pin GPIO tambahan seperti pada implementasi Arduino.

---

## Instalasi

### 1. Clone / salin proyek

```bash
cd ~
# Salin semua file .py, requirements.txt, dan PT_Sucofindo.png ke satu folder
```

### 2. Install dependensi Python

```bash
pip install -r requirements.txt
```

### 3. Install driver USB RS485 (sesuai platform)

**Linux (Raspberry Pi / Orange Pi)**
Driver sudah built-in di kernel. Langsung plug-and-play.
```bash
# Cek port setelah colok adapter:
ls /dev/ttyUSB*
# atau
ls /dev/ttyACM*

# Tambahkan user ke grup dialout agar bisa akses port serial (sekali saja):
sudo usermod -aG dialout $USER
# Logout lalu login ulang agar berlaku
```

**Windows**
Install driver sesuai chip adapter:
- **CH340/CH341** → [wch-ic.com](https://www.wch-ic.com/downloads/CH341SER_EXE.html)
- **CP210x** → [silabs.com](https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers)
- **FT232** → [ftdichip.com](https://ftdichip.com/drivers/vcp-drivers/)
- **PL2303** → [prolific.com.tw](http://www.prolific.com.tw/US/ShowProduct.aspx?p_id=225&pcid=41)

Setelah install, cek di **Device Manager → Ports (COM & LPT)**. Catat nomor COM (misal: `COM5`).

### 4. Install library GPIO/SPI (hanya Raspberry Pi / Orange Pi)

```bash
# Raspberry Pi:
pip install RPi.GPIO spidev

# Orange Pi:
pip install OPi.GPIO spidev
```

---

## Konfigurasi

Konfigurasi disimpan di `config.json` (dibuat otomatis saat pertama kali disimpan dari GUI).
Bisa juga dibuat manual sebelum menjalankan aplikasi.

### Contoh `config.json`

```json
{
  "serial_port":            "/dev/ttyUSB0",
  "baud_rate":              9600,
  "slave_id_ph":            2,
  "slave_id_tss":           10,
  "slave_id_debit":         1,

  "server_url1":            "https://sparing.mitramutiara.co.id/api/post-data",
  "secret_key_url1":        "https://sparing.mitramutiara.co.id/api/get-key",
  "uid1":                   "AGM03",

  "server_url2":            "https://sparing.kemenlh.go.id/api/send-hourly",
  "secret_key_url2":        "https://sparing.kemenlh.go.id/api/secret-sensor",
  "uid2":                   "ID_STASIUN_ANDA",

  "interval_seconds":       120,
  "data_batch_size":        30,

  "offset_ph":              0.0,
  "offset_tss":             0.0,
  "offset_debit":           0.0,

  "simulate_sensors":       false
}
```

### Parameter Penting

| Parameter | Default | Keterangan |
|---|---|---|
| `serial_port` | `/dev/ttyUSB0` (Linux) / `COM3` (Windows) | Port USB RS485 |
| `baud_rate` | `9600` | Harus sama dengan setting sensor |
| `slave_id_*` | pH=2, TSS=10, Debit=1 | Modbus slave address sensor |
| `uid1` | `AGM03` | ID stasiun untuk Server 1 |
| `uid2` | `tesuid2` | ID stasiun untuk Server 2 |
| `interval_seconds` | `120` | Interval baca sensor (detik) |
| `data_batch_size` | `30` | Jumlah data sebelum dikirim |
| `offset_ph` | `0.0` | Koreksi nilai pH |
| `simulate_sensors` | `false` | `true` untuk uji tanpa hardware |

---

## Cara Menjalankan

```bash
python main.py
```

### Autostart saat boot (Raspberry Pi)

**Metode 1 — systemd service**
```bash
sudo nano /etc/systemd/system/sparing.service
```
```ini
[Unit]
Description=SPARING Monitor
After=network.target

[Service]
User=pi
WorkingDirectory=/home/pi/project-UBT
ExecStart=/usr/bin/python3 /home/pi/project-UBT/main.py
Restart=always
Environment=DISPLAY=:0

[Install]
WantedBy=multi-user.target
```
```bash
sudo systemctl enable sparing
sudo systemctl start sparing
```

**Metode 2 — autostart desktop (LXDE/Wayfire)**
```bash
mkdir -p ~/.config/autostart
nano ~/.config/autostart/sparing.desktop
```
```ini
[Desktop Entry]
Type=Application
Name=SPARING Monitor
Exec=python3 /home/pi/project-UBT/main.py
```

---

## Tampilan GUI

```
┌──────────────────────────────────────────────────────────────────┐
│  [LOGO]  SISTEM PEMANTAUAN KUALITAS AIR (SPARING)    14:32:05   │
├──────────────────────────────┬───────────────────────────────────┤
│  ┌──pH──┐┌──TSS─┐┌─DEBIT─┐  │  STATUS KONEKSI                   │
│  │ 7.52 ││ 75.3 ││0.0245 │  │  RS485 USB  : ● Terhubung         │
│  └──────┘└──────┘└───────┘  │  Internet   : ● Terhubung         │
│  ┌─ARUS─┐┌─TEGANGAN────────┐│  Server 1   : ● Terhubung         │
│  │ 1.25 ││    12.4         ││  Server 2   : ● Terhubung         │
│  └──────┘└─────────────────┘│  Port: /dev/ttyUSB0               │
│                              │  [↻ Hubungkan Ulang] [⌕ Scan]    │
│  INFO PENGIRIMAN DATA        │                                   │
│  Data Terkumpul : 15 / 30   │  OFFSET KALIBRASI                 │
│  Kirim Terakhir : 14:30:00  │  Offset pH   : [  0.00 ]          │
│  Buffer Offline : 0 batch   │  Offset TSS  : [  0.00 ]          │
│  ▓▓▓▓▓▓▓▓▓▓▓░░░░░░░░░ 50% │  Offset Debit: [  0.00 ]          │
│                              │                                   │
│  LOG AKTIVITAS               │  [ ⚙ Pengaturan Koneksi ]        │
│  [14:32:03] Data 15/30 —... │                                   │
│  [14:30:03] Data 14/30 —... │                                   │
├──────────────────────────────┴───────────────────────────────────┤
│  [14:32:03] Data 15/30 ...      Mode: LIVE | Port: /dev/ttyUSB0 │
└──────────────────────────────────────────────────────────────────┘
```

---

## Alamat Register Modbus

### Sensor pH (Slave ID 2)

| Register | Keterangan |
|---|---|
| 0 | — (tidak digunakan) |
| 1 | Nilai pH × 100 (integer) |

Contoh: register[1] = 752 → pH = 7.52

### Sensor TSS (Slave ID 10)

| Register | Keterangan |
|---|---|
| 2 | Low word float (bagian D) |
| 3 | High word float (bagian C) |

Format float IEEE 754 dengan byte order **CDAB** (mixed-endian).

### Flow Meter Debit (Slave ID 1)

| Register | Keterangan |
|---|---|
| 15 | Word A (bit 63–48) |
| 16 | Word B (bit 47–32) |
| 17 | Word C (bit 31–16) |
| 18 | Word D (bit 15–0) |

Format double IEEE 754 64-bit, byte order **ABCD** (big-endian per word).

---

## Troubleshooting

### USB RS485 tidak terdeteksi

```bash
# Linux — cek apakah adapter terdeteksi oleh kernel:
dmesg | grep -i "tty\|usb\|ch34\|cp21\|ft232\|pl230"

# Pastikan user punya izin akses:
ls -la /dev/ttyUSB0
# Jika perlu: sudo chmod a+rw /dev/ttyUSB0

# Windows — buka Device Manager, pastikan tidak ada tanda seru (!)
```

### Modbus timeout / gagal baca

1. Pastikan **baud rate** di `config.json` sama dengan setting sensor
2. Pastikan **slave ID** benar (cek manual sensor)
3. Coba kurangi timeout di `sensors.py` → `ModbusSerialClient(timeout=2)`
4. Periksa sambungan kabel A/B RS485 (coba tukar jika terbalik)
5. Pastikan sensor mendapat daya yang cukup

### Mode simulasi aktif padahal hardware ada

Pastikan `pymodbus` sudah terinstall:
```bash
pip install pymodbus
```
Lalu set `"simulate_sensors": false` di `config.json`.

### GUI tidak muncul di Raspberry Pi (headless)

Pastikan variabel `DISPLAY` sudah di-set:
```bash
export DISPLAY=:0
python main.py
```

### JWT gagal dibuat

Secret key belum diambil dari server. Pastikan:
1. Koneksi internet aktif
2. URL `secret_key_url1` dan `secret_key_url2` benar dan dapat diakses
3. Jika server tidak dapat diakses, aplikasi akan menggunakan secret key default (`sparing1` / `sparing2`)
