# ColdGuardian — Developer Handbook

> Internal reference for the ColdGuardian development team. Contains project structure, commands, service management, MQTT debugging, and troubleshooting.
>
> **Project root:** `/mnt/d/ColdGuardian` inside WSL  
> **Backend entry point:** `main.py`  
> **Python dependency manager:** `uv`  
> **Containers:** Podman

---

## 1. Quick Reference

### Start the project

Open a WSL terminal in the project directory:

```bash
cd /mnt/d/ColdGuardian
```

Start the existing infrastructure containers:

```bash
podman start coldchain-postgres
podman start coldchain-mosquitto
```

Activate the Python environment by installing/synchronizing dependencies:

```bash
uv sync
```

Start the backend:

```bash
uv run --env-file .env python main.py
```

Keep this terminal running while using the application.

### Useful URLs

Assuming the default local configuration:

| Purpose | URL |
|---|---|
| Application/API | http://127.0.0.1:8000 |
| Interactive API docs | http://127.0.0.1:8000/docs |
| Health check | http://127.0.0.1:8000/health |
| Readiness check | http://127.0.0.1:8000/readyz |

These URLs depend on the actual routes and environment configuration in the current code.

### Stop the backend

Press `Ctrl+C` in the terminal running `main.py`.

Stop the infrastructure containers when needed:

```bash
podman stop coldchain-mosquitto
podman stop coldchain-postgres
```

Stopping containers does not delete their persistent volumes.

---

## 2. Project File Reference

Current root directory:

```text
ColdGuardian/
├── .git/
├── .env
├── .env.example
├── API_SPEC.md
├── Caddyfile
├── frontend/
├── main.py
├── main.py.before-alert-count-detail-fix
├── main.py.before-alert-count-fix
├── mosquitto.conf
├── PKG-2026-0001_tracking_qr.png
├── publish_test.py
├── __pycache__/
├── pyproject.toml
├── README.md
├── .test-cookies/
├── test.py
├── uv.lock
└── .venv/
```

### `main.py`

Main Python application entry point.

It contains the backend implementation, including API functionality and MQTT-related processing. It is the first file to inspect when debugging backend behavior.

Run it with:

```bash
uv run --env-file .env python main.py
```

Inspect available command-line options before assuming additional commands exist:

```bash
uv run --env-file .env python main.py --help
```

The help command may not be implemented; check the actual startup/argument parsing code if it fails.

### `API_SPEC.md`

API contract and endpoint documentation.

Use it to check:
- Endpoint paths and HTTP methods.
- Request and response formats.
- Authentication requirements.
- Device, package, shipment, and alert operations.
- Expected behavior before changing API code.

When changing API behavior, update this document if the contract changes.

### `frontend/`

Web dashboard source files.

Inspect the directory to identify HTML, CSS, JavaScript, and any assets:

```bash
find frontend -maxdepth 3 -type f -print
```

Search for API calls and backend URLs:

```bash
grep -RInE 'fetch\(|/api/v1|localhost|127\.0\.0\.1' frontend
```

Do not assume the frontend uses a Node build process unless its files and configuration confirm that.

### `.env`

Local environment configuration.

Contains database connection details, MQTT settings, URLs, session configuration, and other runtime settings.

**Never commit `.env` or share its contents publicly.**

Inspect variable names without displaying values:

```bash
cut -d= -f1 .env
```

Edit it locally when configuring the application.

### `.env.example`

Template for environment configuration.

It should contain placeholder values, not actual secrets. Keep it committed so teammates can create their own `.env`.

Initial setup:

```bash
cp .env.example .env
```

Check that the variable names in `.env.example` match those expected by `main.py`.

### `pyproject.toml`

Defines Python project metadata and dependencies used by `uv`.

When adding a dependency, use `uv add` rather than manually guessing dependency versions.

Example:

```bash
uv add fastapi
```

Only add a dependency if the project actually needs it.

### `uv.lock`

Locks resolved dependency versions.

Keep this file in Git. It helps teammates install consistent dependency versions.

Synchronize the environment:

```bash
uv sync
```

### `mosquitto.conf`

Configuration for the Mosquitto MQTT broker.

Inspect it before changing broker behavior:

```bash
cat mosquitto.conf
```

Changes may require restarting the broker:

```bash
podman restart coldchain-mosquitto
```

### `Caddyfile`

Configuration for Caddy, an optional reverse proxy.

It may be useful for HTTP routing or HTTPS deployment. It is not necessarily used during local development.

Inspect it:

```bash
cat Caddyfile
```

Do not assume Caddy is running unless a service or container has been configured for it.

### `publish_test.py`

Likely a test utility for publishing messages or exercising MQTT-related functionality.

Inspect its behavior before running it:

```bash
less publish_test.py
```

Look for arguments and MQTT topics:

```bash
grep -nE 'argparse|sys\.argv|publish|topic|host|port' publish_test.py
```

Do not run it against a shared broker without understanding what messages it publishes.

### `test.py`

Test or diagnostic script.

Inspect it before running:

```bash
less test.py
```

Check whether it makes network requests, changes database records, or requires a running backend.

If the project uses `pytest`, the usual command is:

```bash
uv run pytest
```

But first verify that `pytest` is installed and that the tests are compatible with its discovery rules. A file named `test.py` is not guaranteed to be a complete automated test suite.

### `main.py.before-alert-count-detail-fix`

### `main.py.before-alert-count-fix`

Older backend backup snapshots.

These are not required for normal application startup. They may be deleted after verifying that their contents are no longer needed.

Compare a backup against the current version before removal:

```bash
diff -u main.py.before-alert-count-fix main.py | less
```

### `PKG-2026-0001_tracking_qr.png`

Generated QR image for a package tracking link.

Keep it if the team needs it for the demonstration. Otherwise, it can be regenerated if the application's QR-generation functionality supports that.

Important: a QR URL containing `127.0.0.1` works only on the device hosting the application. A phone needs a URL pointing to a reachable host.

### `.venv/`

Local Python virtual environment created and managed by `uv`.

Keep it on your development machine. Do not commit it.

Recreate it if necessary:

```bash
uv sync
```

### `__pycache__/`

Generated Python bytecode cache. Safe to delete; Python recreates it when needed.

### `.test-cookies/`

Likely local authentication/session test artifacts. Keep it if the test scripts depend on it, but do not commit session cookies.

### `.git/`

Git repository metadata and history.

**Never delete this directory to clean the project.**

---

## 3. Initial Setup

### Step 1: Check tools

```bash
python --version
uv --version
podman --version
git --version
```

### Step 2: Configure environment

```bash
cd /mnt/d/ColdGuardian
cp .env.example .env
```

If `.env` already exists, do not overwrite it blindly. Edit the existing file instead.

Check that the database and MQTT values match the services you are running.

### Step 3: Start PostgreSQL

If the container already exists:

```bash
podman start coldchain-postgres
```

For a new development database container:

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

Replace the example password and use the same credentials in `DATABASE_URL`.

The exact PostgreSQL volume mount path can vary by image version. Verify the image's documented data directory before changing a working container.

Check status:

```bash
podman ps -a
podman logs coldchain-postgres
```

Check PostgreSQL readiness inside the container:

```bash
podman exec coldchain-postgres pg_isready
```

### Step 4: Start Mosquitto

If the container already exists:

```bash
podman start coldchain-mosquitto
```

For a new local broker container:

```bash
podman run -d \
  --name coldchain-mosquitto \
  -p 127.0.0.1:1883:1883 \
  -v "$PWD/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
  -v coldchain_mosquitto_data:/mosquitto/data \
  docker.io/eclipse-mosquitto:2
```

Check broker logs:

```bash
podman logs -f coldchain-mosquitto
```

The command binds MQTT to the host's loopback interface. An ESP32 connected over Wi-Fi cannot reach the laptop through `127.0.0.1`; it needs the laptop's LAN IP and a broker configuration that permits LAN connections.

Do not expose an unauthenticated broker to an untrusted network.

### Step 5: Install Python dependencies

```bash
uv sync
```

### Step 6: Run the backend

```bash
uv run --env-file .env python main.py
```

Check the terminal output for database connection errors, MQTT connection errors, and application startup failures.

---

## 4. Administrator Setup

If the application supports the documented `create-admin` command, create the first administrator with:

```bash
uv run --env-file .env python main.py create-admin --email admin@example.test
```

Use the actual administrator email and follow the password prompts.

If this command is rejected, inspect the command-line handling in `main.py`. Do not create database rows manually without understanding the schema and password hashing logic.

The application uses session-based authentication according to the current project design. Do not assume a bearer JWT is required for dashboard requests.

---

## 5. Podman Commands

### List all containers

```bash
podman ps -a
```

### List running containers

```bash
podman ps
```

### Start containers

```bash
podman start coldchain-postgres
podman start coldchain-mosquitto
```

### Stop containers

```bash
podman stop coldchain-mosquitto
podman stop coldchain-postgres
```

### Restart a container

```bash
podman restart coldchain-mosquitto
```

### View logs

```bash
podman logs coldchain-postgres
podman logs coldchain-mosquitto
```

Follow logs continuously:

```bash
podman logs -f coldchain-mosquitto
```

### Inspect container configuration

```bash
podman inspect coldchain-postgres
podman inspect coldchain-mosquitto
```

### Check storage

```bash
podman volume ls
```

**Warning:** removing a container and removing its persistent volume are different operations. Do not remove the PostgreSQL volume unless you intentionally want to delete the stored database data.

---

## 6. MQTT Debugging

MQTT is the messaging layer between the tracker and backend.

The ESP32 connects over Wi-Fi to the broker. It publishes messages to topics and subscribes to topics where the backend publishes configuration or acknowledgements.

### Find the tracker ID

Use the actual registered device ID from the dashboard or database.

Example device ID:

```text
tracker-30-76-F5-F4-0C-F0
```

Replace this example with the current tracker ID when testing.

### Subscribe to device messages

```bash
podman exec coldchain-mosquitto mosquitto_sub \
  -h 127.0.0.1 -p 1883 \
  -t 'coldchain/tracker-30-76-F5-F4-0C-F0/messages' -v
```

This can show device registration, telemetry, or explicit event messages, depending on the firmware's message format.

### Subscribe to configuration

```bash
podman exec coldchain-mosquitto mosquitto_sub \
  -h 127.0.0.1 -p 1883 \
  -t 'coldchain/tracker-30-76-F5-F4-0C-F0/config' -v
```

This is useful for verifying that the device receives its assigned package, shipment, thresholds, reporting interval, and monitoring state.

### Subscribe to registration acknowledgements

```bash
podman exec coldchain-mosquitto mosquitto_sub \
  -h 127.0.0.1 -p 1883 \
  -t 'coldchain/tracker-30-76-F5-F4-0C-F0/registration/ack' -v
```

Verify this topic against the current backend implementation before relying on it.

### Subscribe to all device message topics

```bash
podman exec coldchain-mosquitto mosquitto_sub \
  -h 127.0.0.1 -p 1883 \
  -t 'coldchain/+/messages' -v
```

This wildcard subscribes to matching device message topics. It does not subscribe to every topic under `coldchain/`.

### Publish a test message

Do not publish an arbitrary JSON payload to a production or shared broker. The backend may reject malformed messages or alter device state.

Before testing, inspect the message schema and publishing code in `main.py` and `publish_test.py`.

---

## 7. Understanding the Data Flow

### A. Tracker registration

1. ESP32 connects to Wi-Fi.
2. ESP32 connects to the configured MQTT broker.
3. ESP32 publishes its registration message.
4. Backend receives the message and validates the device identity and payload.
5. Backend registers or recognizes the device.
6. Backend sends a registration acknowledgement and/or device configuration.
7. ESP32 applies the configuration it receives.

The exact acknowledgement behavior depends on the current implementation.

### B. Configuration synchronization

The backend sends configuration associated with the registered tracker.

The configuration may include:
- Device identity and configuration version.
- Assigned package and shipment.
- Package status and shipment status.
- Product details.
- Temperature minimum and maximum.
- Shock threshold.
- Reporting interval.
- Whether monitoring is enabled.
- Tracking URL.

The ESP32 should use the configuration assigned to its own device ID.

### C. Telemetry and events

1. ESP32 reads the temperature sensor.
2. ESP32 reads the accelerometer.
3. ESP32 obtains GNSS data when a fix is available.
4. Firmware compares readings against its assigned thresholds.
5. Firmware publishes telemetry and explicit breach events.
6. Backend validates and stores accepted messages.
7. Dashboard and API expose the recorded state.

**Design rule:** routine telemetry and breach events are distinct. The backend should not silently invent a temperature or shock breach if the design expects the ESP32 to evaluate thresholds and publish explicit events.

### D. Shipment lifecycle

A package and its shipment have lifecycle states.

Examples include:

```text
packed -> in_transit -> delivered
```

Shipment states may also include `partially_delivered`, depending on the implementation.

Monitoring can be disabled when the shipment or package is delivered. If the configuration reports `monitoring.enabled: false`, check the assignment and lifecycle state before debugging MQTT connectivity.

---

## 8. Troubleshooting the Current Tracker

Example tracker:

```text
tracker-30-76-F5-F4-0C-F0
```

Example package:

```text
PKG-2026-0001
```

Example shipment:

```text
SHP-2026-0001
```

### Problem: configuration says monitoring is disabled

Inspect the configuration topic:

```bash
podman exec coldchain-mosquitto mosquitto_sub \
  -h 127.0.0.1 -p 1883 \
  -t 'coldchain/tracker-30-76-F5-F4-0C-F0/config' -v
```

Check:
- Is the tracker assigned?
- What is the package status?
- What is the shipment status?
- Is monitoring enabled?
- Are the configured thresholds correct?
- Is the configuration version changing after dashboard updates?

If both the package and shipment are marked `delivered`, the backend may correctly disable monitoring.

For a new monitoring test, create a fresh package/shipment, assign the tracker, and dispatch it through the intended application workflow. Do not edit database statuses manually unless you understand all related lifecycle and audit logic.

### Problem: MQTT is connected, but no alerts appear

Check in this order:

1. Is the tracker receiving configuration?
2. Is monitoring enabled?
3. Is the sensor reading valid?
4. Is the measured value outside the configured threshold?
5. Does firmware publish an explicit breach event?
6. Is the event sent to the correct topic?
7. Does the backend accept the event schema?
8. Is the alert visible in the API/dashboard?

A connected MQTT client does not prove that the firmware is generating events.

### Problem: temperature reading is too high

A reading near `30.6°C` is outside a `2°C–8°C` operating range.

Check:
- DS18B20 wiring and power.
- Sensor initialization and error handling.
- Whether the sensor is physically measuring ambient temperature.
- Whether the tracker has received the correct package configuration.
- Whether monitoring is enabled.

Do not create a real medicine shipment based on an uncalibrated prototype sensor. For a demonstration, clearly distinguish simulated data from measured data.

### Problem: GNSS has no fix

The NEO-6M may take time to acquire satellites and generally performs poorly indoors.

Check:
- Antenna connection and placement.
- UART wiring and baud rate.
- Power stability.
- Firmware's GNSS status output.
- Whether the test is being conducted indoors.

A GNSS receiver without a valid fix should not be treated as having a reliable current position.

### Problem: the QR link does not open on a phone

If the URL contains:

```text
http://127.0.0.1:8000/track/...
```

the phone tries to connect to itself, not the laptop.

Configure the public tracking base URL to use an address reachable from the phone, such as the laptop's LAN IP for a local demo. Ensure the server is listening on a suitable interface and firewall rules permit access.

Do not use an internal development address for a public deployment.

---

## 9. PostgreSQL Inspection

### Open a PostgreSQL shell

```bash
podman exec -it coldchain-postgres \
  psql -U coldchain -d coldchain
```

The username and database name must match the container configuration.

### List tables

Inside `psql`:

```sql
\dt
```

### Describe a table

```sql
\d table_name
```

Replace `table_name` with an actual table name returned by `\dt`.

### List databases

```sql
\l
```

### Exit

```sql
\q
```

Do not guess table names or insert/update records directly without checking the current schema and application constraints.

---

## 10. Python and Dependency Commands

### Install/synchronize dependencies

```bash
uv sync
```

### Add a dependency

```bash
uv add package-name
```

### Add a development-only dependency

```bash
uv add --dev package-name
```

### Remove a dependency

```bash
uv remove package-name
```

### Run a Python script

```bash
uv run python script.py
```

For the backend with environment variables:

```bash
uv run --env-file .env python main.py
```

### List installed packages

```bash
uv pip list
```

### Run tests

If `pytest` is installed and the project tests support it:

```bash
uv run pytest
```

Run a specific test file if it is compatible with pytest:

```bash
uv run pytest test.py -v
```

If the file is a standalone script rather than a pytest suite, inspect its instructions and use its intended command instead.

---

## 11. Git Commands

### Check working tree

```bash
git status --short
```

### Show tracked files

```bash
git ls-files
```

### Inspect changes

```bash
git diff
```

### Stage selected files

```bash
git add main.py README.md API_SPEC.md
```

Stage other files only after reviewing them.

### Commit

```bash
git commit -m "Describe the change"
```

### Push

```bash
git push
```

If Git asks for credentials, use the configured credential manager or the appropriate remote authentication method. Do not put passwords or access tokens directly in repository files.

### Check whether a file is ignored

```bash
git check-ignore -v .env
git check-ignore -v .venv/
```

A file already tracked by Git remains tracked even after adding it to `.gitignore`.

---

## 12. Recommended `.gitignore`

Use the following in the project-root `.gitignore`:

```gitignore
# Secrets and local environment
.env
.env.*
!.env.example
!.env.template

# Python
__pycache__/
*.py[cod]
*.pyo
*.pyd
.venv/
venv/
env/

# Test and tooling caches
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
.coverage.*
htmlcov/

# Local cookies/session test state
.test-cookies/
.cookies
cookies.txt
*.cookie

# Local databases and logs
*.sqlite
*.sqlite3
*.db
*.log

# Editor and operating-system files
.DS_Store
Thumbs.db
.vscode/
.idea/
*.swp
*.swo
*~

# Optional frontend build artifacts
node_modules/
dist/

# Optional local backup files
*.bak
*.orig
```

Keep `pyproject.toml`, `uv.lock`, `API_SPEC.md`, `frontend/`, and the source files tracked.

If the generated QR image should not be versioned, add:

```gitignore
*_tracking_qr.png
```

Only add this rule if you do not want QR images committed. A QR image needed for the demo can remain tracked.

---

## 13. Safe Cleanup

Inspect the repository first:

```bash
git status --short
git ls-files
```

Remove Python cache files:

```bash
rm -rf __pycache__
```

Inspect old backups before deleting them:

```bash
diff -u main.py.before-alert-count-fix main.py | less
diff -u main.py.before-alert-count-detail-fix main.py | less
```

If you have confirmed that the backup files are no longer needed:

```bash
rm -i main.py.before-alert-count-fix \
      main.py.before-alert-count-detail-fix
```

Do not delete:
- `.git/`
- `main.py`
- `frontend/`
- `.env.example`
- `pyproject.toml`
- `uv.lock`
- `API_SPEC.md`
- `mosquitto.conf`

Do not remove `.venv/` merely to clean Git; ignoring it is sufficient.

Never remove PostgreSQL persistent volumes as a routine cleanup step. They may contain all application data.

---

## 14. Before a Demo

Use this checklist before presenting the project:

- [ ] `.env` contains valid local configuration.
- [ ] PostgreSQL container is running.
- [ ] Mosquitto container is running.
- [ ] Backend starts without errors.
- [ ] Dashboard loads.
- [ ] Administrator login works.
- [ ] Tracker connects to Wi-Fi and MQTT.
- [ ] Tracker has the expected registered device ID.
- [ ] Device receives its latest configuration.
- [ ] Package and shipment are in an appropriate active state.
- [ ] Monitoring is enabled for the test shipment.
- [ ] Temperature and accelerometer readings are valid.
- [ ] Breach events are published when test thresholds are crossed.
- [ ] Backend records the events and dashboard displays the alerts.
- [ ] QR tracking opens from another device.
- [ ] No credentials or session cookies are exposed in Git.

---

## 15. Rules for Future Changes

1. Inspect existing code before introducing new files, columns, JSON fields, or endpoints.
2. Keep the API implementation and `API_SPEC.md` synchronized.
3. Keep `.env.example` synchronized with the environment variables the backend actually reads.
4. Keep `uv.lock` updated when dependencies change.
5. Do not duplicate configuration values in firmware and backend when the package configuration is intended to be authoritative.
6. Do not treat routine telemetry as an alert unless the system's explicit event model requires it.
7. Use the registered `device_id` consistently across registration, configuration, telemetry, and events.
8. Test MQTT messages and lifecycle transitions before changing database records manually.
9. Never commit `.env`, private credentials, or session cookies.
10. Prefer small, reviewed changes over replacing the whole backend implementation.

---

## 16. Useful One-Line Diagnostics

```bash
# Project files
find . -maxdepth 2 -type f -not -path './.git/*' -print

# Container status
podman ps -a

# Backend and broker logs
podman logs --tail 100 coldchain-mosquitto
podman logs --tail 100 coldchain-postgres

# Python dependencies
uv sync

# Git state
git status --short

# Environment variable names, not values
cut -d= -f1 .env

# Check API availability
curl -i http://127.0.0.1:8000/health

# Check readiness
curl -i http://127.0.0.1:8000/readyz
```

**End of internal developer handbook.**
