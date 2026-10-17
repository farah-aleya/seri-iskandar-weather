/*
  Seri Iskandar Rain-Alert Device (bonus hardware)
  ESP32 DevKit + 3 LEDs (green, yellow, red) + active buzzer

  Every 10 minutes the ESP32 asks Open-Meteo for the next 3 hours of
  rain probability and rainfall in Seri Iskandar, then lights:
    GREEN  : no rain expected      (max chance < 40% and total < 0.2 mm)
    YELLOW : rain possible         (max chance >= 40% or total >= 0.2 mm)
    RED    : rain very likely      (max chance >= 70% or total >= 1.0 mm)
             + buzzer beeps 3 times when the level first turns red
  Same rule as the web dashboard banner.

  If Wi-Fi or the API fails, the yellow LED blinks.

  Uses millis() instead of delay(), so the LEDs and buzzer stay responsive.

  Libraries: ArduinoJson (v7) by Benoit Blanchon (install from Library Manager)
  Board:     "ESP32 Dev Module" (Espressif ESP32 board package)

  Wiring (each LED through a 220 ohm resistor to GND):
    GPIO 25 -> green LED
    GPIO 26 -> yellow LED
    GPIO 27 -> red LED
    GPIO 14 -> buzzer (+), buzzer (-) -> GND
*/

#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <HTTPClient.h>
#include <ArduinoJson.h>

// ---- change these ----
const char* WIFI_SSID = "YOUR_WIFI_NAME";
const char* WIFI_PASS = "YOUR_WIFI_PASSWORD";
// ----------------------

const char* URL =
  "https://api.open-meteo.com/v1/forecast?latitude=4.36&longitude=100.97"
  "&hourly=precipitation_probability,precipitation&forecast_hours=3"
  "&timezone=Asia%2FKuala_Lumpur";

const int PIN_GREEN = 25, PIN_YELLOW = 26, PIN_RED = 27, PIN_BUZZER = 14;
const unsigned long CHECK_EVERY_MS = 10UL * 60UL * 1000UL;  // 10 minutes
const unsigned long BLINK_MS = 500;

enum Level { GREEN, YELLOW, RED, ERROR_STATE };
Level level = ERROR_STATE, lastLevel = ERROR_STATE;

unsigned long lastCheck = 0, lastBlink = 0, lastBeep = 0;
bool blinkOn = false;
int beepsLeft = 0;

void setLeds(bool g, bool y, bool r) {
  digitalWrite(PIN_GREEN, g);
  digitalWrite(PIN_YELLOW, y);
  digitalWrite(PIN_RED, r);
}

bool connectWifi() {
  if (WiFi.status() == WL_CONNECTED) return true;
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long start = millis();
  while (WiFi.status() != WL_CONNECTED && millis() - start < 15000) delay(250);
  return WiFi.status() == WL_CONNECTED;
}

Level checkForecast() {
  if (!connectWifi()) { Serial.println("Wi-Fi failed"); return ERROR_STATE; }

  WiFiClientSecure client;
  client.setInsecure();  // skip certificate check to keep the demo simple
  HTTPClient http;
  http.begin(client, URL);
  int code = http.GET();
  if (code != 200) { Serial.printf("HTTP error %d\n", code); http.end(); return ERROR_STATE; }

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, http.getStream());
  http.end();
  if (err) { Serial.printf("JSON error: %s\n", err.c_str()); return ERROR_STATE; }

  JsonArray prob = doc["hourly"]["precipitation_probability"];
  JsonArray rain = doc["hourly"]["precipitation"];
  int maxProb = 0;
  float totalMm = 0;
  for (size_t i = 0; i < prob.size(); i++) {
    maxProb = max(maxProb, prob[i].as<int>());
    totalMm += rain[i].as<float>();
  }
  Serial.printf("Next 3 h: max chance %d%%, total %.1f mm\n", maxProb, totalMm);

  if (maxProb >= 70 || totalMm >= 1.0) return RED;
  if (maxProb >= 40 || totalMm >= 0.2) return YELLOW;
  return GREEN;
}

void setup() {
  Serial.begin(115200);
  pinMode(PIN_GREEN, OUTPUT);
  pinMode(PIN_YELLOW, OUTPUT);
  pinMode(PIN_RED, OUTPUT);
  pinMode(PIN_BUZZER, OUTPUT);
  setLeds(true, true, true);   // lamp test
  delay(1000);
  setLeds(false, false, false);
  level = checkForecast();
  lastCheck = millis();
}

void loop() {
  unsigned long now = millis();

  // 1. check the forecast every 10 minutes
  if (now - lastCheck >= CHECK_EVERY_MS) {
    lastCheck = now;
    level = checkForecast();
  }

  // 2. start 3 beeps when the level changes to RED
  if (level == RED && lastLevel != RED) beepsLeft = 6;  // 3 on + 3 off
  lastLevel = level;

  // 3. show the level
  switch (level) {
    case GREEN:  setLeds(true, false, false); break;
    case YELLOW: setLeds(false, true, false); break;
    case RED:    setLeds(false, false, true); break;
    case ERROR_STATE:
      if (now - lastBlink >= BLINK_MS) { lastBlink = now; blinkOn = !blinkOn; }
      setLeds(false, blinkOn, false);
      break;
  }

  // 4. buzzer pattern without delay()
  if (beepsLeft > 0 && now - lastBeep >= 200) {
    lastBeep = now;
    digitalWrite(PIN_BUZZER, beepsLeft % 2 == 0 ? HIGH : LOW);
    beepsLeft--;
  }
  if (beepsLeft == 0) digitalWrite(PIN_BUZZER, LOW);
}
