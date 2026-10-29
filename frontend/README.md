# ColdGuardian frontend — operational workflows

Plain HTML, CSS and JavaScript. No frontend build step is required.

## Files

- `index.html` — dashboard shell, login form and operational views
- `main.css` — yellow/cream responsive styling
- `script.js` — session login, readiness, live overview, charts and selected tracker telemetry
- `operations.js` — package, shipment, device and global-alert workflows

## Run locally

Run the backend in WSL from `/mnt/d/ColdGuardian`:

```bash
uv run --env-file .env python main.py
```

Serve this frontend directory from WSL in another terminal:

```bash
cd /mnt/d/ColdGuardian/frontend
python3 -m http.server 5173 --bind 0.0.0.0
```

Open <http://localhost:5173/>. The backend should allow the frontend origin in `CORS_ORIGINS`, for example `http://localhost:5173,http://127.0.0.1:5173`.

The API base defaults to the same hostname used by the frontend at port 8000. To override it for a different setup, define `window.COLDGUARDIAN_API_BASE_URL` before `script.js` in `index.html`.

## Workflows included

- Login/logout using the backend's HttpOnly session cookie and in-memory CSRF token.
- Overview with inventory summary, live telemetry, temperature and acceleration charts, GPS link, package assignment/config status, and recent alert handling.
- Create package with SKU/name/quantity/unit/batch/expiry, temperature minimum/maximum, shock threshold, and optional tracker assignment.
- Edit packed packages, including thresholds and tracker assignment. Backend rules remain authoritative; packages that are not `packed` cannot be edited.
- Read/recover the current tracking URL as an administrator, rotate a lost/legacy token with confirmation, open the public tracking page, and open the backend PNG endpoint for printable QR labels. The ESP32 separately receives the URL in retained MQTT config and generates its OLED QR locally.
- Create/edit packed shipments, select unattached packed packages, inspect shipment events, dispatch shipments, and mark in-transit packages delivered.
- Inspect registered devices and their diagnostic JSON.
- Filter all alerts and acknowledge/resolve alert lifecycle records.

The UI uses the API contract's exact field names and never invents a REST telemetry-ingestion route. The ESP32 continues to send telemetry and explicit events over MQTT.

## Not included yet

User invitations/role changes, password change, session revocation and audit-log views are not part of this operations UI. Those API endpoints remain available in the backend.
