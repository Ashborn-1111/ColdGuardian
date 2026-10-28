import hashlib
import json
import logging
import math
import os
import re
import secrets
from contextlib import asynccontextmanager
from datetime import date, datetime, timezone
from hmac import compare_digest
from typing import Any, Literal
from urllib.parse import quote

import paho.mqtt.client as mqtt
import psycopg
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
import uvicorn


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://coldchain:dev_only_change_me@127.0.0.1:5432/coldchain",
)
MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "coldchain/+/messages")
MQTT_USERNAME = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None
ONLINE_TIMEOUT_SECONDS = int(os.getenv("ONLINE_TIMEOUT_SECONDS", "60"))
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_PORT = int(os.getenv("API_PORT", "8000"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
COLDCHAIN_ADMIN_TOKEN = os.getenv("COLDCHAIN_ADMIN_TOKEN") or None

# The MVP uses a 15-second reporting interval. Temperature/shock thresholds
# are never defaulted here; they come from the package record.
REPORTING_INTERVAL_SECONDS = 15
DEVICE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
SHIPMENT_STATUSES = ("packed", "in_transit", "partially_delivered", "delivered")
PACKAGE_STATUSES = ("packed", "in_transit", "delivered")
ALERT_TYPES = ("temperature_breach", "shock_detected")
ALERT_STATUSES = ("open", "acknowledged", "resolved")

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("coldchain")

mqtt_client: mqtt.Client | None = None
mqtt_connected = False
mqtt_subscribed = False
bearer_scheme = HTTPBearer(auto_error=False)


def connect_db():
    """Open a PostgreSQL connection; the database must already exist."""
    return psycopg.connect(DATABASE_URL, row_factory=dict_row)


def api_error(status_code: int, code: str, message: str) -> None:
    raise HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message},
    )


def require_admin(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> str:
    """Shared-token authentication for all operator/dashboard endpoints."""
    if not COLDCHAIN_ADMIN_TOKEN:
        api_error(503, "ADMIN_AUTH_NOT_CONFIGURED", "Set COLDCHAIN_ADMIN_TOKEN before using admin endpoints.")
    if credentials is None or credentials.scheme.lower() != "bearer":
        api_error(401, "AUTHENTICATION_REQUIRED", "Provide an Authorization: Bearer token.")
    if not compare_digest(credentials.credentials, COLDCHAIN_ADMIN_TOKEN):
        api_error(401, "INVALID_TOKEN", "The bearer token is invalid.")
    # A shared demo token does not identify an individual operator.
    return "admin"


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def checked_number(value: Any, field: str, minimum=None, maximum=None, *, required=False):
    if value is None:
        if required:
            raise ValueError(f"{field} is required")
        return None
    if not is_number(value):
        raise ValueError(f"{field} must be a finite number")
    if minimum is not None and value < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{field} must be <= {maximum}")
    return float(value)


def parse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("observed_at must be an ISO 8601 string with a timezone")
    text_value = value.strip()
    if text_value.endswith("Z"):
        text_value = text_value[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text_value)
    except ValueError as exc:
        raise ValueError("Invalid observed_at timestamp") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("observed_at must include a timezone")
    return stamp


def utc_iso(stamp: datetime | None = None) -> str:
    value = stamp or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def online_status(last_seen_at: datetime | None) -> str:
    if last_seen_at is None:
        return "offline"
    age = (datetime.now(timezone.utc) - last_seen_at).total_seconds()
    return "online" if age <= ONLINE_TIMEOUT_SECONDS else "offline"


# ---------------------------------------------------------------------------
# Database schema
# ---------------------------------------------------------------------------

SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS package_id_seq START 1;
CREATE SEQUENCE IF NOT EXISTS shipment_id_seq START 1;

CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    firmware_version TEXT NOT NULL,
    hardware_model TEXT,
    capabilities JSONB NOT NULL DEFAULT '{}'::JSONB,
    registered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    latest_observed_at TIMESTAMPTZ NOT NULL,
    latest_message JSONB NOT NULL,
    last_boot_id TEXT,
    config_version INTEGER NOT NULL DEFAULT 0 CHECK (config_version >= 0),
    config_applied_version INTEGER NOT NULL DEFAULT 0 CHECK (config_applied_version >= 0)
);

CREATE TABLE IF NOT EXISTS shipments (
    shipment_id TEXT PRIMARY KEY,
    origin TEXT NOT NULL,
    destination TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'packed'
        CHECK (status IN ('packed', 'in_transit', 'partially_delivered', 'delivered')),
    expected_delivery_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS packages (
    package_id TEXT PRIMARY KEY,
    product_sku TEXT NOT NULL,
    product_name TEXT NOT NULL,
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    unit TEXT NOT NULL,
    batch_number TEXT,
    expiry_date DATE,
    min_temperature_c DOUBLE PRECISION NOT NULL CHECK (min_temperature_c BETWEEN -100 AND 100),
    max_temperature_c DOUBLE PRECISION NOT NULL CHECK (max_temperature_c BETWEEN -100 AND 100),
    shock_threshold_g DOUBLE PRECISION NOT NULL CHECK (shock_threshold_g > 0 AND shock_threshold_g <= 1000),
    device_id TEXT UNIQUE REFERENCES devices(device_id),
    shipment_id TEXT REFERENCES shipments(shipment_id),
    status TEXT NOT NULL DEFAULT 'packed' CHECK (status IN ('packed', 'in_transit', 'delivered')),
    tracking_token_hash TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dispatched_at TIMESTAMPTZ,
    delivered_at TIMESTAMPTZ,
    CHECK (min_temperature_c <= max_temperature_c)
);

CREATE TABLE IF NOT EXISTS telemetry (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    package_id TEXT REFERENCES packages(package_id),
    shipment_id TEXT REFERENCES shipments(shipment_id),
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    boot_id TEXT NOT NULL,
    message_seq BIGINT NOT NULL CHECK (message_seq >= 0),
    config_version INTEGER NOT NULL DEFAULT 0 CHECK (config_version >= 0),
    temperature_valid BOOLEAN NOT NULL,
    temperature_c DOUBLE PRECISION,
    motion_valid BOOLEAN NOT NULL,
    peak_acceleration_magnitude_g DOUBLE PRECISION,
    shock_count_since_last_report INTEGER NOT NULL DEFAULT 0 CHECK (shock_count_since_last_report >= 0),
    location_fix_valid BOOLEAN NOT NULL DEFAULT FALSE,
    latitude DOUBLE PRECISION,
    longitude DOUBLE PRECISION,
    accuracy_m DOUBLE PRECISION,
    satellites_used INTEGER NOT NULL DEFAULT 0 CHECK (satellites_used >= 0),
    raw_payload JSONB NOT NULL,
    UNIQUE (device_id, boot_id, message_seq),
    CHECK (temperature_c IS NULL OR temperature_c BETWEEN -100 AND 100),
    CHECK (peak_acceleration_magnitude_g IS NULL OR peak_acceleration_magnitude_g >= 0),
    CHECK (latitude IS NULL OR latitude BETWEEN -90 AND 90),
    CHECK (longitude IS NULL OR longitude BETWEEN -180 AND 180),
    CHECK (accuracy_m IS NULL OR accuracy_m >= 0),
    CHECK (location_fix_valid OR (latitude IS NULL AND longitude IS NULL AND accuracy_m IS NULL))
);

CREATE TABLE IF NOT EXISTS alerts (
    alert_id TEXT PRIMARY KEY,
    source_device_id TEXT NOT NULL REFERENCES devices(device_id),
    source_boot_id TEXT NOT NULL,
    source_message_seq BIGINT NOT NULL CHECK (source_message_seq >= 0),
    package_id TEXT REFERENCES packages(package_id),
    shipment_id TEXT REFERENCES shipments(shipment_id),
    type TEXT NOT NULL CHECK (type IN ('temperature_breach', 'shock_detected')),
    severity TEXT NOT NULL DEFAULT 'warning',
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'acknowledged', 'resolved')),
    observed_at TIMESTAMPTZ NOT NULL,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    details JSONB NOT NULL DEFAULT '{}'::JSONB,
    raw_payload JSONB NOT NULL,
    acknowledged_at TIMESTAMPTZ,
    acknowledged_by TEXT,
    resolved_at TIMESTAMPTZ,
    resolved_by TEXT,
    resolution_note TEXT,
    UNIQUE (source_device_id, source_boot_id, source_message_seq)
);

CREATE TABLE IF NOT EXISTS processed_messages (
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    boot_id TEXT NOT NULL,
    message_seq BIGINT NOT NULL CHECK (message_seq >= 0),
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (device_id, boot_id, message_seq)
);

CREATE TABLE IF NOT EXISTS shipment_events (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shipment_id TEXT NOT NULL REFERENCES shipments(shipment_id),
    package_id TEXT REFERENCES packages(package_id),
    event_type TEXT NOT NULL,
    from_status TEXT,
    to_status TEXT,
    actor TEXT NOT NULL,
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata JSONB NOT NULL DEFAULT '{}'::JSONB
);

CREATE INDEX IF NOT EXISTS idx_telemetry_device_time ON telemetry (device_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_package_time ON telemetry (package_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_telemetry_shipment_time ON telemetry (shipment_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_status_time ON alerts (status, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_device_time ON alerts (source_device_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_package_time ON alerts (package_id, observed_at DESC);
CREATE INDEX IF NOT EXISTS idx_shipment_events_timeline ON shipment_events (shipment_id, occurred_at ASC);
"""

REQUIRED_COLUMNS = {
    "devices": {"firmware_version", "hardware_model", "capabilities", "latest_observed_at", "latest_message", "config_version"},
    "shipments": {"origin", "destination", "expected_delivery_at", "dispatched_at", "delivered_at"},
    "packages": {"product_sku", "product_name", "quantity", "min_temperature_c", "max_temperature_c", "shock_threshold_g", "tracking_token_hash"},
    "telemetry": {"boot_id", "message_seq", "config_version", "temperature_valid", "motion_valid", "peak_acceleration_magnitude_g", "raw_payload"},
    "alerts": {"source_device_id", "source_boot_id", "source_message_seq", "severity", "details", "raw_payload", "resolved_at"},
}


def ensure_schema() -> None:
    """Create the current schema, but refuse to silently run against the old schema."""
    with connect_db() as conn:
        for table, required in REQUIRED_COLUMNS.items():
            exists = conn.execute("SELECT to_regclass(%s) AS name", (f"public.{table}",)).fetchone()["name"]
            if exists:
                columns = {
                    row["column_name"]
                    for row in conn.execute(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema = 'public' AND table_name = %s",
                        (table,),
                    ).fetchall()
                }
                missing = required - columns
                if missing:
                    missing_text = ", ".join(sorted(missing))
                    raise RuntimeError(
                        f"The existing {table} table uses an older/incompatible schema "
                        f"(missing: {missing_text}). For this disposable MVP database, "
                        "back up anything needed and recreate the database/volume, or run "
                        "a deliberate migration. CREATE TABLE IF NOT EXISTS is not a migration."
                    )
        for statement in SCHEMA_SQL.split(";"):
            if statement.strip():
                conn.execute(statement)
    logger.info("Database schema checked.")


# ---------------------------------------------------------------------------
# MQTT envelope and payload validation
# ---------------------------------------------------------------------------


def validate_envelope(data: Any, topic: str):
    if not isinstance(data, dict):
        raise ValueError("MQTT message must be a JSON object")
    parts = topic.split("/")
    if len(parts) != 3 or parts[0] != "coldchain" or parts[2] != "messages":
        raise ValueError("Invalid topic; expected coldchain/{device_id}/messages")

    required = (
        "schema_version", "message_type", "device_id", "firmware_version",
        "boot_id", "message_seq", "config_version", "observed_at", "uptime_ms", "payload",
    )
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"Missing envelope fields: {', '.join(missing)}")

    if not isinstance(data["schema_version"], int) or isinstance(data["schema_version"], bool) or data["schema_version"] != 1:
        raise ValueError("schema_version must be integer 1")
    if data["message_type"] not in ("register", "telemetry", "event"):
        raise ValueError("message_type must be register, telemetry or event")
    device_id = data["device_id"]
    if not isinstance(device_id, str) or not DEVICE_ID_PATTERN.fullmatch(device_id):
        raise ValueError("device_id must contain 1-64 letters, digits, '_' or '-'")
    if device_id != parts[1]:
        raise ValueError("Topic device_id does not match envelope device_id")
    for name in ("firmware_version", "boot_id"):
        if not isinstance(data[name], str) or not data[name].strip() or len(data[name]) > 128:
            raise ValueError(f"{name} must be a non-empty string (maximum 128 characters)")
    for name in ("message_seq", "config_version", "uptime_ms"):
        value = data[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
    if not isinstance(data["payload"], dict):
        raise ValueError("payload must be a JSON object")
    observed_at = parse_timestamp(data["observed_at"])
    return device_id, data["message_type"], observed_at, data["payload"]


def validate_registration_payload(payload: dict) -> tuple[str | None, dict]:
    allowed = {"hardware_model", "capabilities"}
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError("Registration may contain only hardware_model and capabilities; thresholds belong to packages")
    if "hardware_model" not in payload or "capabilities" not in payload:
        raise ValueError("Registration payload requires hardware_model and capabilities")
    hardware_model = payload.get("hardware_model")
    if hardware_model is not None and (not isinstance(hardware_model, str) or not hardware_model.strip()):
        raise ValueError("hardware_model must be a non-empty string or null")
    capabilities = payload.get("capabilities")
    if not isinstance(capabilities, dict):
        raise ValueError("capabilities must be an object")
    for key, value in capabilities.items():
        if not isinstance(key, str) or not isinstance(value, bool):
            raise ValueError("capabilities must map strings to booleans")
    return hardware_model, capabilities


def parse_telemetry_payload(payload: dict) -> dict:
    """Validate the final nested telemetry format; the acceleration unit is g."""
    temperature = payload.get("temperature")
    motion = payload.get("motion")
    location = payload.get("location")
    if not isinstance(temperature, dict) or not isinstance(motion, dict) or not isinstance(location, dict):
        raise ValueError("telemetry requires temperature, motion and location objects")

    if "valid" not in temperature or "celsius" not in temperature:
        raise ValueError("temperature requires valid and celsius fields")
    temperature_valid = temperature.get("valid")
    if not isinstance(temperature_valid, bool):
        raise ValueError("temperature.valid must be boolean")
    temperature_c = checked_number(temperature.get("celsius"), "temperature.celsius", -100, 100)
    if temperature_valid and temperature_c is None:
        raise ValueError("A valid temperature reading requires temperature.celsius")
    if not temperature_valid and temperature_c is not None:
        raise ValueError("Invalid temperature must use celsius=null")

    required_motion = {"valid", "peak_acceleration_magnitude_g", "shock_count_since_last_report"}
    if not required_motion.issubset(motion):
        raise ValueError("motion requires valid, peak_acceleration_magnitude_g and shock_count_since_last_report")
    motion_valid = motion.get("valid")
    if not isinstance(motion_valid, bool):
        raise ValueError("motion.valid must be boolean")
    peak_g = checked_number(
        motion.get("peak_acceleration_magnitude_g"),
        "motion.peak_acceleration_magnitude_g", 0, None,
    )
    if motion_valid and peak_g is None:
        raise ValueError("Valid motion requires peak_acceleration_magnitude_g")
    if not motion_valid and peak_g is not None:
        raise ValueError("Invalid motion must use peak_acceleration_magnitude_g=null")
    shock_count = motion.get("shock_count_since_last_report")
    if not isinstance(shock_count, int) or isinstance(shock_count, bool) or shock_count < 0:
        raise ValueError("motion.shock_count_since_last_report must be a non-negative integer")

    required_location = {"fix_valid", "latitude", "longitude", "accuracy_m", "satellites_used"}
    if not required_location.issubset(location):
        raise ValueError("location requires fix_valid, latitude, longitude, accuracy_m and satellites_used")
    fix_valid = location.get("fix_valid")
    if not isinstance(fix_valid, bool):
        raise ValueError("location.fix_valid must be boolean")
    latitude = checked_number(location.get("latitude"), "location.latitude", -90, 90)
    longitude = checked_number(location.get("longitude"), "location.longitude", -180, 180)
    accuracy = checked_number(location.get("accuracy_m"), "location.accuracy_m", 0, None)
    satellites = location.get("satellites_used")
    if not isinstance(satellites, int) or isinstance(satellites, bool) or satellites < 0:
        raise ValueError("location.satellites_used must be a non-negative integer")
    if fix_valid and (latitude is None or longitude is None):
        raise ValueError("A valid location fix requires latitude and longitude")
    if not fix_valid and any(value is not None for value in (latitude, longitude, accuracy)):
        raise ValueError("When location.fix_valid is false, coordinates and accuracy must be null")

    return {
        "temperature_valid": temperature_valid,
        "temperature_c": temperature_c,
        "motion_valid": motion_valid,
        "peak_acceleration_magnitude_g": peak_g,
        "shock_count_since_last_report": shock_count,
        "location_fix_valid": fix_valid,
        "latitude": latitude,
        "longitude": longitude,
        "accuracy_m": accuracy,
        "satellites_used": satellites,
    }


def validate_event_payload(payload: dict) -> tuple[str, dict]:
    event_type = payload.get("type")
    if event_type not in ALERT_TYPES:
        raise ValueError("event type must be temperature_breach or shock_detected")

    if event_type == "temperature_breach":
        checked_number(payload.get("temperature_c"), "temperature_c", -100, 100, required=True)
        direction = payload.get("breach_direction")
        if direction not in ("below_minimum", "above_maximum"):
            raise ValueError("breach_direction must be below_minimum or above_maximum")
        minimum = checked_number(payload.get("min_temperature_c"), "min_temperature_c", -100, 100, required=True)
        maximum = checked_number(payload.get("max_temperature_c"), "max_temperature_c", -100, 100, required=True)
        if minimum > maximum:
            raise ValueError("min_temperature_c cannot exceed max_temperature_c")
    else:
        checked_number(
            payload.get("peak_acceleration_magnitude_g"),
            "peak_acceleration_magnitude_g", 0, None, required=True,
        )
        checked_number(payload.get("threshold_g"), "threshold_g", 0.000001, 1000, required=True)
        duration = payload.get("duration_ms")
        if duration is not None and (
            not isinstance(duration, int) or isinstance(duration, bool) or duration < 0
        ):
            raise ValueError("duration_ms must be a non-negative integer when supplied")
    return event_type, payload


def current_assignment(conn, device_id: str):
    return conn.execute(
        """
        SELECT p.package_id, p.shipment_id, p.product_sku, p.product_name,
               p.min_temperature_c, p.max_temperature_c, p.shock_threshold_g,
               p.status AS package_status, s.status AS shipment_status
        FROM packages p
        LEFT JOIN shipments s ON s.shipment_id = p.shipment_id
        WHERE p.device_id = %s
        """,
        (device_id,),
    ).fetchone()


def require_registered(conn, device_id: str) -> None:
    row = conn.execute("SELECT 1 FROM devices WHERE device_id = %s", (device_id,)).fetchone()
    if row is None:
        raise ValueError(f"Device {device_id} has not registered")


def bump_config_versions(conn, device_ids) -> list[str]:
    ids = sorted({value for value in device_ids if value})
    if not ids:
        return []
    rows = conn.execute(
        "UPDATE devices SET config_version = config_version + 1 "
        "WHERE device_id = ANY(%s) RETURNING device_id",
        (ids,),
    ).fetchall()
    return [row["device_id"] for row in rows]


def update_device_diagnostics(conn, envelope: dict, observed_at: datetime) -> None:
    """Update live diagnostics after an accepted, non-duplicate device message."""
    conn.execute(
        """
        UPDATE devices SET
            last_seen_at = NOW(),
            latest_observed_at = GREATEST(latest_observed_at, %s),
            latest_message = CASE WHEN %s >= latest_observed_at THEN %s ELSE latest_message END,
            last_boot_id = CASE WHEN %s >= latest_observed_at THEN %s ELSE last_boot_id END,
            firmware_version = CASE WHEN %s >= latest_observed_at THEN %s ELSE firmware_version END,
            config_applied_version = CASE WHEN %s >= latest_observed_at THEN %s ELSE config_applied_version END
        WHERE device_id = %s
        """,
        (
            observed_at, observed_at, Jsonb(envelope), observed_at, envelope["boot_id"],
            observed_at, envelope["firmware_version"], observed_at, envelope["config_version"],
            envelope["device_id"],
        ),
    )


# ---------------------------------------------------------------------------
# MQTT publishing and registration
# ---------------------------------------------------------------------------


def publish_json(client: mqtt.Client, topic: str, payload: dict, *, retain=False) -> None:
    try:
        info = client.publish(topic, json.dumps(payload, allow_nan=False), qos=1, retain=retain)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            logger.error("MQTT publish failed for %s (rc=%s)", topic, info.rc)
    except Exception:
        # The PostgreSQL transaction has already committed before publication.
        logger.exception("Could not publish MQTT message to %s", topic)


def publish_registration_ack(
    client: mqtt.Client, device_id: str, status: str, reason: str | None = None,
) -> None:
    publish_json(client, f"coldchain/{device_id}/registration/ack", {
        "message_type": "registration_ack",
        "device_id": device_id,
        "status": status,
        "reason": reason,
    })


def publish_device_config(client: mqtt.Client, device_id: str) -> None:
    """Publish authoritative package configuration (or explicit unassigned config)."""
    with connect_db() as conn:
        device = conn.execute(
            "SELECT device_id, config_version FROM devices WHERE device_id = %s",
            (device_id,),
        ).fetchone()
        if device is None:
            logger.warning("Cannot publish config for unknown device %s", device_id)
            return
        assignment = current_assignment(conn, device_id)

    assigned = assignment is not None
    package_status = assignment["package_status"] if assigned else None
    shipment_status = assignment["shipment_status"] if assigned else None
    monitoring_enabled = bool(
        assigned
        and package_status != "delivered"
        and shipment_status != "delivered"
    )
    package_config = None
    temperature_config = None
    shock_config = None
    if assigned:
        package_config = {
            "product_sku": assignment["product_sku"],
            "product_name": assignment["product_name"],
        }
        temperature_config = {
            "min_temperature_c": assignment["min_temperature_c"],
            "max_temperature_c": assignment["max_temperature_c"],
        }
        shock_config = {"threshold_g": assignment["shock_threshold_g"]}

    config = {
        "schema_version": 1,
        "message_type": "device_config",
        "device_id": device_id,
        "config_version": device["config_version"],
        "issued_at": utc_iso(),
        "assignment": {
            "assigned": assigned,
            "package_id": assignment["package_id"] if assigned else None,
            "shipment_id": assignment["shipment_id"] if assigned else None,
            "package_status": package_status,
            "shipment_status": shipment_status,
        },
        "package": package_config,
        "monitoring": {
            "enabled": monitoring_enabled,
            "reporting_interval_seconds": REPORTING_INTERVAL_SECONDS,
            "temperature": temperature_config,
            "shock": shock_config,
        },
    }
    publish_json(client, f"coldchain/{device_id}/config", config, retain=True)


def handle_registration(client: mqtt.Client, envelope: dict, observed_at: datetime, payload: dict) -> None:
    hardware_model, capabilities = validate_registration_payload(payload)
    device_id = envelope["device_id"]
    with connect_db() as conn:
        existing = conn.execute(
            "SELECT 1 FROM devices WHERE device_id = %s", (device_id,),
        ).fetchone()
        conn.execute(
            """
            INSERT INTO devices (
                device_id, firmware_version, hardware_model, capabilities,
                last_seen_at, latest_observed_at, latest_message, last_boot_id,
                config_applied_version
            ) VALUES (%s, %s, %s, %s, NOW(), %s, %s, %s, %s)
            ON CONFLICT (device_id) DO UPDATE SET
                firmware_version = CASE WHEN EXCLUDED.latest_observed_at >= devices.latest_observed_at
                    THEN EXCLUDED.firmware_version ELSE devices.firmware_version END,
                hardware_model = EXCLUDED.hardware_model,
                capabilities = EXCLUDED.capabilities,
                last_seen_at = NOW(),
                latest_observed_at = GREATEST(devices.latest_observed_at, EXCLUDED.latest_observed_at),
                latest_message = CASE WHEN EXCLUDED.latest_observed_at >= devices.latest_observed_at
                    THEN EXCLUDED.latest_message ELSE devices.latest_message END,
                last_boot_id = CASE WHEN EXCLUDED.latest_observed_at >= devices.latest_observed_at
                    THEN EXCLUDED.last_boot_id ELSE devices.last_boot_id END,
                config_applied_version = CASE WHEN EXCLUDED.latest_observed_at >= devices.latest_observed_at
                    THEN EXCLUDED.config_applied_version ELSE devices.config_applied_version END
            """,
            (
                device_id, envelope["firmware_version"], hardware_model,
                Jsonb(capabilities), observed_at, Jsonb(envelope), envelope["boot_id"],
                envelope["config_version"],
            ),
        )
    status = "already_registered" if existing else "registered"
    logger.info("Device %s: %s", device_id, status)
    publish_registration_ack(client, device_id, status)
    # Always publish a config, including the unassigned config for a new tracker.
    publish_device_config(client, device_id)


def ingest_telemetry(envelope: dict, observed_at: datetime, values: dict) -> bool:
    device_id = envelope["device_id"]
    with connect_db() as conn:
        require_registered(conn, device_id)
        inserted = conn.execute(
            """
            INSERT INTO processed_messages (device_id, boot_id, message_seq)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING message_seq
            """,
            (device_id, envelope["boot_id"], envelope["message_seq"]),
        ).fetchone()
        if inserted is None:
            logger.info("Ignoring duplicate message %s/%s/%s", device_id, envelope["boot_id"], envelope["message_seq"])
            return False
        assignment = current_assignment(conn, device_id)
        conn.execute(
            """
            INSERT INTO telemetry (
                device_id, package_id, shipment_id, observed_at, boot_id,
                message_seq, config_version, temperature_valid, temperature_c,
                motion_valid, peak_acceleration_magnitude_g,
                shock_count_since_last_report, location_fix_valid, latitude,
                longitude, accuracy_m, satellites_used, raw_payload
            ) VALUES (
                %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
            )
            """,
            (
                device_id,
                assignment["package_id"] if assignment else None,
                assignment["shipment_id"] if assignment else None,
                observed_at, envelope["boot_id"], envelope["message_seq"],
                envelope["config_version"], values["temperature_valid"], values["temperature_c"],
                values["motion_valid"], values["peak_acceleration_magnitude_g"],
                values["shock_count_since_last_report"], values["location_fix_valid"],
                values["latitude"], values["longitude"], values["accuracy_m"],
                values["satellites_used"], Jsonb(envelope),
            ),
        )
        update_device_diagnostics(conn, envelope, observed_at)
    logger.info("Telemetry saved for %s (seq=%s)", device_id, envelope["message_seq"])
    return True


def ingest_event(envelope: dict, observed_at: datetime, event_type: str, details: dict) -> bool:
    device_id = envelope["device_id"]
    with connect_db() as conn:
        require_registered(conn, device_id)
        inserted = conn.execute(
            """
            INSERT INTO processed_messages (device_id, boot_id, message_seq)
            VALUES (%s, %s, %s)
            ON CONFLICT DO NOTHING
            RETURNING message_seq
            """,
            (device_id, envelope["boot_id"], envelope["message_seq"]),
        ).fetchone()
        if inserted is None:
            logger.info("Ignoring duplicate event %s/%s/%s", device_id, envelope["boot_id"], envelope["message_seq"])
            return False
        assignment = current_assignment(conn, device_id)
        alert_id = f"alert-{secrets.token_hex(8)}"
        conn.execute(
            """
            INSERT INTO alerts (
                alert_id, source_device_id, source_boot_id, source_message_seq,
                package_id, shipment_id, type, severity, status, observed_at,
                details, raw_payload
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, 'warning', 'open', %s, %s, %s)
            """,
            (
                alert_id, device_id, envelope["boot_id"], envelope["message_seq"],
                assignment["package_id"] if assignment else None,
                assignment["shipment_id"] if assignment else None,
                event_type, observed_at, Jsonb(details), Jsonb(envelope),
            ),
        )
        update_device_diagnostics(conn, envelope, observed_at)
    logger.warning("Alert saved: alert_id=%s device=%s type=%s", alert_id, device_id, event_type)
    return True


def on_connect(client, userdata, flags, reason_code, properties):
    global mqtt_connected, mqtt_subscribed
    mqtt_connected = (reason_code == 0)
    mqtt_subscribed = False
    if mqtt_connected:
        logger.info("Connected to MQTT broker at %s:%s", MQTT_HOST, MQTT_PORT)
        result, _mid = client.subscribe(MQTT_TOPIC, qos=1)
        if result != mqtt.MQTT_ERR_SUCCESS:
            logger.error("Could not subscribe to %s (rc=%s)", MQTT_TOPIC, result)
        try:
            with connect_db() as conn:
                rows = conn.execute("SELECT device_id FROM devices ORDER BY device_id").fetchall()
            for row in rows:
                publish_device_config(client, row["device_id"])
        except Exception:
            logger.exception("Could not refresh device configs after MQTT connect")
    else:
        logger.error("MQTT connection failed: %s", reason_code)


def on_subscribe(client, userdata, mid, reason_code_list, properties):
    global mqtt_subscribed
    mqtt_subscribed = all(not getattr(code, "is_failure", False) for code in reason_code_list)
    if mqtt_subscribed:
        logger.info("MQTT subscription is active")
    else:
        logger.error("MQTT subscription was rejected: %s", reason_code_list)


def on_disconnect(client, userdata, disconnect_flags, reason_code, properties):
    global mqtt_connected, mqtt_subscribed
    mqtt_connected = False
    mqtt_subscribed = False
    logger.warning("MQTT disconnected: %s", reason_code)


def on_message(client, userdata, msg):
    candidate_id = None
    candidate_register = False
    try:
        parts = msg.topic.split("/")
        if len(parts) == 3 and parts[0] == "coldchain" and parts[2] == "messages":
            candidate_id = parts[1]
        data = json.loads(msg.payload.decode("utf-8"))
        candidate_register = isinstance(data, dict) and data.get("message_type") == "register"
        device_id, message_type, observed_at, payload = validate_envelope(data, msg.topic)
        if message_type == "register":
            handle_registration(client, data, observed_at, payload)
        elif message_type == "telemetry":
            values = parse_telemetry_payload(payload)
            ingest_telemetry(data, observed_at, values)
        elif message_type == "event":
            event_type, details = validate_event_payload(payload)
            ingest_event(data, observed_at, event_type, details)
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
        logger.warning("MQTT message rejected on %s: %s", msg.topic, exc)
        if candidate_register and candidate_id and DEVICE_ID_PATTERN.fullmatch(candidate_id):
            publish_registration_ack(client, candidate_id, "rejected", "Invalid registration payload")
    except psycopg.Error:
        logger.exception("PostgreSQL error while processing MQTT message on %s", msg.topic)
    except Exception:
        logger.exception("Unexpected error while processing MQTT message on %s", msg.topic)


def start_mqtt() -> mqtt.Client:
    client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id="coldchain_backend",
    )
    if MQTT_USERNAME:
        client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)
    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()
    return client


# ---------------------------------------------------------------------------
# REST request models
# ---------------------------------------------------------------------------


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PackageCreate(StrictModel):
    product_sku: str = Field(min_length=1, max_length=100)
    product_name: str = Field(min_length=1, max_length=200)
    quantity: int = Field(strict=True, gt=0, le=1_000_000)
    unit: str = Field(min_length=1, max_length=40)
    batch_number: str | None = Field(default=None, max_length=100)
    expiry_date: date | None = None
    min_temperature_c: float = Field(ge=-100, le=100, allow_inf_nan=False)
    max_temperature_c: float = Field(ge=-100, le=100, allow_inf_nan=False)
    shock_threshold_g: float = Field(gt=0, le=1000, allow_inf_nan=False)
    device_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("min_temperature_c", "max_temperature_c", "shock_threshold_g", mode="before")
    @classmethod
    def threshold_must_be_finite_json_number(cls, value):
        if not is_number(value):
            raise ValueError("must be a finite JSON number")
        return value

    @field_validator("product_sku", "product_name", "unit")
    @classmethod
    def non_blank_strings(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("batch_number")
    @classmethod
    def trim_batch(cls, value):
        if value is None:
            return None
        value = value.strip()
        return value or None

    @model_validator(mode="after")
    def valid_temperature_range(self):
        if self.min_temperature_c > self.max_temperature_c:
            raise ValueError("min_temperature_c cannot exceed max_temperature_c")
        return self


class PackageUpdate(StrictModel):
    product_sku: str | None = Field(default=None, min_length=1, max_length=100)
    product_name: str | None = Field(default=None, min_length=1, max_length=200)
    quantity: int | None = Field(default=None, strict=True, gt=0, le=1_000_000)
    unit: str | None = Field(default=None, min_length=1, max_length=40)
    batch_number: str | None = Field(default=None, max_length=100)
    expiry_date: date | None = None
    min_temperature_c: float | None = Field(default=None, ge=-100, le=100, allow_inf_nan=False)
    max_temperature_c: float | None = Field(default=None, ge=-100, le=100, allow_inf_nan=False)
    shock_threshold_g: float | None = Field(default=None, gt=0, le=1000, allow_inf_nan=False)
    device_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")

    @field_validator("min_temperature_c", "max_temperature_c", "shock_threshold_g", mode="before")
    @classmethod
    def update_threshold_must_be_finite_json_number(cls, value):
        if value is None:
            return None
        if not is_number(value):
            raise ValueError("must be a finite JSON number")
        return value

    @field_validator("product_sku", "product_name", "unit")
    @classmethod
    def trim_update_strings(cls, value):
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("batch_number")
    @classmethod
    def trim_update_batch(cls, value):
        if value is None:
            return None
        value = value.strip()
        return value or None


class ShipmentCreate(StrictModel):
    origin: str = Field(min_length=1, max_length=200)
    destination: str = Field(min_length=1, max_length=200)
    expected_delivery_at: datetime | None = None
    package_ids: list[str] = Field(min_length=1, max_length=100)

    @field_validator("origin", "destination")
    @classmethod
    def trim_location(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("package_ids")
    @classmethod
    def unique_package_ids(cls, values):
        if len(values) != len(set(values)):
            raise ValueError("package_ids must contain unique IDs")
        return values

    @field_validator("expected_delivery_at")
    @classmethod
    def timestamp_has_timezone(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expected_delivery_at must include a timezone")
        return value


class ShipmentUpdate(StrictModel):
    origin: str | None = Field(default=None, min_length=1, max_length=200)
    destination: str | None = Field(default=None, min_length=1, max_length=200)
    expected_delivery_at: datetime | None = None

    @field_validator("origin", "destination")
    @classmethod
    def trim_update_location(cls, value):
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("expected_delivery_at")
    @classmethod
    def update_timestamp_has_timezone(cls, value):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expected_delivery_at must include a timezone")
        return value


class PackageDeliver(StrictModel):
    recipient_name: str | None = Field(default=None, max_length=200)
    note: str | None = Field(default=None, max_length=1000)


class AlertResolve(StrictModel):
    resolution_note: str | None = Field(default=None, max_length=1000)


# ---------------------------------------------------------------------------
# Application lifecycle and helper queries
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    global mqtt_client, mqtt_connected, mqtt_subscribed
    ensure_schema()
    mqtt_client = start_mqtt()
    try:
        yield
    finally:
        if mqtt_client is not None:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
            mqtt_client = None
        mqtt_connected = False
        mqtt_subscribed = False


app = FastAPI(title="ColdChain API", version="1.0.0", lifespan=lifespan)
router = APIRouter(prefix="/api/v1", dependencies=[Depends(require_admin)])


def get_device_or_404(conn, device_id: str):
    row = conn.execute(
        "SELECT device_id, firmware_version, hardware_model, capabilities, registered_at, "
        "last_seen_at, latest_observed_at, latest_message, last_boot_id, config_version, "
        "config_applied_version FROM devices WHERE device_id = %s",
        (device_id,),
    ).fetchone()
    if row is None:
        api_error(404, "DEVICE_NOT_FOUND", "Device not found.")
    return row


def package_public_columns() -> str:
    return """p.package_id, p.product_sku, p.product_name, p.quantity, p.unit,
              p.batch_number, p.expiry_date, p.min_temperature_c, p.max_temperature_c,
              p.shock_threshold_g, p.device_id, p.shipment_id, p.status,
              p.created_at, p.updated_at, p.dispatched_at, p.delivered_at"""


def fetch_package_detail(package_id: str):
    with connect_db() as conn:
        row = conn.execute(
            f"""
            SELECT {package_public_columns()},
                   d.firmware_version, d.last_seen_at, d.config_version, d.config_applied_version,
                   d.hardware_model, d.capabilities,
                   s.status AS shipment_status, s.origin, s.destination, s.expected_delivery_at,
                   s.dispatched_at AS shipment_dispatched_at, s.delivered_at AS shipment_delivered_at,
                   lt.observed_at AS latest_observed_at, lt.received_at AS latest_received_at,
                   lt.temperature_valid AS latest_temperature_valid,
                   lt.temperature_c AS latest_temperature_c,
                   lt.motion_valid AS latest_motion_valid,
                   lt.peak_acceleration_magnitude_g AS latest_peak_acceleration_magnitude_g,
                   lt.location_fix_valid AS latest_location_fix_valid,
                   lt.latitude AS latest_latitude, lt.longitude AS latest_longitude,
                   (SELECT COUNT(*)::INTEGER FROM alerts a
                    WHERE a.package_id = p.package_id AND a.status <> 'resolved') AS open_alert_count
            FROM packages p
            LEFT JOIN devices d ON d.device_id = p.device_id
            LEFT JOIN shipments s ON s.shipment_id = p.shipment_id
            LEFT JOIN LATERAL (
                SELECT observed_at, received_at, temperature_valid, temperature_c,
                       motion_valid, peak_acceleration_magnitude_g,
                       location_fix_valid, latitude, longitude
                FROM telemetry t WHERE t.package_id = p.package_id
                ORDER BY observed_at DESC, id DESC LIMIT 1
            ) lt ON TRUE
            WHERE p.package_id = %s
            """,
            (package_id,),
        ).fetchone()
    if row is None:
        api_error(404, "PACKAGE_NOT_FOUND", "Package not found.")

    data = dict(row)
    device = None
    if data["device_id"] is not None:
        device = {
            "device_id": data["device_id"],
            "status": online_status(data["last_seen_at"]),
            "firmware_version": data["firmware_version"],
            "hardware_model": data["hardware_model"],
            "capabilities": data["capabilities"],
            "last_seen_at": data["last_seen_at"],
            "config_version": data["config_version"],
            "config_applied_version": data["config_applied_version"],
        }
    shipment = None
    if data["shipment_id"] is not None:
        shipment = {
            "shipment_id": data["shipment_id"],
            "status": data["shipment_status"],
            "origin": data["origin"],
            "destination": data["destination"],
            "expected_delivery_at": data["expected_delivery_at"],
            "dispatched_at": data["shipment_dispatched_at"],
            "delivered_at": data["shipment_delivered_at"],
        }
    latest_telemetry = None
    if data["latest_observed_at"] is not None:
        latest_telemetry = {
            "observed_at": data["latest_observed_at"],
            "received_at": data["latest_received_at"],
            "temperature_valid": data["latest_temperature_valid"],
            "temperature_c": data["latest_temperature_c"],
            "motion_valid": data["latest_motion_valid"],
            "peak_acceleration_magnitude_g": data["latest_peak_acceleration_magnitude_g"],
            "location_fix_valid": data["latest_location_fix_valid"],
            "latitude": data["latest_latitude"],
            "longitude": data["latest_longitude"],
        }
    reserved = {
        "firmware_version", "last_seen_at", "config_version", "config_applied_version",
        "hardware_model", "capabilities", "shipment_status", "origin", "destination",
        "expected_delivery_at", "shipment_dispatched_at", "shipment_delivered_at",
        "latest_observed_at", "latest_received_at", "latest_temperature_valid",
        "latest_temperature_c", "latest_motion_valid", "latest_peak_acceleration_magnitude_g",
        "latest_location_fix_valid", "latest_latitude", "latest_longitude",
    }
    package = {key: value for key, value in data.items() if key not in reserved and key != "open_alert_count"}
    return {
        **package,
        "device": device,
        "shipment": shipment,
        "latest_telemetry": latest_telemetry,
        "open_alert_count": data["open_alert_count"],
    }


def fetch_shipment_detail(shipment_id: str):
    with connect_db() as conn:
        shipment = conn.execute(
            "SELECT shipment_id, origin, destination, status, expected_delivery_at, "
            "created_at, updated_at, dispatched_at, delivered_at FROM shipments WHERE shipment_id = %s",
            (shipment_id,),
        ).fetchone()
        if shipment is None:
            api_error(404, "SHIPMENT_NOT_FOUND", "Shipment not found.")
        package_rows = conn.execute(
            """
            SELECT p.package_id, p.product_sku, p.product_name, p.quantity, p.unit,
                   p.status, p.device_id, p.shipment_id, d.last_seen_at,
                   lt.temperature_valid AS latest_temperature_valid,
                   lt.temperature_c AS latest_temperature_c,
                   lt.observed_at AS latest_observed_at,
                   (SELECT COUNT(*)::INTEGER FROM alerts a
                    WHERE a.package_id = p.package_id AND a.status <> 'resolved') AS open_alert_count
            FROM packages p
            LEFT JOIN devices d ON d.device_id = p.device_id
            LEFT JOIN LATERAL (
                SELECT temperature_valid, temperature_c, observed_at
                FROM telemetry t WHERE t.package_id = p.package_id
                ORDER BY observed_at DESC, id DESC LIMIT 1
            ) lt ON TRUE
            WHERE p.shipment_id = %s ORDER BY p.package_id
            """,
            (shipment_id,),
        ).fetchall()
        open_alert_count = conn.execute(
            "SELECT COUNT(*)::INTEGER AS count FROM alerts "
            "WHERE shipment_id = %s AND status <> 'resolved'",
            (shipment_id,),
        ).fetchone()["count"]
    packages = []
    for row in package_rows:
        packages.append({
            "package_id": row["package_id"],
            "product_sku": row["product_sku"],
            "product_name": row["product_name"],
            "quantity": row["quantity"],
            "unit": row["unit"],
            "status": row["status"],
            "device_id": row["device_id"],
            "device_status": online_status(row["last_seen_at"]) if row["device_id"] else None,
            "latest_temperature_valid": row["latest_temperature_valid"],
            "latest_temperature_c": row["latest_temperature_c"],
            "latest_observed_at": row["latest_observed_at"],
            "last_seen_at": row["last_seen_at"],
            "open_alert_count": row["open_alert_count"],
        })
    delivered_count = sum(1 for package in packages if package["status"] == "delivered")
    return {
        **dict(shipment),
        "package_count": len(packages),
        "delivered_package_count": delivered_count,
        "open_alert_count": open_alert_count,
        "packages": packages,
    }


def insert_shipment_event(conn, shipment_id: str, event_type: str, actor: str,
                          *, package_id=None, from_status=None, to_status=None, metadata=None):
    conn.execute(
        """INSERT INTO shipment_events
           (shipment_id, package_id, event_type, from_status, to_status, actor, metadata)
           VALUES (%s, %s, %s, %s, %s, %s, %s)""",
        (shipment_id, package_id, event_type, from_status, to_status, actor, Jsonb(metadata or {})),
    )


def publish_configs(device_ids):
    client = mqtt_client
    if client is None:
        return
    for device_id in sorted({value for value in device_ids if value}):
        publish_device_config(client, device_id)


def list_alert_sql(where_parts: list[str], params: list[Any], limit: int, offset: int):
    where = " AND ".join(where_parts) if where_parts else "TRUE"
    with connect_db() as conn:
        total = conn.execute(f"SELECT COUNT(*)::INTEGER AS count FROM alerts a WHERE {where}", params).fetchone()["count"]
        rows = conn.execute(
            f"""
            SELECT a.alert_id, a.source_device_id AS device_id, a.source_boot_id,
                   a.source_message_seq, a.package_id, a.shipment_id, a.type,
                   a.severity, a.status, a.observed_at, a.received_at, a.details,
                   a.acknowledged_at, a.acknowledged_by, a.resolved_at,
                   a.resolved_by, a.resolution_note
            FROM alerts a WHERE {where}
            ORDER BY a.observed_at DESC, a.received_at DESC
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        ).fetchall()
    return {"items": rows, "limit": limit, "offset": offset, "total": total}


# ---------------------------------------------------------------------------
# Public health/readiness
# ---------------------------------------------------------------------------


@app.get("/health")
def health():
    """Liveness only: it does not promise PostgreSQL or MQTT are ready."""
    return {"status": "ok"}


@app.get("/readyz")
def readyz():
    db_ready = False
    try:
        with connect_db() as conn:
            conn.execute("SELECT 1")
        db_ready = True
    except Exception:
        logger.exception("Readiness check: PostgreSQL unavailable")
    mqtt_ready = mqtt_connected and mqtt_subscribed
    status = "ok" if db_ready and mqtt_ready else "not_ready"
    body = {
        "status": status,
        "postgresql": "ready" if db_ready else "not_ready",
        "mqtt_consumer": "ready" if mqtt_ready else "not_ready",
    }
    return JSONResponse(status_code=200 if status == "ok" else 503, content=body)


# ---------------------------------------------------------------------------
# Package endpoints (admin)
# ---------------------------------------------------------------------------


@router.post("/packages", status_code=201)
def create_package(body: PackageCreate, actor: str = Depends(require_admin)):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    year = datetime.now(timezone.utc).year
    device_ids: list[str] = []
    with connect_db() as conn:
        if body.device_id is not None:
            device = conn.execute("SELECT 1 FROM devices WHERE device_id = %s FOR UPDATE", (body.device_id,)).fetchone()
            if device is None:
                api_error(404, "DEVICE_NOT_FOUND", "The supplied device is not registered.")
            assigned = conn.execute("SELECT package_id FROM packages WHERE device_id = %s", (body.device_id,)).fetchone()
            if assigned is not None:
                api_error(409, "DEVICE_ALREADY_ASSIGNED", "The tracker is already assigned to another package.")
        sequence = conn.execute("SELECT nextval('package_id_seq') AS value").fetchone()["value"]
        package_id = f"PKG-{year}-{sequence:04d}"
        row = conn.execute(
            """
            INSERT INTO packages (
                package_id, product_sku, product_name, quantity, unit, batch_number,
                expiry_date, min_temperature_c, max_temperature_c, shock_threshold_g,
                device_id, status, tracking_token_hash
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'packed', %s)
            RETURNING package_id, product_sku, product_name, quantity, unit, batch_number,
                      expiry_date, min_temperature_c, max_temperature_c, shock_threshold_g,
                      device_id, shipment_id, status, created_at, updated_at
            """,
            (
                package_id, body.product_sku, body.product_name, body.quantity, body.unit,
                body.batch_number, body.expiry_date, body.min_temperature_c, body.max_temperature_c,
                body.shock_threshold_g, body.device_id, token_hash,
            ),
        ).fetchone()
        if body.device_id:
            device_ids = bump_config_versions(conn, [body.device_id])
    publish_configs(device_ids)
    return {"package": row, "tracking_url": f"{PUBLIC_BASE_URL}/track/{quote(token, safe='')}"}


@router.get("/packages")
def list_packages(
    status: Literal["packed", "in_transit", "delivered"] | None = None,
    shipment_id: str | None = None,
    device_id: str | None = None,
    product_sku: str | None = None,
    batch_number: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    where_parts = ["TRUE"]
    params: list[Any] = []
    for column, value in (
        ("p.status", status), ("p.shipment_id", shipment_id), ("p.device_id", device_id),
        ("p.product_sku", product_sku), ("p.batch_number", batch_number),
    ):
        if value is not None:
            where_parts.append(f"{column} = %s")
            params.append(value)
    where = " AND ".join(where_parts)
    with connect_db() as conn:
        total = conn.execute(f"SELECT COUNT(*)::INTEGER AS count FROM packages p WHERE {where}", params).fetchone()["count"]
        rows = conn.execute(
            f"""
            SELECT {package_public_columns()}, d.last_seen_at,
                   lt.temperature_valid AS latest_temperature_valid,
                   lt.temperature_c AS latest_temperature_c,
                   lt.observed_at AS latest_observed_at,
                   (SELECT COUNT(*)::INTEGER FROM alerts a
                    WHERE a.package_id = p.package_id AND a.status <> 'resolved') AS open_alert_count
            FROM packages p
            LEFT JOIN devices d ON d.device_id = p.device_id
            LEFT JOIN LATERAL (
                SELECT temperature_valid, temperature_c, observed_at
                FROM telemetry t WHERE t.package_id = p.package_id
                ORDER BY observed_at DESC, id DESC LIMIT 1
            ) lt ON TRUE
            WHERE {where}
            ORDER BY p.created_at DESC, p.package_id
            LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["last_seen_at"] = row["last_seen_at"]
        item.pop("min_temperature_c", None)
        item.pop("max_temperature_c", None)
        item.pop("shock_threshold_g", None)
        item["status"] = row["status"]
        item["device_status"] = online_status(row["last_seen_at"]) if row["device_id"] else None
        items.append(item)
    return {"items": items, "limit": limit, "offset": offset, "total": total}


@router.get("/packages/{package_id}")
def get_package(package_id: str):
    return fetch_package_detail(package_id)


@router.patch("/packages/{package_id}")
def update_package(package_id: str, body: PackageUpdate):
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        api_error(400, "NO_FIELDS_SUPPLIED", "Supply at least one package field to update.")
    for field_name in ("product_sku", "product_name", "quantity", "unit", "min_temperature_c", "max_temperature_c", "shock_threshold_g"):
        if field_name in changes and changes[field_name] is None:
            api_error(422, "NULL_NOT_ALLOWED", f"{field_name} cannot be null.")

    device_ids: list[str] = []
    allowed_fields = {
        "product_sku", "product_name", "quantity", "unit", "batch_number", "expiry_date",
        "min_temperature_c", "max_temperature_c", "shock_threshold_g", "device_id",
    }
    if not set(changes).issubset(allowed_fields):
        api_error(422, "INVALID_FIELDS", "One or more fields are not editable.")

    with connect_db() as conn:
        current = conn.execute(
            "SELECT * FROM packages WHERE package_id = %s FOR UPDATE", (package_id,),
        ).fetchone()
        if current is None:
            api_error(404, "PACKAGE_NOT_FOUND", "Package not found.")
        if current["status"] != "packed":
            api_error(409, "PACKAGE_NOT_EDITABLE", "Package metadata, thresholds and assignment can only be edited while packed.")
        merged = dict(current)
        merged.update(changes)
        if merged["min_temperature_c"] > merged["max_temperature_c"]:
            api_error(422, "INVALID_TEMPERATURE_RANGE", "min_temperature_c cannot exceed max_temperature_c.")

        old_device_id = current["device_id"]
        new_device_id = merged["device_id"]
        if new_device_id is not None:
            device = conn.execute("SELECT 1 FROM devices WHERE device_id = %s FOR UPDATE", (new_device_id,)).fetchone()
            if device is None:
                api_error(404, "DEVICE_NOT_FOUND", "The supplied device is not registered.")
            other = conn.execute(
                "SELECT package_id FROM packages WHERE device_id = %s AND package_id <> %s",
                (new_device_id, package_id),
            ).fetchone()
            if other is not None:
                api_error(409, "DEVICE_ALREADY_ASSIGNED", "The tracker is already assigned to another package.")

        assignments = ", ".join(f"{name} = %s" for name in changes)
        values = [changes[name] for name in changes]
        row = conn.execute(
            f"""UPDATE packages SET {assignments}, updated_at = NOW() WHERE package_id = %s
                RETURNING package_id, product_sku, product_name, quantity, unit, batch_number,
                          expiry_date, min_temperature_c, max_temperature_c, shock_threshold_g,
                          device_id, shipment_id, status, created_at, updated_at, dispatched_at, delivered_at""",
            [*values, package_id],
        ).fetchone()
        # A product/threshold/assignment edit changes the published config.
        device_ids = bump_config_versions(conn, [old_device_id, new_device_id])
    publish_configs(device_ids)
    return row


@router.get("/packages/{package_id}/telemetry")
def package_telemetry(
    package_id: str,
    limit: int = Query(default=100, ge=1, le=500),
    before: datetime | None = None,
):
    if before is not None and (before.tzinfo is None or before.utcoffset() is None):
        api_error(422, "TIMEZONE_REQUIRED", "before must include a timezone.")
    with connect_db() as conn:
        exists = conn.execute("SELECT 1 FROM packages WHERE package_id = %s", (package_id,)).fetchone()
        if exists is None:
            api_error(404, "PACKAGE_NOT_FOUND", "Package not found.")
        where = "package_id = %s"
        params: list[Any] = [package_id]
        if before is not None:
            where += " AND observed_at < %s"
            params.append(before)
        rows = conn.execute(
            f"""
            SELECT observed_at, received_at, device_id, boot_id, message_seq, config_version,
                   temperature_valid, temperature_c, motion_valid,
                   peak_acceleration_magnitude_g, shock_count_since_last_report,
                   location_fix_valid, latitude, longitude, accuracy_m, satellites_used
            FROM telemetry WHERE {where}
            ORDER BY observed_at DESC, id DESC LIMIT %s
            """,
            [*params, limit + 1],
        ).fetchall()
    more = len(rows) > limit
    items = rows[:limit]
    next_before = items[-1]["observed_at"] if more and items else None
    return {"package_id": package_id, "items": items, "limit": limit, "next_before": next_before}


@router.post("/packages/{package_id}/deliver")
def deliver_package(package_id: str, body: PackageDeliver | None = None, actor: str = Depends(require_admin)):
    body = body or PackageDeliver()
    device_ids: list[str] = []
    already_delivered = False
    with connect_db() as conn:
        # Read the shipment ID first, then lock shipment before package. Dispatch
        # takes locks in that same order, avoiding a shipment/package deadlock.
        package_ref = conn.execute(
            "SELECT package_id, shipment_id, status FROM packages WHERE package_id = %s",
            (package_id,),
        ).fetchone()
        if package_ref is None:
            api_error(404, "PACKAGE_NOT_FOUND", "Package not found.")
        if package_ref["status"] == "delivered":
            already_delivered = True
        else:
            if package_ref["status"] != "in_transit" or package_ref["shipment_id"] is None:
                api_error(409, "PACKAGE_NOT_IN_TRANSIT", "Only a package in a shipment that is in transit can be delivered.")
            shipment = conn.execute(
                "SELECT status FROM shipments WHERE shipment_id = %s FOR UPDATE",
                (package_ref["shipment_id"],),
            ).fetchone()
            if shipment is None:
                api_error(409, "PACKAGE_HAS_NO_SHIPMENT", "Package has no valid shipment.")
            package = conn.execute(
                "SELECT package_id, shipment_id, device_id, status FROM packages "
                "WHERE package_id = %s FOR UPDATE",
                (package_id,),
            ).fetchone()
            # Another delivery request may have completed while this one waited.
            if package["status"] == "delivered":
                already_delivered = True
            else:
                if package["status"] != "in_transit":
                    api_error(409, "PACKAGE_NOT_IN_TRANSIT", "Only a package in transit can be delivered.")
                conn.execute(
                    "UPDATE packages SET status = 'delivered', delivered_at = NOW(), updated_at = NOW() "
                    "WHERE package_id = %s",
                    (package_id,),
                )
                metadata = {}
                if body.recipient_name:
                    metadata["recipient_name"] = body.recipient_name.strip()
                if body.note:
                    metadata["note"] = body.note.strip()
                insert_shipment_event(
                    conn, package["shipment_id"], "package_delivered", actor,
                    package_id=package_id, from_status="in_transit", to_status="delivered", metadata=metadata,
                )
                counts = conn.execute(
                    "SELECT COUNT(*)::INTEGER AS total, "
                    "COUNT(*) FILTER (WHERE status = 'delivered')::INTEGER AS delivered "
                    "FROM packages WHERE shipment_id = %s",
                    (package["shipment_id"],),
                ).fetchone()
                if counts["delivered"] == counts["total"]:
                    conn.execute(
                        "UPDATE shipments SET status = 'delivered', delivered_at = COALESCE(delivered_at, NOW()), "
                        "updated_at = NOW() WHERE shipment_id = %s",
                        (package["shipment_id"],),
                    )
                elif counts["delivered"] > 0:
                    conn.execute(
                        "UPDATE shipments SET status = 'partially_delivered', updated_at = NOW() WHERE shipment_id = %s",
                        (package["shipment_id"],),
                    )
                else:
                    conn.execute(
                        "UPDATE shipments SET status = 'in_transit', updated_at = NOW() WHERE shipment_id = %s",
                        (package["shipment_id"],),
                    )
                affected = conn.execute(
                    "SELECT device_id FROM packages WHERE shipment_id = %s AND device_id IS NOT NULL",
                    (package["shipment_id"],),
                ).fetchall()
                device_ids = bump_config_versions(conn, [row["device_id"] for row in affected])
    publish_configs(device_ids)
    detail = fetch_package_detail(package_id)
    return {
        "package_id": package_id,
        "status": detail["status"],
        "delivered_at": detail["delivered_at"],
        "shipment_id": detail["shipment_id"],
        "shipment_status": detail["shipment"]["status"] if detail["shipment"] else None,
        "already_delivered": already_delivered,
    }

# ---------------------------------------------------------------------------
# Shipment endpoints (admin)
# ---------------------------------------------------------------------------


@router.post("/shipments", status_code=201)
def create_shipment(body: ShipmentCreate, actor: str = Depends(require_admin)):
    year = datetime.now(timezone.utc).year
    device_ids: list[str] = []
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT package_id, status, shipment_id, device_id FROM packages "
            "WHERE package_id = ANY(%s) FOR UPDATE",
            (body.package_ids,),
        ).fetchall()
        found_ids = {row["package_id"] for row in rows}
        missing = [package_id for package_id in body.package_ids if package_id not in found_ids]
        if missing:
            api_error(404, "PACKAGE_NOT_FOUND", f"Unknown package ID(s): {', '.join(missing)}")
        for row in rows:
            if row["status"] != "packed" or row["shipment_id"] is not None:
                api_error(409, "PACKAGE_NOT_AVAILABLE", f"Package {row['package_id']} must be packed and not attached to another shipment.")
        sequence = conn.execute("SELECT nextval('shipment_id_seq') AS value").fetchone()["value"]
        shipment_id = f"SHP-{year}-{sequence:04d}"
        shipment = conn.execute(
            "INSERT INTO shipments (shipment_id, origin, destination, expected_delivery_at, status) "
            "VALUES (%s, %s, %s, %s, 'packed') "
            "RETURNING shipment_id, origin, destination, expected_delivery_at, status, created_at, updated_at",
            (shipment_id, body.origin, body.destination, body.expected_delivery_at),
        ).fetchone()
        conn.execute(
            "UPDATE packages SET shipment_id = %s, updated_at = NOW() WHERE package_id = ANY(%s)",
            (shipment_id, body.package_ids),
        )
        insert_shipment_event(
            conn, shipment_id, "shipment_created", actor,
            from_status=None, to_status="packed", metadata={"package_ids": body.package_ids},
        )
        device_ids = bump_config_versions(conn, [row["device_id"] for row in rows])
        package_count = len(rows)
    publish_configs(device_ids)
    return {
        **dict(shipment),
        "package_ids": body.package_ids,
        "package_count": package_count,
    }


@router.get("/shipments")
def list_shipments(
    status: Literal["packed", "in_transit", "partially_delivered", "delivered"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    where = "WHERE s.status = %s" if status is not None else ""
    params = [status] if status is not None else []
    with connect_db() as conn:
        total = conn.execute(f"SELECT COUNT(*)::INTEGER AS count FROM shipments s {where}", params).fetchone()["count"]
        rows = conn.execute(
            f"""
            SELECT s.shipment_id, s.origin, s.destination, s.status, s.expected_delivery_at,
                   s.created_at, s.updated_at, s.dispatched_at, s.delivered_at,
                   COUNT(DISTINCT p.package_id)::INTEGER AS package_count,
                   COUNT(DISTINCT p.package_id) FILTER (WHERE p.status = 'delivered')::INTEGER AS delivered_package_count,
                   COUNT(DISTINCT a.alert_id) FILTER (WHERE a.status <> 'resolved')::INTEGER AS open_alert_count
            FROM shipments s LEFT JOIN packages p ON p.shipment_id = s.shipment_id
            LEFT JOIN alerts a ON a.shipment_id = s.shipment_id
            {where}
            GROUP BY s.shipment_id
            ORDER BY s.created_at DESC LIMIT %s OFFSET %s
            """,
            [*params, limit, offset],
        ).fetchall()
    return {"items": rows, "limit": limit, "offset": offset, "total": total}


@router.get("/shipments/{shipment_id}")
def get_shipment(shipment_id: str):
    return fetch_shipment_detail(shipment_id)


@router.patch("/shipments/{shipment_id}")
def update_shipment(shipment_id: str, body: ShipmentUpdate):
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        api_error(400, "NO_FIELDS_SUPPLIED", "Supply origin, destination or expected_delivery_at.")
    for required_text in ("origin", "destination"):
        if required_text in changes and changes[required_text] is None:
            api_error(422, "NULL_NOT_ALLOWED", f"{required_text} cannot be null.")
    with connect_db() as conn:
        shipment = conn.execute(
            "SELECT status FROM shipments WHERE shipment_id = %s FOR UPDATE", (shipment_id,),
        ).fetchone()
        if shipment is None:
            api_error(404, "SHIPMENT_NOT_FOUND", "Shipment not found.")
        if shipment["status"] != "packed":
            api_error(409, "SHIPMENT_NOT_EDITABLE", "Shipment details can only be edited before dispatch.")
        sets = ", ".join(f"{field_name} = %s" for field_name in changes)
        conn.execute(
            f"UPDATE shipments SET {sets}, updated_at = NOW() WHERE shipment_id = %s",
            [*changes.values(), shipment_id],
        )
    return fetch_shipment_detail(shipment_id)


@router.post("/shipments/{shipment_id}/dispatch")
def dispatch_shipment(shipment_id: str, actor: str = Depends(require_admin)):
    device_ids: list[str] = []
    already_dispatched = False
    with connect_db() as conn:
        shipment = conn.execute(
            "SELECT status, dispatched_at FROM shipments WHERE shipment_id = %s FOR UPDATE",
            (shipment_id,),
        ).fetchone()
        if shipment is None:
            api_error(404, "SHIPMENT_NOT_FOUND", "Shipment not found.")
        if shipment["status"] != "packed":
            if shipment["status"] in ("in_transit", "partially_delivered", "delivered"):
                already_dispatched = True
            else:
                api_error(409, "INVALID_STATUS_TRANSITION", "Only packed shipments can be dispatched.")
        else:
            packages = conn.execute(
                """
                SELECT p.package_id, p.status, p.device_id, p.min_temperature_c,
                       p.max_temperature_c, p.shock_threshold_g, d.device_id AS registered_device
                FROM packages p LEFT JOIN devices d ON d.device_id = p.device_id
                WHERE p.shipment_id = %s FOR UPDATE OF p
                """,
                (shipment_id,),
            ).fetchall()
            if not packages:
                api_error(409, "EMPTY_SHIPMENT", "A shipment must contain at least one package before dispatch.")
            for package in packages:
                if package["status"] != "packed":
                    api_error(409, "PACKAGE_NOT_PACKED", f"Package {package['package_id']} is not packed.")
                if package["device_id"] is None or package["registered_device"] is None:
                    api_error(409, "TRACKER_REQUIRED", f"Package {package['package_id']} must have a registered tracker before dispatch.")
                if (
                    package["min_temperature_c"] is None
                    or package["max_temperature_c"] is None
                    or package["min_temperature_c"] > package["max_temperature_c"]
                    or package["shock_threshold_g"] is None
                    or package["shock_threshold_g"] <= 0
                ):
                    api_error(409, "PACKAGE_CONFIGURATION_INVALID", f"Package {package['package_id']} does not have valid monitoring thresholds.")
            conn.execute(
                "UPDATE shipments SET status = 'in_transit', dispatched_at = NOW(), updated_at = NOW() "
                "WHERE shipment_id = %s",
                (shipment_id,),
            )
            conn.execute(
                "UPDATE packages SET status = 'in_transit', dispatched_at = NOW(), updated_at = NOW() "
                "WHERE shipment_id = %s",
                (shipment_id,),
            )
            insert_shipment_event(
                conn, shipment_id, "shipment_dispatched", actor,
                from_status="packed", to_status="in_transit", metadata={},
            )
            assigned = conn.execute(
                "SELECT device_id FROM packages WHERE shipment_id = %s AND device_id IS NOT NULL",
                (shipment_id,),
            ).fetchall()
            device_ids = bump_config_versions(conn, [row["device_id"] for row in assigned])
    publish_configs(device_ids)
    detail = fetch_shipment_detail(shipment_id)
    detail["already_dispatched"] = already_dispatched
    return detail


@router.get("/shipments/{shipment_id}/events")
def shipment_events(shipment_id: str):
    with connect_db() as conn:
        exists = conn.execute("SELECT 1 FROM shipments WHERE shipment_id = %s", (shipment_id,)).fetchone()
        if exists is None:
            api_error(404, "SHIPMENT_NOT_FOUND", "Shipment not found.")
        rows = conn.execute(
            "SELECT id, event_type, package_id, from_status, to_status, actor, occurred_at, metadata "
            "FROM shipment_events WHERE shipment_id = %s ORDER BY occurred_at ASC, id ASC",
            (shipment_id,),
        ).fetchall()
    return {"shipment_id": shipment_id, "items": rows}


# ---------------------------------------------------------------------------
# Device endpoints (admin)
# ---------------------------------------------------------------------------


@router.get("/devices")
def list_devices(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    with connect_db() as conn:
        total = conn.execute("SELECT COUNT(*)::INTEGER AS count FROM devices").fetchone()["count"]
        rows = conn.execute(
            """
            SELECT d.device_id, d.firmware_version, d.registered_at, d.last_seen_at,
                   d.config_version, d.config_applied_version,
                   p.package_id, p.shipment_id
            FROM devices d LEFT JOIN packages p ON p.device_id = d.device_id
            ORDER BY d.device_id LIMIT %s OFFSET %s
            """,
            (limit, offset),
        ).fetchall()
    items = []
    for row in rows:
        items.append({
            **dict(row),
            "status": online_status(row["last_seen_at"]),
        })
    return {"items": items, "limit": limit, "offset": offset, "total": total}


@router.get("/devices/{device_id}")
def get_device(device_id: str):
    with connect_db() as conn:
        device = get_device_or_404(conn, device_id)
        assignment = current_assignment(conn, device_id)
    return {
        **dict(device),
        "status": online_status(device["last_seen_at"]),
        "assignment": dict(assignment) if assignment else None,
    }


@router.get("/devices/{device_id}/settings")
def get_device_settings(device_id: str):
    with connect_db() as conn:
        device = conn.execute(
            "SELECT device_id, config_version FROM devices WHERE device_id = %s", (device_id,),
        ).fetchone()
        if device is None:
            api_error(404, "DEVICE_NOT_FOUND", "Device not found.")
        assignment = current_assignment(conn, device_id)
    assigned = assignment is not None
    return {
        "device_id": device_id,
        "config_version": device["config_version"],
        "assigned": assigned,
        "package_id": assignment["package_id"] if assigned else None,
        "shipment_id": assignment["shipment_id"] if assigned else None,
        "min_temperature_c": assignment["min_temperature_c"] if assigned else None,
        "max_temperature_c": assignment["max_temperature_c"] if assigned else None,
        "shock_threshold_g": assignment["shock_threshold_g"] if assigned else None,
        "reporting_interval_seconds": REPORTING_INTERVAL_SECONDS,
    }


@router.patch("/devices/{device_id}/settings")
def retired_device_settings(device_id: str):
    api_error(
        410,
        "DEVICE_SETTINGS_MOVED_TO_PACKAGE",
        "Edit monitoring thresholds through PATCH /api/v1/packages/{package_id}.",
    )


@router.get("/devices/{device_id}/telemetry")
def device_telemetry(
    device_id: str,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    with connect_db() as conn:
        get_device_or_404(conn, device_id)
        total = conn.execute(
            "SELECT COUNT(*)::INTEGER AS count FROM telemetry WHERE device_id = %s", (device_id,),
        ).fetchone()["count"]
        rows = conn.execute(
            """
            SELECT id, observed_at, received_at, boot_id, message_seq, config_version,
                   temperature_valid, temperature_c, motion_valid,
                   peak_acceleration_magnitude_g, shock_count_since_last_report,
                   location_fix_valid, latitude, longitude, accuracy_m, satellites_used
            FROM telemetry WHERE device_id = %s
            ORDER BY observed_at DESC, id DESC LIMIT %s OFFSET %s
            """,
            (device_id, limit, offset),
        ).fetchall()
    return {"items": rows, "limit": limit, "offset": offset, "total": total}


# ---------------------------------------------------------------------------
# Alert endpoints (admin)
# ---------------------------------------------------------------------------


@router.get("/alerts")
def list_alerts(
    status: Literal["open", "acknowledged", "resolved"] | None = None,
    device_id: str | None = None,
    package_id: str | None = None,
    shipment_id: str | None = None,
    type: Literal["temperature_breach", "shock_detected"] | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    parts = []
    params: list[Any] = []
    for column, value in (
        ("a.status", status), ("a.source_device_id", device_id),
        ("a.package_id", package_id), ("a.shipment_id", shipment_id), ("a.type", type),
    ):
        if value is not None:
            parts.append(f"{column} = %s")
            params.append(value)
    return list_alert_sql(parts, params, limit, offset)


@router.get("/alerts/{alert_id}")
def get_alert(alert_id: str):
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT alert_id, source_device_id AS device_id, source_boot_id,
                   source_message_seq, package_id, shipment_id, type, severity,
                   status, observed_at, received_at, details, acknowledged_at,
                   acknowledged_by, resolved_at, resolved_by, resolution_note
            FROM alerts WHERE alert_id = %s
            """,
            (alert_id,),
        ).fetchone()
    if row is None:
        api_error(404, "ALERT_NOT_FOUND", "Alert not found.")
    return row


@router.post("/alerts/{alert_id}/acknowledge")
def acknowledge_alert(alert_id: str, actor: str = Depends(require_admin)):
    with connect_db() as conn:
        row = conn.execute(
            """
            UPDATE alerts SET status = 'acknowledged', acknowledged_at = NOW(), acknowledged_by = %s
            WHERE alert_id = %s AND status = 'open'
            RETURNING alert_id, source_device_id AS device_id, source_boot_id,
                      source_message_seq, package_id, shipment_id, type, severity,
                      status, observed_at, received_at, details, acknowledged_at,
                      acknowledged_by, resolved_at, resolved_by, resolution_note
            """,
            (actor, alert_id),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT alert_id, source_device_id AS device_id, source_boot_id,
                       source_message_seq, package_id, shipment_id, type, severity,
                       status, observed_at, received_at, details, acknowledged_at,
                       acknowledged_by, resolved_at, resolved_by, resolution_note
                FROM alerts WHERE alert_id = %s
                """,
                (alert_id,),
            ).fetchone()
            if row is None:
                api_error(404, "ALERT_NOT_FOUND", "Alert not found.")
            # Idempotent: repeat acknowledgements preserve the first actor/time.
    return row


@router.post("/alerts/{alert_id}/resolve")
def resolve_alert(alert_id: str, body: AlertResolve | None = None, actor: str = Depends(require_admin)):
    body = body or AlertResolve()
    with connect_db() as conn:
        row = conn.execute(
            """
            UPDATE alerts SET status = 'resolved', resolved_at = NOW(), resolved_by = %s,
                              resolution_note = %s
            WHERE alert_id = %s AND status <> 'resolved'
            RETURNING alert_id, source_device_id AS device_id, source_boot_id,
                      source_message_seq, package_id, shipment_id, type, severity,
                      status, observed_at, received_at, details, acknowledged_at,
                      acknowledged_by, resolved_at, resolved_by, resolution_note
            """,
            (actor, body.resolution_note, alert_id),
        ).fetchone()
        if row is None:
            row = conn.execute(
                """
                SELECT alert_id, source_device_id AS device_id, source_boot_id,
                       source_message_seq, package_id, shipment_id, type, severity,
                       status, observed_at, received_at, details, acknowledged_at,
                       acknowledged_by, resolved_at, resolved_by, resolution_note
                FROM alerts WHERE alert_id = %s
                """,
                (alert_id,),
            ).fetchone()
            if row is None:
                api_error(404, "ALERT_NOT_FOUND", "Alert not found.")
    return row


# ---------------------------------------------------------------------------
# Optional inventory summary
# ---------------------------------------------------------------------------


@router.get("/inventory/summary")
def inventory_summary():
    with connect_db() as conn:
        package_counts = conn.execute(
            "SELECT COUNT(*)::INTEGER AS total, "
            "COUNT(*) FILTER (WHERE status = 'packed')::INTEGER AS packed, "
            "COUNT(*) FILTER (WHERE status = 'in_transit')::INTEGER AS in_transit, "
            "COUNT(*) FILTER (WHERE status = 'delivered')::INTEGER AS delivered "
            "FROM packages"
        ).fetchone()
        packages_with_alerts = conn.execute(
            "SELECT COUNT(DISTINCT package_id)::INTEGER AS count FROM alerts "
            "WHERE status <> 'resolved' AND package_id IS NOT NULL"
        ).fetchone()["count"]
        shipments_in_transit = conn.execute(
            "SELECT COUNT(*)::INTEGER AS count FROM shipments WHERE status = 'in_transit'"
        ).fetchone()["count"]
        devices = conn.execute("SELECT last_seen_at FROM devices").fetchall()
    online = sum(1 for row in devices if online_status(row["last_seen_at"]) == "online")
    return {
        "packages_total": package_counts["total"],
        "packages_packed": package_counts["packed"],
        "packages_in_transit": package_counts["in_transit"],
        "packages_delivered": package_counts["delivered"],
        "packages_with_open_alerts": packages_with_alerts,
        "shipments_in_transit": shipments_in_transit,
        "devices_online": online,
        "devices_offline": len(devices) - online,
    }


# ---------------------------------------------------------------------------
# Public, read-only tracking
# ---------------------------------------------------------------------------


@app.get("/api/v1/public/tracking/{token}")
def public_tracking(token: str):
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with connect_db() as conn:
        row = conn.execute(
            """
            SELECT p.package_id, p.product_name, p.quantity, p.unit, p.status AS package_status,
                   p.shipment_id, p.updated_at AS package_updated_at,
                   s.origin, s.destination, s.status AS shipment_status,
                   s.expected_delivery_at, s.updated_at AS shipment_updated_at,
                   lt.temperature_valid, lt.temperature_c, lt.observed_at,
                   lt.received_at,
                   (SELECT COUNT(*)::INTEGER FROM alerts a
                    WHERE a.package_id = p.package_id AND a.status <> 'resolved') AS open_alert_count,
                   GREATEST(p.updated_at, COALESCE(s.updated_at, p.updated_at),
                            COALESCE(lt.received_at, p.updated_at)) AS updated_at
            FROM packages p
            LEFT JOIN shipments s ON s.shipment_id = p.shipment_id
            LEFT JOIN LATERAL (
                SELECT temperature_valid, temperature_c, observed_at, received_at
                FROM telemetry t WHERE t.package_id = p.package_id
                ORDER BY observed_at DESC, id DESC LIMIT 1
            ) lt ON TRUE
            WHERE p.tracking_token_hash = %s
            """,
            (token_hash,),
        ).fetchone()
        if row is None:
            api_error(404, "TRACKING_NOT_FOUND", "Tracking token is invalid or no longer available.")
        events = conn.execute(
            """
            SELECT event_type, occurred_at FROM shipment_events
            WHERE shipment_id = %s AND (package_id IS NULL OR package_id = %s)
            ORDER BY occurred_at ASC, id ASC
            """,
            (row["shipment_id"], row["package_id"]),
        ).fetchall() if row["shipment_id"] else []

    unresolved_alert = row["open_alert_count"] > 0
    if unresolved_alert:
        condition = "alert"
    elif row["temperature_valid"]:
        condition = "normal"
    else:
        condition = "unknown"
    labels = {
        "shipment_created": "Shipment created",
        "shipment_dispatched": "Shipment dispatched",
        "package_delivered": "Package delivered",
    }
    return {
        "package": {
            "package_id": row["package_id"],
            "product_name": row["product_name"],
            "quantity": row["quantity"],
            "unit": row["unit"],
            "status": row["package_status"],
        },
        "shipment": None if row["shipment_id"] is None else {
            "shipment_id": row["shipment_id"],
            "origin": row["origin"],
            "destination": row["destination"],
            "status": row["shipment_status"],
            "expected_delivery_at": row["expected_delivery_at"],
        },
        "monitoring": {
            "temperature_valid": bool(row["temperature_valid"]),
            "temperature_c": row["temperature_c"] if row["temperature_valid"] else None,
            "observed_at": row["observed_at"],
            "condition": condition,
            "open_alert_count": row["open_alert_count"],
        },
        "timeline": [
            {"event_type": event["event_type"], "occurred_at": event["occurred_at"],
             "label": labels.get(event["event_type"], "Shipment update")}
            for event in events
        ],
        "updated_at": row["updated_at"],
    }


@app.get("/track/{token}", response_class=HTMLResponse)
def public_tracking_page(token: str):
    """Small read-only tracking page; a separate frontend can replace this route."""
    return HTMLResponse("""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Package tracking</title><style>body{font:16px system-ui;max-width:720px;margin:3rem auto;padding:0 1rem;color:#222}.muted{color:#666}</style></head>
<body><h1>Package tracking</h1><p class="muted">Read-only tracking information. This page cannot confirm delivery.</p><div id="result">Loading tracking information…</div>
<script>
const token = decodeURIComponent(window.location.pathname.split('/').pop());
const root = document.getElementById('result');
function esc(s) { return String(s ?? '').replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',"'":'&#39;'}[c])); }
fetch('/api/v1/public/tracking/' + encodeURIComponent(token)).then(async r => { const d = await r.json(); if (!r.ok) throw new Error(d.detail?.message || d.detail || 'Tracking unavailable');
root.innerHTML = '<h2>' + esc(d.package.product_name) + '</h2><p>Package: ' + esc(d.package.package_id) + '</p><p>Status: ' + esc(d.package.status) + '</p><p>Temperature: ' + (d.monitoring.temperature_valid ? esc(d.monitoring.temperature_c) + ' °C' : 'Unknown') + '</p><p>Condition: ' + esc(d.monitoring.condition) + '</p><h3>Timeline</h3><ul>' + d.timeline.map(e => '<li>' + esc(e.label) + ' — ' + esc(e.occurred_at) + '</li>').join('') + '</ul>';
}).catch(e => { root.textContent = e.message; });
</script></body></html>""")

app.include_router(router)


if __name__ == "__main__":
    uvicorn.run(app, host=API_HOST, port=API_PORT)
