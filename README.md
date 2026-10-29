# ColdGuardian

**IoT-based cold-chain monitoring and shipment tracking.**

ColdGuardian uses ESP32-based trackers to monitor temperature, shock, and location during the transportation of temperature-sensitive goods. Sensor events are transmitted over MQTT to a Python backend, stored in PostgreSQL, and exposed through a web dashboard and REST API.

## Architecture

```text
  DS18B20 ─┐
  ADXL345 ─┼──> ESP32 Tracker
  NEO-6M  ─┤       │
  OLED/QR ─┘       │ Wi-Fi + MQTT
                   ▼
              Mosquitto Broker
                   │
                   ▼
          Python MQTT Consumer
                   │
                   ▼
              PostgreSQL
                   ▲
                   │
             FastAPI Backend
                   ▲
                   │ HTTP / JSON
                   ▼
            Web Dashboard
```

The ESP32 reads sensor data and evaluates temperature and shock thresholds using the configuration assigned to its package. The backend manages devices, shipments, configuration, telemetry, alerts, users, and tracking.

## Technology Stack

| Component | Technology |
|---|---|
| Tracker firmware | ESP32, Arduino framework, C++ |
| Temperature | DS18B20 |
| Acceleration / shock | ADXL345 |
| Location | NEO-6M GNSS |
| Messaging | MQTT, Mosquitto |
| Backend | Python, FastAPI, Uvicorn |
| MQTT client | Paho MQTT |
| Validation | Pydantic |
| Database | PostgreSQL |
| Database driver | Psycopg 3 |
| Frontend | HTML, CSS, JavaScript |
| Python environment | `uv` |
| Containers | Podman |

## Features

- Device registration and configuration synchronization.
- Package and shipment lifecycle management.
- Per-package temperature and shock thresholds.
- Sensor telemetry and explicit device-generated breach events.
- Alert listing, acknowledgement, and resolution.
- Device status and telemetry history.
- Web dashboard and REST API.
- User authentication, role-based access, and audit records.
- Read-only public package tracking through QR codes.
- Health and readiness endpoints.

See [`API_SPEC.md`](API_SPEC.md) for API contracts and endpoint details.

## Repository Structure

```text
ColdGuardian/
├── frontend/          # Web dashboard
├── main.py            # API, MQTT consumer, and backend logic
├── API_SPEC.md        # API specification
├── pyproject.toml     # Python project dependencies
├── uv.lock            # Locked dependency versions
├── mosquitto.conf     # Local MQTT broker configuration
├── Caddyfile          # Optional reverse-proxy configuration
├── .env.example       # Environment variable template
├── .gitignore
└── README.md
```

## Setup

### 1. Configure environment variables

```bash
cp .env.example .env
```

Edit `.env` with the correct database credentials, MQTT settings, and application URLs. Never commit `.env`.

### 2. Start the infrastructure

If the containers already exist:

```bash
podman start coldchain-postgres
podman start coldchain-mosquitto
```

For a new local PostgreSQL container:

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

For a new local Mosquitto container:

```bash
podman run -d \
  --name coldchain-mosquitto \
  -p 127.0.0.1:1883:1883 \
  -v "$PWD/mosquitto.conf:/mosquitto/config/mosquitto.conf:ro" \
  -v coldchain_mosquitto_data:/mosquitto/data \
  docker.io/eclipse-mosquitto:2
```

Use matching credentials in `.env`. The commands above assume the application and containers can reach the services at the configured addresses. If the ESP32 connects over Wi-Fi, configure the broker and firewall for LAN access and use the host's LAN IP instead of `127.0.0.1`.

### 3. Install dependencies

```bash
uv sync
```

### 4. Run the backend

```bash
uv run --env-file .env python main.py
```

Use the startup output to confirm the API and MQTT client initialized successfully.

### 5. Create the first administrator

```bash
uv run --env-file .env python main.py create-admin --email admin@example.test
```

Follow the prompts to set the password. Replace the example email with the intended administrator account.

## Local URLs

With the default configuration:

- Dashboard/API: `http://127.0.0.1:8000`
- API documentation: `http://127.0.0.1:8000/docs`
- Health check: `http://127.0.0.1:8000/health`
- Readiness check: `http://127.0.0.1:8000/readyz`

These paths assume the current application routes and environment configuration. The dashboard's actual path may differ.

**For QR tracking:** configure `PUBLIC_BASE_URL` to an address reachable by the phone scanning the QR code. A URL containing `127.0.0.1` refers to the scanning device itself, not the server.

## MQTT Message Flow

The tracker communicates with the backend through Mosquitto.

| Topic | Purpose |
|---|---|
| `coldchain/{device_id}/messages` | Device registration and telemetry/event messages |
| `coldchain/{device_id}/registration/ack` | Registration acknowledgement |
| `coldchain/{device_id}/config` | Assigned configuration |
| `coldchain/{device_id}/config` | Configuration synchronization; verify retained-message behavior against the implementation |

The backend publishes device configuration, including assignment details, package thresholds, reporting interval, and monitoring status. The tracker must apply the configuration received for its registered device.

Messages use QoS 1 where configured. Device message identifiers such as `boot_id` and `message_seq` support duplicate detection.

## Important Design Rules

- Package configuration is the source of truth for temperature and shock thresholds.
- The ESP32 evaluates sensor readings and emits explicit breach events.
- The backend persists and exposes telemetry and device-generated events; it must not silently invent breach events from routine telemetry.
- Monitoring must follow the package and shipment lifecycle. A delivered shipment can result in monitoring being disabled.
- Each tracker must use its own registered `device_id` and assigned configuration.
- A tracker connected to Wi-Fi must use a broker address reachable over the network.

## Security Notes

- Keep secrets in `.env`; commit `.env.example` without real credentials.
- Use authenticated sessions and the required CSRF protections for applicable state-changing requests.
- Keep public QR tracking read-only.
- Do not expose an anonymous MQTT broker to an untrusted network.
- Configure HTTPS, MQTT authentication/ACLs, and TLS before production deployment.

## Troubleshooting

Check container status:

```bash
podman ps -a
podman logs coldchain-postgres
podman logs coldchain-mosquitto
```

Inspect device messages:

```bash
podman exec coldchain-mosquitto mosquitto_sub \
  -h 127.0.0.1 -p 1883 \
  -t 'coldchain/tracker-30-76-F5-F4-0C-F0/messages' -v
```

Inspect device configuration:

```bash
podman exec coldchain-mosquitto mosquitto_sub \
  -h 127.0.0.1 -p 1883 \
  -t 'coldchain/tracker-30-76-F5-F4-0C-F0/config' -v
```

Replace the example device ID with the registered tracker ID. Check the API health/readiness endpoints and backend logs if startup or database connectivity fails.

## Development

Keep `API_SPEC.md`, `pyproject.toml`, and `uv.lock` synchronized with changes to the API and dependencies. Run the project's tests before committing changes.

## License

No license is specified here. Add a `LICENSE` file before distributing the project publicly.
