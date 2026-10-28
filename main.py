import os
import json
import math
import re
import secrets
import hashlib
import logging
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict


# ============================================================
# CONFIGURATION
# ============================================================

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "coldchain/+/messages")
MQTT_USERNAME = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://coldchain:dev_only_change_me@127.0.0.1:5432/coldchain"
)

ONLINE_TIMEOUT_SECONDS = int(os.getenv("ONLINE_TIMEOUT_SECONDS", "60"))

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s: %(message)s"
)
logger = logging.getLogger("coldchain")

DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ============================================================
# DATABASE SETUP
# ============================================================

def create_database():
    """Create the PostgreSQL database and MVP tables."""

    conn_info = conninfo_to_dict(DATABASE_URL)
    database_name = conn_info.get("dbname", "coldchain")

    # Create database if it does not exist.
    admin_info = dict(conn_info)
    admin_info["dbname"] = "postgres"

    with psycopg.connect(**admin_info, autocommit=True) as conn:
        exists = conn.execute(
            "SELECT 1 FROM pg_database WHERE datname = %s",
            (database_name,)
        ).fetchone()

        if not exists:
            conn.execute(
                sql.SQL("CREATE DATABASE {}").format(
                    sql.Identifier(database_name)
                )
            )
            logger.info("Database created: %s", database_name)
        else:
            logger.info("Database already exists: %s", database_name)

    # Create tables in foreign-key dependency order.
    with psycopg.connect(DATABASE_URL) as conn:

        conn.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                firmware_version TEXT NOT NULL,
                hardware_model TEXT,
                capabilities JSONB NOT NULL DEFAULT '{}'::jsonb,
                registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                latest_observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                latest_message JSONB NOT NULL DEFAULT '{}'::jsonb,
                last_boot_id TEXT,
                config_version INTEGER NOT NULL DEFAULT 0,
                config_applied_version INTEGER NOT NULL DEFAULT 0
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS shipments (
                shipment_id TEXT PRIMARY KEY,
                origin TEXT NOT NULL,
                destination TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'packed', 'in_transit',
                        'partially_delivered', 'delivered'
                    )
                ),
                expected_delivery_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dispatched_at TIMESTAMPTZ,
                delivered_at TIMESTAMPTZ
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS packages (
                package_id TEXT PRIMARY KEY,
                product_sku TEXT NOT NULL,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit TEXT NOT NULL,
                batch_number TEXT,
                expiry_date DATE,
                min_temperature_c DOUBLE PRECISION NOT NULL,
                max_temperature_c DOUBLE PRECISION NOT NULL,
                shock_threshold_g DOUBLE PRECISION NOT NULL,
                device_id TEXT UNIQUE REFERENCES devices(device_id),
                shipment_id TEXT REFERENCES shipments(shipment_id),
                status TEXT NOT NULL CHECK (
                    status IN ('packed', 'in_transit', 'delivered')
                ),
                tracking_token_hash TEXT NOT NULL UNIQUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                dispatched_at TIMESTAMPTZ,
                delivered_at TIMESTAMPTZ,

                CHECK (min_temperature_c <= max_temperature_c),
                CHECK (
                    min_temperature_c BETWEEN -100 AND 100
                    AND max_temperature_c BETWEEN -100 AND 100
                ),
                CHECK (shock_threshold_g > 0 AND shock_threshold_g <= 1000)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS processed_messages (
                device_id TEXT NOT NULL REFERENCES devices(device_id),
                boot_id TEXT NOT NULL,
                message_seq BIGINT NOT NULL CHECK (message_seq >= 0),
                processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY (device_id, boot_id, message_seq)
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS telemetry (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                device_id TEXT NOT NULL REFERENCES devices(device_id),
                package_id TEXT REFERENCES packages(package_id),
                shipment_id TEXT REFERENCES shipments(shipment_id),
                observed_at TIMESTAMPTZ NOT NULL,
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                boot_id TEXT NOT NULL,
                message_seq BIGINT NOT NULL CHECK (message_seq >= 0),
                config_version INTEGER NOT NULL DEFAULT 0,
                temperature_valid BOOLEAN NOT NULL,
                temperature_c DOUBLE PRECISION,
                motion_valid BOOLEAN NOT NULL,
                peak_acceleration_magnitude_g DOUBLE PRECISION,
                shock_count_since_last_report INTEGER NOT NULL DEFAULT 0,
                location_fix_valid BOOLEAN NOT NULL DEFAULT FALSE,
                latitude DOUBLE PRECISION,
                longitude DOUBLE PRECISION,
                accuracy_m DOUBLE PRECISION,
                satellites_used INTEGER NOT NULL DEFAULT 0,
                raw_payload JSONB NOT NULL,
                UNIQUE (device_id, boot_id, message_seq)
            )
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telemetry_package_time
            ON telemetry (package_id, observed_at DESC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telemetry_device_time
            ON telemetry (device_id, observed_at DESC)
        """)

        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_telemetry_shipment_time
            ON telemetry (shipment_id, observed_at DESC)
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                alert_id TEXT PRIMARY KEY,
                source_device_id TEXT NOT NULL REFERENCES devices(device_id),
                source_boot_id TEXT NOT NULL,
                source_message_seq BIGINT NOT NULL,
                package_id TEXT REFERENCES packages(package_id),
                shipment_id TEXT REFERENCES shipments(shipment_id),
                type TEXT NOT NULL CHECK (
                    type IN ('temperature_breach', 'shock_detected')
                ),
                severity TEXT NOT NULL DEFAULT 'warning',
                status TEXT NOT NULL DEFAULT 'open' CHECK (
                    status IN ('open', 'acknowledged', 'resolved')
                ),
                observed_at TIMESTAMPTZ NOT NULL,
                received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                details JSONB NOT NULL DEFAULT '{}'::jsonb,
                raw_payload JSONB NOT NULL,
                acknowledged_at TIMESTAMPTZ,
                acknowledged_by TEXT,
                resolved_at TIMESTAMPTZ,
                resolved_by TEXT,
                resolution_note TEXT,
                UNIQUE (
                    source_device_id,
                    source_boot_id,
                    source_message_seq
                )
            )
        """)

        conn.commit()

    logger.info("Database and tables are ready.")


# ============================================================
# VALIDATION HELPERS
# ============================================================

def is_number(value):
    """Accept finite JSON numbers, but not booleans."""
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
    )


def parse_timestamp(value):
    """Parse an ISO 8601 timestamp and require a timezone."""

    if not isinstance(value, str):
        raise ValueError("observed_at must be a timestamp string")

    timestamp_text = value.strip()

    if timestamp_text.endswith("Z"):
        timestamp_text = timestamp_text[:-1] + "+00:00"

    try:
        timestamp = datetime.fromisoformat(timestamp_text)
    except ValueError as exc:
        raise ValueError("Invalid observed_at timestamp") from exc

    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")

    return timestamp


def validate_envelope(data, topic):
    """Validate common envelope fields and topic/device identity."""

    if not isinstance(data, dict):
        raise ValueError("The JSON envelope must be an object")

    parts = topic.split("/")

    if (
        len(parts) != 3
        or parts[0] != "coldchain"
        or parts[2] != "messages"
    ):
        raise ValueError("Invalid MQTT topic")

    topic_device_id = parts[1]
    device_id = data.get("device_id")

    if not isinstance(device_id, str):
        raise ValueError("Missing or invalid device_id")

    if not DEVICE_ID_PATTERN.fullmatch(device_id):
        raise ValueError("device_id has an invalid format")

    if device_id != topic_device_id:
        raise ValueError("Topic device_id does not match JSON device_id")

    if data.get("schema_version") != 1:
        raise ValueError("Unsupported schema_version")

    if data.get("message_type") not in ("register", "telemetry", "event"):
        raise ValueError("Unsupported message_type")

    for key in ("firmware_version", "boot_id"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ValueError(f"{key} must be a non-empty string")

    for key in ("message_seq", "config_version", "uptime_ms"):
        value = data.get(key)

        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value < 0
        ):
            raise ValueError(f"{key} must be a non-negative integer")

    if not isinstance(data.get("payload"), dict):
        raise ValueError("payload must be a JSON object")

    observed_at = parse_timestamp(data.get("observed_at"))

    return device_id, observed_at


# ============================================================
# MQTT PUBLISH HELPERS
# ============================================================

def publish_json(client, topic, payload, retain=False):
    result = client.publish(
        topic,
        json.dumps(payload, allow_nan=False),
        qos=1,
        retain=retain
    )

    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        logger.error("Publish failed for %s: rc=%s", topic, result.rc)
        return False

    logger.info("Message queued for %s", topic)
    return True


def publish_registration_ack(client, device_id, status, reason=None):
    topic = f"coldchain/{device_id}/registration/ack"

    publish_json(client, topic, {
        "message_type": "registration_ack",
        "device_id": device_id,
        "status": status,
        "reason": reason
    })


def publish_device_config(client, device_id):
    """Publish config based on the current package assignment."""

    with psycopg.connect(DATABASE_URL) as conn:
        device = conn.execute("""
            SELECT config_version
            FROM devices
            WHERE device_id = %s
        """, (device_id,)).fetchone()

        if device is None:
            return

        config_version = device[0]

        assignment = conn.execute("""
            SELECT
                p.package_id,
                p.shipment_id,
                p.status,
                p.product_sku,
                p.product_name,
                p.min_temperature_c,
                p.max_temperature_c,
                p.shock_threshold_g,
                s.status
            FROM packages p
            LEFT JOIN shipments s ON s.shipment_id = p.shipment_id
            WHERE p.device_id = %s
        """, (device_id,)).fetchone()

    if assignment is None:
        config = {
            "schema_version": 1,
            "message_type": "device_config",
            "device_id": device_id,
            "config_version": config_version,
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "assignment": {
                "assigned": False,
                "package_id": None,
                "shipment_id": None,
                "package_status": None,
                "shipment_status": None
            },
            "package": None,
            "monitoring": {
                "enabled": False,
                "reporting_interval_seconds": 15,
                "temperature": None,
                "shock": None
            }
        }
    else:
        (
            package_id, shipment_id, package_status, product_sku,
            product_name, min_temp, max_temp, shock_threshold,
            shipment_status
        ) = assignment

        config = {
            "schema_version": 1,
            "message_type": "device_config",
            "device_id": device_id,
            "config_version": config_version,
            "issued_at": datetime.now(timezone.utc).isoformat(),
            "assignment": {
                "assigned": True,
                "package_id": package_id,
                "shipment_id": shipment_id,
                "package_status": package_status,
                "shipment_status": shipment_status
            },
            "package": {
                "product_sku": product_sku,
                "product_name": product_name
            },
            "monitoring": {
                "enabled": package_status == "in_transit",
                "reporting_interval_seconds": 15,
                "temperature": {
                    "min_temperature_c": min_temp,
                    "max_temperature_c": max_temp
                },
                "shock": {
                    "threshold_g": shock_threshold
                }
            }
        }

    publish_json(
        client,
        f"coldchain/{device_id}/config",
        config,
        retain=True
    )


# ============================================================
# REGISTRATION HANDLER
# ============================================================

def handle_registration(client, data, device_id, observed_at):
    body = data["payload"]

    hardware_model = body.get("hardware_model")
    capabilities = body.get("capabilities", {})

    if hardware_model is not None and not isinstance(hardware_model, str):
        raise ValueError("hardware_model must be a string or null")

    if not isinstance(capabilities, dict):
        raise ValueError("capabilities must be a JSON object")

    if not all(
        isinstance(key, str) and isinstance(value, bool)
        for key, value in capabilities.items()
    ):
        raise ValueError("capabilities must contain boolean values")

    with psycopg.connect(DATABASE_URL) as conn:
        existing = conn.execute("""
            SELECT 1 FROM devices WHERE device_id = %s
        """, (device_id,)).fetchone()

        conn.execute("""
            INSERT INTO devices (
                device_id,
                firmware_version,
                hardware_model,
                capabilities,
                registered_at,
                last_seen_at,
                latest_observed_at,
                latest_message,
                last_boot_id,
                config_version
            )
            VALUES (%s, %s, %s, %s, NOW(), NOW(), %s, %s, %s, 0)
            ON CONFLICT (device_id)
            DO UPDATE SET
                firmware_version = EXCLUDED.firmware_version,
                hardware_model = EXCLUDED.hardware_model,
                capabilities = EXCLUDED.capabilities,
                last_seen_at = NOW(),
                latest_observed_at = EXCLUDED.latest_observed_at,
                latest_message = EXCLUDED.latest_message,
                last_boot_id = EXCLUDED.last_boot_id
        """, (
            device_id,
            data["firmware_version"],
            hardware_model,
            json.dumps(capabilities),
            observed_at,
            json.dumps(data),
            data["boot_id"]
        ))

    status = "already_registered" if existing else "registered"

    logger.info("Device %s: %s", device_id, status)

    # Publish only after the database transaction commits.
    publish_registration_ack(client, device_id, status)
    publish_device_config(client, device_id)


# ============================================================
# DEVICE ASSIGNMENT
# ============================================================

def get_assignment(conn, device_id):
    """Resolve package/shipment from PostgreSQL, never from MQTT JSON."""

    return conn.execute("""
        SELECT p.package_id, p.shipment_id
        FROM packages p
        WHERE p.device_id = %s
    """, (device_id,)).fetchone()


# ============================================================
# MESSAGE DEDUPLICATION
# ============================================================

def mark_message_processed(conn, device_id, boot_id, message_seq):
    """Return True for a new message, False for a duplicate."""

    row = conn.execute("""
        INSERT INTO processed_messages (
            device_id, boot_id, message_seq
        )
        VALUES (%s, %s, %s)
        ON CONFLICT DO NOTHING
        RETURNING device_id
    """, (device_id, boot_id, message_seq)).fetchone()

    return row is not None


# ============================================================
# TELEMETRY HANDLER
# ============================================================

def handle_telemetry(data, device_id, observed_at):
    body = data["payload"]

    temperature = body.get("temperature")
    motion = body.get("motion")
    location = body.get("location")

    if not all(isinstance(x, dict) for x in (temperature, motion, location)):
        raise ValueError(
            "Telemetry requires temperature, motion, and location objects"
        )

    temperature_valid = temperature.get("valid")
    motion_valid = motion.get("valid")
    location_valid = location.get("fix_valid")

    if not all(
        isinstance(x, bool)
        for x in (temperature_valid, motion_valid, location_valid)
    ):
        raise ValueError("Sensor validity fields must be booleans")

    temperature_c = temperature.get("celsius")
    acceleration = motion.get("peak_acceleration_magnitude_g")
    shock_count = motion.get("shock_count_since_last_report", 0)

    latitude = location.get("latitude")
    longitude = location.get("longitude")
    accuracy_m = location.get("accuracy_m")
    satellites = location.get("satellites_used", 0)

    # Invalid sensor readings must be null, not zero.
    if temperature_valid:
        if not is_number(temperature_c):
            raise ValueError("Valid temperature requires a finite celsius value")
        if not -100 <= temperature_c <= 100:
            raise ValueError("Temperature is outside the allowed range")
    elif temperature_c is not None:
        raise ValueError("Invalid temperature must have celsius=null")

    if motion_valid:
        if not is_number(acceleration) or acceleration < 0:
            raise ValueError("Valid motion requires non-negative acceleration")
    elif acceleration is not None:
        raise ValueError("Invalid motion must have acceleration=null")

    if (
        not isinstance(shock_count, int)
        or isinstance(shock_count, bool)
        or shock_count < 0
    ):
        raise ValueError("shock_count_since_last_report must be >= 0")

    if (
        not isinstance(satellites, int)
        or isinstance(satellites, bool)
        or satellites < 0
    ):
        raise ValueError("satellites_used must be >= 0")

    if location_valid:
        if (
            not is_number(latitude)
            or not is_number(longitude)
            or not is_number(accuracy_m)
            or not -90 <= latitude <= 90
            or not -180 <= longitude <= 180
            or accuracy_m < 0
        ):
            raise ValueError("Invalid GPS coordinates or accuracy")
    elif any(x is not None for x in (latitude, longitude, accuracy_m)):
        raise ValueError(
            "Location values must be null when fix_valid is false"
        )

    with psycopg.connect(DATABASE_URL) as conn:
        # Reject unregistered devices.
        registered = conn.execute("""
            SELECT 1 FROM devices WHERE device_id = %s
        """, (device_id,)).fetchone()

        if registered is None:
            raise ValueError("Telemetry from an unregistered device")

        is_new = mark_message_processed(
            conn, device_id, data["boot_id"], data["message_seq"]
        )

        if not is_new:
            logger.info("Duplicate telemetry ignored: %s", device_id)
            return

        assignment = get_assignment(conn, device_id)
        package_id, shipment_id = assignment if assignment else (None, None)

        conn.execute("""
            INSERT INTO telemetry (
                device_id, package_id, shipment_id,
                observed_at, boot_id, message_seq, config_version,
                temperature_valid, temperature_c,
                motion_valid, peak_acceleration_magnitude_g,
                shock_count_since_last_report,
                location_fix_valid, latitude, longitude,
                accuracy_m, satellites_used, raw_payload
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s,
                %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
        """, (
            device_id, package_id, shipment_id,
            observed_at, data["boot_id"], data["message_seq"],
            data["config_version"],
            temperature_valid, temperature_c,
            motion_valid, acceleration, shock_count,
            location_valid, latitude, longitude,
            accuracy_m, satellites, json.dumps(data)
        ))

        update_device_diagnostics(conn, data, device_id, observed_at)

    logger.info(
        "Telemetry saved for %s: temperature=%s",
        device_id, temperature_c
    )


# ============================================================
# EVENT / ALERT HANDLER
# ============================================================

def handle_event(data, device_id, observed_at):
    body = data["payload"]
    event_type = body.get("type")

    if event_type not in ("temperature_breach", "shock_detected"):
        raise ValueError("Unsupported event type")

    # Validate fields specific to each event.
    if event_type == "temperature_breach":
        reading = body.get("temperature_c")
        minimum = body.get("min_temperature_c")
        maximum = body.get("max_temperature_c")
        direction = body.get("breach_direction")

        if not all(is_number(x) for x in (reading, minimum, maximum)):
            raise ValueError("Temperature event values must be finite numbers")

        if not -100 <= reading <= 100:
            raise ValueError("Temperature reading is out of range")

        if not -100 <= minimum <= maximum <= 100:
            raise ValueError("Invalid temperature limits")

        if direction not in ("below_minimum", "above_maximum"):
            raise ValueError("Invalid breach_direction")

        if direction == "below_minimum" and not reading < minimum:
            raise ValueError("Reading does not match below_minimum")

        if direction == "above_maximum" and not reading > maximum:
            raise ValueError("Reading does not match above_maximum")

    elif event_type == "shock_detected":
        acceleration = body.get("peak_acceleration_magnitude_g")
        threshold = body.get("threshold_g")
        duration = body.get("duration_ms")

        if (
            not is_number(acceleration)
            or acceleration < 0
            or not is_number(threshold)
            or threshold <= 0
        ):
            raise ValueError("Invalid shock acceleration or threshold")

        if (
            not isinstance(duration, int)
            or isinstance(duration, bool)
            or duration < 0
        ):
            raise ValueError("duration_ms must be a non-negative integer")

    with psycopg.connect(DATABASE_URL) as conn:
        registered = conn.execute("""
            SELECT 1 FROM devices WHERE device_id = %s
        """, (device_id,)).fetchone()

        if registered is None:
            raise ValueError("Event from an unregistered device")

        is_new = mark_message_processed(
            conn, device_id, data["boot_id"], data["message_seq"]
        )

        if not is_new:
            logger.info("Duplicate event ignored: %s", device_id)
            return

        assignment = get_assignment(conn, device_id)
        package_id, shipment_id = assignment if assignment else (None, None)

        alert_id = "alert-" + secrets.token_hex(12)

        conn.execute("""
            INSERT INTO alerts (
                alert_id,
                source_device_id,
                source_boot_id,
                source_message_seq,
                package_id,
                shipment_id,
                type,
                severity,
                status,
                observed_at,
                details,
                raw_payload
            )
            VALUES (
                %s, %s, %s, %s, %s, %s, %s,
                'warning', 'open', %s, %s, %s
            )
        """, (
            alert_id,
            device_id,
            data["boot_id"],
            data["message_seq"],
            package_id,
            shipment_id,
            event_type,
            observed_at,
            json.dumps(body),
            json.dumps(data)
        ))

        update_device_diagnostics(conn, data, device_id, observed_at)

    logger.warning(
        "ALERT SAVED: id=%s device=%s type=%s",
        alert_id, device_id, event_type
    )


# ============================================================
# DEVICE DIAGNOSTICS
# ============================================================

def update_device_diagnostics(conn, data, device_id, observed_at):
    conn.execute("""
        UPDATE devices
        SET firmware_version = %s,
            last_seen_at = NOW(),
            latest_observed_at = %s,
            latest_message = %s,
            last_boot_id = %s,
            config_applied_version = %s
        WHERE device_id = %s
    """, (
        data["firmware_version"],
        observed_at,
        json.dumps(data),
        data["boot_id"],
        data["config_version"],
        device_id
    ))


# ============================================================
# MQTT CALLBACKS
# ============================================================

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        logger.info("Connected to MQTT broker")
        client.subscribe(MQTT_TOPIC, qos=1)
        logger.info("Subscribed to %s", MQTT_TOPIC)
    else:
        logger.error("MQTT connection failed: %s", reason_code)


def on_subscribe(client, userdata, mid, reason_code_list, properties):
    logger.info("Subscription result: %s", reason_code_list)


def on_message(client, userdata, msg):
    logger.info("Received topic: %s", msg.topic)

    try:
        payload_text = msg.payload.decode("utf-8")
        logger.info("Data: %s", payload_text)

        data = json.loads(payload_text)

        device_id, observed_at = validate_envelope(data, msg.topic)
        message_type = data["message_type"]

        if message_type == "register":
            handle_registration(client, data, device_id, observed_at)

        elif message_type == "telemetry":
            handle_telemetry(data, device_id, observed_at)

        elif message_type == "event":
            handle_event(data, device_id, observed_at)

    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        KeyError
    ) as exc:
        logger.warning("Message rejected: %s", exc)

    except psycopg.Error:
        # Transaction context rolls back on exception.
        logger.exception("PostgreSQL error while processing MQTT message")

    except Exception:
        logger.exception("Unexpected error while processing MQTT message")


# ============================================================
# MAIN
# ============================================================

def main():
    create_database()

    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="coldchain_backend"
    )

    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_message = on_message

    logger.info("Connecting to MQTT broker at %s:%s", MQTT_HOST, MQTT_PORT)

    try:
        client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
        client.loop_forever()
    except KeyboardInterrupt:
        logger.info("Stopping MQTT subscriber")
    finally:
        client.disconnect()


if __name__ == "__main__":
    main()