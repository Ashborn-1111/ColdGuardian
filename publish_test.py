"""Contract-matching MQTT simulator for the ColdChain MVP.

Usage (from the project root):
    uv run --env-file .env python publish_test.py
    uv run --env-file .env python publish_test.py --device-id tracker-002

The simulator registers a device using hardware/capability metadata only. The
backend returns a registration ACK and publishes a separate retained config.
If an active package is assigned to the device, the simulator publishes one
routine telemetry message and simulated device-originated breach/shock events.
It does not change the database directly.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import threading
import time
from datetime import datetime, timezone
from typing import Any

import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("TEST_MQTT_USERNAME") or os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("TEST_MQTT_PASSWORD") or os.getenv("MQTT_PASSWORD") or None
DEFAULT_DEVICE_ID = os.getenv("DEVICE_ID", "tracker-001")
ACK_TIMEOUT_SECONDS = float(os.getenv("TEST_ACK_TIMEOUT_SECONDS", "10"))
CONFIG_TIMEOUT_SECONDS = float(os.getenv("TEST_CONFIG_TIMEOUT_SECONDS", "10"))


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Simulate a ColdChain ESP32 tracker over MQTT.")
    parser.add_argument("--device-id", default=DEFAULT_DEVICE_ID, help="Tracker ID (default: DEVICE_ID or tracker-001)")
    parser.add_argument("--ack-timeout", type=float, default=ACK_TIMEOUT_SECONDS)
    parser.add_argument("--config-timeout", type=float, default=CONFIG_TIMEOUT_SECONDS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    device_id = args.device_id
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", device_id) is None:
        print("Invalid device ID. Use 1-64 ASCII letters, digits, '_' or '-'.", file=sys.stderr)
        return 2

    message_topic = f"coldchain/{device_id}/messages"
    ack_topic = f"coldchain/{device_id}/registration/ack"
    config_topic = f"coldchain/{device_id}/config"

    connected = threading.Event()
    subscribed = threading.Event()
    ack_event = threading.Event()
    config_event = threading.Event()
    lock = threading.Lock()
    state: dict[str, Any] = {"ack": None, "config": None, "connection_error": None}

    def on_connect(client, userdata, flags, reason_code, properties):
        if reason_code != 0:
            state["connection_error"] = str(reason_code)
            print(f"MQTT connection failed: {reason_code}", file=sys.stderr)
            return
        connected.set()
        result, _mid = client.subscribe([(ack_topic, 1), (config_topic, 1)])
        if result != mqtt.MQTT_ERR_SUCCESS:
            state["connection_error"] = f"subscribe rc={result}"
            print(f"MQTT subscribe request failed (rc={result})", file=sys.stderr)

    def on_subscribe(client, userdata, mid, reason_code_list, properties):
        failed = any(getattr(code, "is_failure", False) for code in reason_code_list)
        if failed:
            state["connection_error"] = f"subscription rejected: {reason_code_list}"
            print(f"MQTT subscription rejected: {reason_code_list}", file=sys.stderr)
            return
        subscribed.set()

    def on_message(client, userdata, msg):
        try:
            decoded = json.loads(msg.payload.decode("utf-8"))
            if not isinstance(decoded, dict):
                return
        except (UnicodeDecodeError, json.JSONDecodeError):
            print(f"Ignoring non-JSON message on {msg.topic}", file=sys.stderr)
            return

        with lock:
            if msg.topic == ack_topic and decoded.get("message_type") == "registration_ack":
                state["ack"] = decoded
                ack_event.set()
            elif msg.topic == config_topic and decoded.get("message_type") == "device_config":
                state["config"] = decoded
                config_event.set()

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"coldchain_sim_{device_id}_{secrets.token_hex(4)}",
    )
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    try:
        print(f"Connecting to MQTT broker {MQTT_HOST}:{MQTT_PORT}")
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        client.loop_start()
        if not connected.wait(8):
            detail = state.get("connection_error") or "connection callback timeout"
            print(f"Could not connect to MQTT broker ({detail}). Check Mosquitto and .env.", file=sys.stderr)
            return 1
        if not subscribed.wait(8):
            detail = state.get("connection_error") or "subscription acknowledgement timeout"
            print(f"Could not subscribe to ACK/config topics ({detail}). Check broker ACLs.", file=sys.stderr)
            return 1

        # This simulator starts a fresh in-memory session on every invocation and
        # does not persist an applied config to flash. Therefore it reports version
        # 0 in registration and applies the backend's authoritative config afterward.
        # This also lets the simulator recover cleanly after a disposable DB reset,
        # when Mosquitto may still have an old retained config from the previous DB.
        boot_id = secrets.token_hex(4)
        message_seq = 0

        def envelope(message_type: str, payload: dict, *, config_version: int, uptime_ms: int) -> dict:
            nonlocal message_seq
            result = {
                "schema_version": 1,
                "message_type": message_type,
                "device_id": device_id,
                "firmware_version": "0.1.0-simulator",
                "boot_id": boot_id,
                "message_seq": message_seq,
                "config_version": config_version,
                "observed_at": utc_now(),
                "uptime_ms": uptime_ms,
                "payload": payload,
            }
            message_seq += 1
            return result

        def publish_device_message(body: dict) -> None:
            info = client.publish(message_topic, json.dumps(body, allow_nan=False), qos=1, retain=False)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                raise RuntimeError(f"MQTT publish failed (rc={info.rc})")
            info.wait_for_publish(timeout=5)
            if not info.is_published():
                raise TimeoutError(f"Timed out waiting for publish completion on {message_topic}")

        # Clear events before publishing so the response must correspond to this registration.
        ack_event.clear()
        config_event.clear()
        register = envelope(
            "register",
            {
                "hardware_model": "esp32-devkit-v1-simulator",
                "capabilities": {
                    "temperature": True,
                    "accelerometer": True,
                    "gnss": False,
                    "epaper": False,
                },
            },
            config_version=0,
            uptime_ms=0,
        )
        print(f"Publishing registration seq={register['message_seq']} to {message_topic}")
        publish_device_message(register)

        if not ack_event.wait(args.ack_timeout):
            print(
                "No registration ACK received. Check that the API is running and /readyz reports ready; "
                "then inspect backend logs and Mosquitto ACLs.",
                file=sys.stderr,
            )
            return 1
        with lock:
            ack = dict(state["ack"])
        print(f"Registration ACK: status={ack.get('status')!r}, reason={ack.get('reason')!r}")
        if ack.get("status") == "rejected":
            print("Backend rejected registration. Its log should contain the exact payload validation error.", file=sys.stderr)
            return 1
        if ack.get("status") not in ("registered", "already_registered"):
            print(f"Unexpected registration ACK: {ack}", file=sys.stderr)
            return 1

        # The authoritative settings are not in the ACK. They arrive separately
        # through the retained config topic, as defined by the current contract.
        if not config_event.wait(args.config_timeout):
            print("Registration ACK received, but no device_config arrived. Check backend config publish logs.", file=sys.stderr)
            return 1
        with lock:
            config = dict(state["config"])

        print("Received device_config:")
        print(json.dumps(config, indent=2))
        assignment = config.get("assignment") or {}
        monitoring = config.get("monitoring") or {}
        if not assignment.get("assigned"):
            print(
                "Device is registered but unassigned. Create a package through the authenticated API and set "
                f"device_id={device_id!r}; then rerun this simulator to publish telemetry/events."
            )
            return 0
        if not monitoring.get("enabled"):
            print("A package is assigned, but monitoring is disabled (for example, package/shipment is delivered). No telemetry/events sent.")
            return 0

        version = config.get("config_version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            print("device_config has an invalid config_version", file=sys.stderr)
            return 1
        temperature_config = monitoring.get("temperature")
        shock_config = monitoring.get("shock")
        if not isinstance(temperature_config, dict) or not isinstance(shock_config, dict):
            print("Assigned config is missing package thresholds", file=sys.stderr)
            return 1
        minimum = temperature_config.get("min_temperature_c")
        maximum = temperature_config.get("max_temperature_c")
        shock_threshold = shock_config.get("threshold_g")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (minimum, maximum, shock_threshold)):
            print("Assigned config contains invalid thresholds", file=sys.stderr)
            return 1
        if minimum > maximum or shock_threshold <= 0:
            print("Assigned config violates threshold bounds", file=sys.stderr)
            return 1

        # One ordinary observation from the configured range.
        normal_temperature = (minimum + maximum) / 2.0
        normal_acceleration = shock_threshold * 0.5
        telemetry = envelope(
            "telemetry",
            {
                "temperature": {"valid": True, "celsius": normal_temperature},
                "motion": {
                    "valid": True,
                    "peak_acceleration_magnitude_g": normal_acceleration,
                    "shock_count_since_last_report": 0,
                },
                "location": {
                    "fix_valid": False,
                    "latitude": None,
                    "longitude": None,
                    "accuracy_m": None,
                    "satellites_used": 0,
                },
            },
            config_version=version,
            uptime_ms=15000,
        )
        publish_device_message(telemetry)
        print(f"Published telemetry seq={telemetry['message_seq']} (temperature={normal_temperature:.3f} C)")

        # Simulate local firmware event generation, rather than asking the backend
        # to infer a breach from the telemetry row.
        if maximum < 100:
            breach_value = maximum + (100.0 - maximum) / 2.0
            direction = "above_maximum"
        elif minimum > -100:
            breach_value = minimum - (minimum + 100.0) / 2.0
            direction = "below_minimum"
        else:
            breach_value = None
            direction = None

        if breach_value is not None and -100 <= breach_value <= 100:
            temperature_event = envelope(
                "event",
                {
                    "type": "temperature_breach",
                    "temperature_c": breach_value,
                    "breach_direction": direction,
                    "min_temperature_c": minimum,
                    "max_temperature_c": maximum,
                },
                config_version=version,
                uptime_ms=16000,
            )
            publish_device_message(temperature_event)
            print(f"Published simulated temperature_breach seq={temperature_event['message_seq']}")
        else:
            print("Skipped temperature-breach simulation: the configured interval leaves no representable out-of-range value within -100..100 C.")

        shock_value = shock_threshold * 1.25
        shock_event = envelope(
            "event",
            {
                "type": "shock_detected",
                "peak_acceleration_magnitude_g": shock_value,
                "threshold_g": shock_threshold,
                "duration_ms": 35,
            },
            config_version=version,
            uptime_ms=17000,
        )
        publish_device_message(shock_event)
        print(f"Published simulated shock_detected seq={shock_event['message_seq']} (peak={shock_value:.3f} g)")
        print("Messages sent. Allow a moment for the backend to process them, then inspect the authenticated API.")
        return 0
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        print(f"Simulator failed: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            client.loop_stop()
            client.disconnect()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
