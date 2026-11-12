#!/usr/bin/env python3
"""Refreshes the Android device list (see app/core/devicenames.py).

In Docker:   docker compose exec metadata-suite python -m app.core.devicenames update
On the host: python scripts/update_android_devices.py   (writes to ANDROID_DEVICES_FILE or ./config/android_devices.json)
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("ANDROID_DEVICES_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "config", "android_devices.json"))
from app.core import devicenames  # noqa: E402

try:
    print(f"{devicenames.update_android_db()} Geräte -> {os.path.normpath(devicenames.ANDROID_DB_PATH)}")
except Exception as e:  # noqa: BLE001
    print(f"Fehler: {e}", file=sys.stderr)
    sys.exit(1)
