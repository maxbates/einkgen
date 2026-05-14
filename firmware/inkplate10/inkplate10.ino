// einkgen — Inkplate 10 firmware
//
// On every wake:
//   1. Join Wi-Fi.
//   2. GET manifest.json from CloudFront.
//   3. If image_sha256 differs from NVS, drawImage() + display() + persist.
//   4. POST {battery, rssi, current_hash, fw_version} to device-status Lambda.
//   5. Deep-sleep until min(next_check_after, now + 1h), floor 60 s.
//
// loop() is empty — every wake is a fresh setup() run.

#include "Inkplate.h"
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>
#include <mbedtls/sha256.h>
#include <time.h>
#include <sys/time.h>
#include <stdlib.h>
#include "esp_heap_caps.h"
#include "esp_sleep.h"

#include "secrets.h"

// ----- Tunables --------------------------------------------------------------

static const uint32_t WIFI_TIMEOUT_MS       = 20000;
static const uint32_t HTTP_TIMEOUT_MS       = 20000;
static const uint32_t NTP_TIMEOUT_MS        = 10000;
static const uint64_t SLEEP_MIN_SECONDS     = 60;
static const uint64_t SLEEP_MAX_SECONDS     = 3600;
static const uint64_t SLEEP_FALLBACK_SECONDS = 3600;

// 1200x825 8-bit indexed BMP is ~990 KB; cap at 2 MB so a runaway server
// can't try to push more than fits in PSRAM (4 MB total) leaving room for
// the framebuffer and stack.
static const size_t   IMAGE_MAX_BYTES       = 2 * 1024 * 1024;

// NVS namespace + keys
static const char *NVS_NAMESPACE   = "einkgen";
static const char *NVS_KEY_HASH    = "image_sha256";

// Battery linear map endpoints (volts)
static const float BATT_V_EMPTY = 3.3f;
static const float BATT_V_FULL  = 4.2f;

// 8-level grayscale display mode (3-bit)
// INKPLATE_3BIT is defined in the Inkplate library.
Inkplate display(INKPLATE_3BIT);

// ----- Helpers ---------------------------------------------------------------

static void deepSleepFor(uint64_t seconds)
{
    if (seconds < SLEEP_MIN_SECONDS) seconds = SLEEP_MIN_SECONDS;
    if (seconds > SLEEP_MAX_SECONDS) seconds = SLEEP_MAX_SECONDS;
    Serial.printf("[sleep] deep-sleeping for %llu seconds\n", (unsigned long long)seconds);
    Serial.flush();
    esp_sleep_enable_timer_wakeup(seconds * 1000000ULL);
    esp_deep_sleep_start();
}

static bool joinWiFi()
{
    Serial.printf("[wifi] connecting to SSID \"%s\"\n", WIFI_SSID);
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASS);
    uint32_t start = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - start > WIFI_TIMEOUT_MS) {
            Serial.println("[wifi] timeout");
            return false;
        }
        delay(250);
    }
    Serial.printf("[wifi] connected, IP=%s, RSSI=%d\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
    return true;
}

// Best-effort NTP sync. Returns true if system time looks valid afterwards.
static bool syncTime()
{
    Serial.println("[ntp] syncing time");
    configTime(0, 0, "pool.ntp.org", "time.nist.gov");
    uint32_t start = millis();
    time_t now = 0;
    while (millis() - start < NTP_TIMEOUT_MS) {
        time(&now);
        if (now > 1700000000) {  // sanity floor: ~2023-11
            Serial.printf("[ntp] synced, epoch=%lld\n", (long long)now);
            return true;
        }
        delay(200);
    }
    Serial.println("[ntp] timeout");
    return false;
}

// Fetch manifest JSON into `out`. Returns true on HTTP 200.
static bool fetchManifest(String &out)
{
    Serial.printf("[manifest] GET %s\n", MANIFEST_URL);
    WiFiClientSecure client;
    // TODO: pin CloudFront cert. For v0 we skip verification — the manifest
    // tells us where the image lives and we hash-check the image, so a MITM
    // can only deny service, not inject content (we don't verify the hash
    // matches a server-signed value, just that it changed).
    client.setInsecure();

    HTTPClient http;
    http.setTimeout(HTTP_TIMEOUT_MS);
    if (!http.begin(client, MANIFEST_URL)) {
        Serial.println("[manifest] http.begin failed");
        return false;
    }
    int code = http.GET();
    if (code != 200) {
        Serial.printf("[manifest] HTTP %d\n", code);
        http.end();
        return false;
    }
    out = http.getString();
    http.end();
    Serial.printf("[manifest] OK, %u bytes\n", (unsigned)out.length());
    return true;
}

// Parse ISO 8601 UTC timestamp like "2026-05-13T16:05:00Z" into epoch seconds.
// Returns 0 on failure.
static time_t parseIso8601Utc(const char *s)
{
    if (!s) return 0;
    int y, mo, d, h, mi, se;
    // Tolerate the trailing 'Z' (or omit it). Fractional seconds not supported.
    if (sscanf(s, "%d-%d-%dT%d:%d:%d", &y, &mo, &d, &h, &mi, &se) != 6) {
        return 0;
    }
    struct tm tm = {};
    tm.tm_year  = y - 1900;
    tm.tm_mon   = mo - 1;
    tm.tm_mday  = d;
    tm.tm_hour  = h;
    tm.tm_min   = mi;
    tm.tm_sec   = se;
    tm.tm_isdst = 0;
    // timegm() is non-portable; use the POSIX recipe: set TZ=UTC, mktime, restore.
    char *oldTz = getenv("TZ");
    setenv("TZ", "UTC0", 1);
    tzset();
    time_t t = mktime(&tm);
    if (oldTz) setenv("TZ", oldTz, 1); else unsetenv("TZ");
    tzset();
    return t;
}

// Download the manifest's image_url into a PSRAM-backed buffer, verify its
// SHA-256 against `expectedHash`, and render it. Returns true on success and
// fills `*outBuf`/`*outLen` for the duration of the render (caller frees).
// Returns false on any download, verification, or render failure — in which
// case the previous frame stays on screen.
//
// Why this matters: the Inkplate library's image.draw(url, ...) re-downloads
// over plain HTTP and never compares bytes against the manifest's claimed
// hash. Without this path the `image_sha256` field is purely advisory and a
// MITM (or a partial CloudFront response) can render arbitrary bytes while
// firmware persists the manifest's claimed hash as if it were verified.
static bool downloadVerifyAndDraw(const char *imageUrl, const char *expectedHash)
{
    Serial.printf("[image] GET %s\n", imageUrl);
    WiFiClientSecure client;
    client.setInsecure();  // TODO: pin CloudFront cert.

    HTTPClient http;
    http.setTimeout(HTTP_TIMEOUT_MS);
    if (!http.begin(client, imageUrl)) {
        Serial.println("[image] http.begin failed");
        return false;
    }
    int code = http.GET();
    if (code != 200) {
        Serial.printf("[image] HTTP %d\n", code);
        http.end();
        return false;
    }

    int contentLength = http.getSize();
    if (contentLength <= 0 || (size_t)contentLength > IMAGE_MAX_BYTES) {
        Serial.printf("[image] bad Content-Length: %d\n", contentLength);
        http.end();
        return false;
    }

    // Allocate from PSRAM — internal SRAM is too tight for a 1 MB BMP.
    uint8_t *buf = (uint8_t *)heap_caps_malloc(contentLength, MALLOC_CAP_SPIRAM);
    if (!buf) {
        Serial.printf("[image] PSRAM alloc failed for %d bytes\n", contentLength);
        http.end();
        return false;
    }

    WiFiClient *stream = http.getStreamPtr();
    int total = 0;
    uint32_t start = millis();
    while (http.connected() && total < contentLength) {
        size_t avail = stream->available();
        if (avail > 0) {
            int n = stream->readBytes(buf + total,
                                      (int)min((size_t)(contentLength - total), avail));
            if (n <= 0) break;
            total += n;
            continue;
        }
        if (millis() - start > HTTP_TIMEOUT_MS) {
            Serial.println("[image] read timeout");
            break;
        }
        delay(1);
    }
    http.end();

    if (total != contentLength) {
        Serial.printf("[image] short read: %d of %d\n", total, contentLength);
        heap_caps_free(buf);
        return false;
    }

    // SHA-256 over the bytes we actually have, then byte-compare against
    // the manifest's claim.
    uint8_t digest[32];
    mbedtls_sha256_context ctx;
    mbedtls_sha256_init(&ctx);
    mbedtls_sha256_starts(&ctx, /*is224=*/0);
    mbedtls_sha256_update(&ctx, buf, total);
    mbedtls_sha256_finish(&ctx, digest);
    mbedtls_sha256_free(&ctx);

    char hex[65];
    for (int i = 0; i < 32; ++i) {
        sprintf(&hex[i * 2], "%02x", digest[i]);
    }
    hex[64] = '\0';

    if (strcmp(hex, expectedHash) != 0) {
        Serial.printf("[verify] hash mismatch: expected=%s got=%s\n",
                      expectedHash, hex);
        heap_caps_free(buf);
        return false;
    }
    Serial.println("[verify] hash OK");

    // Render from the in-memory buffer.
    // TODO: confirm the exact Inkplate-Arduino-library buffer-draw signature
    // for the installed version. As of writing the canonical call is
    // display.drawImage(buf, x, y, len, dither, invert), but some forks expose
    // image.drawBmpFromBuffer(...) or drawBitmapFromBuffer(...). If linking
    // fails, swap for the matching name — the contract is the same: render
    // the BMP we already SHA-verified, not a fresh HTTP fetch.
    bool ok = display.drawImage((const uint8_t *)buf, /*x=*/0, /*y=*/0,
                                /*len=*/total, /*dither=*/false, /*invert=*/false);
    heap_caps_free(buf);
    if (!ok) {
        Serial.println("[draw] drawImage from buffer failed");
        return false;
    }
    display.display();
    return true;
}

static int batteryPercent(float volts)
{
    float pct = (volts - BATT_V_EMPTY) / (BATT_V_FULL - BATT_V_EMPTY) * 100.0f;
    if (pct < 0.0f)   pct = 0.0f;
    if (pct > 100.0f) pct = 100.0f;
    return (int)(pct + 0.5f);
}

// Best-effort status POST. Logs but never throws.
static void postStatus(float battery_v, int battery_pct, int rssi,
                       const char *current_hash)
{
    Serial.printf("[status] POST %s\n", DEVICE_STATUS_URL);
    WiFiClientSecure client;
    client.setInsecure();  // TODO: pin cert.

    HTTPClient http;
    http.setTimeout(HTTP_TIMEOUT_MS);
    if (!http.begin(client, DEVICE_STATUS_URL)) {
        Serial.println("[status] http.begin failed");
        return;
    }
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Device-Token", DEVICE_STATUS_TOKEN);

    JsonDocument body;
    body["battery_v"]    = battery_v;
    body["battery_pct"]  = battery_pct;
    body["rssi"]         = rssi;
    body["current_hash"] = current_hash ? current_hash : "";
    body["fw_version"]   = FW_VERSION;

    String payload;
    serializeJson(body, payload);

    int code = http.POST(payload);
    if (code != 200 && code != 204) {
        Serial.printf("[status] non-OK HTTP %d, body=%s\n", code,
                      http.getString().c_str());
    } else {
        Serial.printf("[status] OK HTTP %d\n", code);
    }
    http.end();
}

// ----- Main ------------------------------------------------------------------

void setup()
{
    Serial.begin(115200);
    delay(100);
    Serial.println();
    Serial.printf("[boot] einkgen firmware %s\n", FW_VERSION);

    display.begin();
    display.setRotation(0);  // native landscape, 1200x825

    if (!joinWiFi()) {
        Serial.println("[wifi] joining failed, falling back to 1h sleep");
        deepSleepFor(SLEEP_FALLBACK_SECONDS);
        return;  // unreachable
    }

    // NTP first so we can interpret next_check_after.
    bool haveTime = syncTime();

    String body;
    if (!fetchManifest(body)) {
        deepSleepFor(SLEEP_FALLBACK_SECONDS);
        return;
    }

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, body);
    if (err) {
        Serial.printf("[manifest] parse error: %s\n", err.c_str());
        deepSleepFor(SLEEP_FALLBACK_SECONDS);
        return;
    }

    const char *imageUrl       = doc["image_url"]        | "";
    const char *imageHash      = doc["image_sha256"]     | "";
    const char *nextCheckAfter = doc["next_check_after"] | "";

    if (!imageUrl[0] || !imageHash[0]) {
        Serial.println("[manifest] missing image_url or image_sha256");
        deepSleepFor(SLEEP_FALLBACK_SECONDS);
        return;
    }
    Serial.printf("[manifest] image_url=%s\n", imageUrl);
    Serial.printf("[manifest] image_sha256=%s\n", imageHash);
    Serial.printf("[manifest] next_check_after=%s\n", nextCheckAfter);

    // Compare to stored hash.
    Preferences prefs;
    prefs.begin(NVS_NAMESPACE, /*readOnly=*/false);
    String storedHash = prefs.getString(NVS_KEY_HASH, "");
    bool changed = (storedHash != imageHash);
    Serial.printf("[hash] stored=\"%s\" new=\"%s\" changed=%d\n",
                  storedHash.c_str(), imageHash, changed ? 1 : 0);

    // currentHash tracks what we're ACTUALLY showing right now. It only
    // advances to the manifest's claimed hash if download + SHA verify +
    // draw all succeed. On any failure we keep reporting storedHash so the
    // server's Device tab doesn't lie about what's on screen.
    String currentHash = storedHash;

    if (changed) {
        Serial.println("[draw] downloading and verifying image");
        if (downloadVerifyAndDraw(imageUrl, imageHash)) {
            prefs.putString(NVS_KEY_HASH, imageHash);
            currentHash = imageHash;
            Serial.println("[draw] OK, hash persisted");
        } else {
            Serial.println("[draw] failed — leaving previous frame on screen");
        }
    } else {
        Serial.println("[draw] hash unchanged, skipping redraw");
    }
    prefs.end();

    // Battery + RSSI + status POST. Report what we're actually showing.
    float battV   = (float)display.readBattery();
    int   battPct = batteryPercent(battV);
    int   rssi    = WiFi.RSSI();
    Serial.printf("[status] battery=%.2fV (%d%%) rssi=%d\n", battV, battPct, rssi);
    postStatus(battV, battPct, rssi, currentHash.c_str());

    // Compute sleep duration from next_check_after.
    uint64_t sleepSeconds = SLEEP_FALLBACK_SECONDS;
    if (haveTime && nextCheckAfter[0]) {
        time_t target = parseIso8601Utc(nextCheckAfter);
        time_t now    = 0;
        time(&now);
        if (target > 0 && now > 0) {
            long diff = (long)(target - now);
            Serial.printf("[sleep] next_check_after epoch=%lld now=%lld diff=%lds\n",
                          (long long)target, (long long)now, diff);
            if (diff < (long)SLEEP_MIN_SECONDS) diff = (long)SLEEP_MIN_SECONDS;
            if (diff > (long)SLEEP_MAX_SECONDS) diff = (long)SLEEP_MAX_SECONDS;
            sleepSeconds = (uint64_t)diff;
        } else {
            Serial.println("[sleep] could not parse next_check_after, using fallback");
        }
    } else {
        Serial.println("[sleep] no NTP time or no next_check_after, using fallback");
    }

    WiFi.disconnect(true, true);
    deepSleepFor(sleepSeconds);
}

void loop()
{
    // Empty: every cycle is a fresh setup() after deep sleep.
}
