#!/usr/bin/env python3
"""ColdGuardian end-to-end test: login, ensure a package assignment, create its QR, then monitor live telemetry/alerts."""
from __future__ import annotations

import json
import sys
import time
from getpass import getpass
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPCookieProcessor

BASE = "http://127.0.0.1:8000"
DEVICE_ID = "tracker-30-76-F5-F4-0C-F0"
REPORTING_INTERVAL_SECONDS = 15
PACKAGE_BODY = {
    "product_sku": "MED-001",
    "product_name": "Temperature-sensitive medicine",
    "quantity": 1,
    "unit": "package",
    "batch_number": "BATCH-2026-01",
    "min_temperature_c": 2.0,
    "max_temperature_c": 8.0,
    "shock_threshold_g": 4.0,
    "device_id": DEVICE_ID,
}

opener = build_opener(HTTPCookieProcessor(CookieJar()))


class TestFailure(RuntimeError):
    pass


def request_json(path: str, method: str = "GET", body=None, csrf: str | None = None):
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if csrf:
        headers["X-CSRF-Token"] = csrf
    req = Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=12) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise TestFailure(f"HTTP {exc.code} for {method} {path}: {detail}") from exc
    except URLError as exc:
        raise TestFailure(f"Cannot reach {BASE}: {exc}") from exc


def dump(obj) -> str:
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


def create_qr(tracking_url: str, package_id: str) -> Path:
    try:
        import qrcode
    except ImportError as exc:
        raise TestFailure(
            'QR library missing. Run with: uv run --env-file .env --with "qrcode[pil]" python coldguardian_live_test.py'
        ) from exc
    filename = Path.cwd() / f"{package_id}_tracking_qr.png"
    qr = qrcode.QRCode(version=None, box_size=10, border=4)
    qr.add_data(tracking_url)
    qr.make(fit=True)
    qr.make_image(fill_color="black", back_color="white").save(filename)
    return filename


def wait_for_device() -> dict:
    print(f"\nWaiting for auto-registration of {DEVICE_ID} (up to 120 seconds)...")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = request_json("/api/v1/devices?limit=100")
        device = next((d for d in result.get("items", []) if d.get("device_id") == DEVICE_ID), None)
        if device:
            print("PASS: backend has automatically registered the ESP32.")
            return request_json("/api/v1/devices/" + quote(DEVICE_ID, safe=""))
        print("Device not registered yet; check Serial Monitor for MQTT/REGISTERED status...")
        time.sleep(3)
    raise TestFailure("ESP32 did not register within 120 seconds. No package was created.")


def main() -> int:
    print("=" * 68)
    print("COLDGUARDIAN END-TO-END LIVE TEST")
    print("=" * 68)

    ready = request_json("/readyz")
    print("\nBackend readiness:")
    print(dump(ready))
    if ready.get("status") != "ok":
        raise TestFailure("Backend is not ready. Fix PostgreSQL/schema/MQTT readiness first.")

    email = input("Admin email: ").strip()
    password = getpass("Admin password: ")
    print("Logging in...")
    session = request_json("/api/v1/auth/login", "POST", {"email": email, "password": password})
    del password
    csrf = session.get("csrf_token")
    if not csrf:
        raise TestFailure("Login response did not contain csrf_token.")
    print(f"PASS: logged in as {session.get('user', {}).get('email', email)}")

    device = wait_for_device()
    assignment = device.get("assignment")
    tracking_url = None
    created_now = False

    if assignment:
        package_id = assignment["package_id"]
        package = request_json("/api/v1/packages/" + quote(package_id, safe=""))
        print(f"\nDevice already has package {package_id}; not creating a duplicate.")
        print("Its existing tracking token cannot be recovered from the database. The script will not rotate it silently.")
    else:
        print("\nCreating package assigned to the registered ESP32...")
        result = request_json("/api/v1/packages", "POST", PACKAGE_BODY, csrf)
        package = result["package"]
        package_id = package["package_id"]
        tracking_url = result.get("tracking_url")
        created_now = True
        print("PASS: package created and assigned.")

    print("\nPACKAGE DETAILS")
    print(dump({k: package.get(k) for k in (
        "package_id", "product_sku", "product_name", "quantity", "unit", "batch_number",
        "min_temperature_c", "max_temperature_c", "shock_threshold_g", "device_id", "status",
    )}))

    if tracking_url:
        qr_path = create_qr(tracking_url, package_id)
        print(f"\nQR PNG saved: {qr_path}")
        print("Tracking URL (treat as a secret; anyone with it can view this package):")
        print(tracking_url)
        print("QR code contains the tracking URL; it does not prove delivery or change package status.")
    elif created_now:
        print("WARNING: package was created, but no tracking_url was returned.")

    settings = request_json("/api/v1/devices/" + quote(DEVICE_ID, safe="") + "/settings")
    print("\nBACKEND DEVICE SETTINGS")
    print(dump(settings))
    if settings.get("reporting_interval_seconds") != REPORTING_INTERVAL_SECONDS:
        print("WARNING: backend reporting interval is not 15 seconds.")
    if settings.get("min_temperature_c") != PACKAGE_BODY["min_temperature_c"] or settings.get("max_temperature_c") != PACKAGE_BODY["max_temperature_c"] or settings.get("shock_threshold_g") != PACKAGE_BODY["shock_threshold_g"]:
        print("NOTE: settings differ from this script's default package values; reporting the stored values above.")

    print("\nLIVE MONITOR")
    print("Polling backend every 3 seconds. Device telemetry should arrive about every 15 seconds.")
    print("Try real sensor inputs now. Press Ctrl+C when finished.\n")
    seen_telemetry: set[tuple] = set()
    seen_alerts: set[str] = set()
    last_wait_notice = 0.0
    last_config_state = None

    while True:
        current_device = request_json("/api/v1/devices/" + quote(DEVICE_ID, safe=""))
        config_state = (
            current_device.get("config_version"),
            current_device.get("config_applied_version"),
            (current_device.get("assignment") or {}).get("package_id"),
        )
        if config_state != last_config_state:
            print(
                f"[CONFIG] version={config_state[0]} applied={config_state[1]} "
                f"assigned_package={config_state[2]}"
            )
            last_config_state = config_state

        telemetry_result = request_json(
            "/api/v1/devices/" + quote(DEVICE_ID, safe="") + "/telemetry?limit=10"
        )
        items = list(reversed(telemetry_result.get("items", [])))
        for row in items:
            key = (row.get("boot_id"), row.get("message_seq"))
            if key in seen_telemetry:
                continue
            seen_telemetry.add(key)
            print("\n[TELEMETRY RECEIVED]")
            print(f"  observed_at:       {row.get('observed_at')}")
            print(f"  received_at:       {row.get('received_at')}")
            print(f"  sequence / config: {row.get('message_seq')} / {row.get('config_version')}")
            print(f"  temperature:       {row.get('temperature_c')} C (valid={row.get('temperature_valid')})")
            print(f"  peak acceleration: {row.get('peak_acceleration_magnitude_g')} g (valid={row.get('motion_valid')})")
            print(f"  shock count:       {row.get('shock_count_since_last_report')}")
            print(f"  GPS fix:           {row.get('location_fix_valid')}")
            if row.get("location_fix_valid"):
                print(f"  coordinates:       {row.get('latitude')}, {row.get('longitude')}")

        alert_result = request_json(
            "/api/v1/alerts?package_id=" + quote(package_id, safe="") + "&limit=100"
        )
        for alert in alert_result.get("items", []):
            alert_id = alert.get("alert_id")
            if alert_id in seen_alerts:
                continue
            seen_alerts.add(alert_id)
            print("\n[NEW ALERT]")
            print(dump(alert))

        now = time.monotonic()
        if not telemetry_result.get("items") and now - last_wait_notice >= REPORTING_INTERVAL_SECONDS:
            print("[WAITING] No telemetry has reached the backend yet. Confirm config is applied and wait for the next 15-second report.")
            last_wait_notice = now
        elif telemetry_result.get("items") and not items:
            # Don't spam the console between reporting intervals.
            if now - last_wait_notice >= REPORTING_INTERVAL_SECONDS:
                print("[OK] No new telemetry/alert since the last check.")
                last_wait_notice = now
        time.sleep(3)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped live test.")
    except TestFailure as exc:
        print(f"\nTEST FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)

