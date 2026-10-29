#!/usr/bin/env python3
"""Publish demo telemetry as five registered ColdGuardian trackers over MQTT.

Run from the project root in WSL:
  uv run --env-file .env python simulate_five_trackers.py
  uv run --env-file .env python simulate_five_trackers.py --once
  uv run --env-file .env python simulate_five_trackers.py --with-alerts --once
  uv run --env-file .env python simulate_five_trackers.py --simulated-gps

All values are synthetic demo data, not real sensor measurements or locations.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt

DEVICE_IDS = [f"coldguardian-sim-{i:03d}" for i in range(1, 6)]
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None
FIRMWARE_VERSION = "0.1.0-simulator"
STOP = threading.Event()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interval-seconds", type=float, default=15.0, help="Seconds between telemetry rounds (default: 15).")
    parser.add_argument("--once", action="store_true", help="Send one telemetry round and exit.")
    parser.add_argument("--with-alerts", action="store_true", help="Send one temperature-breach and one shock event for each assigned, monitoring-enabled simulator.")
    parser.add_argument("--simulated-gps", action="store_true", help="Include clearly synthetic coordinates near Pune (not actual tracker GPS).")
    return parser.parse_args()


def new_client(client_id: str) -> mqtt.Client:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv311,
    )
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    return client


def publish(client: mqtt.Client, device_id: str, envelope: dict[str, Any]) -> None:
    topic = f"coldchain/{device_id}/messages"
    info = client.publish(topic, json.dumps(envelope, separators=(",", ":"), allow_nan=False), qos=1, retain=False)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"MQTT publish failed for {device_id} (rc={info.rc})")
    info.wait_for_publish(timeout=5)
    if not info.is_published():
        raise TimeoutError(f"No PUBACK for {device_id} message seq={envelope['message_seq']}")


def main() -> int:
    args = parse_args()
    if not math.isfinite(args.interval_seconds) or args.interval_seconds < 1:
        print("--interval-seconds must be a finite value >= 1", file=sys.stderr)
        return 2

    connected = threading.Event()
    subscribed = threading.Event()
    connection_errors: list[str] = []
    configs: dict[str, dict[str, Any]] = {}
    config_events = {device_id: threading.Event() for device_id in DEVICE_IDS}
    boot_ids = {device_id: secrets.token_hex(4) for device_id in DEVICE_IDS}
    sequences = {device_id: 0 for device_id in DEVICE_IDS}
    start_time = time.monotonic()

    client = new_client(f"cg-sim-five-{secrets.token_hex(5)}")

    def on_connect(c, userdata, flags, reason_code, properties):
        if getattr(reason_code, "is_failure", False):
            connection_errors.append(f"MQTT connection refused: {reason_code}")
            connected.set()
            return
        connected.set()
        result, _mid = c.subscribe([(f"coldchain/{device_id}/config", 1) for device_id in DEVICE_IDS])
        if result != mqtt.MQTT_ERR_SUCCESS:
            connection_errors.append(f"Subscribe request failed with rc={result}")
            subscribed.set()

    def on_subscribe(c, userdata, mid, reason_code_list, properties):
        if any(getattr(code, "is_failure", False) for code in reason_code_list):
            connection_errors.append(f"Subscription rejected: {reason_code_list}")
        subscribed.set()

    def on_message(c, userdata, msg):
        try:
            data = json.loads(msg.payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        device_id = data.get("device_id")
        if device_id in configs and data.get("message_type") != "device_config":
            return
        if device_id in DEVICE_IDS and data.get("message_type") == "device_config":
            configs[device_id] = data
            config_events[device_id].set()

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    def envelope(device_id: str, message_type: str, payload: dict[str, Any], config_version: int) -> dict[str, Any]:
        sequence = sequences[device_id]
        sequences[device_id] += 1
        return {
            "schema_version": 1,
            "message_type": message_type,
            "device_id": device_id,
            "firmware_version": FIRMWARE_VERSION,
            "boot_id": boot_ids[device_id],
            "message_seq": sequence,
            "config_version": config_version,
            "observed_at": utc_now(),
            "uptime_ms": int((time.monotonic() - start_time) * 1000),
            "payload": payload,
        }

    def current_values(index: int, device_id: str, config: dict[str, Any], round_number: int) -> tuple[float, float, int, dict[str, Any]]:
        assignment = config.get("assignment") or {}
        monitoring = config.get("monitoring") or {}
        temp_settings = monitoring.get("temperature") or {}
        shock_settings = monitoring.get("shock") or {}
        if assignment.get("assigned") and monitoring.get("enabled"):
            minimum = float(temp_settings.get("min_temperature_c", 2.0))
            maximum = float(temp_settings.get("max_temperature_c", 8.0))
            threshold = float(shock_settings.get("threshold_g", 4.0))
        else:
            minimum, maximum, threshold = 2.0, 8.0, 4.0

        midpoint = (minimum + maximum) / 2.0
        # Keep ordinary demo telemetry in range and vary it slightly from tracker to tracker / round to round.
        span = max(0.1, maximum - minimum)
        temp = min(maximum, max(minimum, midpoint + math.sin(round_number * 0.7 + index) * span * 0.12))
        acceleration = max(0.01, threshold * (0.08 + ((index + round_number) % 4) * 0.035))
        shock_count = 0

        if args.simulated_gps:
            # Synthetic demo coordinates only. They are deliberately labelled as generated in the CLI output.
            latitude = 18.5204 + index * 0.0008 + round_number * 0.0001
            longitude = 73.8567 + index * 0.0007 + round_number * 0.0001
            location = {
                "fix_valid": True,
                "latitude": round(latitude, 6),
                "longitude": round(longitude, 6),
                "accuracy_m": 12.0 + index,
                "satellites_used": 7 + (index % 3),
            }
        else:
            location = {
                "fix_valid": False,
                "latitude": None,
                "longitude": None,
                "accuracy_m": None,
                "satellites_used": 0,
            }
        return temp, acceleration, shock_count, location

    def publish_round(round_number: int, send_events: bool) -> None:
        print(f"\nTelemetry round {round_number + 1} at {utc_now()}")
        for index, device_id in enumerate(DEVICE_IDS):
            config = configs[device_id]
            version = config.get("config_version", 0)
            assignment = config.get("assignment") or {}
            monitoring = config.get("monitoring") or {}
            temp, acceleration, shock_count, location = current_values(index, device_id, config, round_number)
            payload = {
                "temperature": {"valid": True, "celsius": round(temp, 3)},
                "motion": {
                    "valid": True,
                    "peak_acceleration_magnitude_g": round(acceleration, 4),
                    "shock_count_since_last_report": shock_count,
                },
                "location": location,
            }
            envelope_data = envelope(device_id, "telemetry", payload, version)
            publish(client, device_id, envelope_data)
            print(f"  PASS {device_id}: telemetry seq={envelope_data['message_seq']}, temp={temp:.2f} C, peak={acceleration:.3f} g, config=v{version}")

            if send_events:
                if not assignment.get("assigned") or not monitoring.get("enabled"):
                    print(f"  SKIP {device_id}: no assigned, monitoring-enabled package; no synthetic alerts sent.")
                    continue
                temp_config = monitoring.get("temperature") or {}
                shock_config = monitoring.get("shock") or {}
                minimum = float(temp_config.get("min_temperature_c"))
                maximum = float(temp_config.get("max_temperature_c"))
                threshold = float(shock_config.get("threshold_g"))
                if maximum < 100:
                    event_temp = maximum + min(1.0, max(0.1, (100.0 - maximum) / 2))
                    direction = "above_maximum"
                elif minimum > -100:
                    event_temp = minimum - min(1.0, max(0.1, (minimum + 100.0) / 2))
                    direction = "below_minimum"
                else:
                    event_temp = None
                    direction = None
                if event_temp is not None:
                    event_payload = {
                        "type": "temperature_breach",
                        "temperature_c": round(event_temp, 3),
                        "breach_direction": direction,
                        "min_temperature_c": minimum,
                        "max_temperature_c": maximum,
                    }
                    event_envelope = envelope(device_id, "event", event_payload, version)
                    publish(client, device_id, event_envelope)
                    print(f"  PASS {device_id}: synthetic temperature_breach event seq={event_envelope['message_seq']}")

                event_peak = min(1000.0, threshold * 1.5 if threshold < 1000 else threshold)
                event_payload = {
                    "type": "shock_detected",
                    "peak_acceleration_magnitude_g": round(event_peak, 4),
                    "threshold_g": threshold,
                    "duration_ms": 35,
                }
                event_envelope = envelope(device_id, "event", event_payload, version)
                publish(client, device_id, event_envelope)
                print(f"  PASS {device_id}: synthetic shock_detected event seq={event_envelope['message_seq']}")

    try:
        print(f"Connecting to MQTT broker {MQTT_HOST}:{MQTT_PORT}…")
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.loop_start()
        if not connected.wait(8): raise RuntimeError("Timed out connecting to MQTT broker.")
        if connection_errors: raise RuntimeError(connection_errors[0])
        if not subscribed.wait(8): raise RuntimeError("Timed out subscribing to retained device configs.")
        if connection_errors: raise RuntimeError(connection_errors[0])

        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and not all(e.is_set() for e in config_events.values()):
            time.sleep(0.1)
        missing = [device_id for device_id, event in config_events.items() if not event.is_set()]
        if missing:
            print("Missing retained config for: " + ", ".join(missing), file=sys.stderr)
            print("Run create_five_trackers.py first and ensure main.py is connected to this same MQTT broker.", file=sys.stderr)
            return 1

        print("\nReceived retained configs:")
        for device_id in DEVICE_IDS:
            config = configs[device_id]
            assignment = config.get("assignment") or {}
            monitoring = config.get("monitoring") or {}
            print(f"  {device_id}: config=v{config.get('config_version')}, assigned={assignment.get('assigned')}, monitoring={monitoring.get('enabled')}, package={assignment.get('package_id')}")
        if args.simulated_gps:
            print("NOTE: GPS coordinates are synthetic demo coordinates around Pune; they are not real tracker positions.")
        if args.with_alerts:
            print("NOTE: synthetic events are only sent for trackers with an assigned package and monitoring enabled.")

        round_number = 0
        while not STOP.is_set():
            publish_round(round_number, args.with_alerts and round_number == 0)
            round_number += 1
            if args.once:
                break
            STOP.wait(args.interval_seconds)
        print("\nSimulation stopped cleanly.")
        return 0
    except KeyboardInterrupt:
        STOP.set()
        print("\nInterrupted by user.")
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:
            pass


def _handle_signal(signum, frame):
    STOP.set()

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)

if __name__ == "__main__":
    raise SystemExit(main())
