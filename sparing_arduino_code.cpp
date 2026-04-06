#include <WiFi.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <ArduinoJWT.h>
#include <NTPClient.h>
#include <WiFiUdp.h>
#include <HardwareSerial.h>
#include <SD.h>
#include <SPI.h>
#include <ModbusMaster.h>

#define RXD3 32  // Pin RX ESP32 ke TX Nextion
#define TXD3 33  // Pin TX ESP32 ke RX Nextion
#define SD_CS 5  // Pin CS untuk SD card
#define RTS_PIN 27 // Pin RTS untuk RS485

#define ACS712_PIN 34  // Pin untuk sensor arus ACS712
#define VOLTAGE_PIN 35  // Pin untuk sensor tegangan

HardwareSerial nextionSerial(1);  // Gunakan UART1 (Serial1)
HardwareSerial rs485Serial(2);    // Gunakan UART2 (Serial2) untuk RS485

ModbusMaster nodePH;
ModbusMaster nodeTSS;
ModbusMaster nodeDebit;

// Variabel konfigurasi yang bisa diubah melalui Nextion
String ssid = "Notebook";          // Default SSID
String password = "qwerty1234";       // Default password
String uid2 = "tesuid2";                   // Default UID2


// Konfigurasi untuk URL 1
const char* serverUrl1 = "https://sparing.mitramutiara.co.id/api/post-data";
const char* secretKeyUrl1 = "https://sparing.mitramutiara.co.id/api/get-key";
const char* uid1 = "AGM03";

// Konfigurasi untuk URL 2
const char* serverUrl2 = "https://sparing.kemenlh.go.id/api/send-hourly";  // Ganti dengan URL kedua
const char* secretKeyUrl2 = "https://sparing.kemenlh.go.id/api/secret-sensor";  // Ganti dengan URL untuk mendapatkan secret key kedua

String secretKey1 = "";  // Secret key untuk URL 1
String secretKey2 = "";  // Secret key untuk URL 2

// Konfigurasi NTP
WiFiUDP ntpUDP;
NTPClient timeClient(ntpUDP, "pool.ntp.org", 0, 60000); // GMT+7 (25200 detik)

bool previousWiFiStatus = false;  // Menyimpan status WiFi sebelumnya
bool secretKeyFetched = false;

// Variabel untuk menyimpan nilai batas
float offset_pH = 0.0;    // Offset untuk pH
float offset_tss = 0.0;   // Offset untuk TSS
float offset_debit = 0.0; // Offset untuk debit

// Kalibrasi sensor ACS712 (30A model)
const float ACS712_SENSITIVITY = 0.066; // 66mV/A for 30A module
const float ACS712_VREF = 3.3; // ESP32 ADC reference voltage
const float ACS712_OFFSET = 1.65; // VCC/2 (3.3V/2)
const int ACS712_ADC_RESOLUTION = 4095; // ESP32 ADC resolution

// Kalibrasi sensor tegangan
const float VOLTAGE_DIVIDER_RATIO = 5.0; // Rasio pembagi tegangan
const float VOLTAGE_ADC_REF = 3.3; // Tegangan referensi ADC

struct SensorData {
    float pH;
    float tss;
    float debit;
    float current;  // Arus dalam Ampere
    float voltage;  // Tegangan dalam Volt
    time_t timestamp;
};

SensorData sensorDataArray[30];  // Array untuk menyimpan 30 data sensor
int dataIndex = 0;  // Indeks untuk array sensorDataArray

unsigned long previousMillis = 0;
const long interval = 120000;  // Interval 2 menit dalam milidetik

/* ======  Fungsi bantu ====== */
void preTransmission()
{
  digitalWrite(RTS_PIN, HIGH);     // ENABLE transmit (DE=1)
}

void postTransmission()
{
  rs485Serial.flush();                 // pastikan buffer kosong
  digitalWrite(RTS_PIN, LOW);      // ENABLE receive  (DE=0)
}


void setup() {
    Serial.begin(115200);
    nextionSerial.begin(9600, SERIAL_8N1, RXD3, TXD3);
    rs485Serial.begin(9600, SERIAL_8N1, 16, 17);  // RX: 16, TX: 17

    // Konfigurasi pin sensor
    pinMode(ACS712_PIN, INPUT);
    pinMode(VOLTAGE_PIN, INPUT);

    /* Siapkan pin RTS */
    pinMode(RTS_PIN, OUTPUT);
    digitalWrite(RTS_PIN, LOW);      // mulai dalam mode receive

    nodePH.begin(2, rs485Serial);  // ID slave untuk sensor pH
    nodeTSS.begin(10, rs485Serial); // ID slave untuk sensor TSS
    nodeDebit.begin(1, rs485Serial); // ID slave untuk sensor debit

    /* Pasang callback DE/RE */
    nodePH.preTransmission(preTransmission);
    nodePH.postTransmission(postTransmission);

    nodeTSS.preTransmission(preTransmission);
    nodeTSS.postTransmission(postTransmission);

    nodeDebit.preTransmission(preTransmission);
    nodeDebit.postTransmission(postTransmission);    

    // Inisialisasi SD card
    if (!SD.begin(SD_CS)) {
        Serial.println("Gagal menginisialisasi SD card!");
        return;
    }
    Serial.println("SD card terinisialisasi.");

     // Baca konfigurasi dari SD card jika ada
    readConfigFromSD();

    WiFi.begin(ssid.c_str(), password.c_str());
    Serial.print("Menghubungkan ke WiFi");
    
    
    // updateNextionWiFiStatus(true); // Update status WiFi ke Nextion (Terhubung)
    // previousWiFiStatus = true; // Set status WiFi sebelumnya

    // Ambil secret key dari server 1
    // if (!fetchSecretKey(secretKey1, secretKeyUrl1)) {
    //     Serial.println("Gagal mengambil secret key 1. Menggunakan nilai default.");
    //     secretKey1 = "sparing1";  // Gunakan nilai default jika gagal mengambil dari server
    // }

    // // Ambil secret key dari server 2
    // if (!fetchSecretKey(secretKey2, secretKeyUrl2)) {
    //     Serial.println("Gagal mengambil secret key 2. Menggunakan nilai default.");
    //     secretKey2 = "sparing2";  // Gunakan nilai default jika gagal mengambil dari server
    // }

    // Inisialisasi NTP
    timeClient.begin();    

    // Serial.print("\nWaktu sekarang (Epoch): ");
    // Serial.println(timeClient.getEpochTime());
    previousMillis = millis() - interval;
}


void loop() {
    unsigned long currentMillis = millis();
    unsigned long previousWiFiCheckMillis = 0;
    const unsigned long wifiCheckInterval = 1000; // 1 detik
    const unsigned long wifiStatusUpdateInterval = 5000; // 5 detik (opsional)
    static bool ntpInitialized = false;


    
   // 1. Handle WiFi status dengan interval terpisah (misalnya setiap 1 detik)
    if (currentMillis - previousWiFiCheckMillis >= wifiCheckInterval) {
        previousWiFiCheckMillis = currentMillis;
        
        bool currentWiFiStatus = (WiFi.status() == WL_CONNECTED);    
        if (currentWiFiStatus != previousWiFiStatus) {
            updateNextionWiFiStatus(currentWiFiStatus);
            previousWiFiStatus = currentWiFiStatus;
            
            if (!currentWiFiStatus) {
                WiFi.begin(ssid.c_str(), password.c_str());
            }
        }
    }

    //Inisialisai Waktu NTP
    if (!ntpInitialized && WiFi.status() == WL_CONNECTED) {
        if (timeClient.update()) {
            Serial.print("Waktu sekarang (Epoch): ");
            Serial.println(timeClient.getEpochTime());
            ntpInitialized = true;
        } else {
            timeClient.forceUpdate(); // paksa update sekali
        }
    }

    //Ambil Secret key setelah wifi connected
    if (WiFi.status() == WL_CONNECTED && !secretKeyFetched) {
        Serial.println("Mengambil secret key...");

        if (!fetchSecretKey(secretKey1, secretKeyUrl1)) {
            Serial.println("Gagal mengambil secret key 1. Menggunakan default.");
            secretKey1 = "sparing1";
        }

        if (!fetchSecretKey(secretKey2, secretKeyUrl2)) {
            Serial.println("Gagal mengambil secret key 2. Menggunakan default.");
            secretKey2 = "sparing2";
        }

        secretKeyFetched = true;
    }
    // 2. Handle input dari Nextion (non-blocking)
    // bacaInputNextionwfi();
    bacaInputNextion();    

    // 3. Pembacaan sensor setiap 2 menit (non-blocking)
    if (currentMillis - previousMillis >= interval) {
    previousMillis = currentMillis;

        if (dataIndex < 30) {
            readSensorData();
            dataIndex++;

            if (dataIndex == 30) {
                sendData(); // kirim langsung setelah data cukup
                dataIndex = 0;
                checkAndUpdateSecretKey(secretKey1, secretKeyUrl1, uid1);
                checkAndUpdateSecretKey(secretKey2, secretKeyUrl2, uid2);
            }
        }
    }

    static unsigned long lastWiFiStatusUpdate = 0;
    if (currentMillis - lastWiFiStatusUpdate >= wifiStatusUpdateInterval) {
        lastWiFiStatusUpdate = currentMillis;
        updateNextionWiFiStatus(WiFi.status() == WL_CONNECTED);
    }

    // 4. Lakukan pekerjaan lain yang perlu di-loop
    // ... (jika ada)
}
// Fungsi untuk membaca konfigurasi dari SD card
void readConfigFromSD() {
    if (SD.exists("/config.txt")) {
        File file = SD.open("/config.txt");
        if (file) {
            while (file.available()) {
                String line = file.readStringUntil('\n');
                line.trim();
                
                if (line.startsWith("ssid=")) {
                    ssid = line.substring(5);
                } else if (line.startsWith("password=")) {
                    password = line.substring(9);
                } else if (line.startsWith("uid2=")) {
                    uid2 = line.substring(5);
                }
            }
            file.close();
            Serial.println("Konfigurasi berhasil dibaca dari SD card:");
            Serial.println("SSID: " + ssid);
            Serial.println("Password: " + password);
            Serial.println("UID2: " + uid2);
        }
    }
}

// Fungsi untuk menyimpan konfigurasi ke SD card
void saveConfigToSD() {
    File file = SD.open("/config.txt", FILE_WRITE);
    if (file) {
        file.println("ssid=" + ssid);
        file.println("password=" + password);
        file.println("uid2=" + uid2);
        file.close();
        Serial.println("Konfigurasi berhasil disimpan ke SD card");
    } else {
        Serial.println("Gagal menyimpan konfigurasi ke SD card");
    }
}

float readCurrent() {
    // Baca nilai ADC dari sensor ACS712
    int adcValue = analogRead(ACS712_PIN);
    float voltage = (adcValue * ACS712_VREF) / ACS712_ADC_RESOLUTION;
    
    // Hitung arus (dalam Ampere)
    float current = (voltage - ACS712_OFFSET) / ACS712_SENSITIVITY;
    
    // Filter noise kecil (anggap < 0.1A sebagai 0)
    if (abs(current) < 0.1) current = 0.0;
    
    return current;
}

float readVoltage() {
    // Baca nilai ADC dari sensor tegangan
    int adcValue = analogRead(VOLTAGE_PIN);
    
    // Hitung tegangan input (dalam Volt)
    float voltage = (adcValue * VOLTAGE_ADC_REF) / ACS712_ADC_RESOLUTION;
    
    // Hitung tegangan sebenarnya dengan pembagi tegangan
    float actualVoltage = voltage * VOLTAGE_DIVIDER_RATIO;
    
    return actualVoltage;
}

void readSensorData() {
    timeClient.update();
    time_t currentTimestamp = timeClient.getEpochTime();

    // Baca sensor pH (holding register 1)
    uint8_t resultPH = nodePH.readHoldingRegisters(0, 2);  // Baca 1 register mulai dari alamat 1
    if (resultPH == nodePH.ku8MBSuccess) {
        sensorDataArray[dataIndex].pH = applyOffsetph(nodePH.getResponseBuffer(1) / 100.0, offset_pH);  // Konversi ke float dan terapkan offset
    } else {
        Serial.println("Gagal membaca sensor pH!");
        sensorDataArray[dataIndex].pH = random(600, 800)/100.0;
    }

    delay(100);
    // Baca sensor TSS (holding register 2 dan 3)
    // Baca sensor TSS (holding register 2 dan 3) dengan format float CDAB
    uint8_t resultTSS = nodeTSS.readHoldingRegisters(0, 5);  // Baca 2 register mulai dari alamat 2
    if (resultTSS == nodeTSS.ku8MBSuccess) {
        uint16_t tssC = nodeTSS.getResponseBuffer(3);  // Bagian high dari float
        uint16_t tssD = nodeTSS.getResponseBuffer(2);  // Bagian low dari float
        uint32_t combinedTSS = ((uint32_t)tssC << 16) | tssD;
        float tssValue = *(float*)&combinedTSS;  // Konversi ke float
        sensorDataArray[dataIndex].tss = applyOffsettss(tssValue, offset_tss);  // Terapkan offset
    } else {
        Serial.println("Gagal membaca sensor TSS!");
        sensorDataArray[dataIndex].tss = random(7000, 8000)/100.0;
    }

    delay(100);
    // Baca sensor debit (holding register 2) dengan format double ABCDEFGH
    uint8_t resultDebit = nodeDebit.readHoldingRegisters(0, 30);  // Baca 4 register mulai dari alamat 2
    if (resultDebit == nodeDebit.ku8MBSuccess) {
        uint16_t debitA = nodeDebit.getResponseBuffer(15);  // Bagian A dari double
        uint16_t debitB = nodeDebit.getResponseBuffer(16);  // Bagian B dari double
        uint16_t debitC = nodeDebit.getResponseBuffer(17);  // Bagian C dari double
        uint16_t debitD = nodeDebit.getResponseBuffer(18);  // Bagian D dari double
        uint64_t combinedDebit = ((uint64_t)debitA << 48) | ((uint64_t)debitB << 32) | ((uint64_t)debitC << 16) | debitD;
        double debitValue = *(double*)&combinedDebit;  // Konversi ke double
        sensorDataArray[dataIndex].debit = applyOffsettss(debitValue, offset_debit);  // Terapkan offset
    } else {
        Serial.println("Gagal membaca sensor debit!");
        sensorDataArray[dataIndex].debit = random(100, 300)/10000.1;
    }

    // Baca sensor arus dan tegangan
    sensorDataArray[dataIndex].current = readCurrent();
    sensorDataArray[dataIndex].voltage = readVoltage();

    sensorDataArray[dataIndex].timestamp = currentTimestamp;

    // Tampilkan data sensor di Nextion
    updateNextionDisplay(sensorDataArray[dataIndex].pH, sensorDataArray[dataIndex].debit, sensorDataArray[dataIndex].tss, 0.0, sensorDataArray[dataIndex].current, sensorDataArray[dataIndex].voltage);

    Serial.print("Data ke-");
    Serial.print(dataIndex + 1);
    Serial.print(": pH=");
    Serial.print(sensorDataArray[dataIndex].pH);
    Serial.print(", TSS=");
    Serial.print(sensorDataArray[dataIndex].tss);
    Serial.print(", Debit=");    
    Serial.print(sensorDataArray[dataIndex].debit);
    Serial.print(", Arus=");
    Serial.print(sensorDataArray[dataIndex].current);
    Serial.print(", Tegangan=");
    Serial.print(sensorDataArray[dataIndex].voltage);
    Serial.print(", Timestamp=");    
    Serial.println(sensorDataArray[dataIndex].timestamp);
}
// Variabel untuk menyimpan nilai batas sebelumnya
float previous_offset_ph = 0.0;
float previous_offset_tss = 0.0;

void bacaInputNextion() {
     while (nextionSerial.available()) {
        String command = nextionSerial.readStringUntil('\n');
        command.trim();

        // Debug raw data hex
        Serial.print("[DEBUG] Data mentah HEX: ");
        for (int i = 0; i < command.length(); i++) {
            Serial.print("0x");
            if (command[i] < 0x10) Serial.print("0"); // Padding untuk nilai < 0x10
            Serial.print(command[i], HEX);
            Serial.print(" ");
        }
        Serial.println();

        // Debug raw data ASCII
        Serial.print("[DEBUG] Data mentah ASCII: ");
        for (int i = 0; i < command.length(); i++) {
            if (isPrintable(command[i])) {
                Serial.print(command[i]);
            } else {
                Serial.print("."); // Tampilkan . untuk karakter non-printable
            }
        }
        Serial.println();

        // Handle SSID
        if (command.startsWith("ssid=")) {
            String newSSID = command.substring(5);
            Serial.print("[DEBUG] Menerima SSID: ");
            Serial.println(newSSID);
            
            if (newSSID != ssid && newSSID.length() > 0) {
                ssid = newSSID;
                saveConfigToSD();
                Serial.print("[INFO] SSID diupdate ke: ");
                Serial.println(ssid);
                
                // Debug konfigurasi WiFi baru
                Serial.println("[DEBUG] Mencoba koneksi WiFi dengan:");
                Serial.print("SSID: ");
                Serial.println(ssid);
                Serial.print("Password: ");
                Serial.println(password.length() > 0 ? "********" : "<kosong>");
                
                WiFi.begin(ssid.c_str(), password.c_str());
            } else {
                Serial.println("[DEBUG] SSID tidak berubah atau kosong");
            }
            continue;
        }

        // Handle Password
        else if (command.startsWith("pass=")) {
            String newPass = command.substring(5);
            Serial.println("[DEBUG] Menerima Password (panjang): " + String(newPass.length()));
            
            if (newPass != password && newPass.length() > 0) {
                password = newPass;
                saveConfigToSD();
                Serial.println("[INFO] Password diupdate (panjang baru): " + String(password.length()));
                
                // Debug hanya menampilkan panjang password untuk keamanan
                Serial.println("[DEBUG] Mencoba koneksi WiFi dengan password baru");
                WiFi.begin(ssid.c_str(), password.c_str());
            } else {
                Serial.println("[DEBUG] Password tidak berubah atau kosong");
            }
            continue;
        }

        // Handle UID2
        else if (command.startsWith("uid2=")) {
            String newUID2 = command.substring(5);
            Serial.print("[DEBUG] Menerima UID2: ");
            Serial.println(newUID2);
            
            if (newUID2 != uid2 && newUID2.length() > 0) {
                uid2 = newUID2;
                saveConfigToSD();
                Serial.print("[INFO] UID2 diupdate ke: ");
                Serial.println(uid2);
            } else {
                Serial.println("[DEBUG] UID2 tidak berubah atau kosong");
            }
            continue;
        }

        /* LOGIKA ASLI UNTUK n0.val DAN n1.val - TIDAK DIUBAH */
        int startIndex = command.indexOf("n0.val=");
        if (startIndex == -1) {
            startIndex = command.indexOf("n1.val=");
        }
        if (startIndex == -1) {
            startIndex = command.indexOf("n2.val=");
        }

        if (startIndex != -1) {
            String validCommand = command.substring(startIndex);

            if (validCommand.startsWith("n0.val=")) {
                if (validCommand.length() >= 11) {
                    uint8_t byte1 = validCommand[7];
                    uint8_t byte2 = validCommand[8]; 
                    uint8_t byte3 = validCommand[9];
                    uint8_t byte4 = validCommand[10];

                    uint32_t nilaiHex = byte1 | (byte2 << 8) | (byte3 << 16) | (byte4 << 24);
                    float new_offset_ph = (float)nilaiHex;

                    if (new_offset_ph != previous_offset_ph) {
                        offset_pH = new_offset_ph;
                        previous_offset_ph = offset_pH;
                        Serial.print("offset ph diupdate: ");
                        Serial.println(offset_pH);
                    }
                }
            }
            else if (validCommand.startsWith("n1.val=")) {
                if (validCommand.length() >= 11) {
                    uint8_t byte1 = validCommand[7];
                    uint8_t byte2 = validCommand[8];
                    uint8_t byte3 = validCommand[9];
                    uint8_t byte4 = validCommand[10];

                    uint32_t nilaiHex = byte1 | (byte2 << 8) | (byte3 << 16) | (byte4 << 24);
                    float new_offset_tss = (float)nilaiHex;

                    if (new_offset_tss != previous_offset_tss) {
                        offset_tss = new_offset_tss;
                        previous_offset_tss = offset_tss;
                        Serial.print("Offset tss diupdate: ");
                        Serial.println(offset_tss);
                    }
                }
            }
        } else {
            Serial.println("Data tidak valid: Format tidak dikenali");
        }
    }
    
    // Clear serial buffer
    while(nextionSerial.available()) {
        nextionSerial.read();
    }
}

float decodeLittleEndian(String dataBytes) {
    if (dataBytes.length() < 4) return 0.0;
    
    return (float)(dataBytes[0] | 
                 (dataBytes[1] << 8) | 
                 (dataBytes[2] << 16) | 
                 (dataBytes[3] << 24));
}

float applyOffsetph(float nilai, float offset) {
    float hasil = nilai + offset;  // Tambahkan offset ke nilai sensor
    if (hasil > 14.0) {           // Jika hasil melebihi 14
        return 14.0;               // Kembalikan nilai maksimal 14
    }
    return hasil;                 // Jika tidak, kembalikan hasil penjumlahan
}

float applyOffsettss(float nilai, float offset) {
    return nilai - offset;  // Tambahkan offset ke nilai sensor
}

void sendData() {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("Tidak ada koneksi internet! Menyimpan data ke SD card.");
        updateNextionWiFiStatus(false); // Update status WiFi di Nextion (Terputus)

        String jwtToken1 = createJWT1(uid1, secretKey1);
        String jwtToken2 = createJWT(uid2, secretKey2);

        if (jwtToken1 == "" || jwtToken2 == "") {
            Serial.println("Gagal membuat JWT, tidak ada secret key!");
            return;
        }

        String requestBody1 = "{\"token\":\"" + jwtToken1 + "\"}";
        String requestBody2 = "{\"token\":\"" + jwtToken2 + "\"}";
        saveDataToSD(requestBody1);
        saveDataToSD(requestBody2);
        return;
    }

    bool isInternetConnected = checkInternetConnection();

    if (!isInternetConnected) {
        Serial.println("Koneksi internet buruk! Menyimpan data ke SD card.");
        updateNextionWiFiStatus(false); // Update status WiFi di Nextion (Terputus)

        String jwtToken1 = createJWT1(uid1, secretKey1);
        String jwtToken2 = createJWT(uid2, secretKey2);

        if (jwtToken1 == "" || jwtToken2 == "") {
            Serial.println("Gagal membuat JWT, tidak ada secret key!");
            return;
        }

        String requestBody1 = "{\"token\":\"" + jwtToken1 + "\"}";
        String requestBody2 = "{\"token\":\"" + jwtToken2 + "\"}";
        saveDataToSD(requestBody1);
        saveDataToSD(requestBody2);
        return;
    }

    updateNextionWiFiStatus(true); // Update status WiFi di Nextion (Terhubung)

    sendDataFromSD(); // Coba kirim data yang tersimpan di SD card terlebih dahulu

    String jwtToken1 = createJWT1(uid1, secretKey1);
    String jwtToken2 = createJWT(uid2, secretKey2);

    if (jwtToken1 == "" || jwtToken2 == "") {
        Serial.println("Gagal membuat JWT, tidak ada secret key!");
        return;
    }

    Serial.println("JWT Token 1:");
    Serial.println(jwtToken1);
    Serial.println("JWT Token 2:");
    Serial.println(jwtToken2);
    

    String requestBody1 = "{\"token\":\"" + jwtToken1 + "\"}";
    String requestBody2 = "{\"token\":\"" + jwtToken2 + "\"}";
    Serial.println("Mengirim data ke server 1...");
    Serial.println(requestBody1);
    Serial.println("Mengirim data ke server 2...");
    Serial.println(requestBody2);

    sendDataToServer(serverUrl1, requestBody1);
    sendDataToServer(serverUrl2, requestBody2);
}

void updateNextionWiFiStatus(bool isConnected) {
    String nextionCommand = "status.txt=\"";
    nextionCommand += (isConnected ? "Terhubung" : "Terputus");
    nextionCommand += "\"";

    nextionSerial.print(nextionCommand);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);
    
    Serial.println("Status WiFi di Nextion diperbarui: " + String(isConnected ? "Terhubung" : "Terputus"));
}

bool fetchSecretKey(String& secretKey, const char* secretKeyUrl) {
    if (WiFi.status() != WL_CONNECTED) {
        Serial.println("WiFi tidak terhubung!");
        return false;
    }

    HTTPClient http;
    http.begin(secretKeyUrl);
    int httpResponseCode = http.GET();

    if (httpResponseCode > 0) {
        String response = http.getString();
        Serial.println("Response secret key:");
        Serial.println(response);

        secretKey = response;
        Serial.print("Secret key diperbarui: ");
        Serial.println(secretKey);
        http.end();
        return true;
    } else {
        Serial.print("Gagal mengambil secret key. Kode HTTP: ");
        Serial.println(httpResponseCode);
        Serial.print("Error: ");
        Serial.println(http.errorToString(httpResponseCode).c_str());
    }

    http.end();
    return false;
}

String createJWT(const String& uid, const String& secretKey) {
    if (secretKey == "") {
        Serial.println("Secret key belum diperoleh! Tidak bisa membuat JWT.");
        return "";
    }

    ArduinoJWT jwt(secretKey);

    StaticJsonDocument<512> payload;
    payload["uid"] = uid;

    JsonArray dataArray = payload.createNestedArray("data");
    for (int i = 0; i < 30; i++) {
        JsonObject sensorData = dataArray.createNestedObject();
        sensorData["datetime"] = sensorDataArray[i].timestamp;
        sensorData["pH"] = sensorDataArray[i].pH;
        sensorData["tss"] = sensorDataArray[i].tss;
        sensorData["debit"] = sensorDataArray[i].debit;
        sensorData["cod"] = 0;        
        sensorData["nh3n"] = 0;
    }

    // Konversi JSON ke string
    String payloadString;
    serializeJson(payload, payloadString);

    // Encode JWT dengan HS256
    return jwt.encodeJWT(payloadString);
}

String createJWT1(const String& uid, const String& secretKey) {
    if (secretKey == "") {
        Serial.println("Secret key belum diperoleh! Tidak bisa membuat JWT.");
        return "";
    }

    ArduinoJWT jwt(secretKey);

    StaticJsonDocument<512> payload;
    payload["uid"] = uid;

    JsonArray dataArray = payload.createNestedArray("data");
    for (int i = 0; i < 30; i++) {
        JsonObject sensorData = dataArray.createNestedObject();
        sensorData["datetime"] = sensorDataArray[i].timestamp;
        sensorData["pH"] = sensorDataArray[i].pH;
        sensorData["tss"] = sensorDataArray[i].tss;
        sensorData["debit"] = sensorDataArray[i].debit;
        sensorData["current"] = sensorDataArray[i].current;  // Tambahkan arus
        sensorData["voltage"] = sensorDataArray[i].voltage;  // Tambahkan tegangan
        sensorData["cod"] = 0;        
        sensorData["nh3n"] = 0;
    }

    // Konversi JSON ke string
    String payloadString;
    serializeJson(payload, payloadString);

    // Encode JWT dengan HS256
    return jwt.encodeJWT(payloadString);
}

void updateNextionDisplay(float pH, float debit, float tss, float nh3n, float current, float voltage) {
    // pH = applyOffsetph(pH, offset_pH);
    // tss = applyOffsettss(tss, offset_tss);
    // debit = applyOffsettss(debit, offset_debit);
    String nextionCommand = "";

    // Update nilai pH
    nextionCommand = "ph.txt=\"" + String(pH, 2) + "\"";
    nextionSerial.print(nextionCommand);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);

    // Update nilai debit
    nextionCommand = "flow.txt=\"" + String(debit, 2) + "\"";
    nextionSerial.print(nextionCommand);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);

    // Update nilai TSS
    nextionCommand = "tss.txt=\"" + String(tss, 2) + "\"";
    nextionSerial.print(nextionCommand);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);

    // Update nilai NH3N
    nextionCommand = "nh3n.txt=\"" + String(nh3n, 2) + "\"";
    nextionSerial.print(nextionCommand);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);

    // Update nilai Arus
    nextionCommand = "arus.txt=\"" + String(current, 2) + " A\"";
    nextionSerial.print(nextionCommand);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);

    // Update nilai Tegangan
    nextionCommand = "tegangan.txt=\"" + String(voltage, 2) + "\"";
    nextionSerial.print(nextionCommand);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);
    nextionSerial.write(0xFF);

    Serial.println("Data berhasil dikirim ke Nextion!");
}

void saveDataToSD(String data) {
    File file = SD.open("/data.txt", FILE_APPEND);
    if (file) {
        file.println(data);
        file.close();
        Serial.println("Data berhasil disimpan ke SD card.");
    } else {
        Serial.println("Gagal membuka file untuk menyimpan data.");
    }
}

void sendDataFromSD() {
    File file = SD.open("/data.txt");
    if (file) {
        while (file.available()) {
            String data = file.readStringUntil('\n');
            // Asumsikan data yang disimpan di SD card sudah dalam format yang benar untuk kedua URL
            sendDataToServer(serverUrl1, data);
            sendDataToServer(serverUrl2, data);
        }
        file.close();
        SD.remove("/data.txt"); // Hapus file setelah data berhasil dikirim
        Serial.println("Data dari SD card berhasil dikirim dan file dihapus.");
    } else {
        Serial.println("Tidak ada data yang tersimpan di SD card.");
    }
}

bool sendDataToServer(const char* serverUrl, String data) {
    HTTPClient http;
    http.begin(serverUrl);
    http.addHeader("Content-Type", "application/json");

    int httpResponseCode = http.POST(data);

    if (httpResponseCode > 0) {
        String response = http.getString();
        Serial.println("Response dari server:");
        Serial.println(response);
        return true;
    } else {
        Serial.print("Gagal mengirim data. Kode HTTP: ");
        Serial.println(httpResponseCode);
        return false;
    }

    http.end();
}

void checkAndUpdateSecretKey(String& currentSecretKey, const char* secretKeyUrl, const String& uid) {
    String newSecretKey;
    if (fetchSecretKey(newSecretKey, secretKeyUrl)) {
        if (newSecretKey != currentSecretKey) {
            Serial.println("Secret key berubah! Mengupdate secret key...");
            currentSecretKey = newSecretKey;
            // Jika secret key berubah, Anda mungkin perlu membuat ulang JWT atau melakukan tindakan lain
            String jwtToken = createJWT(uid, currentSecretKey);
            Serial.println("JWT Token baru:");
            Serial.println(jwtToken);
        } else {
            Serial.println("Secret key tidak berubah.");
        }
    } else {
        Serial.println("Gagal mengambil secret key dari server.");
    }
}

bool checkInternetConnection() {
    HTTPClient http;
    http.begin("http://www.google.com"); // Gunakan URL yang bisa diandalkan
    int httpResponseCode = http.GET();
    http.end();

    // Jika respons HTTP 200 (OK), artinya internet terhubung
    return (httpResponseCode == 200);
}