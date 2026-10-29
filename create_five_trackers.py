#!/usr/bin/env python3
"""Register five simulated ColdGuardian ESP32 trackers through the real MQTT path.

Run from the project root in WSL:
  uv run --env-file .env python create_five_trackers.py

This creates device rows through MQTT registration; it does not create packages.
Assign packages to the simulated devices in the frontend before using --with-alerts.
"""
from __future__ import annotations

import json
import os
import secrets
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def new_client(client_id: str) -> mqtt.Client:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=client_id,
        protocol=mqtt.MQTTv311,
    )
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    return client


def publish(client: mqtt.Client, topic: str, message: dict[str, Any]) -> None:
    info = client.publish(topic, json.dumps(message, separators=(",", ":"), allow_nan=False), qos=1, retain=False)
    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"MQTT publish failed for {topic} (rc={info.rc})")
    info.wait_for_publish(timeout=5)
    if not info.is_published():
        raise TimeoutError(f"No PUBACK while publishing to {topic}")


def main() -> int:
    connected = threading.Event()
    subscribed = threading.Event()
    connection_errors: list[str] = []
    acknowledgements: dict[str, dict[str, Any]] = {}
    configurations: dict[str, dict[str, Any]] = {}
    ack_events = {device_id: threading.Event() for device_id in DEVICE_IDS}
    config_events = {device_id: threading.Event() for device_id in DEVICE_IDS}
    boot_ids = {device_id: secrets.token_hex(4) for device_id in DEVICE_IDS}

    client = new_client(f"cg-register-five-{secrets.token_hex(5)}")

    def on_connect(c, userdata, flags, reason_code, properties):
        if getattr(reason_code, "is_failure", False):
            connection_errors.append(f"MQTT connection refused: {reason_code}")
            connected.set()
            return
        connected.set()
        topics = []
        for device_id in DEVICE_IDS:
            topics.extend([
                (f"coldchain/{device_id}/registration/ack", 1),
                (f"coldchain/{device_id}/config", 1),
            ])
        result, _mid = c.subscribe(topics)
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
        for device_id in DEVICE_IDS:
            if msg.topic == f"coldchain/{device_id}/registration/ack" and data.get("message_type") == "registration_ack":
                acknowledgements[device_id] = data
                ack_events[device_id].set()
            elif msg.topic == f"coldchain/{device_id}/config" and data.get("message_type") == "device_config":
                configurations[device_id] = data
                config_events[device_id].set()

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    try:
        print(f"Connecting to MQTT broker {MQTT_HOST}:{MQTT_PORT}…")
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
        client.loop_start()
        if not connected.wait(8):
            raise RuntimeError("Timed out connecting to MQTT. Check Mosquitto and .env.")
        if connection_errors:
            raise RuntimeError(connection_errors[0])
        if not subscribed.wait(8):
            raise RuntimeError("Timed out subscribing to registration ACK/config topics.")
        if connection_errors:
            raise RuntimeError(connection_errors[0])

        # Let any retained configs arrive so re-running this script does not claim
        # config_version=0 for an already-assigned simulator device.
        time.sleep(1.0)
        sequence = {device_id: 0 for device_id in DEVICE_IDS}
        for device_id in DEVICE_IDS:
            existing_config = configurations.get(device_id) or {}
            current_version = existing_config.get("config_version", 0)
            if not isinstance(current_version, int) or isinstance(current_version, bool) or current_version < 0:
                current_version = 0
            envelope = {
                "schema_version": 1,
                "message_type": "register",
                "device_id": device_id,
                "firmware_version": FIRMWARE_VERSION,
                "boot_id": boot_ids[device_id],
                "message_seq": sequence[device_id],
                "config_version": current_version,
                "observed_at": utc_now(),
                "uptime_ms": 0,
                "payload": {
                    "hardware_model": "esp32-devkit-v1-simulator",
                    "capabilities": {
                        "temperature": True,
                        "accelerometer": True,
                        "gnss": True,
                        "epaper": False,
                        "oled": True,
                    },
                },
            }
            publish(client, f"coldchain/{device_id}/messages", envelope)
            print(f"Published register for {device_id} (config_version={current_version}).")
            sequence[device_id] += 1

        print("Waiting for backend registration acknowledgements and config messages…")
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if all(event.is_set() for event in ack_events.values()) and all(event.is_set() for event in config_events.values()):
                break
            time.sleep(0.1)

        failed = False
        print("\nRESULTS")
        for device_id in DEVICE_IDS:
            ack = acknowledgements.get(device_id)
            config = configurations.get(device_id)
            if not ack:
                print(f"FAIL {device_id}: no registration ACK received")
                failed = True
                continue
            if ack.get("status") == "rejected":
                print(f"FAIL {device_id}: registration rejected: {ack.get('reason')}")
                failed = True
                continue
            if not config:
                print(f"WARN {device_id}: ACK={ack.get('status')}, but no config was received")
                failed = True
                continue
            assignment = config.get("assignment") or {}
            monitoring = config.get("monitoring") or {}
            print(
                f"PASS {device_id}: ACK={ack.get('status')}, config=v{config.get('config_version')}, "
                f"assigned={assignment.get('assigned')}, monitoring={monitoring.get('enabled')}"
            )
        print("\nNo packages were created. Assign package(s) to these device IDs in the Packages UI to test package-specific thresholds and alerts.")
        return 1 if failed else 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        try:
            client.disconnect()
            client.loop_stop()
        except Exception:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
