# ColdChain Telemetry Backend

Backend for the ColdChain monitoring and shipment-tracking MVP. It receives ESP32 messages over MQTT, persists device telemetry and explicitly reported incidents in PostgreSQL, exposes a session-protected REST API for the dashboard, and serves a read-only public tracking page for package QR codes.

> **Contract reminder:** a package owns its product metadata and monitoring thresholds. The ESP32 does the local temperature/shock comparisons and sends explicit event messages. The Python backend stores routine telemetry; it does **not** infer alerts by comparing telemetry with thresholds.

## Contents

- [Features and architecture](#features-and-architecture)
- [Stack](#stack)
- [Project files](#project-files)
- [Setup on Debian/WSL](#setup-on-debianwsl)
- [Start PostgreSQL with Podman](#start-postgresql-with-podman)
- [Start Mosquitto with Podman](#start-mosquitto-with-podman)
- [Configure environment](#configure-environment)
- [Install dependencies and run](#install-dependencies-and-run)
- [Bootstrap the first administrator](#bootstrap-the-first-administrator)
- [Accounts and authorization](#accounts-and-authorization)
- [Browser session and CSRF requirements](#browser-session-and-csrf-requirements)
- [Package, shipment and delivery workflow](#package-shipment-and-delivery-workflow)
- [MQTT and ESP32 contract](#mqtt-and-esp32-contract)
- [Test the running system](#test-the-running-system)
- [Database schema and migrations](#database-schema-and-migrations)
- [API documentation](#api-documentation)
- [Troubleshooting](#troubleshooting)
- [Security and deployment limits](#security-and-deployment-limits)

## Features and architecture

- **ESP32 → MQTT → PostgreSQL:** registration, telemetry and event messages arrive at `coldchain/{device_id}/messages`.
- **Device identity and diagnostics:** firmware version, hardware model, capabilities, last-seen time, latest accepted envelope, last boot ID, and configuration versions.
- **Package-owned thresholds:** each package stores its temperature minimum/maximum and shock threshold. There are no global temperature or shock threshold environment variables.
- **Retained MQTT configuration:** when assignment, package details, thresholds or shipment/package lifecycle state changes, the backend increments the affected device configuration version and publishes the current config.
- **Shipment lifecycle:** create a shipment with packages, dispatch it, confirm delivery package by package, and inspect its append-only event timeline.
- **Explicit alerts:** only device-reported `temperature_breach` and `shock_detected` events create alert rows. Alerts can be acknowledged and resolved.
- **Message deduplication:** `(device_id, boot_id, message_seq)` prevents MQTT QoS 1 redelivery from duplicating registration, telemetry or alerts.
- **Dashboard authentication:** Argon2id password hashes, opaque database-backed sessions, HttpOnly cookies, CSRF checks, login throttling, and Admin/Operator role checks.
- **Invite-only accounts:** the first admin is created through a CLI command. Admins invite users with single-use activation links; there is no unrestricted public sign-up.
- **User administration:** admins can list accounts and invitations, change roles, disable/reactivate users, revoke sessions, issue password-reset invitations and inspect the audit log.
- **Read-only QR tracking:** a random bearer token is generated at package creation. Only its SHA-256 hash is stored. A public QR scan cannot dispatch or deliver a package.
- **Health and readiness:** `/health` reports process liveness; `/readyz` requires PostgreSQL/schema and the MQTT consumer to be ready.

## Stack

- **ESP32 + Arduino framework:** sensor sampling, local threshold comparison, event detection, and MQTT publishing.
- **Mosquitto:** MQTT broker; can run in Podman for local development.
- **Paho MQTT 2.x:** Python MQTT client.
- **FastAPI + Uvicorn:** REST API and small built-in activation/tracking pages.
- **Pydantic v2:** strict validation of REST request models.
- **Psycopg 3 + PostgreSQL:** persistence, transaction boundaries, deduplication, sessions and audit history.
- **argon2-cffi:** password hashing.
- **Python 3.11+ recommended.**
- `uv` is optional. A standard Python virtual environment works too.

## Project files

The backend bundle contains:

| File | Purpose |
|---|---|
| `Backend.py` | FastAPI app, database schema checks, MQTT consumer, API routes, and `create-admin` CLI command |
| `requirements.txt` | Runtime Python dependencies |
| `.env.example` | Example environment configuration; copy it to `.env` and edit it |
| `README.md` | Setup, operation, security and troubleshooting guide |
| `API_SPEC.md` | Detailed frontend/API and MQTT contract |
| `mosquitto.conf` | Anonymous, persistent broker config intended only for loopback development |

Do not commit `.env`, passwords, broker credentials, invitation links, session cookies, tracking-token URLs or generated secrets.

## Setup on Debian/WSL

Commands below assume a Debian 13 or similar Linux shell. Run commands from the directory containing `Backend.py`.

Install Podman and basic tools:

```bash
sudo apt update
sudo apt install -y podman python3 python3-venv
```

Install `uv` only if you prefer it, following the [official uv installation guide](https://docs.astral.sh/uv/getting-started/installation/).

If native Mosquitto is installed and already occupies port 1883, stop it before using the container:

```bash
sudo systemctl disable --now mosquitto
```

If the service is not installed, skip that command.

## Start PostgreSQL with Podman

Create the PostgreSQL container and named volume once:

```bash
podman run -d \
  --name coldchain-postgres \
  -e POSTGRES_USER=coldchain \
  -e POSTGRES_PASSWORD=change_this_password \
  -e POSTGRES_DB=coldchain \
  -p 127.0.0.1:5432:5432 \
  -v coldchain_pgdata:/var/lib/postgresql \
  docker.io/library/postgres:18
```

Use the same password in `DATABASE_URL` in `.env`. The host port is bound to loopback; it is available to processes running on the same host/WSL environment and is not exposed to the LAN.

If the container already exists, start it instead of creating another one:

```bash
podman start coldchain-postgres
```

Check status and readiness:

```bash
podman ps
podman exec coldchain-postgres pg_isready -U coldchain -d coldchain
podman logs --tail=100 coldchain-postgres
```

Useful commands:

```bash
podman stop coldchain-postgres
podman start coldchain-postgres
podman logs -f coldchain-postgres
```

The named volume `coldchain_pgdata` preserves database files when the container stops. Do not remove it unless you intend to discard the database data.

## Start Mosquitto with Podman

The bundled `mosquitto.conf` enables anonymous access for isolated localhost testing only. The container port is published on host loopback. **Do not expose this anonymous broker to your LAN or the internet.**

Create the container once:

```bash
podman run -d \
  --name coldchain-mosquitto \
  -p 127.0.0.1:1883:1883 \
  -v "$PWD/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
  -v coldchain_mosquitto_data:/mosquitto/data \
  docker.io/eclipse-mosquitto:2
```

If the container already exists, check its current config/port mapping and start it:

```bash
podman start coldchain-mosquitto
```

If it has an incorrect mapping, recreate the container after confirming that you do not need its container-specific configuration. Do not add `--volumes` unless you intend to remove its named data volume as well.

Check the broker:

```bash
podman ps
podman logs --tail=100 coldchain-mosquitto
```

Useful commands:

```bash
podman stop coldchain-mosquitto
podman start coldchain-mosquitto
podman logs -f coldchain-mosquitto
```

`coldchain_mosquitto_data` persists retained MQTT messages and broker state across container restarts.

### Physical ESP32 connectivity

A device cannot reach the laptop using `127.0.0.1`; that address points to the device itself. The commands above intentionally keep MQTT loopback-only.

Before connecting a physical tracker across Wi-Fi/LAN, configure Mosquitto usernames/passwords, per-device ACLs, and preferably TLS; then bind/publish the broker on an appropriate LAN interface and set the ESP32's broker host to the laptop's reachable LAN IP. A matching `device_id` in topic and JSON is only a consistency check, not authentication. Permit each tracker to publish only to its own `coldchain/{device_id}/messages` topic and subscribe only to its own ACK/config topics. Only the backend should publish config messages.

## Configure environment

Create the local environment file:

```bash
cp .env.example .env
```

Edit values as needed. The current `.env.example` contains:

```dotenv
DATABASE_URL=postgresql://coldchain:change_this_password@127.0.0.1:5432/coldchain

MQTT_HOST=127.0.0.1
MQTT_PORT=1883
MQTT_TOPIC=coldchain/+/messages
MQTT_USERNAME=
MQTT_PASSWORD=

ONLINE_TIMEOUT_SECONDS=60
LOG_LEVEL=INFO
API_HOST=0.0.0.0
API_PORT=8000

PUBLIC_BASE_URL=http://127.0.0.1:8000
FRONTEND_BASE_URL=http://127.0.0.1:8000
ACTIVATION_BASE_URL=http://127.0.0.1:8000/activate

COOKIE_SECURE=false
COOKIE_SAMESITE=lax
SESSION_COOKIE_NAME=coldchain_session
SESSION_TTL_SECONDS=604800
SESSION_IDLE_TIMEOUT_SECONDS=43200
CORS_ORIGINS=http://localhost:5173,http://127.0.0.1:5173

LOGIN_WINDOW_SECONDS=900
LOGIN_MAX_ATTEMPTS=10
INVITATION_TTL_HOURS=24
```

| Variable | Meaning |
|---|---|
| `DATABASE_URL` | Connection string for an **already-created** PostgreSQL database |
| `MQTT_HOST`, `MQTT_PORT` | Broker address/port as seen by the backend process |
| `MQTT_TOPIC` | Incoming-device subscription; normally `coldchain/+/messages` |
| `MQTT_USERNAME`, `MQTT_PASSWORD` | Optional credentials for the backend's MQTT client |
| `ONLINE_TIMEOUT_SECONDS` | A device is online when the latest accepted message is no older than this; default `60` |
| `LOG_LEVEL` | Python logging level, e.g. `INFO` or `DEBUG` |
| `API_HOST`, `API_PORT` | Uvicorn bind address and port |
| `PUBLIC_BASE_URL` | Origin placed in newly generated public tracking URLs |
| `FRONTEND_BASE_URL` | Base origin for account activation URLs unless overridden |
| `ACTIVATION_BASE_URL` | Exact URL used for invitation/password-reset links; defaults to `/activate` on the frontend base |
| `COOKIE_SECURE` | Set `true` when using HTTPS; `false` is only for local HTTP development |
| `COOKIE_SAMESITE` | `lax`, `strict`, or `none`; `none` requires `COOKIE_SECURE=true` |
| `SESSION_COOKIE_NAME` | Cookie name; default `coldchain_session` |
| `SESSION_TTL_SECONDS` | Absolute session expiry; default seven days |
| `SESSION_IDLE_TIMEOUT_SECONDS` | Server-side idle expiry; default 12 hours |
| `CORS_ORIGINS` | Comma-separated exact frontend origins allowed to make credentialed requests |
| `LOGIN_WINDOW_SECONDS` | Failed-login throttling window; default 15 minutes |
| `LOGIN_MAX_ATTEMPTS` | Failed attempts per client IP per window, in the current process; default `10` |
| `INVITATION_TTL_HOURS` | Lifetime of activation/password-reset links; default 24 hours |

There are **no** `DEFAULT_MAX_TEMPERATURE_C`, `DEFAULT_SHOCK_THRESHOLD_G`, or device-owned threshold environment variables. Temperature limits and shock thresholds come from the assigned package. The reporting interval is currently fixed in the backend at 15 seconds and is included in device config.

### Load the environment

`Backend.py` does not automatically parse `.env`. In Bash, you can export it for CLI and server commands:

```bash
set -a
source .env
set +a
```

Keep the shell open for the commands below. Alternatively, use an environment loader you already trust. Do not commit `.env`.

For local browser testing, `COOKIE_SECURE=false` is needed when serving plain HTTP. In an HTTPS deployment set it to `true`. `CORS_ORIGINS` must be the exact origin(s), including scheme and port; do not use `*` with credentialed cookies.

If the frontend runs on Vite at `http://localhost:5173`, set `FRONTEND_BASE_URL` to that origin and implement the activation page there, or leave `ACTIVATION_BASE_URL` pointing at the backend's built-in `/activate` page. For QR links opened on a phone, set `PUBLIC_BASE_URL` to a URL that phone can reach; `127.0.0.1` only works on the machine that owns the address.

## Install dependencies and run

Using `venv` and pip:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python Backend.py
```

The application starts on `API_HOST:API_PORT`, normally `http://127.0.0.1:8000` from the local browser. The server binds `0.0.0.0` by default, but firewall and URL settings still govern reachability.

Using `uv` instead:

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt
python Backend.py
```

The `uv` commands above install the provided `requirements.txt`; this bundle does not require a `pyproject.toml`.

When startup finishes, visit:

- Interactive API docs: <http://127.0.0.1:8000/docs>
- Liveness: <http://127.0.0.1:8000/health>
- Readiness: <http://127.0.0.1:8000/readyz>
- Public tracking page pattern: `http://127.0.0.1:8000/track/{token}`

`/health` is intentionally only a liveness endpoint. `/readyz` returns HTTP 503 until the configured PostgreSQL database/schema and MQTT connection/subscription are ready.

## Bootstrap the first administrator

Run this **once** against the intended database after loading `.env` and installing dependencies:

```bash
python Backend.py create-admin --email admin@example.test
```

The command checks/creates the schema in the already-existing database, prompts for the password twice, hashes it with Argon2id, and creates the initial administrator. The password is not supplied as a command-line argument. Passwords must be 12–128 characters.

It refuses to create another initial administrator if an active admin already exists. Do not work around this by inserting password hashes manually; sign in to the dashboard and use user management after bootstrapping.

Then run the server:

```bash
python Backend.py
# Equivalent explicit command:
python Backend.py serve
```

If `/readyz` remains at 503, inspect server logs and check PostgreSQL and Mosquitto. The process can remain alive for `/health` even if dependency initialization fails, but MQTT ingestion is not started unless the schema check succeeds.

## Accounts and authorization

There is no unrestricted `POST /register` or public self-sign-up. The account lifecycle is:

1. Run the CLI bootstrap command once to create the first active admin.
2. Log in with `POST /api/v1/auth/login`.
3. The admin creates an invitation with `POST /api/v1/auth/users/invitations`. `operator` is the default role; only an admin can create invitations or assign the `admin` role.
4. Share the returned one-time `activation_url` privately. The recipient sets their own password via the activation page or `POST /api/v1/auth/activate`.
5. Users log in normally and receive an opaque server-side session cookie.
6. Admins manage existing accounts through `GET /api/v1/auth/users` and `PATCH /api/v1/auth/users/{user_id}`. Admins can also revoke sessions, revoke invitations, issue password-reset invitations, and inspect the audit log.

Roles:

- **`admin`:** all operator functionality plus account/invitation management, tracking-token rotation and audit-log access.
- **`operator`:** package and shipment operations, devices, telemetry, inventory summary and alert handling. Operators cannot manage user accounts or rotate public tracking tokens.
- **Public visitor:** liveness/readiness, login, invitation activation, public tracking JSON and the read-only tracking page only.

Authorization is checked in the backend. Hiding an admin button in the frontend is not a security control. The system prevents demoting or disabling the last active administrator. Disabling an account revokes its active sessions. A password reset revokes the account's active sessions; a normal password change revokes the user's other sessions.

### Invitation and password reset behavior

Activation and password-reset links are single-use and expire after `INVITATION_TTL_HOURS` (24 by default). Raw tokens appear only in the create-invitation response; PostgreSQL stores token hashes. Creating a new activation/reset invitation for an email revokes older outstanding invitations for that email. The link carries the token in a URL fragment (`#token=...`) so it is not sent to the web server in the initial HTTP request. The built-in `/activate` page reads the fragment and submits the token/password to the public activation endpoint.

## Browser session and CSRF requirements

Because the dashboard uses cookie-based authentication, the frontend must:

1. Send requests with cookies enabled, e.g. `fetch(url, { credentials: "include" })`.
2. Never read or store the session cookie in JavaScript; it is `HttpOnly`.
3. Capture `csrf_token` from successful login or `GET /api/v1/auth/me` and keep it in application memory.
4. Send `X-CSRF-Token: <csrf_token>` on **every authenticated state-changing request** (`POST`, `PATCH`, `DELETE`), including logout and user management.
5. After page reload, call `/api/v1/auth/me` to check the session and obtain a new CSRF token for the existing session.
6. Handle `401` by showing the login screen and `403` as a permission/CSRF failure, not as a successful action.

Login and invitation activation are public and do not require a session cookie or CSRF header. Public tracking is read-only. For cross-origin frontend/API deployments, CORS must allow the exact frontend origin, credentials, and `X-CSRF-Token`; the frontend cannot use wildcard origins with credentials.

The in-process login limiter is suitable only for a single-process hackathon demo. Its counters reset on server restart and are not shared between worker processes. Production needs a shared limiter and additional operational controls.

## Package, shipment and delivery workflow

1. A physical device publishes a valid `register` envelope. The backend creates/updates its device identity/capabilities and responds on its registration ACK topic. Registration does not accept thresholds.
2. An operator creates a package via `POST /api/v1/packages`. The request supplies product fields and the effective `min_temperature_c`, `max_temperature_c`, and `shock_threshold_g`. `device_id` is optional but, if supplied, must already be registered and not assigned to another package.
3. The backend creates a random public tracking token, stores its hash, and returns `tracking_url` once. Store or print the URL/QR securely; the raw token cannot be reconstructed from the database.
4. If a device is assigned, the backend increments `config_version` and publishes retained config to `coldchain/{device_id}/config`. If no device is assigned yet, create the package first and assign the tracker later using `PATCH /api/v1/packages/{package_id}` while the package is still packed.
5. Create a shipment using `POST /api/v1/shipments` with a non-empty `package_ids` list. Packages must exist, be packed, and not already belong to a shipment. Shipment and package attachment plus the `shipment_created` event are committed together.
6. Dispatch with `POST /api/v1/shipments/{shipment_id}/dispatch`. Every package must be packed and have a registered assigned tracker; the backend also checks that monitoring thresholds are valid and the tracker has reported applying its current configuration version. Dispatch updates shipment/packages and inserts a timeline event in one transaction. The repeated dispatch action is idempotent.
7. Confirm delivery with authenticated `POST /api/v1/packages/{package_id}/deliver`. This is a deliberate operator/admin action; public QR scanning cannot confirm delivery. The package/shipment lifecycle updates and `package_delivered` event are handled transactionally. Repeated delivery does not create another event.
8. The public tracking page calls `GET /api/v1/public/tracking/{token}`. The response is sanitized and read-only.

Status values:

- Package: `packed`, `in_transit`, `delivered`.
- Shipment: `packed`, `in_transit`, `partially_delivered`, `delivered`.
- Alert: `open`, `acknowledged`, `resolved`.

Generic package PATCH cannot set a status, shipment ID, tracking hash or lifecycle timestamp. Use dispatch/delivery action endpoints so state transitions are controlled and auditable.

## MQTT and ESP32 contract

Incoming topic: `coldchain/{device_id}/messages`, QoS 1 recommended, retain false. The backend subscribes to `coldchain/+/messages`. It replies on `coldchain/{device_id}/registration/ack` and publishes retained authoritative config to `coldchain/{device_id}/config`.

Every device-originated envelope has exactly these fields:

```json
{
  "schema_version": 1,
  "message_type": "telemetry",
  "device_id": "tracker-001",
  "firmware_version": "0.1.0",
  "boot_id": "7f3a9c21",
  "message_seq": 42,
  "config_version": 3,
  "observed_at": "2026-10-28T01:15:15Z",
  "uptime_ms": 15000,
  "payload": {}
}
```

Requirements:

- `schema_version` must be integer `1`.
- `device_id` must match the MQTT topic segment and `^[A-Za-z0-9_-]{1,64}$`.
- `firmware_version` and `boot_id` are non-empty strings, at most 128 characters.
- `message_seq`, `config_version`, and `uptime_ms` are non-negative integers. Start sequence at zero after every reboot with a new random `boot_id`; sequence increases across registration, telemetry and events for that boot.
- `observed_at` must be timezone-aware ISO 8601/RFC 3339. UTC `Z` is preferred.
- Unknown envelope fields are rejected. `payload` must be an object.
- A new device must register with `config_version: 0`. Telemetry/events from unregistered devices are rejected. Reported config versions newer than the backend's current version are rejected.
- Insert the deduplication key and the telemetry/alert row in the same transaction. Do not send duplicate message keys.

### Registration payload

```json
{
  "hardware_model": "esp32-devkit-v1",
  "capabilities": {
    "temperature": true,
    "accelerometer": true,
    "gnss": true,
    "epaper": false
  }
}
```

Both `hardware_model` and `capabilities` keys are required. `hardware_model` may be `null` or a non-empty string; `capabilities` is an object mapping capability names to booleans. **Do not include package thresholds or per-device settings in registration.**

### Telemetry payload

```json
{
  "temperature": {"valid": true, "celsius": 5.4},
  "motion": {
    "valid": true,
    "peak_acceleration_magnitude_g": 1.08,
    "shock_count_since_last_report": 0
  },
  "location": {
    "fix_valid": false,
    "latitude": null,
    "longitude": null,
    "accuracy_m": null,
    "satellites_used": 0
  }
}
```

The three objects and their exact keys are required. Invalid temperature/motion readings use `valid: false` with a `null` value; never substitute zero for an invalid sensor. If `location.fix_valid` is false, latitude/longitude/accuracy must all be `null`. If a valid fix is claimed, latitude and longitude are required. Acceleration telemetry is expressed in **g** as `peak_acceleration_magnitude_g`, not `m/s²`.

### Event payloads

Temperature breach:

```json
{
  "type": "temperature_breach",
  "temperature_c": 9.1,
  "breach_direction": "above_maximum",
  "min_temperature_c": 2.0,
  "max_temperature_c": 8.0
}
```

`breach_direction` is `below_minimum` or `above_maximum`. Include the local reading and the bounds used by firmware.

Shock detected:

```json
{
  "type": "shock_detected",
  "peak_acceleration_magnitude_g": 5.7,
  "threshold_g": 4.0,
  "duration_ms": 35
}
```

`duration_ms` is optional and, if present, must be a non-negative integer. Only an explicit supported event creates an alert. Firmware should emit a temperature event on entering a breach episode, not on every main loop iteration. The backend does not calculate a shock threshold from telemetry.

### Registration ACK and retained device config

A successful registration ACK includes `message_type: "registration_ack"`, `device_id`, `status` (`registered` or `already_registered`), and `reason: null`. Invalid registration payloads may receive `status: "rejected"` and a reason. Subscribe to ACK and config **before** publishing registration.

Assigned config contains `schema_version`, `message_type: "device_config"`, `device_id`, `config_version`, `issued_at`, `assignment`, `package`, and `monitoring`. The `assignment` object provides `assigned`, package/shipment IDs and current statuses. `package` contains `product_sku` and `product_name`. `monitoring.temperature` contains min/max Celsius limits and `monitoring.shock.threshold_g` carries the shock threshold. `monitoring.enabled` is false for an unassigned device and for delivered packages/shipments. Unassigned config has `package: null`, temperature/shock config null and monitoring disabled. The backend currently publishes `reporting_interval_seconds: 15`.

Firmware must validate configuration and reject stale config versions. Include the version actually applied in all subsequent telemetry/event messages. A config publish cannot prove the firmware applied it; the dispatch endpoint requires the backend to have observed the latest applied version in an accepted device message.

## Test the running system

The backend bundle does not include the project's prior `publish_test.py` simulator. If that file exists in your main repository, update it to the version-1 envelope above; do not reuse the older simulator unchanged.

1. Start PostgreSQL and Mosquitto.
2. Load `.env` into the shell.
3. Create the initial admin once.
4. Start `python Backend.py`.
5. Check `/health` and `/readyz`.
6. Use `/docs` to log in and test protected endpoints. In Swagger, login sets the cookie, but you must manually copy the returned `csrf_token` into the `X-CSRF-Token` header for state-changing requests; the built-in docs UI will not automatically inject that header.
7. Publish a valid register envelope from the simulator/device; check `GET /api/v1/devices` and `/api/v1/devices/{device_id}`.
8. Create a package with temperature/shock thresholds, optionally assigning the registered tracker. Confirm a retained config appears on the device config topic.
9. Create a shipment including the package, wait until the tracker reports the current `config_version`, dispatch, publish routine telemetry and an explicit event, then inspect telemetry and alerts.
10. Open the returned tracking URL in a separate browser/private window. Verify it exposes only public tracking data and offers no state-changing delivery action.

To inspect MQTT locally, use an MQTT client such as `mosquitto_sub`/`mosquitto_pub` installed on the host or a temporary client container connected to the broker. Remember that the local broker permits anonymous access only because it is loopback-bound.

## Database schema and migrations

The configured PostgreSQL **database must already exist**. The application creates/checks tables and indexes at startup; it does not create the database, drop existing data or automatically migrate old table definitions. If a pre-existing table is missing columns required by this backend, startup's schema check raises an incompatibility error, `/readyz` stays at 503, and MQTT ingestion does not start.

Main tables:

| Table | Purpose |
|---|---|
| `devices` | Identity, firmware/capabilities, last-seen and config diagnostics |
| `packages` | Product metadata, package status, package-owned thresholds, optional device/shipment assignment, tracking-token hash |
| `shipments` | Origin/destination, ETA, status and lifecycle timestamps |
| `telemetry` | Typed routine sensor observations plus validated raw envelope JSONB |
| `alerts` | Explicit device-originated alert events and acknowledgement/resolution lifecycle |
| `processed_messages` | Deduplication key for all device message types |
| `shipment_events` | Append-only shipment/package lifecycle timeline |
| `users` | Admin/operator accounts and Argon2id password hashes |
| `user_sessions` | Hashes of opaque session tokens, expiry and revocation state |
| `user_invitations` | Hashed, one-time activation/password-reset token records |
| `audit_log` | User/admin security and account-management actions |

Inspect the schema:

```bash
podman exec -it coldchain-postgres psql -U coldchain -d coldchain -c '\dt'
podman exec -it coldchain-postgres psql -U coldchain -d coldchain -c '\d packages'
```

### Optional destructive reset for a disposable development database

**This deletes the `coldchain` database and all its data. Back up first; do not run this against data you need.** Stop the API first.

```bash
podman exec -it coldchain-postgres psql -U coldchain -d postgres \
  -c 'DROP DATABASE IF EXISTS coldchain WITH (FORCE)'
podman exec -it coldchain-postgres psql -U coldchain -d postgres \
  -c 'CREATE DATABASE coldchain OWNER coldchain'
```

Afterward, run the CLI or API again; tables will be created in the empty, existing database. This reset does not remove the Podman volume, but it does delete the named database inside it. A persistent volume is not a backup.

## API documentation

`API_SPEC.md` is the detailed implementation contract for frontend development. It documents authentication/CSRF, roles, all current REST endpoints, request and response shapes, pagination, lifecycle rules, error responses and the MQTT payload contract.

Interactive OpenAPI documentation is also available at `/docs` while the backend is running. If code and this document differ, treat the current `Backend.py` and its generated OpenAPI schema as the implementation truth, then update the document before the frontend is integrated.

## Troubleshooting

### `coldchain-postgres` or `coldchain-mosquitto` container already exists

Start the existing container instead of running `podman run` again:

```bash
podman start coldchain-postgres
podman start coldchain-mosquitto
```

If startup fails, inspect its logs. Verify the existing port mapping and volume before removing/recreating a container.

### PostgreSQL connection refused or authentication failed

Check `podman ps`, `podman logs coldchain-postgres`, the result of `pg_isready`, and `DATABASE_URL`. The database must already exist and credentials must match the container.

### Schema incompatibility error

The database contains a table from an earlier schema. `CREATE TABLE IF NOT EXISTS` is not a migration. Back up useful data and apply a deliberate migration or reset only a disposable database using the procedure above.

### `/health` works but `/readyz` returns 503

This means the process is alive but at least one dependency is not ready. Check logs, database connection/schema initialization, Mosquitto host/port and whether the MQTT consumer connected and successfully subscribed to `coldchain/+/messages`.

### No registration ACK

Confirm MQTT is connected, the device subscribes to its ACK/config topics before publishing, its topic is exactly `coldchain/{device_id}/messages`, the envelope ID matches that topic segment, and the envelope/payload uses schema version 1.

### Telemetry or alerts are missing

The device must register first. Confirm the envelope contains all ten required fields and that no extra fields are present. For telemetry, all three nested objects must contain exactly the specified keys. Alerts require `message_type: "event"` and a supported event type. Check backend logs for the rejected-message reason.

### Login works but a dashboard mutation returns 403

The frontend likely omitted or sent an outdated `X-CSRF-Token`. Get the current token from the login response or `/api/v1/auth/me` and include it on every authenticated state-changing request. Also ensure credentialed CORS requests use the exact configured origin.

### Browser cannot use session cookie across frontend/API origins

Verify `credentials: "include"`, `CORS_ORIGINS`, cookie `SameSite`/`Secure` attributes and whether the frontend/API are same-site. `SameSite=None` requires HTTPS and `COOKIE_SECURE=true`. Avoid cross-site cookie deployment for the MVP if same-origin/reverse-proxy hosting is practical.

### QR code opens on the laptop but not the phone

Replace `PUBLIC_BASE_URL` with a phone-reachable server address and configure the host firewall/network. Do not assume `127.0.0.1` means the laptop when opened on a phone.

### Browser shows 404 at `/`

There is no general dashboard homepage bundled here. Use `/docs`, `/health`, `/readyz`, the `/activate` page, or a package tracking URL. The actual operator dashboard is a separate frontend that consumes the protected API.

## Security and deployment limits

This is a controlled hackathon/MVP backend, not a production-hardened deployment.

- Use individual dashboard accounts. Do not share one admin password across the team.
- Do not expose anonymous Mosquitto to the LAN/internet. Add broker credentials, topic ACLs and TLS before connecting hardware outside loopback-only development.
- `COOKIE_SECURE=false` is only for local HTTP. Use HTTPS and `COOKIE_SECURE=true` outside local development.
- Keep exact `CORS_ORIGINS`; never use `*` with credentialed cookies.
- Invitation and tracking URLs are bearer secrets. Share privately; avoid posting them in source control, screenshots, public issue trackers or chat rooms with broad access. Rotate a tracking token if exposed.
- The built-in login limiter is process-local, not a distributed/rate-limiting service.
- The API does not yet implement a full dashboard, email delivery, a separate receiver identity, device certificate enrollment, or production multi-tenant isolation.
- The API's authenticated device diagnostic response includes the latest raw accepted MQTT envelope. Keep dashboard access restricted.
- Apply real migrations, backups, retention policies, monitoring, structured deployment secrets and a production reverse proxy before production.

For the frontend contract, see [`API_SPEC.md`](API_SPEC.md).
