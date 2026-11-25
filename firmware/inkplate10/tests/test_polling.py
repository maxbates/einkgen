"""Compile and run the real sketch's setup() against host test doubles."""
import os
from pathlib import Path
import subprocess
import tempfile

HERE = Path(__file__).resolve().parent
SKETCH = HERE.parent / "inkplate10.ino"
HEADERS = (
    "Inkplate.h", "HTTPClient.h", "Preferences.h", "WiFi.h", "WiFiClientSecure.h",
    "ArduinoJson.h", "mbedtls/sha256.h", "esp_heap_caps.h", "esp_sleep.h", "secrets.h",
)

def test_setup_polling():
    with tempfile.TemporaryDirectory(prefix="inkplate-host-") as directory:
        build = Path(directory)
        # Quote-includes in the sketch must resolve to the stubs, even when
        # the developer's original sketch directory contains real secrets.h.
        sketch = build / SKETCH.name
        sketch.write_bytes(SKETCH.read_bytes())
        for header in HEADERS:
            path = build / header
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('#include "host_stubs.h"\n')
        exe = build / "test_polling"
        subprocess.run([
            os.environ.get("CXX", "c++"), "-std=c++17", "-Wall", "-Wextra",
            "-Werror", "-Wno-deprecated-declarations", "-I", str(build),
            "-I", str(HERE), f'-DSKETCH="{sketch}"',
            str(HERE / "test_polling.cpp"), "-o", str(exe),
        ], check=True)
        subprocess.run([str(exe)], check=True)


if __name__ == "__main__":
    test_setup_polling()
