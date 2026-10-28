import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USERNAME = os.getenv("TEST_MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("TEST_MQTT_PASSWORD") or None

DEVICES = {
    "tracker-001": {
        "settings": {
            "min_temperature_c": 2.0,
            "max_temperature_c": 8.0,
            "shock_threshold_g": 4.0,
        },
        "messages": [
            {
                "type": "telemetry",
                "uptime_ms": 15000,
                "payload": {
                    "temperature": {
                        "valid": True,
                        "celsius": 5.4,
                    },
                    "motion": {
                        "valid": True,
                        "peak_acceleration_magnitude_g": 1.08,
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
            },

            {
                "type": "telemetry",
                "uptime_ms": 30000,
                "payload": {
                    "temperature": {
                        "valid": True,
                        "celsius": 9.1,
                    },
                    "motion": {
                        "valid": True,
                        "peak_acceleration_magnitude_g": 5.7,
                        "shock_count_since_last_report": 1,
                    },
                    "location": {
                        "fix_valid": False,
                        "latitude": None,
                        "longitude": None,
                        "accuracy_m": None,
                        "satellites_used": 0,
                    },
                },
            },

            {
                "type": "event",
                "uptime_ms": 30100,
                "payload": {
                    "type": "temperature_breach",
                    "temperature_c": 9.1,
                    "maximum_allowed_c": 8.0,
                },
            },

            {
                "type": "event",
                "uptime_ms": 30200,
                "payload": {
                    "type": "shock_detected",
                    "peak_acceleration_magnitude_g": 5.7,
                    "threshold_g": 4.0,
                    "duration_ms": 35,
                },
            },

        ],
    },

    "tracker-002": {
        "settings": {
            "min_temperature_c": 2.0,
            "max_temperature_c": 8.0,
            "shock_threshold_g": 4.0,
        },
        "messages": [
            {
                "type": "telemetry",
                "uptime_ms": 15000,
                "payload": {
                    "temperature": {
                        "valid": True,
                        "celsius": 6.2,
                    },
                    "motion": {
                        "valid": True,
                        "peak_acceleration_magnitude_g": 1.4,
                        "shock_count_since_last_report": 0,
                    },
                    "location": {
                        "fix_valid": True,
                        "latitude": 19.0760,
                        "longitude": 72.8777,
                        "accuracy_m": 8.0,
                        "satellites_used": 7,
                    },
                },
            },

            {
                "type": "telemetry",
                "uptime_ms": 30000,
                "payload": {
                    "temperature": {
                        "valid": True,
                        "celsius": 9.5,
                    },
                    "motion": {
                        "valid": True,
                        "peak_acceleration_magnitude_g": 1.7,
                        "shock_count_since_last_report": 0,
                    },
                    "location": {
                        "fix_valid": True,
                        "latitude": 19.0765,
                        "longitude": 72.8780,
                        "accuracy_m": 7.0,
                        "satellites_used": 8,
                    },
                },
            },

            {
                "type": "event",
                "uptime_ms": 30100,
                "payload": {
                    "type": "temperature_breach",
                    "temperature_c": 9.5,
                    "maximum_allowed_c": 8.0,
                },
            },

            {
                "type": "telemetry",
                "uptime_ms": 45000,
                "payload": {
                    "temperature": {
                        "valid": True,
                        "celsius": 7.2,
                    },
                    "motion": {
                        "valid": True,
                        "peak_acceleration_magnitude_g": 5.7,
                        "shock_count_since_last_report": 1,
                    },
                    "location": {
                        "fix_valid": True,
                        "latitude": 19.0770,
                        "longitude": 72.8785,
                        "accuracy_m": 6.0,
                        "satellites_used": 8,
                    },
                },
            },

            {
                "type": "event",
                "uptime_ms": 45100,
                "payload": {
                    "type": "shock_detected",
                    "peak_acceleration_magnitude_g": 5.7,
                    "threshold_g": 4.0,
                    "duration_ms": 35,
                },
            },

        ],
    },
}


MESSAGE_TOPICS = {
    device_id: f"coldchain/{device_id}/messages"
    for device_id in DEVICES
}

ACK_TOPICS = {
    device_id: f"coldchain/{device_id}/registration/ack"
    for device_id in DEVICES
}

CONFIG_TOPICS = {
    device_id: f"coldchain/{device_id}/config"
    for device_id in DEVICES
}

BOOT_IDS = {
    device_id: uuid.uuid4().hex[:8]
    for device_id in DEVICES
}

MESSAGE_SEQUENCES = {
    device_id: -1
    for device_id in DEVICES
}

SUBSCRIPTIONS_READY = threading.Event()

REGISTRATION_ACK_EVENTS = {
    device_id: threading.Event()
    for device_id in DEVICES
}

REGISTRATION_RESPONSES = {
    device_id: {}
    for device_id in DEVICES
}

CONFIGURATION_UPDATES = {
    device_id: {}
    for device_id in DEVICES
}

CALLBACK_ERROR = {}


def timestamp() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def make_message(
    device_id: str,
    message_type: str,
    payload: dict,
    uptime_ms: int,
) -> dict:
    MESSAGE_SEQUENCES[device_id] += 1

    return {
        "schema_version": 1,
        "message_type": message_type,
        "device_id": device_id,
        "firmware_version": "0.1.0-test",
        "boot_id": BOOT_IDS[device_id],
        "message_seq": MESSAGE_SEQUENCES[device_id],
        "observed_at": timestamp(),
        "uptime_ms": uptime_ms,
        "payload": payload,
    }


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code.is_failure:
        CALLBACK_ERROR["error"] = (
            f"MQTT connection failed: {reason_code}"
        )
        SUBSCRIPTIONS_READY.set()
        return

    # Subscribe to every device's ACK and config topics first.
    topics = []

    for device_id in DEVICES:
        topics.append((ACK_TOPICS[device_id], 1))
        topics.append((CONFIG_TOPICS[device_id], 1))

    result, _ = client.subscribe(topics)

    if result != mqtt.MQTT_ERR_SUCCESS:
        CALLBACK_ERROR["error"] = (
            f"Failed to subscribe to MQTT topics: {result}"
        )
        SUBSCRIPTIONS_READY.set()


def on_subscribe(
    client,
    userdata,
    mid,
    reason_code_list,
    properties,
):
    if any(code.is_failure for code in reason_code_list):
        CALLBACK_ERROR["error"] = (
            "Broker rejected one or more MQTT subscriptions"
        )

    SUBSCRIPTIONS_READY.set()


def on_message(client, userdata, message):
    try:
        payload = json.loads(message.payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        print(f"Ignoring invalid JSON on {message.topic}")
        return

    for device_id in DEVICES:
        if message.topic == ACK_TOPICS[device_id]:
            REGISTRATION_RESPONSES[device_id].clear()
            REGISTRATION_RESPONSES[device_id].update(payload)

            REGISTRATION_ACK_EVENTS[device_id].set()

            print(
                f"[{device_id}] Registration ACK: {payload}"
            )
            return

        if message.topic == CONFIG_TOPICS[device_id]:
            CONFIGURATION_UPDATES[device_id].clear()
            CONFIGURATION_UPDATES[device_id].update(payload)

            print(
                f"[{device_id}] Received configuration: {payload}"
            )
            return


def publish(client, device_id: str, message: dict):
    topic = MESSAGE_TOPICS[device_id]

    info = client.publish(
        topic,
        json.dumps(message),
        qos=1,
    )

    info.wait_for_publish(timeout=10)

    if not info.is_published():
        raise RuntimeError(
            f"[{device_id}] Timed out publishing "
            f"{message['message_type']}"
        )

    print(
        f"[{device_id}] Published {message['message_type']} "
        f"(seq={message['message_seq']}) to {topic}"
    )

    time.sleep(0.3)


def register_device(client, device_id: str) -> dict:
    print(f"\n{'=' * 55}")
    print(f"Registering device: {device_id}")
    print(f"{'=' * 55}")

    registration = make_message(
        device_id=device_id,
        message_type="register",
        payload={
            "settings": DEVICES[device_id]["settings"]
        },
        uptime_ms=0,
    )

    publish(client, device_id, registration)

    # Wait for this particular device's acknowledgement.
    if not REGISTRATION_ACK_EVENTS[device_id].wait(timeout=10):
        raise RuntimeError(
            f"[{device_id}] No registration acknowledgement. "
            "Check the backend, broker, device registration, "
            "and MQTT ACLs."
        )

    response = REGISTRATION_RESPONSES[device_id]

    if response.get("status") == "rejected":
        raise RuntimeError(
            f"[{device_id}] Registration rejected: "
            f"{response.get('reason', 'unknown reason')}"
        )

    settings = response.get("settings")

    if not isinstance(settings, dict):
        raise RuntimeError(
            f"[{device_id}] Registration ACK did not "
            "include settings"
        )

    # Validate settings needed by the test scenarios.
    required_settings = (
        "min_temperature_c",
        "max_temperature_c",
        "shock_threshold_g",
    )

    for key in required_settings:
        if key not in settings:
            raise RuntimeError(
                f"[{device_id}] Missing setting: {key}"
            )

    print(f"[{device_id}] Registration status: "
          f"{response.get('status', 'accepted')}")
    print(
        f"[{device_id}] Temperature range: "
        f"{settings['min_temperature_c']} - "
        f"{settings['max_temperature_c']} C"
    )
    print(
        f"[{device_id}] Shock threshold: "
        f"{settings['shock_threshold_g']} g"
    )

    return settings


def send_device_messages(client, device_id: str):
    print(f"\nSending messages for {device_id}")

    for message_data in DEVICES[device_id]["messages"]:
        message = make_message(
            device_id=device_id,
            message_type=message_data["type"],
            payload=message_data["payload"],
            uptime_ms=message_data["uptime_ms"],
        )

        publish(client, device_id, message)

def main():
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=f"coldchain-test-{uuid.uuid4().hex[:8]}",
    )

    if MQTT_USERNAME:
        client.username_pw_set(
            MQTT_USERNAME,
            MQTT_PASSWORD,
        )

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    client.connect(
        MQTT_HOST,
        MQTT_PORT,
        keepalive=60,
    )

    client.loop_start()

    try:
        # Wait until ACK/config subscriptions are ready.
        if not SUBSCRIPTIONS_READY.wait(timeout=10):
            raise RuntimeError(
                "Timed out subscribing to MQTT topics"
            )

        if "error" in CALLBACK_ERROR:
            raise RuntimeError(CALLBACK_ERROR["error"])

        # Register both devices before sending their telemetry.
        canonical_settings = {}

        for device_id in DEVICES:
            canonical_settings[device_id] = register_device(
                client,
                device_id,
            )

        # Send each device's independently configured messages.
        for device_id in DEVICES:
            send_device_messages(client, device_id)

        print("\nAll device simulations completed.")

    finally:
        client.disconnect()
        client.loop_stop()


if __name__ == "__main__":
    main()
