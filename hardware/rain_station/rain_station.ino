/*
  Seri Iskandar Rain-Alert Station (bonus hardware)
  ESP32 + BME280 + 1.3" OLED + raindrop sensor + traffic light LED module + passive buzzer

  What it does
  - Reads local temperature, humidity and air pressure (BME280).
  - Detects rain falling on the sensor plate right now (raindrop sensor).
  - Every 10 minutes, gets the next 3 hours of rain forecast for
    Seri Iskandar from Open-Meteo over Wi-Fi.
  - Shows one alert level on the OLED, the traffic light and the buzzer:
        RAINING NOW    plate is wet                                  red blinking + 3 beeps
        RAIN LIKELY    chance >= 70% or >= 1.0 mm in next 3 h       red          + 3 beeps
        RAIN POSSIBLE  chance >= 40% or >= 0.2 mm in next 3 h       yellow
        NO RAIN        otherwise                                     green
  - Works standalone: if Wi-Fi is down, it uses a local rule
    (humidity >= 90% and pressure falling >= 1 hPa over 3 h).
  Same thresholds as the web dashboard banner. Timing uses millis(), not delay().

  Wiring: see hardware/README.md
    I2C bus (shared on breadboard): SDA = GPIO 21, SCL = GPIO 22  (OLED + BME280)
    Rain sensor: VCC = GPIO 32 (powered only while reading), AO = GPIO 34
    Traffic light: R = GPIO 27, Y = GPIO 26, G = GPIO 25
    Passive buzzer signal: GPIO 13

  Libraries (Arduino IDE > Tools > Manage Libraries):
    U8g2 (by oliver)
    Adafruit BME280 Library (+ Adafruit Unified Sensor)
    ArduinoJson (by Benoit Blanchon, version 7)
  Board: "ESP32 Dev Module" (esp32 by Espressif Systems)
*/

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <Adafruit_Sensor.h>
#include <Adafruit_BME280.h>

// ---------- change these ----------
const char* WIFI_SSID = "YOUR WIFI";
const char* WIFI_PASS = "YOUR WIFI PASSWORD";
const int   RAIN_WET_PERCENT = 30;   // calibrate: see README, section 6
// ----------------------------------

const char* URL =
  "https://api.open-meteo.com/v1/forecast?latitude=4.36&longitude=100.97"
  "&hourly=precipitation_probability,precipitation&forecast_hours=3"
  "&timezone=Asia%2FKuala_Lumpur";

// ---------- pins ----------
const int PIN_SDA = 21, PIN_SCL = 22;
const int RAIN_POWER = 32, RAIN_AO = 34;
const int LED_RED = 27, LED_YELLOW = 26, LED_GREEN = 25;
const int BUZZER = 13;

// ---------- timing ----------
const unsigned long SENSOR_EVERY_MS   = 5UL * 1000UL;         // read sensors + redraw
const unsigned long FORECAST_EVERY_MS = 10UL * 60UL * 1000UL; // ask Open-Meteo
const unsigned long PRESSURE_LOG_MS   = 10UL * 60UL * 1000UL; // pressure history for 3-h trend
const unsigned long BLINK_MS = 400;
const int BEEP_HZ = 2500, BEEP_ON_MS = 150, BEEP_OFF_MS = 150;

// 1.3" OLED normally uses the SH1106 chip. If the picture looks shifted or noisy,
// replace the next line with:
//   U8G2_SSD1306_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE);
U8G2_SH1106_128X64_NONAME_F_HW_I2C u8g2(U8G2_R0, U8X8_PIN_NONE);
Adafruit_BME280 bme;
bool bmeOk = false;

// alert levels, ordered by severity
enum Level { NO_RAIN = 0, RAIN_POSSIBLE = 1, RAIN_LIKELY = 2, RAINING_NOW = 3 };
const char* LEVEL_TEXT[] = {"NO RAIN", "RAIN POSSIBLE", "RAIN LIKELY", "RAINING NOW"};
Level level = NO_RAIN, lastLevel = NO_RAIN;

// latest readings
float tempC = NAN, humPct = NAN, pressHpa = NAN, pressTrend = NAN;
int rainRaw = 4095, wetPct = 0;
int fcProb = 0;
float fcMm = 0;
bool forecastOk = false;

// pressure history: one value every 10 min, 19 values = 3 hours
const int HIST = 19;
float pressHist[HIST];
int histCount = 0, histPos = 0;

// timers and buzzer/LED state
unsigned long lastSensor = 0, lastForecast = 0, lastPressLog = 0, lastBlink = 0, lastBeepStep = 0;
bool blinkOn = false;
int beepSteps = 0;      // counts down: even = tone on, odd = tone off
bool toneOn = false;

// ---------- outputs ----------

void setLights(bool r, bool y, bool g) {
  digitalWrite(LED_RED, r);
  digitalWrite(LED_YELLOW, y);
  digitalWrite(LED_GREEN, g);
}

void startBeeps(int count) { beepSteps = count * 2; lastBeepStep = 0; }

void updateBuzzer(unsigned long now) {
  if (beepSteps <= 0) { if (toneOn) { noTone(BUZZER); toneOn = false; } return; }
  unsigned long wait = toneOn ? BEEP_ON_MS : BEEP_OFF_MS;
  if (lastBeepStep != 0 && now - lastBeepStep < wait) return;
  lastBeepStep = now;
  if (beepSteps % 2 == 0) { tone(BUZZER, BEEP_HZ); toneOn = true; }
  else                    { noTone(BUZZER);        toneOn = false; }
  beepSteps--;
}

void updateLights(unsigned long now) {
  switch (level) {
    case NO_RAIN:       setLights(false, false, true); break;
    case RAIN_POSSIBLE: setLights(false, true, false); break;
    case RAIN_LIKELY:   setLights(true, false, false); break;
    case RAINING_NOW:
      if (now - lastBlink >= BLINK_MS) { lastBlink = now; blinkOn = !blinkOn; }
      setLights(blinkOn, false, false);
      break;
  }
}

// ---------- sensors ----------

void readRain() {
  digitalWrite(RAIN_POWER, HIGH);   // power the sensor only while reading (slows corrosion)
  delay(10);
  rainRaw = analogRead(RAIN_AO);    // about 4095 = dry, lower = wetter
  digitalWrite(RAIN_POWER, LOW);
  wetPct = constrain(map(rainRaw, 4095, 0, 0, 100), 0, 100);
}

void readBme() {
  if (!bmeOk) return;
  tempC = bme.readTemperature();
  humPct = bme.readHumidity();
  pressHpa = bme.readPressure() / 100.0F;
}

void logPressure() {
  if (isnan(pressHpa)) return;
  pressHist[histPos] = pressHpa;
  histPos = (histPos + 1) % HIST;
  if (histCount < HIST) histCount++;
  if (histCount == HIST) pressTrend = pressHpa - pressHist[histPos];  // now minus 3 h ago
}

// ---------- forecast ----------

bool connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) delay(250);
  return WiFi.status() == WL_CONNECTED;
}

void getForecast() {
  forecastOk = false;
  if (!connectWifi()) { Serial.println("Wi-Fi failed, using local rule"); return; }

  WiFiClientSecure client;
  client.setInsecure();             // skip certificate check to keep it simple
  HTTPClient http;
  http.begin(client, URL);
  int code = http.GET();
  if (code != 200) { Serial.printf("HTTP error %d\n", code); http.end(); return; }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getString());
  http.end();
  if (err) { Serial.printf("JSON error: %s\n", err.c_str()); return; }

  JsonArray prob = doc["hourly"]["precipitation_probability"];
  JsonArray rain = doc["hourly"]["precipitation"];
  int maxProb = 0;
  float total = 0;
  for (size_t i = 0; i < prob.size(); i++) {
    int p = prob[i].as<int>();
    if (p > maxProb) maxProb = p;
    total += rain[i].as<float>();
  }
  fcProb = maxProb;
  fcMm = total;
  forecastOk = true;
  Serial.printf("Forecast next 3 h: max chance %d%%, total %.1f mm\n", fcProb, fcMm);
}

// ---------- decision ----------

Level decide() {
  if (wetPct >= RAIN_WET_PERCENT) return RAINING_NOW;
  if (forecastOk) {
    if (fcProb >= 70 || fcMm >= 1.0) return RAIN_LIKELY;
    if (fcProb >= 40 || fcMm >= 0.2) return RAIN_POSSIBLE;
    return NO_RAIN;
  }
  // offline fallback: local signs only
  if (!isnan(humPct) && humPct >= 90 && !isnan(pressTrend) && pressTrend <= -1.0) return RAIN_POSSIBLE;
  return NO_RAIN;
}

void updateLevel() {
  level = decide();
  // beep once when the level rises to RAIN LIKELY or RAINING NOW
  if (level >= RAIN_LIKELY && level > lastLevel) startBeeps(3);
  lastLevel = level;
}

// ---------- display ----------

void drawScreen() {
  char line[32];
  const char* alert = LEVEL_TEXT[level];

  u8g2.clearBuffer();

  u8g2.setFont(u8g2_font_6x10_tf);
  u8g2.drawStr(0, 9, "SERI ISKANDAR");
  const char* mode = forecastOk ? "ONLINE" : "LOCAL";
  u8g2.drawStr(128 - u8g2.getStrWidth(mode), 9, mode);
  u8g2.drawHLine(0, 12, 128);

  u8g2.setFont(u8g2_font_helvB12_tr);
  int w = u8g2.getStrWidth(alert);
  if (level != NO_RAIN) { u8g2.drawBox(0, 15, 128, 18); u8g2.setDrawColor(0); }
  u8g2.drawStr((128 - w) / 2, 30, alert);
  u8g2.setDrawColor(1);

  u8g2.setFont(u8g2_font_6x10_tf);
  if (forecastOk) snprintf(line, sizeof(line), "Next 3h: %d%%  %.1fmm", fcProb, fcMm);
  else            snprintf(line, sizeof(line), "Next 3h: no forecast");
  u8g2.drawStr(0, 44, line);

  if (bmeOk) snprintf(line, sizeof(line), "%.1fC %.0f%% %.0fhPa", tempC, humPct, pressHpa);
  else       snprintf(line, sizeof(line), "BME280 not found");
  u8g2.drawStr(0, 54, line);

  snprintf(line, sizeof(line), "Plate: %s %d%%", wetPct >= RAIN_WET_PERCENT ? "WET" : "DRY", wetPct);
  u8g2.drawStr(0, 64, line);

  u8g2.sendBuffer();
}

void selfTest() {
  setLights(false, false, true);  delay(400);
  setLights(false, true, false);  delay(400);
  setLights(true, false, false);  delay(400);
  setLights(false, false, false);
  tone(BUZZER, BEEP_HZ); delay(120); noTone(BUZZER);
}

// ---------- main ----------

void setup() {
  Serial.begin(115200);

  pinMode(RAIN_POWER, OUTPUT);
  digitalWrite(RAIN_POWER, LOW);
  pinMode(LED_RED, OUTPUT);
  pinMode(LED_YELLOW, OUTPUT);
  pinMode(LED_GREEN, OUTPUT);
  pinMode(BUZZER, OUTPUT);
  analogReadResolution(12);

  Wire.begin(PIN_SDA, PIN_SCL);     // one I2C bus shared by the OLED and the BME280
  Wire.setClock(100000);            // 100 kHz: more reliable with long jumper wires
  u8g2.setBusClock(100000);
  u8g2.begin();
  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x10_tf);
  u8g2.drawStr(0, 20, "Rain station");
  u8g2.drawStr(0, 36, "Self-test...");
  u8g2.sendBuffer();

  selfTest();

  bmeOk = bme.begin(0x76, &Wire) || bme.begin(0x77, &Wire);
  if (!bmeOk) Serial.println("BME280 not found: check wiring (it could also be a BMP280, which has no humidity)");

  u8g2.drawStr(0, 52, "Connecting Wi-Fi...");
  u8g2.sendBuffer();

  readBme();
  readRain();
  logPressure();
  getForecast();
  updateLevel();
  drawScreen();
  lastSensor = lastForecast = lastPressLog = millis();
}

void loop() {
  unsigned long now = millis();

  if (now - lastSensor >= SENSOR_EVERY_MS) {
    lastSensor = now;
    readBme();
    readRain();
    updateLevel();
    drawScreen();
    Serial.printf("T=%.1fC H=%.0f%% P=%.1fhPa trend3h=%.1f rainRaw=%d wet=%d%% -> %s\n",
                  tempC, humPct, pressHpa, pressTrend, rainRaw, wetPct, LEVEL_TEXT[level]);
  }

  if (now - lastPressLog >= PRESSURE_LOG_MS) {
    lastPressLog = now;
    logPressure();
  }

  if (now - lastForecast >= FORECAST_EVERY_MS) {
    lastForecast = now;
    getForecast();
    updateLevel();
    drawScreen();
  }

  updateLights(now);
  updateBuzzer(now);
}
