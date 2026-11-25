// einkgen — Inkplate 10 firmware
//
// On every wake (timer or WAKE-button):
//   1. Join Wi-Fi.
//   2. POST {current_sha256} to the device-status Lambda's /wake route.
//      The server compares the reported sha against current/manifest.json.
//      If the device has caught up, /wake pops the generated-queue head
//      and re-points current at it. If the device hasn't drawn the
//      latest yet, /wake is a no-op and returns redraw. Both advance
//      and redraw responses carry the new image fields (image_url,
//      image_sha256, image_bytes) so we can feed them
//      straight into the image GET without a follow-up manifest fetch —
//      CloudFront caches current/manifest.json for 60–300 s and would
//      otherwise return the pre-advance manifest. /wake is best-effort:
//      any error is logged and the legacy manifest-fetch path runs as a
//      fallback, so a flaky network or a server rollback degrades cleanly.
//   3. If /wake didn't return manifest fields (queue_empty or HTTP error),
//      GET manifest.json from CloudFront.
//   4. If image_sha256 differs from NVS, OR battery has crossed the
//      low-battery threshold since last draw, redraw: drawBitmapFromBuffer()
//      + (optionally) drawBatteryOverlay() + display() + persist.
//   5. POST {battery, rssi, current_hash, fw_version} to the status route.
//   6. Deep-sleep for 3 hours, or until the WAKE button is pressed.
//
// loop() is empty — every wake is a fresh setup() run.

#include "Inkplate.h"
#include <HTTPClient.h>
#include <Preferences.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <ArduinoJson.h>
#include <mbedtls/sha256.h>
#include "esp_heap_caps.h"
#include "esp_sleep.h"

#include "secrets.h"

// ----- Tunables --------------------------------------------------------------

static const uint32_t WIFI_TIMEOUT_MS       = 20000;
static const uint32_t HTTP_TIMEOUT_MS       = 20000;

// Local device-poll cadence. The server may send next_check_after for
// older clients; it never controls this device's timer. Waiting three hours
// also on failures bounds retry traffic and battery draw during outages.
static const uint64_t POLL_INTERVAL_SECONDS = 3ULL * 60 * 60;

// WAKE button on Inkplate 10 is wired to GPIO 36 (SENSOR_VP, RTC-domain,
// input-only). Active-low: pressed = 0 V, released = 3.3 V via the on-board
// pull-up. Configured as an EXT0 deep-sleep wake source so a press during
// sleep triggers a fresh setup() and an immediate manifest poll + redraw.
static const gpio_num_t WAKE_BUTTON_GPIO = GPIO_NUM_36;

// 1200x825 8-bit indexed BMP is ~990 KB; cap at 2 MB so a runaway server
// can't try to push more than fits in PSRAM (4 MB total) leaving room for
// the framebuffer and stack.
static const size_t   IMAGE_MAX_BYTES       = 2 * 1024 * 1024;

// NVS namespace + keys
static const char *NVS_NAMESPACE    = "einkgen";
static const char *NVS_KEY_HASH     = "image_sha256";
// Tracks whether the frame currently on the panel includes the low-battery
// overlay, so we know to redraw when the battery crosses the threshold even
// if the manifest hash hasn't changed.
static const char *NVS_KEY_BATT_LOW = "batt_low";

// Battery linear map endpoints (volts)
static const float BATT_V_EMPTY = 3.3f;
static const float BATT_V_FULL  = 4.2f;

// Show the on-display low-battery overlay (iPhone-style icon + percentage in
// the top-right corner) when reported charge drops below this threshold.
// Kept low on purpose: the badge is a "go charge this" cue, not a status bar.
static const int BATT_LOW_THRESHOLD_PCT = 10;

// 8-level grayscale display mode (3-bit)
// INKPLATE_3BIT is defined in the Inkplate library.
Inkplate display(INKPLATE_3BIT);

// Manifest fields embedded in the /wake response (since v0.6.1.0).
// `hasManifest` is true only when the server returned all three image
// fields — i.e. action=advance or action=redraw on a fresh server.
// queue_empty, HTTP errors, and older servers all leave hasManifest
// false and the caller falls back to fetchManifest.
//
// Declared above any function that uses it because the Arduino IDE
// auto-generates function prototypes and inserts them near the top of
// the .ino, BEFORE the user-code definitions. A struct used in a
// prototype parameter list has to be visible at that injection point.
struct WakeResult {
    bool   hasManifest;
    String action;           // "advance" | "redraw" | "queue_empty" | ""
    String imageUrl;
    String imageHash;        // image_sha256
    int    imageBytes;
};

// ----- Helpers ---------------------------------------------------------------

static void sleepUntilNextPoll()
{
    Serial.printf("[sleep] deep-sleeping for %llu seconds (or until WAKE pressed)\n",
                  (unsigned long long)POLL_INTERVAL_SECONDS);
    Serial.flush();
    esp_sleep_enable_timer_wakeup(POLL_INTERVAL_SECONDS * 1000000ULL);
    esp_sleep_enable_ext0_wakeup(WAKE_BUTTON_GPIO, 0);  // 0 = low = pressed
    esp_deep_sleep_start();
}

static const char *wakeReasonName(esp_sleep_wakeup_cause_t cause)
{
    switch (cause) {
        case ESP_SLEEP_WAKEUP_EXT0:  return "wake-button";
        case ESP_SLEEP_WAKEUP_TIMER: return "timer";
        case ESP_SLEEP_WAKEUP_UNDEFINED: return "reset-or-power-on";
        default: return "other";
    }
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
static void drawBatteryOverlay(int pct);

static bool downloadVerifyAndDraw(const char *imageUrl, const char *expectedHash,
                                  bool overlayBattery, int batteryPct)
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

    // Render from the in-memory buffer. The Inkplate Arduino library exposes
    // BMP-from-buffer rendering on the public `image` member of the board
    // driver; it reads width/height from the BMP header itself, so no length
    // arg is needed.
    bool ok = display.image.drawBitmapFromBuffer(buf, /*x=*/0, /*y=*/0,
                                                 /*dither=*/false, /*invert=*/false);
    heap_caps_free(buf);
    if (!ok) {
        Serial.println("[draw] drawBitmapFromBuffer failed");
        return false;
    }
    if (overlayBattery) {
        Serial.printf("[overlay] low-battery badge: %d%%\n", batteryPct);
        drawBatteryOverlay(batteryPct);
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

// Small iPhone-status-bar-style battery badge in the top-right corner with
// the percentage inside the body. Drawn into the framebuffer AFTER drawing
// the image and BEFORE display(), so it ends up composited on the e-paper
// as a single refresh. Sits on a white card so it stays legible over dark
// image regions. Sized to be a "presence" cue from across the room and
// legible up close; the goal is "go charge this", not a live status readout.
// In INKPLATE_3BIT mode, color values are 0 (black) through 7 (white).
static void drawBatteryOverlay(int pct)
{
    const uint16_t INK_BLACK = 0;
    const uint16_t INK_WHITE = 7;

    const int margin   = 18;  // distance from panel edges
    const int bodyW    = 80;  // battery body width
    const int bodyH    = 32;  // battery body height
    const int border   = 2;   // outline thickness
    const int capW     = 5;   // positive-terminal nub width
    const int capH     = 14;  // positive-terminal nub height
    const int cardPad  = 6;   // white card padding around the icon

    const int bodyX = display.width() - margin - bodyW - capW;
    const int bodyY = margin;

    display.fillRect(bodyX - cardPad, bodyY - cardPad,
                     bodyW + capW + 2 * cardPad, bodyH + 2 * cardPad, INK_WHITE);

    // Battery body outline — draw nested rects to fake a thick border.
    for (int i = 0; i < border; ++i) {
        display.drawRect(bodyX + i, bodyY + i,
                         bodyW - 2 * i, bodyH - 2 * i, INK_BLACK);
    }

    // Positive-terminal nub on the right.
    display.fillRect(bodyX + bodyW, bodyY + (bodyH - capH) / 2,
                     capW, capH, INK_BLACK);

    // Proportional fill bar on the left of the interior. At <10% the bar is
    // narrow enough that the centred percentage text below stays on white.
    const int innerX = bodyX + border;
    const int innerY = bodyY + border;
    const int innerW = bodyW - 2 * border;
    const int innerH = bodyH - 2 * border;
    int fillW = (innerW * pct) / 100;
    if (fillW > 0) {
        display.fillRect(innerX, innerY, fillW, innerH, INK_BLACK);
    }

    // Percentage text centred inside the body. setTextColor(fg, bg) repaints
    // each glyph cell, so the text stays readable even where it overlaps the
    // fill bar.
    char label[8];
    snprintf(label, sizeof(label), "%d%%", pct);

    const int textSize = 2;                          // Adafruit-GFX scale
    const int textW    = (int)strlen(label) * 6 * textSize;
    const int textH    = 8 * textSize;

    display.setTextSize(textSize);
    display.setTextColor(INK_BLACK, INK_WHITE);
    display.setCursor(bodyX + (bodyW - textW) / 2,
                      bodyY + (bodyH - textH) / 2);
    display.print(label);
}

// Build the /wake URL from DEVICE_STATUS_URL by stripping any trailing
// slash and appending "/wake". Avoids forcing a secrets.h change when
// upgrading firmware on a device flashed before /wake existed.
static String wakeUrl()
{
    String u = DEVICE_STATUS_URL;
    while (u.length() > 0 && u.charAt(u.length() - 1) == '/') {
        u.remove(u.length() - 1);
    }
    u += "/wake";
    return u;
}

// Best-effort wake POST. Tells the server "the panel is currently
// showing <sha>". On HTTP 200 the response carries `action` and
// (for advance/redraw) the manifest fields the firmware would
// otherwise have to fetch from CloudFront — `out` is populated so
// the caller can skip the follow-up GET. Returns true on HTTP 200;
// the caller never hard-fails on /wake — a failed call just means
// `out.hasManifest = false` and we drop back to the legacy
// fetchManifest path.
static bool postWake(const char *current_hash, WakeResult &out)
{
    out.hasManifest    = false;
    out.action         = "";
    out.imageUrl       = "";
    out.imageHash      = "";
    out.imageBytes     = 0;

    String url = wakeUrl();
    Serial.printf("[wake] POST %s\n", url.c_str());
    WiFiClientSecure client;
    client.setInsecure();  // TODO: pin cert.

    HTTPClient http;
    http.setTimeout(HTTP_TIMEOUT_MS);
    if (!http.begin(client, url)) {
        Serial.println("[wake] http.begin failed");
        return false;
    }
    http.addHeader("Content-Type", "application/json");
    http.addHeader("X-Device-Token", DEVICE_STATUS_TOKEN);

    JsonDocument body;
    // Empty string is fine — server treats it as "unknown sha" and
    // falls into the advance branch (helpful for fresh deploys where
    // NVS hasn't seen anything yet).
    body["current_sha256"] = current_hash ? current_hash : "";

    String payload;
    serializeJson(body, payload);

    int code = http.POST(payload);
    if (code != 200) {
        Serial.printf("[wake] non-OK HTTP %d, body=%s\n", code,
                      http.getString().c_str());
        http.end();
        return false;
    }
    String resp = http.getString();
    Serial.printf("[wake] OK %s\n", resp.c_str());
    http.end();

    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, resp);
    if (err) {
        Serial.printf("[wake] response parse error: %s\n", err.c_str());
        return true;  // HTTP succeeded — caller falls back to fetchManifest.
    }
    const char *action      = doc["action"]           | "";
    const char *imageUrl    = doc["image_url"]        | "";
    const char *imageHash   = doc["image_sha256"]     | "";
    int         imageBytes  = doc["image_bytes"]      | 0;
    out.action = action;
    if (imageUrl[0] && imageHash[0] && imageBytes > 0) {
        out.imageUrl       = imageUrl;
        out.imageHash      = imageHash;
        out.imageBytes     = imageBytes;
        out.hasManifest    = true;
    }
    return true;
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
    Serial.printf("[boot] wake cause: %s\n",
                  wakeReasonName(esp_sleep_get_wakeup_cause()));

    display.begin();
    display.setRotation(0);  // native landscape, 1200x825

    if (!joinWiFi()) {
        Serial.println("[wifi] joining failed, sleeping until the next poll");
        sleepUntilNextPoll();
        return;  // unreachable
    }

    // Hit /wake BEFORE fetching the manifest. The server uses the sha
    // we report to decide whether to pop the next pre-rendered image
    // (advance) or just leave the manifest as-is. On HTTP 200 with
    // action=advance/redraw, the response embeds image_url +
    // image_sha256 + image_bytes so we can skip the
    // follow-up GET current/manifest.json (which would hit CloudFront's
    // 60–300 s cache and reliably return the pre-advance manifest after
    // an advance). A failed /wake or a queue_empty response drops us
    // back to the legacy fetchManifest path so a server rollback
    // degrades cleanly.
    Preferences nvs;
    nvs.begin(NVS_NAMESPACE, /*readOnly=*/false);
    String storedHash = nvs.getString(NVS_KEY_HASH, "");
    bool   wasLow     = nvs.getBool(NVS_KEY_BATT_LOW, false);

    WakeResult wake;
    (void)postWake(storedHash.c_str(), wake);

    // Manifest fields we'll use below. Either come from the /wake
    // response (preferred — fresh and cache-bypassing) or from a
    // follow-up fetchManifest call (legacy fallback).
    String imageUrl;
    String imageHash;

    if (wake.hasManifest && wake.action != "queue_empty") {
        Serial.printf("[wake] using embedded manifest from action=%s\n",
                      wake.action.c_str());
        imageUrl       = wake.imageUrl;
        imageHash      = wake.imageHash;
    } else {
        Serial.printf("[wake] no embedded manifest (action=%s) — falling back to GET\n",
                      wake.action.c_str());
        String body;
        if (!fetchManifest(body)) {
            nvs.end();
            sleepUntilNextPoll();
            return;
        }
        JsonDocument doc;
        DeserializationError err = deserializeJson(doc, body);
        if (err) {
            Serial.printf("[manifest] parse error: %s\n", err.c_str());
            nvs.end();
            sleepUntilNextPoll();
            return;
        }
        imageUrl       = (const char *)(doc["image_url"]        | "");
        imageHash      = (const char *)(doc["image_sha256"]     | "");
    }

    if (imageUrl.length() == 0 || imageHash.length() == 0) {
        Serial.println("[manifest] missing image_url or image_sha256");
        nvs.end();
        sleepUntilNextPoll();
        return;
    }
    Serial.printf("[manifest] image_url=%s\n", imageUrl.c_str());
    Serial.printf("[manifest] image_sha256=%s\n", imageHash.c_str());

    // Read battery before deciding whether to redraw, so we can also trigger
    // a refresh when charge crosses the low-battery threshold (the overlay
    // needs to appear or disappear even if the manifest hash hasn't changed).
    float battV   = (float)display.readBattery();
    int   battPct = batteryPercent(battV);
    bool  isLow   = (battPct < BATT_LOW_THRESHOLD_PCT);

    // storedHash + wasLow were read into NVS already (before postWake);
    // just compare against the manifest now.
    bool   hashChanged = (storedHash != imageHash);
    bool   battChanged = (isLow != wasLow);
    bool   needsRedraw = hashChanged || battChanged;
    Serial.printf("[hash] stored=\"%s\" new=\"%s\" changed=%d\n",
                  storedHash.c_str(), imageHash.c_str(), hashChanged ? 1 : 0);
    Serial.printf("[batt] %d%% low=%d wasLow=%d changed=%d\n",
                  battPct, isLow ? 1 : 0, wasLow ? 1 : 0, battChanged ? 1 : 0);

    // currentHash tracks what we're ACTUALLY showing right now. It only
    // advances to the manifest's claimed hash if download + SHA verify +
    // draw all succeed. On any failure we keep reporting storedHash so the
    // server's Device tab doesn't lie about what's on screen.
    String currentHash = storedHash;

    if (needsRedraw) {
        Serial.println("[draw] downloading and verifying image");
        if (downloadVerifyAndDraw(imageUrl.c_str(), imageHash.c_str(), isLow, battPct)) {
            nvs.putString(NVS_KEY_HASH, imageHash.c_str());
            nvs.putBool(NVS_KEY_BATT_LOW, isLow);
            currentHash = imageHash;
            Serial.println("[draw] OK, hash + battery state persisted");
        } else {
            Serial.println("[draw] failed — leaving previous frame on screen");
        }
    } else {
        Serial.println("[draw] hash + battery state unchanged, skipping redraw");
    }
    nvs.end();

    // RSSI + status POST. Report what we're actually showing.
    int rssi = WiFi.RSSI();
    Serial.printf("[status] battery=%.2fV (%d%%) rssi=%d\n", battV, battPct, rssi);
    postStatus(battV, battPct, rssi, currentHash.c_str());

    WiFi.disconnect(true, true);
    sleepUntilNextPoll();
}

void loop()
{
    // Empty: every cycle is a fresh setup() after deep sleep.
}
