#pragma once

// Doubles for APIs used by the sketch; neither hardware nor JSON itself is
// tested here. The values below model HTTP/JSON outcomes in setup().
#include <algorithm>
#include <cassert>
#include <cstddef>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <utility>
class String : public std::string {
public:
    using std::string::string;
    String(const std::string &s) : std::string(s) {}
    char charAt(size_t i) const { return at(i); }
    void remove(size_t i) { erase(i); }
};
using std::min;

#define WIFI_SSID "test-ssid"
#define WIFI_PASS "test-pass"
#define MANIFEST_URL "https://example.invalid/manifest"
#define DEVICE_STATUS_URL "https://example.invalid/status"
#define DEVICE_STATUS_TOKEN "test-token"
#define FW_VERSION "host-test"

enum { INKPLATE_3BIT, WIFI_STA, WL_CONNECTED, MALLOC_CAP_SPIRAM };
enum gpio_num_t { GPIO_NUM_36 = 36 };
enum esp_sleep_wakeup_cause_t {
    ESP_SLEEP_WAKEUP_UNDEFINED, ESP_SLEEP_WAKEUP_EXT0, ESP_SLEEP_WAKEUP_TIMER
};

struct Scenario {
    esp_sleep_wakeup_cause_t cause = ESP_SLEEP_WAKEUP_TIMER;
    bool online = true;
    int wakeCode = 200;
    int manifestCode = 200;
    int statusCode = 200;
    bool badManifest = false;
    bool emptyManifest = false;
    bool queueEmpty = false;
    bool redraw = false;
    const char *hint = "2099-01-01T00:00:00Z";
    uint32_t ms = 0;
    uint64_t timer = 0;
    int pin = -1;
    int level = -1;
    int sleepCalls = 0;
    int wakePosts = 0;
    int manifestGets = 0;
    int imageGets = 0;
    int statusPosts = 0;
    int frames = 0;
    std::string log;
} state;

uint32_t millis() { return state.ms; }
void delay(uint32_t ms) { state.ms += ms; }

struct Logger {
    void begin(int) {}
    void flush() {}
    void println() { state.log += "\n"; }
    void println(const char *str) { state.log += str; state.log += "\n"; }
    template<class... Args> void printf(const char *fmt, Args... args) {
        char buffer[512];
        std::snprintf(buffer, sizeof(buffer), fmt, args...);
        state.log += buffer;
    }
} Serial;

class Inkplate {
public:
    explicit Inkplate(int) {}
    struct Image {
        bool drawBitmapFromBuffer(uint8_t*, int, int, bool, bool) { return true; }
    } image;
    void begin() {}
    void setRotation(int) {}
    void display() { ++state.frames; }
    double readBattery() { return 4.1; }
    int width() { return 1200; }
    template<class... Args> void fillRect(Args...) {}
    template<class... Args> void drawRect(Args...) {}
    template<class... Args> void setTextColor(Args...) {}
    template<class... Args> void setCursor(Args...) {}
    void setTextSize(int) {}
    void print(const char*) {}
};

struct Preferences {
    void begin(const char*, bool) {}
    void end() {}
    String getString(const char*, const char*) {
        return state.redraw ? "" : std::string(64, '0');
    }
    bool getBool(const char*, bool) { return false; }
    void putString(const char*, const char*) {}
    void putBool(const char*, bool) {}
};

struct WiFiClient {
    size_t available() { return 1; }
    int readBytes(uint8_t *out, int n) { std::memset(out, 0, n); return n; }
};
struct WiFiClientSecure { void setInsecure() {} };

struct WiFiAPI {
    struct Ip { String toString() { return "127.0.0.1"; } };
    Ip localIP() { return {}; }
    void mode(int) {}
    void begin(const char*, const char*) {}
    int status() { return state.online ? WL_CONNECTED : -1; }
    int RSSI() { return -50; }
    void disconnect(bool, bool) {}
} WiFi;

struct HTTPClient {
    String url;
    WiFiClient stream;
    void setTimeout(int) {}
    bool begin(WiFiClientSecure&, const String &where) { url = where; return true; }
    void addHeader(const char*, const char*) {}
    int POST(const String&) {
        if (url == DEVICE_STATUS_URL "/wake") { ++state.wakePosts; return state.wakeCode; }
        assert(url == DEVICE_STATUS_URL);
        ++state.statusPosts;
        return state.statusCode;
    }
    int GET() {
        if (url == MANIFEST_URL) { ++state.manifestGets; return state.manifestCode; }
        assert(url == "https://example.invalid/image");
        ++state.imageGets;
        return 200;
    }
    int getSize() { return 1; }
    bool connected() { return true; }
    WiFiClient* getStreamPtr() { return &stream; }
    String getString() { return url; }
    void end() {}
};

struct JsonField {
    const char *value = nullptr;
    int number = 0;
    const char *operator|(const char *fallback) const { return value ? value : fallback; }
    int operator|(int fallback) const { return number ? number : fallback; }
    template<class T> void operator=(T) {}
};
struct JsonDocument {
    bool manifestGet = false;
    JsonField operator[](const char *key) {
        const String k(key);
        if (k == "action") return {state.queueEmpty ? "queue_empty" : "advance"};
        if (k == "next_check_after") return {state.hint};
        if ((manifestGet && state.emptyManifest) || (!manifestGet && state.queueEmpty))
            return {};
        if (k == "image_url") return {"https://example.invalid/image"};
        if (k == "image_sha256") {
            static const std::string hash(64, '0');
            return {hash.c_str()};
        }
        if (k == "image_bytes") return {nullptr, 1};
        return {};
    }
};
struct DeserializationError {
    bool failed;
    explicit operator bool() const { return failed; }
    const char *c_str() const { return "bad manifest"; }
};
DeserializationError deserializeJson(JsonDocument &doc, const String &body) {
    doc.manifestGet = body == MANIFEST_URL;
    return {doc.manifestGet && state.badManifest};
}
void serializeJson(const JsonDocument&, String&) {}

using mbedtls_sha256_context = int;
void mbedtls_sha256_init(mbedtls_sha256_context*) {}
void mbedtls_sha256_starts(mbedtls_sha256_context*, int) {}
void mbedtls_sha256_update(mbedtls_sha256_context*, const uint8_t*, int) {}
void mbedtls_sha256_finish(mbedtls_sha256_context*, uint8_t *dest) { std::memset(dest, 0, 32); }
void mbedtls_sha256_free(mbedtls_sha256_context*) {}
void* heap_caps_malloc(size_t size, int) { return std::malloc(size); }
void heap_caps_free(void *p) { std::free(p); }

esp_sleep_wakeup_cause_t esp_sleep_get_wakeup_cause() { return state.cause; }
void esp_sleep_enable_timer_wakeup(uint64_t micros) { state.timer = micros; }
void esp_sleep_enable_ext0_wakeup(gpio_num_t pin, int level) {
    state.pin = pin;
    state.level = level;
}
void esp_deep_sleep_start() { ++state.sleepCalls; }
