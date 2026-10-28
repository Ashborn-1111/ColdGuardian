#!/usr/bin/env python3
"""ColdGuardian end-to-end test: API, package, backend QR, MQTT config, telemetry, alerts."""
from __future__ import annotations

import io
import json
import os
import sys
import threading
import time
import uuid
from getpass import getpass
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, build_opener, HTTPCookieProcessor

# Load only simple KEY=VALUE entries from project .env when not already exported.
# This keeps uv run python test.py usable without requiring --env-file.
def load_env_file() -> None:
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


load_env_file()

BASE = os.getenv("CG_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
DEVICE_ID = os.getenv("COLDGUARDIAN_DEVICE_ID", "tracker-30-76-F5-F4-0C-F0")
REPORTING_INTERVAL_SECONDS = 15
LIVE_SECONDS = int(os.getenv("CG_LIVE_TEST_SECONDS", "90"))
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


def api(path: str, method: str = "GET", body=None, csrf: str | None = None):
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    if csrf:
        headers["X-CSRF-Token"] = csrf
    request = Request(BASE + path, data=data, headers=headers, method=method)
    try:
        with opener.open(request, timeout=12) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        hint = ""
        if exc.code == 404 and "tracking-token" in path:
            hint = " The running backend may not have been restarted from the updated main.py."
        if exc.code == 404 and "tracking-qr.png" in path:
            hint = " The running backend has not loaded the tracking QR PNG route from updated main.py. Stop it and restart with: uv run --env-file .env python main.py"
        raise TestFailure(f"HTTP {exc.code} for {method} {path}: {detail}{hint}") from exc
    except URLError as exc:
        raise TestFailure(f"Cannot reach backend {BASE}: {exc}") from exc


def request_raw(url: str, method: str = "GET") -> tuple[bytes, dict[str, str], int]:
    request = Request(url, headers={"Accept": "*/*"}, method=method)
    try:
        with opener.open(request, timeout=12) as response:
            return response.read(), {key.lower(): value for key, value in response.headers.items()}, response.status
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise TestFailure(f"HTTP {exc.code} for {method} {url}: {detail}") from exc
    except URLError as exc:
        raise TestFailure(f"Cannot reach {url}: {exc}") from exc


def dump(obj) -> str:
    return json.dumps(obj, indent=2, default=str, ensure_ascii=False)


def wait_for_device() -> dict:
    print(f"\nWaiting for ESP32 auto-registration: {DEVICE_ID} (up to 120 seconds)...")
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        result = api("/api/v1/devices?limit=100")
        if any(item.get("device_id") == DEVICE_ID for item in result.get("items", [])):
            print("PASS: backend automatically registered the ESP32.")
            return api("/api/v1/devices/" + quote(DEVICE_ID, safe=""))
        print("  waiting for registration (check ESP32 Serial Monitor: MQTT/REGISTERED)...")
        time.sleep(3)
    raise TestFailure("ESP32 did not register in 120 seconds; no package was created.")


def get_qr_png(package_id: str) -> tuple[Path, tuple[int, int]]:
    package_path = quote(package_id, safe="")
    payload, headers, _status = request_raw(
        BASE + f"/api/v1/packages/{package_path}/tracking-qr.png"
    )
    if not payload.startswith(b"\x89PNG\r\n\x1a\n"):
        raise TestFailure("Backend QR endpoint did not return a PNG image.")
    try:
        from PIL import Image
        image = Image.open(io.BytesIO(payload))
        image.verify()
        image = Image.open(io.BytesIO(payload))
        size = image.size
    except Exception as exc:
        raise TestFailure(f"Backend QR PNG is not a valid image: {exc}") from exc
    if size[0] < 100 or size[1] < 100:
        raise TestFailure(f"Backend QR image dimensions look wrong: {size}")

    disposition = headers.get("content-disposition", "")
    # Use a fixed safe local filename regardless of any server-supplied filename.
    output = Path.cwd() / f"{package_id}_tracking_qr.png"
    output.write_bytes(payload)
    return output, size


def check_qr_decode(image_path: Path, expected_url: str) -> None:
    """Decode when OpenCV happens to be installed; do not add a heavy dependency."""
    try:
        import cv2
        import numpy as np
        from PIL import Image
    except ImportError:
        print("NOTE: PNG structure verified; OpenCV not installed, so automatic pixel decode was skipped.")
        print("      The phone scan / OLED scan remains the end-to-end optical check.")
        return
    image_pil = Image.open(image_path).convert("RGB")
    image = cv2.cvtColor(np.array(image_pil), cv2.COLOR_RGB2BGR)
    decoded, _points, _straight = cv2.QRCodeDetector().detectAndDecode(image)
    if decoded:
        if decoded != expected_url:
            raise TestFailure("Decoded QR payload does not match the package tracking URL.")
        print("PASS: QR image decodes to the exact tracking URL.")
    else:
        print("WARNING: OpenCV could not decode this image automatically; PNG is valid, but scan it manually.")


def verify_tracking_url(package_id: str, tracking_url: str) -> None:
    parsed = urlparse(tracking_url)
    token = parsed.path.rsplit("/", 1)[-1]
    if parsed.scheme not in {"http", "https"} or not token or not parsed.path.startswith("/track/"):
        raise TestFailure("Tracking URL has an unexpected format; expected /track/<token>.")

    data = api("/api/v1/public/tracking/" + quote(token, safe=""))
    if data.get("package", {}).get("package_id") != package_id:
        raise TestFailure("Public tracking API resolved the token to a different package.")
    print("PASS: token resolves to the correct package through /api/v1/public/tracking/<token>.")

    # The actual URL must serve the public HTML tracking page.
    page_url = BASE + parsed.path
    page_body, headers, status = request_raw(page_url)
    if status != 200 or b"Package tracking" not in page_body:
        raise TestFailure("/track/<token> did not return the public tracking HTML page.")
    if "text/html" not in headers.get("content-type", ""):
        raise TestFailure("Tracking URL response is not HTML.")
    print("PASS: /track/<token> returns the public tracking page.")

    host = (parsed.hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "0.0.0.0"}:
        print("WARNING: PUBLIC_BASE_URL is loopback; the QR will not work from a phone.")
        print("         Set PUBLIC_BASE_URL in .env to a laptop address the phone can reach, then rotate the token/config.")
    else:
        print(f"Tracking host in QR: {host}; confirm that port {parsed.port or (443 if parsed.scheme == 'https' else 80)} is reachable from the phone.")


def verify_retained_mqtt_config(expected_url: str, package_id: str, target_config_version: int) -> None:
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise TestFailure("paho-mqtt is missing; run `uv sync`.") from exc

    host = os.getenv("MQTT_HOST", "127.0.0.1")
    port = int(os.getenv("MQTT_PORT", "1883"))
    username = os.getenv("MQTT_USERNAME") or None
    password = os.getenv("MQTT_PASSWORD") or None
    topic = f"coldchain/{DEVICE_ID}/config"
    received = threading.Event()
    result: dict = {}
    failures: list[str] = []

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"coldguardian-qr-check-{uuid.uuid4().hex[:8]}",
        protocol=mqtt.MQTTv311,
    )
    if username:
        client.username_pw_set(username, password)

    def on_connect(client, userdata, flags, reason_code, properties):
        if getattr(reason_code, "is_failure", False):
            failures.append(f"MQTT connection rejected: {reason_code}")
            received.set()
            return
        client.subscribe(topic, qos=1)

    def on_message(client, userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except Exception:
            failures.append("Retained MQTT config is not valid JSON.")
            received.set()
            return
        if payload.get("message_type") == "device_config":
            result.clear()
            result.update(payload)
            if (
                payload.get("tracking_url") == expected_url
                and payload.get("config_version", 0) >= target_config_version
                and (payload.get("assignment") or {}).get("package_id") == package_id
            ):
                received.set()

    client.on_connect = on_connect
    client.on_message = on_message
    try:
        client.connect(host, port, keepalive=10)
        client.loop_start()
        if not received.wait(10):
            version = result.get("config_version", "none")
            raise TestFailure(
                f"No matching retained config arrived on {topic} within 10 seconds "
                f"(last config version={version}). Backend must publish the same tracking URL/package to MQTT."
            )
        if failures:
            raise TestFailure(failures[0])
    except TestFailure:
        raise
    except Exception as exc:
        raise TestFailure(f"Could not subscribe to MQTT broker {host}:{port}: {exc}") from exc
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass

    if result.get("device_id") != DEVICE_ID:
        raise TestFailure("Retained MQTT config has the wrong device_id.")
    if result.get("tracking_url") != expected_url:
        raise TestFailure("Retained MQTT config's tracking_url does not match the QR URL.")
    if result.get("config_version", 0) < target_config_version:
        raise TestFailure("Retained MQTT config version is older than expected.")
    if (result.get("assignment") or {}).get("package_id") != package_id:
        raise TestFailure("Retained MQTT config has the wrong package assignment.")
    monitoring = result.get("monitoring") or {}
    temperature = monitoring.get("temperature") or {}
    shock = monitoring.get("shock") or {}
    if temperature.get("min_temperature_c") != 2.0 or temperature.get("max_temperature_c") != 8.0:
        raise TestFailure("MQTT config does not contain temperature thresholds 2–8 C.")
    if shock.get("threshold_g") != 4.0:
        raise TestFailure("MQTT config does not contain the 4.0 g shock threshold.")
    if monitoring.get("reporting_interval_seconds") != REPORTING_INTERVAL_SECONDS:
        raise TestFailure("MQTT config does not specify 15-second telemetry.")
    print("PASS: broker retained the exact package URL + 2–8 C / 4.0 g / 15-second config for the ESP32.")


def main() -> int:
    print("=" * 72)
    print("COLDGUARDIAN CLEAN-START END-TO-END TEST")
    print("=" * 72)
    print(f"API: {BASE}")
    print(f"Device: {DEVICE_ID}")

    # Detect a stale process before we create data. The updated route must appear.
    openapi = api("/openapi.json")
    routes = openapi.get("paths", {})
    expected_qr_route = "/api/v1/packages/{package_id}/tracking-qr.png"
    if expected_qr_route not in routes:
        raise TestFailure(
            "The running API does not expose the backend QR endpoint. Stop it and restart the current main.py with: "
            "uv run --env-file .env python main.py"
        )
    if "/api/v1/packages/{package_id}/tracking-token" not in routes:
        raise TestFailure("Running main.py is missing the tracking-token route; source/runtime mismatch.")
    print("PASS: running API exposes tracking-token and tracking-qr.png routes.")

    readiness = api("/readyz")
    print("\nBackend readiness:\n" + dump(readiness))
    if readiness.get("status") != "ok":
        raise TestFailure("Backend is not ready (PostgreSQL/schema/MQTT consumer).")
    print("PASS: PostgreSQL, schema and MQTT consumer are ready.")

    email = input("Admin email [admin@admin.admin]: ").strip() or "admin@admin.admin"
    password = getpass("Admin password: ")
    session = api("/api/v1/auth/login", "POST", {"email": email, "password": password})
    del password
    csrf = session.get("csrf_token")
    if not csrf:
        raise TestFailure("Login response did not contain csrf_token.")
    print(f"PASS: authenticated as {session.get('user', {}).get('email', email)}.")

    device = wait_for_device()
    telemetry_path = "/api/v1/devices/" + quote(DEVICE_ID, safe="") + "/telemetry?limit=1000"
    alerts_path_device = "/api/v1/alerts?device_id=" + quote(DEVICE_ID, safe="") + "&limit=100"
    seen_telemetry = {
        (row.get("boot_id"), row.get("message_seq"))
        for row in api(telemetry_path).get("items", [])
    }
    seen_alerts = {
        row.get("alert_id") for row in api(alerts_path_device).get("items", []) if row.get("alert_id")
    }

    assignment = device.get("assignment")
    if not assignment:
        print("\nCreating the package and assigning it to the registered ESP32...")
        created = api("/api/v1/packages", "POST", PACKAGE_BODY, csrf)
        package = created["package"]
        package_id = package["package_id"]
        tracking_url = created.get("tracking_url")
        print(f"PASS: created {package_id} and assigned it to {DEVICE_ID}.")
    else:
        package_id = assignment["package_id"]
        package = api("/api/v1/packages/" + quote(package_id, safe=""))
        print(f"\nUsing existing assignment {package_id} (no duplicate package created).")
        patch = {
            "min_temperature_c": 2.0,
            "max_temperature_c": 8.0,
            "shock_threshold_g": 4.0,
        }
        if any(package.get(key) != value for key, value in patch.items()):
            package = api("/api/v1/packages/" + quote(package_id, safe=""), "PATCH", patch, csrf)
        token_info = api("/api/v1/packages/" + quote(package_id, safe="") + "/tracking-token")
        tracking_url = token_info.get("tracking_url")
        if not tracking_url:
            rotated = api(
                "/api/v1/packages/" + quote(package_id, safe="") + "/tracking-token/rotate",
                "POST", csrf=csrf,
            )
            tracking_url = rotated["tracking_url"]
            print(f"PASS: rotated legacy/unavailable token to version {rotated.get('tracking_token_version')}.")

    if not tracking_url:
        raise TestFailure("Package creation/token rotation returned no tracking URL.")
    if len(tracking_url.encode("ascii", errors="ignore")) > 106:
        raise TestFailure(
            f"Tracking URL is {len(tracking_url.encode('ascii', errors='ignore'))} bytes; "
            "the current ESP32 QR firmware supports at most 106. Use a shorter PUBLIC_BASE_URL."
        )

    # Freshly refetch package after potential patch; assert thresholds and assignment.
    package = api("/api/v1/packages/" + quote(package_id, safe=""))
    print("\nPACKAGE DETAILS:\n" + dump({k: package.get(k) for k in (
        "package_id", "product_sku", "product_name", "quantity", "unit", "batch_number",
        "min_temperature_c", "max_temperature_c", "shock_threshold_g", "device_id", "status",
    )}))
    if package.get("device_id") != DEVICE_ID:
        raise TestFailure("Package is not assigned to the expected ESP32.")
    if (package.get("min_temperature_c"), package.get("max_temperature_c"), package.get("shock_threshold_g")) != (2.0, 8.0, 4.0):
        raise TestFailure("Stored package thresholds are not 2–8 C and 4.0 g.")
    print("PASS: package thresholds and tracker assignment match the test configuration.")

    verify_tracking_url(package_id, tracking_url)
    qr_path, qr_size = get_qr_png(package_id)
    print(f"PASS: backend generated a valid QR PNG: {qr_path.resolve()} ({qr_size[0]}x{qr_size[1]} pixels).")
    check_qr_decode(qr_path, tracking_url)

    settings = api("/api/v1/devices/" + quote(DEVICE_ID, safe="") + "/settings")
    print("\nDEVICE SETTINGS:\n" + dump(settings))
    if settings.get("reporting_interval_seconds") != REPORTING_INTERVAL_SECONDS:
        raise TestFailure("Backend settings do not specify a 15-second telemetry interval.")
    if settings.get("assigned") is not True:
        raise TestFailure("Device settings say the device is not assigned.")
    target_version = settings.get("config_version", 0)

    verify_retained_mqtt_config(tracking_url, package_id, target_version)

    print(f"\nWaiting up to 60 seconds for ESP32 telemetry to confirm config version {target_version}...")
    deadline = time.monotonic() + 60
    current = device
    while time.monotonic() < deadline:
        current = api("/api/v1/devices/" + quote(DEVICE_ID, safe=""))
        if current.get("config_applied_version", 0) >= target_version and target_version > 0:
            print("PASS: ESP32 has reported the assigned config version as applied (via telemetry).")
            break
        time.sleep(3)
    else:
        raise TestFailure(
            "MQTT retained config is correct, but ESP32 has not reported applying it. "
            "Check Serial Monitor for 'Tracking QR URL received' and the config version."
        )

    print("\nLIVE SENSOR / ALERT TEST")
    print(f"Monitoring for {LIVE_SECONDS} seconds. Telemetry is expected about every 15 seconds.")
    print("If practical, gently move/tap the ADXL345 board once to test a shock event > 4.0 g.")
    print("Do not strike the OLED or ESP32; move the accelerometer board gently.\n")
    live_deadline = time.monotonic() + LIVE_SECONDS
    fresh_telemetry = 0
    fresh_temp_alert = False
    fresh_shock_alert = False
    latest_temperature = None

    while time.monotonic() < live_deadline:
        device_now = api("/api/v1/devices/" + quote(DEVICE_ID, safe=""))
        items = list(reversed(api(telemetry_path).get("items", [])))
        for row in items:
            key = (row.get("boot_id"), row.get("message_seq"))
            if key in seen_telemetry:
                continue
            seen_telemetry.add(key)
            fresh_telemetry += 1
            latest_temperature = row.get("temperature_c")
            print("\n[NEW TELEMETRY]")
            print(f"  observed_at:       {row.get('observed_at')}")
            print(f"  boot / sequence:   {row.get('boot_id')} / {row.get('message_seq')}")
            print(f"  config version:    {row.get('config_version')}")
            print(f"  temperature:       {row.get('temperature_c')} C valid={row.get('temperature_valid')}")
            print(f"  peak acceleration: {row.get('peak_acceleration_magnitude_g')} g valid={row.get('motion_valid')}")
            print(f"  shock count:       {row.get('shock_count_since_last_report')}")
            print(f"  GPS fix:           {row.get('location_fix_valid')}")
            if row.get("location_fix_valid"):
                print(f"  coordinates:       {row.get('latitude')}, {row.get('longitude')}")

        for alert in api("/api/v1/alerts?package_id=" + quote(package_id, safe="") + "&limit=100").get("items", []):
            alert_id = alert.get("alert_id")
            if not alert_id or alert_id in seen_alerts:
                continue
            seen_alerts.add(alert_id)
            alert_type = alert.get("type")
            fresh_temp_alert |= alert_type == "temperature_breach"
            fresh_shock_alert |= alert_type == "shock_detected"
            print("\n[NEW ALERT]")
            print(dump(alert))
        time.sleep(3)

    print("\n" + "=" * 72)
    print("END-TO-END TEST SUMMARY")
    print("=" * 72)
    print("PASS: API readiness and admin authentication")
    print("PASS: automatic ESP32 registration")
    print(f"PASS: package {package_id}, 2–8 C and shock threshold 4.0 g")
    print("PASS: backend tracking URL resolves to correct package")
    print(f"PASS: backend-generated QR PNG saved to {qr_path.resolve()}")
    print("PASS: retained MQTT configuration contains exact URL and thresholds")
    print(f"PASS: ESP32 applied config version {target_version}")
    if fresh_telemetry:
        print(f"PASS: received {fresh_telemetry} new telemetry records in {LIVE_SECONDS} seconds")
    else:
        print("WARNING: no new telemetry arrived during the live window")
    if fresh_temp_alert:
        print("PASS: temperature-breach alert observed")
    else:
        print("WARNING: no new temperature-breach alert observed; check actual temp and firmware event logic")
    if fresh_shock_alert:
        print("PASS: shock alert observed")
    else:
        print("WARNING: no new shock alert observed; repeat and gently move/tap the ADXL345 board")
    print("NOTE: scan the PNG and the OLED QR with a phone to finish the physical optical test.")
    print("NOTE: GPS fix is expected to remain false indoors; test outside with clear sky view.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("\nStopped by user.")
    except TestFailure as exc:
        print(f"\nTEST FAILED: {exc}", file=sys.stderr)
        raise SystemExit(1)

