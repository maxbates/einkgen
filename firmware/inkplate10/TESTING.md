# Manual test plan — Inkplate 10 firmware

Run `python3 firmware/inkplate10/tests/test_polling.py` from the repo root
for an automated host check of `setup()` against stubbed hardware and network
interfaces. This does not replace Arduino's board build or the checks below.

No hardware-in-the-loop CI: run this checklist with an Inkplate and serial
monitor after flashing an authorized test device. Use a test endpoint when
changing manifests or credentials. Press **WAKE** while the device is
asleep (or use reset to rerun a boot that does not test manual wake).

Before starting:

- `secrets.h` filled in and pointing at a real CloudFront URL + Lambda URL.
- AWS side has at least one published frame so `manifest.json` exists.
- Serial monitor open at **115200 baud**.

---

## 1. First boot with empty NVS

**Setup.** Fresh flash, or run an "erase flash" once from Arduino IDE.

**Expected logs (in order):**
- `[boot] einkgen firmware …`
- `[wifi] connected …`
- `[manifest] OK …`
- `[hash] stored="" new="…" changed=1`
- `[draw] downloading and verifying image`
- `[image] GET …`
- `[verify] hash OK`
- `[draw] OK, hash persisted`
- `[status] OK HTTP 200`
- `[sleep] deep-sleeping for … seconds`

**Expected on panel.** Latest dithered image renders fully.

**Expected on server.** A new `status/device-<id>.json` appears in S3 with
fresh battery / RSSI / current_hash.

---

## 2. Second boot, manifest unchanged

**Setup.** Wake the device (don't republish anything server-side).

**Expected logs.**
- `[hash] stored="…" new="…" changed=0`
- `[draw] hash unchanged, skipping redraw`
- `[status] OK HTTP 200`
- `[sleep] deep-sleeping for … seconds`

**Expected on panel.** No visible refresh (the panel keeps the same frame —
e-ink is bistable, so this is silent and correct).

---

## 3. Manifest changed

**Setup.** Publish a new image server-side
(`einkgen local preview` + manual upload, or full pipeline once it lands).
Wake the device.

**Expected logs.**
- `[hash] stored="<old>" new="<new>" changed=1`
- `[draw] OK, hash persisted`

**Expected on panel.** New image renders.

---

## 4. Wi-Fi fails

**Setup.** Edit `secrets.h` to a wrong password, reflash, boot.

**Expected logs.**
- `[wifi] connecting …`
- `[wifi] timeout` (after ~20 s)
- `[wifi] joining failed, sleeping until the next poll`
- `[sleep] deep-sleeping for 10800 seconds`

**Expected on panel.** Unchanged (no redraw attempted).

Restore real password before continuing.

---

## 5. Manifest fetch 404

**Setup.** Edit `MANIFEST_URL` in `secrets.h` to a path that returns 404
(e.g., `…/current/nope.json`), reflash, boot.

**Expected logs.**
- `[manifest] HTTP 404`
- `[sleep] deep-sleeping for 10800 seconds`

Restore real URL before continuing.

---

## 6. Server hints do not affect the timer

**Setup.** Test both an embedded `/wake` response and the fallback manifest
with `next_check_after` far in the future, seconds away, expired, absent, or
malformed. Keep all required image fields valid.

**Expected logs.** The usual draw/status path and
`[sleep] deep-sleeping for 10800 seconds (or until WAKE pressed)` in each case.
No NTP synchronization for this timer.

---

## 7. Early manual wake versus automatic timer

**Setup.** Wait until Serial shows the sleep message. Press the **WAKE**
button well before three hours and release it.

**Expected.** Serial immediately shows `[boot] wake cause: wake-button`,
then Wi-Fi, a fresh `/wake` poll (or a manifest GET on fallback), and the
usual draw/status checks. Sleep restarts at 10800 seconds. Leave the device
alone for another interval; its next boot should log `wake cause: timer`.

---

## 8. Hash mismatch (MITM / corrupt download)

**Setup.** Server-side, publish a manifest whose `image_sha256` does NOT
match the actual `image.bmp` bytes (easiest: hand-edit the manifest JSON in
S3 to flip a hex character). Wake the device.

**Expected logs.**
- `[image] GET …`
- `[verify] hash mismatch: expected=<manifest-claim> got=<real-bytes-sha>`
- `[draw] failed — leaving previous frame on screen`
- `[status] OK HTTP 200`  *(reports the prior NVS hash, NOT the bogus one)*

**Expected on panel.** Unchanged — previous frame stays visible. NVS
`image_sha256` is unchanged. The Device tab on the web app continues to
show the last verified hash, not the bogus manifest claim.

Restore real manifest before continuing.

---

## 9. Battery POST receives 401

**Setup.** Edit `DEVICE_STATUS_TOKEN` in `secrets.h` to a wrong value,
reflash, boot.

**Expected logs.**
- `[status] non-OK HTTP 401, body=…`
- still proceeds to `[sleep] deep-sleeping for … seconds`

**Expected on panel.** Whatever the manifest says — status failure must
not block the draw path or the sleep path.

Restore real token before continuing.

---

## Spot checks (run occasionally)

- **Power draw in sleep.** Multimeter on the battery line: should be a few
  tens of µA when the panel is asleep. If it's milliamps, Wi-Fi or the
  ESP32 didn't actually deep-sleep.
- **Hash persistence across hard reset.** Pull power for 10 s, replug.
  NVS should survive: log shows `stored="<previous>"` not `stored=""`.
- **Long-soak run.** Leave the device on a charged battery for 12 hours.
  Expect roughly four unattended polls (plus a boot at the start, if
  counted). Extra button presses reset the timer; outages may prevent a
  status report. Count Serial boot causes as well as successful reports.
