# Inkplate 10 firmware

The sketch that runs on the device. On every wake it joins Wi-Fi, fetches
`manifest.json`, redraws the panel if the image hash changed (or if the
battery has crossed the low-battery threshold, so the overlay can appear or
disappear), reports status to the device-status Lambda, then deep-sleeps
until the next check — or until you press the **WAKE** button to force a
refresh.

When reported charge drops below `BATT_LOW_THRESHOLD_PCT` (default 10%) a
small iPhone-status-bar-style battery badge with the percentage is composited
into the top-right corner of the rendered frame — meant as a "go charge this"
cue, sized so its presence reads from across the room and the number reads up
close. The image pipeline is unchanged, the overlay only exists on the panel.

## On-demand refresh

The WAKE button on the back of the Inkplate is wired as an EXT0 deep-sleep
wake source. Pressing it while the device is asleep ends the sleep early
and triggers a fresh `setup()` run, which always polls the manifest and
redraws if the hash changed. Use it when you've just enqueued a prompt and
don't want to wait out the rest of the current sleep window.

Serial output prints the wake cause on every boot (`wake-button` / `timer`
/ `reset-or-power-on`) so it's clear which path you're on.

See [ARCHITECTURE.md](../../ARCHITECTURE.md) for the system overview
(§1 device, §7 manifest, §11 firmware spec).

## Hardware

- [Inkplate 10](https://soldered.com/product/inkplate-10/) — 9.7" e-paper,
  1200×825, 8-level grayscale, ESP32.
- USB-C cable for flashing.
- (Optional) Li-Ion battery (3000 mAh is the Soldered-recommended option).

## Toolchain

Arduino IDE 2.x.

1. **Add Soldered's ESP32 board package** in *Preferences → Additional boards
   manager URLs*:
   ```
   https://raw.githubusercontent.com/SolderedElectronics/Dasduino-Board-Definitions-for-Arduino-IDE/master/package_Dasduino_Boards_index.json
   ```
   Then *Tools → Board → Boards Manager* and install **"Dasduino Boards"**.

2. **Install libraries** via *Sketch → Include Library → Manage Libraries*:
   - `Inkplate Arduino library` (by Soldered) — provides the `Inkplate` class.
   - `ArduinoJson` v7.x (by Benoit Blanchon) — manifest + status payload.

   The ESP32 core (which ships `WiFi`, `WiFiClientSecure`, `HTTPClient`,
   `Preferences`) comes with the Dasduino board package above.

## Build & flash

1. `cp secrets.h.example secrets.h` and fill in:
   - `WIFI_SSID`, `WIFI_PASS`
   - `MANIFEST_URL` — your CloudFront URL ending in `/current/manifest.json`
   - `DEVICE_STATUS_URL` — the device-status API Gateway endpoint
     (the `DeviceStatusUrl` value from `infra/cdk-outputs.json`)
   - `DEVICE_STATUS_TOKEN` — the shared secret stored in AWS Secrets Manager
2. Open `inkplate10.ino` in Arduino IDE.
3. *Tools → Board → Dasduino Boards →* **Soldered Inkplate10**.
4. *Tools → Partition Scheme →* **Huge APP (3MB No OTA / 1MB SPIFFS)**. The
   default partition is too small for HTTPS + JSON + image decode.
5. Connect the Inkplate via USB-C. Select the serial port under *Tools → Port*.
6. Click *Upload*. First flash takes ~30 s.
7. Open *Tools → Serial Monitor* at **115200 baud** to watch the boot logs.

## What you should see on Serial

```
[boot] einkgen firmware 0.1.0
[boot] wake cause: timer
[wifi] connecting to SSID "..."
[wifi] connected, IP=..., RSSI=-58
[ntp] synced, epoch=...
[manifest] GET https://cdn.example.com/current/manifest.json
[manifest] OK, 412 bytes
[manifest] image_url=...
[manifest] image_sha256=...
[manifest] next_check_after=2026-05-13T16:05:00Z
[hash] stored="" new="9f1c..." changed=1
[batt] 90% low=0 wasLow=0 changed=0
[draw] downloading and verifying image
[draw] OK, hash + battery state persisted
[status] battery=4.11V (90%) rssi=-58
[status] POST https://...lambda-url.../
[status] OK HTTP 200
[sleep] next_check_after epoch=... now=... diff=...s
[sleep] deep-sleeping for 3600 seconds
```

## Secrets hygiene

`secrets.h` is gitignored. Never commit Wi-Fi credentials or the device token.
Treat the token like an API key — if it leaks, rotate it in AWS Secrets Manager
and reflash the device.

## Flash-time gotchas

These bit the first hardware-test pass; full walkthrough lives in
[QUICKSTART §5](../../QUICKSTART.md#part-5--flash-the-inkplate-10).

- **"No serial data received"** during upload — close any open Serial
  Monitor (`lsof /dev/cu.usbserial-…` to confirm nothing's holding the
  port), or hold the **WAKE** button on the back of the Inkplate while
  "Connecting……" prints.
- **"Invalid head of packet (0xE0)"** after stub loads — set *Tools →
  Upload Speed* to **115200**. The default 921600 baud-rate jump corrupts
  packets on many USB-C cables and hubs.
- **ArduinoJson** installs from the default Arduino registry via *Sketch
  → Include Library → Manage Libraries…*. No extra board-manager URL is
  needed for it — that URL is only for the Dasduino board package.

## Notes / TODOs

- HTTPS cert verification is currently disabled (`setInsecure()`). For v0 the
  threat model in [ARCHITECTURE §12](../../ARCHITECTURE.md#12-security--threat-model)
  accepts this — the worst case is denial of service. Pin the CloudFront cert
  before shipping outside a home LAN.
- BMP-from-buffer rendering goes through
  `display.image.drawBitmapFromBuffer(buf, x, y, dither, invert)` — confirmed
  on hardware. The `image` accessor lives on the Inkplate10 board driver and
  reads width/height from the BMP header itself (no length arg). The original
  spec called `display.drawImage(...)`; that name isn't exposed by the current
  Soldered `InkplateLibrary`.
- `display.readBattery()` returns a `double`; we cast to `float` and linearly
  map 3.3→4.2 V to 0→100 %. Calibrate against the actual cell once it's wired.
- Partition scheme name may vary between Dasduino board package versions.
  If "Huge APP" isn't listed, pick any non-OTA scheme with ≥3 MB app space.
