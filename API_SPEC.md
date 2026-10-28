# ColdChain API and MQTT Contract

**Version:** 1.1.0 (matches the FastAPI application version in `Backend.py`)  
**Audience:** frontend implementers, backend/firmware developers, simulator authors  
**Base URL for local development:** `http://127.0.0.1:8000`  
**Content type:** JSON request/response bodies use `application/json`, except the HTML routes noted below.

This is the implementation contract for the current `Backend.py`. The frontend should not invent fields that are not listed here. The API is session-cookie authenticated; it does not use bearer JWTs. There is no public self-registration route. The public QR tracking endpoints are read-only.

## 1. Core rules

1. PostgreSQL is authoritative for packages, thresholds, assignment, shipment lifecycle and alert lifecycle.
2. A package owns `min_temperature_c`, `max_temperature_c` and `shock_threshold_g`. Do not save an authoritative second copy of these values on the device.
3. MQTT delivers register, telemetry and device-reported event messages. There is no REST telemetry-ingestion endpoint.
4. The ESP32 compares valid readings against the current package config locally. The backend does not infer temperature/shock alerts from routine telemetry.
5. Public QR tokens only grant read-only package tracking. Scanning a QR never dispatches or delivers a package.
6. Only an Admin can manage users/invitations or rotate a tracking token. Admin and Operator can perform the normal shipment/package/alert operations.
7. Protected mutations require both a valid session cookie and `X-CSRF-Token`.
8. A successful business mutation commits to PostgreSQL before the backend publishes refreshed device config to MQTT.

## 2. Hosts, route visibility and authentication

### Public endpoints

These endpoints do not require a logged-in session:

- `GET /health`
- `GET /readyz`
- `POST /api/v1/auth/login`
- `POST /api/v1/auth/activate` (requires a valid one-time invite/reset token)
- `GET /activate` (built-in activation/password-reset HTML page)
- `GET /api/v1/public/tracking/{token}`
- `GET /track/{token}` (built-in read-only tracking HTML page)

### Authenticated endpoints

All routes listed under `/api/v1` below, other than the explicit public endpoints above, require an active logged-in session. This includes reads. Admin-only routes also require the `admin` role; package/shipment/alert mutations require `admin` or `operator`.

### Roles

| Role | Permissions |
|---|---|
| `admin` | All operator permissions; list/create/manage users and invitations; revoke user sessions; inspect audit log; rotate package tracking tokens |
| `operator` | View dashboard data/devices/telemetry/alerts; create/edit packed packages; create/edit packed shipments; dispatch; confirm delivery; acknowledge/resolve alerts; view inventory summary |
| public | Health/readiness, login, valid invitation activation, public sanitized tracking only |

There is no public registration endpoint. Bootstrap the first admin via `python Backend.py create-admin --email <email>`. An admin then creates one-time activation invitations.

## 3. Browser session and CSRF contract

### Login

`POST /api/v1/auth/login` sets an HttpOnly cookie (default name `coldchain_session`) and returns a `csrf_token`. The cookie contains an opaque random session secret. The database stores only the SHA-256 hash of that secret. The server looks up and validates the session on each protected request.

Request:

```http
POST /api/v1/auth/login
Content-Type: application/json

{"email":"admin@example.test","password":"<password>"}
```

Response (`200`):

```json
{
  "user": {
    "user_id": "<opaque-user-id>",
    "email": "admin@example.test",
    "role": "admin",
    "is_active": true
  },
  "csrf_token": "<session-bound-csrf-token>"
}
```

The `Set-Cookie` response header sets the session cookie. The cookie is `HttpOnly`, uses the configured `SameSite` value, and uses the configured `Secure` flag. Defaults are seven-day absolute expiry and 12-hour idle timeout, both enforced server-side.

### Frontend requirements

For JavaScript `fetch`:

```js
const response = await fetch(`${API_BASE}/api/v1/auth/login`, {
  method: "POST",
  credentials: "include",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ email, password }),
});
const result = await response.json();
```

- Always set `credentials: "include"` if frontend and API are on distinct origins, so browser cookies are sent and accepted.
- Never read the session cookie in JavaScript. It is `HttpOnly`.
- Keep the `csrf_token` returned by login in application memory (for example, app-level state/context); do not persist it in `localStorage`.
- After page reload, call `GET /api/v1/auth/me` to validate the existing session and retrieve a current `csrf_token`.
- Add `X-CSRF-Token: <csrf_token>` to every authenticated `POST`, `PATCH` and `DELETE` request. This includes logout, invitation/user management, package/shipment mutation and alert acknowledge/resolve operations.
- `GET`, `HEAD` and `OPTIONS` requests do not require a CSRF header.
- Login and activation are public, so they do not require a pre-existing CSRF token.
- On `401`, clear frontend user state and show/login redirect. On `403`, show an authorization or CSRF failure; do not assume the operation succeeded.
- Set `CORS_ORIGINS` to exact origins. With cookie credentials, `Access-Control-Allow-Origin: *` is not supported/appropriate.

CSRF tokens are deterministically derived from the session secret on the server and are returned by login and `/auth/me`. If the account is disabled, the session is revoked/expired, or the idle/absolute timeout elapses, protected requests return `401`.

## 4. Conventions and data types

### Timestamp fields

Timestamps are timezone-aware. Send RFC 3339/ISO 8601 strings with an offset, preferably UTC with `Z`, e.g. `2026-10-28T01:15:00Z`. Do not send naive local timestamps. The server creates received/created/dispatch/delivery/ack/resolution timestamps.

### Pagination

List endpoints use zero-based `offset`. Unless stated otherwise, paginated responses have this shape:

```json
{"items": [], "limit": 50, "offset": 0, "total": 0}
```

`total` is the number of matching records before pagination. Query parameters are validated; invalid values produce `422`.

| Endpoint group | Default limit | Maximum limit | Pagination |
|---|---:|---:|---|
| Packages list | 50 | 100 | `limit`, `offset`, `total` |
| Shipments list | 50 | 100 | `limit`, `offset`, `total` |
| Devices list | 50 | 100 | `limit`, `offset`, `total` |
| Device telemetry | 100 | 1000 | `limit`, `offset`, `total` |
| Package telemetry | 100 | 500 | `limit`, `before`, `next_before`; no `total` |
| Alerts list | 50 | 100 | `limit`, `offset`, `total` |
| Users list | 50 | 100 | `limit`, `offset`, `total` |
| Invitations list | 50 | 100 | `limit`, `offset`, `total` |
| Audit log | 100 | 500 | `limit`, `offset`, `total` |

### Error formats

Domain errors raised by the backend use:

```json
{
  "detail": {
    "code": "PACKAGE_NOT_FOUND",
    "message": "Package not found."
  }
}
```

Pydantic request validation errors use FastAPI's usual `422` format, whose `detail` is an array of field errors. Frontend code should handle both shapes safely:

```js
function errorMessage(data) {
  if (typeof data?.detail === "string") return data.detail;
  if (data?.detail?.message) return data.detail.message;
  if (Array.isArray(data?.detail)) {
    return data.detail.map((item) => `${(item.loc || []).join(".")}: ${item.msg}`).join("; ");
  }
  return "Request failed.";
}
```

Important status codes:

| Code | Meaning |
|---:|---|
| `200` | Successful read/update/action |
| `201` | Resource created |
| `400` | Invalid domain request, invalid current password, or invalid invitation |
| `401` | Missing/invalid/expired session or incorrect login credentials |
| `403` | Role permission failure or invalid/missing CSRF token |
| `404` | Resource or tracking token not found |
| `409` | State conflict, duplicate account, unavailable package/device, or invalid lifecycle transition |
| `410` | Retired endpoint, e.g. old device settings PATCH |
| `422` | REST model/field validation failure |
| `429` | Login rate limit reached |
| `503` | Service is live but not ready (PostgreSQL/schema/MQTT consumer) |

Internal exception text/credentials must not be displayed to frontend users. Do not assume every unknown server error follows the domain-error shape.

## 5. Health and readiness

### `GET /health` — public

Liveness only. Returns `200`:

```json
{"status":"ok"}
```

It does not guarantee PostgreSQL or MQTT is ready.

### `GET /readyz` — public

Returns `200` only when PostgreSQL is reachable, schema initialization passed, and the MQTT client is connected and subscribed. Otherwise returns `503`.

```json
{
  "status": "ok",
  "postgresql": "ready",
  "schema": "ready",
  "mqtt_consumer": "ready"
}
```

Each component value may be `not_ready`.

## 6. Authentication and account management

### `POST /api/v1/auth/login` — public

Request fields:

| Field | Type | Constraints |
|---|---|---|
| `email` | string | Valid email form; whitespace trimmed and lowercase stored/queried |
| `password` | string | 1–128 characters at login time |

Successful response returns `user` and `csrf_token`, and sets the session cookie. Invalid credentials produce `401` with code `INVALID_CREDENTIALS`; rate limit produces `429` with code `LOGIN_RATE_LIMITED`.

### `POST /api/v1/auth/activate` — public, valid one-time token required

Used for initial invitation activation and admin-created password reset. It does not log the user in.

Request:

```json
{"token":"<one-time-token-from-invitation-link>","password":"<new-password-at-least-12-characters>"}
```

- `token`: string, length 32–256.
- `password`: string, length 12–128.

Activation response (`200`):

```json
{"status":"activated","email":"operator@example.test","message":"Account activated. You can now log in."}
```

Password reset response (`200`):

```json
{"status":"password_reset","email":"operator@example.test","message":"Password reset. Log in with the new password."}
```

Possible errors include `400 INVITATION_INVALID`, `409 ACCOUNT_ALREADY_EXISTS`, and `404 USER_NOT_FOUND` for a reset invitation whose account no longer exists. An invite/reset token is single use and expires.

### `GET /activate` — public HTML

A small built-in page that reads the `token` from the URL fragment (`#token=...`), asks the user to set/confirm a password and calls `POST /api/v1/auth/activate`. A separate frontend can replace this page; set `ACTIVATION_BASE_URL` to the URL that should receive invitation links.

### `GET /api/v1/auth/me` — authenticated

Returns the current user and CSRF token:

```json
{
  "user": {
    "user_id":"<id>",
    "email":"operator@example.test",
    "role":"operator",
    "is_active":true,
    "created_at":"2026-10-28T01:00:00Z",
    "last_login_at":"2026-10-28T01:10:00Z"
  },
  "csrf_token":"<token>"
}
```

`last_login_at` can be `null` for an account that has not logged in.

### `POST /api/v1/auth/logout` — authenticated + CSRF

Revokes the current session, clears the cookie and records a `logout` audit event.

Response: `{"status":"logged_out"}`.

### `POST /api/v1/auth/password/change` — authenticated + CSRF

Request:

```json
{"current_password":"<current-password>","new_password":"<new-password-at-least-12-characters>"}
```

The new password must be 12–128 characters and must differ from the current password. The endpoint revokes the user's other sessions while preserving the current session, and writes a password-change audit event.

Response: `{"status":"password_changed","other_sessions_revoked":true}`.

Errors: `400 CURRENT_PASSWORD_INVALID`, `422 PASSWORD_UNCHANGED`.

### `GET /api/v1/auth/users` — Admin only

Paginated user list. Query: `limit` default 50/max 100; `offset` default 0/min 0.

Each item contains:

- `user_id`, `email`, `role`, `is_active`
- `created_at`, `updated_at`, `last_login_at`
- `active_session_count` (sessions that are not revoked/expired and are within the idle timeout)

Passwords/password hashes and raw session secrets are never returned.

### `POST /api/v1/auth/users/invitations` — Admin only + CSRF, `201`

Creates an activation invitation; it does **not** create the account until activation succeeds.

Request:

```json
{"email":"operator@example.test","role":"operator"}
```

- `email`: valid email, normalized to lowercase.
- `role`: `admin` or `operator`; defaults to `operator`. Only an admin can issue an admin invitation.

Response contains `invitation_id`, normalized `email`, `role`, `purpose: "activate"`, `expires_at`, and `activation_url`:

```json
{
  "invitation_id":"<uuid>",
  "email":"operator@example.test",
  "role":"operator",
  "purpose":"activate",
  "expires_at":"2026-10-29T01:00:00Z",
  "activation_url":"http://127.0.0.1:8000/activate#token=<raw-one-time-token>"
}
```

The raw token is returned only at creation. The backend stores the hash, never the raw token. Share the URL privately. `409 USER_ALREADY_EXISTS` is returned if the email already has an account.

### `GET /api/v1/auth/users/invitations` — Admin only

Query:

- `status`: optional `pending`, `accepted`, `revoked`, or `expired`.
- `limit`: default 50, max 100.
- `offset`: default 0, min 0.

List items contain `invitation_id`, `email`, `role`, `purpose`, `created_by`, `created_at`, `expires_at`, `accepted_at`, and `revoked_at`. The response never includes token hashes or raw activation URLs.

### `DELETE /api/v1/auth/users/invitations/{invitation_id}` — Admin only + CSRF

Optional JSON body:

```json
{"reason":"Invitation sent to the wrong address"}
```

Body may be omitted. Marks an unused invitation revoked and writes an audit record. Response: `{"invitation_id":"<id>","status":"revoked"}`. Errors: `404 INVITATION_NOT_FOUND`, `409 INVITATION_ALREADY_USED`.

### `POST /api/v1/auth/users/{user_id}/password-reset-invitations` — Admin only + CSRF, `201`

Creates a one-time password-reset invitation for an existing account. The response shape is the same as a normal invitation, but `purpose` is `password_reset`. It does not reveal the user's existing password or automatically change it. After the user follows the URL and sets a new password, all their sessions are revoked.

### `PATCH /api/v1/auth/users/{user_id}` — Admin only + CSRF

Updates the user's role and/or active status. Request may contain either or both:

```json
{"role":"operator","is_active":true}
```

- `role`: `admin` or `operator`.
- `is_active`: boolean.
- Omitted fields stay unchanged. `null` is not accepted. An empty object returns `400 NO_FIELDS_SUPPLIED`.
- Disabling a user revokes their outstanding sessions.
- The endpoint refuses to demote or disable the last active administrator (`409 LAST_ADMIN_REQUIRED`).
- Successful response returns `user_id`, `email`, `role`, `is_active`, `created_at`, `updated_at`, `last_login_at`.

### `POST /api/v1/auth/users/{user_id}/revoke-sessions` — Admin only + CSRF

Revokes all active sessions for that user. Response:

```json
{"user_id":"<id>","revoked_sessions":2}
```

A subsequent protected request using a revoked session returns `401`.

### `GET /api/v1/auth/audit-log` — Admin only

Query: `limit` default 100/max 500, `offset` default 0/min 0.

Response `items` include `audit_id`, `actor_user_id`, `actor_email`, `action`, `target_type`, `target_id`, `metadata`, and `created_at`, plus pagination fields. The log includes bootstrap/login/logout/password changes, invitation operations, role/active-state changes, session revocation, and tracking-token rotation as recorded by the current implementation.

## 7. Packages

### Package status and edit rules

Package statuses are `packed`, `in_transit`, `delivered`. A package's `status`, `shipment_id`, tracking hash and lifecycle timestamps cannot be set through generic PATCH. The backend generates package IDs in the form `PKG-{UTC_YEAR}-{SEQUENCE}`, e.g. `PKG-2026-0042`.

A package has one product type, a positive quantity and one optional assigned tracker. Device assignment must reference a registered, currently unassigned tracker. A device can be assigned to no more than one package at a time. Product metadata, thresholds and tracker assignment can be edited only while status is `packed`.

### `POST /api/v1/packages` — Admin/Operator + CSRF, `201`

Create a package and generate its public tracking URL. Required fields:

| Field | Type | Constraints |
|---|---|---|
| `product_sku` | string | 1–100 chars, non-blank after trim |
| `product_name` | string | 1–200 chars, non-blank after trim |
| `quantity` | integer | strict integer, 1–1,000,000 |
| `unit` | string | 1–40 chars, non-blank after trim |
| `min_temperature_c` | number | finite, -100..100 |
| `max_temperature_c` | number | finite, -100..100 and >= minimum |
| `shock_threshold_g` | number | finite, >0..1000 |

Optional fields:

- `batch_number`: string or `null`, max 100 characters; whitespace-only becomes null.
- `expiry_date`: ISO calendar date `YYYY-MM-DD` or `null`.
- `device_id`: registered device ID or `null`; omit or set null to create without tracker.

Example:

```json
{
  "product_sku":"MED-COLD-001",
  "product_name":"Temperature-sensitive medicine",
  "quantity":24,
  "unit":"vials",
  "batch_number":"LOT-2026-A12",
  "expiry_date":"2027-06-30",
  "min_temperature_c":2.0,
  "max_temperature_c":8.0,
  "shock_threshold_g":4.0,
  "device_id":"tracker-001"
}
```

Response:

```json
{
  "package": {
    "package_id":"PKG-2026-0042",
    "product_sku":"MED-COLD-001",
    "product_name":"Temperature-sensitive medicine",
    "quantity":24,
    "unit":"vials",
    "batch_number":"LOT-2026-A12",
    "expiry_date":"2027-06-30",
    "min_temperature_c":2.0,
    "max_temperature_c":8.0,
    "shock_threshold_g":4.0,
    "device_id":"tracker-001",
    "shipment_id":null,
    "status":"packed",
    "created_at":"2026-10-28T01:15:00Z",
    "updated_at":"2026-10-28T01:15:00Z"
  },
  "tracking_url":"http://127.0.0.1:8000/track/<raw-token>"
}
```

The ID and token in this example are illustrative. The returned token URL is shown once; keep it for printing the QR code. The backend stores only the hash.

Possible conflicts/errors: `404 DEVICE_NOT_FOUND` if `device_id` is not registered; `409 DEVICE_ALREADY_ASSIGNED` if assigned elsewhere; `422` for invalid product/threshold values. If a device is assigned during creation, its config version is incremented and new retained config is published after commit.

### `GET /api/v1/packages` — Admin/Operator

Filters: `status` (`packed|in_transit|delivered`), `shipment_id`, `device_id`, `product_sku`, `batch_number`. Pagination `limit` default 50/max 100, `offset` default 0.

Response item fields include:

- package metadata: `package_id`, `product_sku`, `product_name`, `quantity`, `unit`, `batch_number`, `expiry_date`;
- lifecycle/assignment: `device_id`, `shipment_id`, `status`, `created_at`, `updated_at`, `dispatched_at`, `delivered_at`;
- live summary: `last_seen_at`, `device_status`, `latest_temperature_valid`, `latest_temperature_c`, `latest_observed_at`, `open_alert_count`.

The package list intentionally omits `min_temperature_c`, `max_temperature_c` and `shock_threshold_g`. Fetch the detail endpoint when thresholds are needed.

Example query: `/api/v1/packages?status=in_transit&shipment_id=SHP-2026-0001&limit=50&offset=0`.

### `GET /api/v1/packages/{package_id}` — Admin/Operator

Returns all package fields including all three thresholds and lifecycle timestamps, plus:

- `device`: `null` or an object containing `device_id`, derived `status`, `firmware_version`, `hardware_model`, `capabilities`, `last_seen_at`, `config_version`, `config_applied_version`.
- `shipment`: `null` or an object containing `shipment_id`, `status`, `origin`, `destination`, `expected_delivery_at`, `dispatched_at`, `delivered_at`.
- `latest_telemetry`: `null` or an object containing `observed_at`, `received_at`, `temperature_valid`, `temperature_c`, `motion_valid`, `peak_acceleration_magnitude_g`, `location_fix_valid`, `latitude`, `longitude`, `accuracy_m`, `satellites_used`.
- `open_alert_count`: count of package alerts whose status is not `resolved` (thus includes open and acknowledged alerts).

Unknown package: `404 PACKAGE_NOT_FOUND`. Missing relation values are `null`; the backend does not fabricate a device or shipment.

### `PATCH /api/v1/packages/{package_id}` — Admin/Operator + CSRF

Edits only while the package is `packed`. Allowed fields: `product_sku`, `product_name`, `quantity`, `unit`, `batch_number`, `expiry_date`, `min_temperature_c`, `max_temperature_c`, `shock_threshold_g`, `device_id`.

PATCH semantics:

- Omitted means unchanged.
- `device_id: null` unassigns the current tracker.
- Required product/threshold fields cannot be set to null.
- `batch_number: null` and `expiry_date: null` clear those optional values.
- `status`, `shipment_id`, `tracking_token_hash`, `tracking_url`, and timestamps are not editable here.
- `device_id` must already exist and cannot be assigned to a different package.
- Temperature minimum must not exceed maximum.
- Both the old and new assigned trackers have their config version incremented and refreshed config published after commit.

Example:

```json
{
  "product_name":"Temperature-sensitive medicine",
  "quantity":30,
  "min_temperature_c":2.0,
  "max_temperature_c":8.0,
  "shock_threshold_g":3.5,
  "device_id":"tracker-001"
}
```

Returns the updated package row including threshold and lifecycle fields. Errors include `400 NO_FIELDS_SUPPLIED`, `404 PACKAGE_NOT_FOUND`/`DEVICE_NOT_FOUND`, `409 PACKAGE_NOT_EDITABLE`/`DEVICE_ALREADY_ASSIGNED`, and `422` for validation/range failures.

### `POST /api/v1/packages/{package_id}/tracking-token/rotate` — Admin only + CSRF

Invalidates the current public token and returns a new tracking URL:

```json
{"package_id":"PKG-2026-0042","tracking_url":"http://127.0.0.1:8000/track/<new-raw-token>"}
```

The raw token is returned only once and the action is audited. Existing printed QR codes containing the old token stop working. `404 PACKAGE_NOT_FOUND` if the package does not exist.

### `GET /api/v1/packages/{package_id}/telemetry` — Admin/Operator

Query: `limit` default 100/max 500, optional `before` timezone-aware timestamp. Returns observations newer than the cursor condition in descending order; use the `next_before` value returned by the previous page to request older readings.

Response:

```json
{
  "package_id":"PKG-2026-0042",
  "items":[
    {
      "observed_at":"2026-10-28T01:15:15Z",
      "received_at":"2026-10-28T01:15:16Z",
      "device_id":"tracker-001",
      "boot_id":"7f3a9c21",
      "message_seq":42,
      "config_version":3,
      "temperature_valid":true,
      "temperature_c":5.4,
      "motion_valid":true,
      "peak_acceleration_magnitude_g":1.08,
      "shock_count_since_last_report":0,
      "location_fix_valid":false,
      "latitude":null,
      "longitude":null,
      "accuracy_m":null,
      "satellites_used":0
    }
  ],
  "limit":100,
  "next_before":null
}
```

If there are more readings, `next_before` contains the `observed_at` timestamp of the last included row; otherwise it is `null`. Because the cursor is a timestamp rather than a compound key, observations that share the same `observed_at` boundary may be skipped on a subsequent page. Keep this in mind if designing high-volume pagination. Errors: `404 PACKAGE_NOT_FOUND`, `422 TIMEZONE_REQUIRED`.

### `POST /api/v1/packages/{package_id}/deliver` — Admin/Operator + CSRF

Explicitly confirms package delivery. Optional body:

```json
{"recipient_name":"Hospital receiving desk","note":"Package received"}
```

Both fields are optional; `recipient_name` max 200 characters, `note` max 1000. The server owns the actor and timestamps; there is no client-supplied actor or timestamp.

Requirements: package must belong to a shipment and be `in_transit`. The endpoint updates package status/timestamp, appends `package_delivered`, recomputes shipment status and timestamps, increments affected config versions, commits and then publishes updated config. Repeating delivery returns success with `already_delivered: true` and does not append another event.

Response:

```json
{
  "package_id":"PKG-2026-0042",
  "status":"delivered",
  "delivered_at":"2026-10-28T02:00:00Z",
  "shipment_id":"SHP-2026-0001",
  "shipment_status":"partially_delivered",
  "already_delivered":false
}
```

Errors include `404 PACKAGE_NOT_FOUND`, `409 PACKAGE_NOT_IN_TRANSIT`, and `409 PACKAGE_HAS_NO_SHIPMENT`. Public tracking tokens cannot call this endpoint because it requires a dashboard session and CSRF token.

## 8. Shipments

### Shipment status

`packed` → `in_transit` → `partially_delivered` → `delivered` (the final two states depend on delivery results). Shipment statuses are recalculated after package delivery. Generic PATCH cannot set status.

### `POST /api/v1/shipments` — Admin/Operator + CSRF, `201`

Create a shipment and attach already-created package IDs in one transaction. Request:

```json
{
  "origin":"Pune Warehouse",
  "destination":"Kolhapur Hospital",
  "expected_delivery_at":"2026-10-29T10:00:00Z",
  "package_ids":["PKG-2026-0042","PKG-2026-0043"]
}
```

- `origin`, `destination`: non-blank strings, max 200 characters.
- `expected_delivery_at`: optional timezone-aware timestamp or null.
- `package_ids`: required non-empty list, max 100, no duplicate IDs.
- Every package must exist, be `packed`, and have no shipment already. A tracker is not required at creation time; it is required for dispatch.

Response includes shipment fields (`shipment_id`, origin/destination, expected delivery, status, creation/update timestamps), the supplied `package_ids`, and `package_count`. IDs are generated as `SHP-{UTC_YEAR}-{SEQUENCE}`. The `shipment_created` event is inserted transactionally; package-assigned tracker config versions are incremented/refreshed after commit.

### `GET /api/v1/shipments` — Admin/Operator

Optional filter: `status` = `packed|in_transit|partially_delivered|delivered`. Pagination: `limit` default 50/max 100, `offset` default 0.

Each item contains shipment header/lifecycle fields, `package_count`, `delivered_package_count`, and `open_alert_count`. `open_alert_count` counts alerts whose status is `open` or `acknowledged`.

### `GET /api/v1/shipments/{shipment_id}` — Admin/Operator

Returns shipment header and lifecycle timestamps plus:

- `package_count`
- `delivered_package_count`
- `open_alert_count`
- `packages`: each contains `package_id`, `product_sku`, `product_name`, `quantity`, `unit`, `status`, `device_id`, `device_status`, `latest_temperature_valid`, `latest_temperature_c`, `latest_observed_at`, `last_seen_at`, and `open_alert_count`.

Unknown ID: `404 SHIPMENT_NOT_FOUND`.

### `PATCH /api/v1/shipments/{shipment_id}` — Admin/Operator + CSRF

Only editable while shipment status is `packed`. Allowed fields: `origin`, `destination`, `expected_delivery_at`. Omitted fields remain unchanged; `expected_delivery_at: null` clears ETA. Origin/destination must be non-blank and cannot be null. Request must include at least one field.

Returns the refreshed full shipment detail. Errors: `400 NO_FIELDS_SUPPLIED`, `404 SHIPMENT_NOT_FOUND`, `409 SHIPMENT_NOT_EDITABLE`, `422` for invalid fields.

### `POST /api/v1/shipments/{shipment_id}/dispatch` — Admin/Operator + CSRF

Optional body:

```json
{"note":"Released for transport"}
```

Dispatch requires: shipment exists and is `packed`; at least one package; every package is `packed`; every package has a registered, assigned tracker; thresholds are valid; and the tracker has reported an applied config version at least as new as the backend's current version.

In one transaction, the backend changes shipment and package status to `in_transit`, sets dispatch timestamps, and appends one `shipment_dispatched` event. It increments configuration versions and publishes refreshed config after commit.

A repeat dispatch for a shipment already in transit/partially delivered/delivered is idempotent: it does not add another event and response includes `already_dispatched: true`. Response otherwise includes full shipment detail, including packages/counts and `already_dispatched`.

Common errors: `404 SHIPMENT_NOT_FOUND`, `409 INVALID_STATUS_TRANSITION`, `409 EMPTY_SHIPMENT`, `409 PACKAGE_NOT_PACKED`, `409 TRACKER_REQUIRED`, `409 PACKAGE_CONFIGURATION_INVALID`, `409 TRACKER_CONFIG_NOT_APPLIED`.

### `GET /api/v1/shipments/{shipment_id}/events` — Admin/Operator

Returns timeline oldest first:

```json
{
  "shipment_id":"SHP-2026-0001",
  "items":[
    {
      "id":1,
      "event_type":"shipment_created",
      "package_id":null,
      "from_status":null,
      "to_status":"packed",
      "actor":"admin@example.test",
      "occurred_at":"2026-10-28T01:20:00Z",
      "metadata":{"package_ids":["PKG-2026-0042"]}
    }
  ]
}
```

Current event types include `shipment_created`, `shipment_dispatched`, `package_delivered`. `actor` is taken from the authenticated user for operator actions; metadata is an object. Errors: `404 SHIPMENT_NOT_FOUND`.

## 9. Devices and telemetry

A device record is created by an accepted MQTT `register` message, not a REST endpoint. A package may be created without a device and assigned later while packed. Device status is derived: `online` when `now - last_seen_at <= ONLINE_TIMEOUT_SECONDS`, else `offline`.

### `GET /api/v1/devices` — Admin/Operator

Pagination `limit` default 50/max 100, `offset` default 0.

Items contain `device_id`, `firmware_version`, `registered_at`, `last_seen_at`, `config_version`, `config_applied_version`, assigned `package_id`/`shipment_id` (nullable), and derived `status`.

### `GET /api/v1/devices/{device_id}` — Admin/Operator

Returns device diagnostics: `device_id`, `firmware_version`, `hardware_model`, `capabilities`, `registered_at`, `last_seen_at`, `latest_observed_at`, `latest_message`, `last_boot_id`, `config_version`, `config_applied_version`, derived `status`, and `assignment`.

`latest_message` is the full latest accepted MQTT envelope. This is authenticated diagnostic data and must not be exposed through public tracking. `assignment` is null when unassigned; otherwise it includes package/shipment identity/status and the package product/threshold information used in configuration.

### `GET /api/v1/devices/{device_id}/settings` — Admin/Operator

Returns the effective settings derived from current assignment; it is not a mutable device-settings store.

Assigned response:

```json
{
  "device_id":"tracker-001",
  "config_version":3,
  "assigned":true,
  "package_id":"PKG-2026-0042",
  "shipment_id":"SHP-2026-0001",
  "min_temperature_c":2.0,
  "max_temperature_c":8.0,
  "shock_threshold_g":4.0,
  "reporting_interval_seconds":15
}
```

For an unassigned device, `assigned` is false and package/shipment IDs and all three threshold fields are `null`. `reporting_interval_seconds` is currently fixed at 15. If device not found, `404 DEVICE_NOT_FOUND`.

### `PATCH /api/v1/devices/{device_id}/settings` — retired (`410`)

This legacy endpoint always returns `410 DEVICE_SETTINGS_MOVED_TO_PACKAGE`. Update thresholds through `PATCH /api/v1/packages/{package_id}` instead. The global router still requires login; a protected mutation also passes the CSRF check before the route returns 410.

### `GET /api/v1/devices/{device_id}/telemetry` — Admin/Operator

Query: `limit` default 100/max 1000; `offset` default 0/min 0. Response has `items`, `limit`, `offset`, `total`. Each item contains `id`, `observed_at`, `received_at`, `boot_id`, `message_seq`, `config_version`, `temperature_valid`, `temperature_c`, `motion_valid`, `peak_acceleration_magnitude_g`, `shock_count_since_last_report`, `location_fix_valid`, `latitude`, `longitude`, `accuracy_m`, `satellites_used`. Device-oriented output omits `raw_payload`, `package_id` and `shipment_id`. Unknown device returns `404 DEVICE_NOT_FOUND`.

## 10. Alerts

Alert types: `temperature_breach`, `shock_detected`. Alert statuses: `open`, `acknowledged`, `resolved`. Severity is currently set to `warning` for device-originated events. An alert has the source device/boot/message sequence, optional package/shipment snapshot, observed/received timestamps, validated `details`, and lifecycle fields.

### `GET /api/v1/alerts` — Admin/Operator

Optional filters:

- `status`: `open|acknowledged|resolved`
- `device_id`
- `package_id`
- `shipment_id`
- `type`: `temperature_breach|shock_detected`

Pagination: `limit` default 50/max 100, `offset` default 0.

Each item contains `alert_id`, `device_id`, `source_boot_id`, `source_message_seq`, `package_id`, `shipment_id`, `type`, `severity`, `status`, `observed_at`, `received_at`, `details`, `acknowledged_at`, `acknowledged_by`, `resolved_at`, `resolved_by`, `resolution_note`. Raw MQTT envelopes are not included in alert list/detail responses.

### `GET /api/v1/alerts/{alert_id}` — Admin/Operator

Returns one alert with the same fields as a list item. Unknown ID: `404 ALERT_NOT_FOUND`.

### `POST /api/v1/alerts/{alert_id}/acknowledge` — Admin/Operator + CSRF

No request body is required. The actor is taken from the logged-in account. Sets status to `acknowledged`, records server acknowledgement time and actor, and leaves the alert row in place. Repeating acknowledgement is idempotent and preserves the original actor/time. A resolved alert cannot be acknowledged (`409 ALERT_ALREADY_RESOLVED`). Unknown alert: `404 ALERT_NOT_FOUND`.

### `POST /api/v1/alerts/{alert_id}/resolve` — Admin/Operator + CSRF

Optional body:

```json
{"resolution_note":"Package received and excursion reviewed"}
```

Sets `resolved`, records server time/actor, and saves the optional note. An already resolved alert is returned unchanged (idempotent). This action does not delete the alert. Unknown alert: `404 ALERT_NOT_FOUND`.

## 11. Inventory summary

### `GET /api/v1/inventory/summary` — Admin/Operator

Returns aggregate counts:

```json
{
  "packages_total":12,
  "packages_packed":4,
  "packages_in_transit":5,
  "packages_delivered":3,
  "packages_with_open_alerts":2,
  "shipments_in_transit":3,
  "devices_online":5,
  "devices_offline":1
}
```

`packages_with_open_alerts` counts distinct packages with alerts not yet resolved. Acknowledged alerts still count as unresolved/open for this summary.

## 12. Public read-only QR tracking

### `GET /track/{token}` — public HTML

Minimal built-in page that calls the JSON route below. Frontend can replace it, but it must remain read-only.

### `GET /api/v1/public/tracking/{token}` — public JSON

The tracking token is a high-entropy bearer secret generated when the package is created. The DB stores its SHA-256 hash. The endpoint sets `Cache-Control: no-store` and `Referrer-Policy: no-referrer`.

Response:

```json
{
  "package": {
    "package_id":"PKG-2026-0042",
    "product_name":"Temperature-sensitive medicine",
    "quantity":24,
    "unit":"vials",
    "status":"in_transit"
  },
  "shipment": {
    "shipment_id":"SHP-2026-0001",
    "origin":"Pune Warehouse",
    "destination":"Kolhapur Hospital",
    "status":"in_transit",
    "expected_delivery_at":"2026-10-29T10:00:00Z"
  },
  "monitoring": {
    "temperature_valid":true,
    "temperature_c":5.4,
    "observed_at":"2026-10-28T01:15:15Z",
    "condition":"normal",
    "open_alert_count":0
  },
  "timeline":[
    {
      "event_type":"shipment_dispatched",
      "occurred_at":"2026-10-28T01:25:00Z",
      "label":"Shipment dispatched"
    }
  ],
  "updated_at":"2026-10-28T01:15:16Z"
}
```

Behavior:

- `shipment` is `null` until the package is attached to a shipment.
- If there is no valid latest temperature, `temperature_valid` is false, `temperature_c` is null and `condition` is `unknown` unless an unresolved alert exists.
- `condition` is `alert` when there is any package alert not resolved; otherwise it is `normal` for a valid latest temperature or `unknown` when there is no valid temperature. The backend does not independently evaluate package threshold ranges here.
- Timeline includes sanitized shipment/package lifecycle events only. Event labels are generic for unknown event types.
- No token hash, MQTT payload, device diagnostics, threshold details, credentials or private account information is returned.
- Unknown/rotated token returns `404 TRACKING_NOT_FOUND`.
- There is no public mutation endpoint. A QR scan cannot prove delivery and cannot change package/shipment status.

## 13. REST request examples for the frontend

### Fetch list with filters/pagination

```js
const response = await fetch(
  `${API_BASE}/api/v1/packages?status=in_transit&limit=50&offset=0`,
  { credentials: "include" }
);
if (response.status === 401) {
  // Clear the UI's user state and ask the user to log in again.
}
const data = await response.json();
// data.items, data.limit, data.offset, data.total
```

### Authenticated mutation helper

```js
async function apiMutation(path, method, body, csrfToken) {
  const response = await fetch(`${API_BASE}${path}`, {
    method,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      "X-CSRF-Token": csrfToken,
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = response.status === 204 ? null : await response.json().catch(() => null);
  if (!response.ok) {
    const error = new Error(
      data?.detail?.message ?? data?.detail ?? `Request failed (${response.status})`
    );
    error.status = response.status;
    error.code = data?.detail?.code;
    error.data = data;
    throw error;
  }
  return data;
}
```

### Admin invite flow

```js
// 1. Login; browser stores the HttpOnly cookie automatically.
const loginResponse = await fetch(`${API_BASE}/api/v1/auth/login`, {
  method: "POST",
  credentials: "include",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ email, password }),
});
const login = await loginResponse.json();
const csrf = login.csrf_token;

// 2. Admin invites an operator; show/copy activation_url exactly once.
const invite = await apiMutation(
  "/api/v1/auth/users/invitations",
  "POST",
  { email: "operator@example.test", role: "operator" },
  csrf
);
console.log(invite.activation_url);
```

The frontend should present actionable errors but must not display stack traces or raw internal exception details. Keep one-time URLs private and do not log them to analytics.

## 14. MQTT device-to-backend contract

MQTT is a separate transport from the browser REST API. Browser sessions do not authenticate tracker MQTT connections. Configure broker-level credentials/ACLs separately before allowing trackers on a network.

### Topics

| Direction | Topic | QoS / retain | Purpose |
|---|---|---|---|
| Device → backend | `coldchain/{device_id}/messages` | QoS 1 recommended; retain false | register, telemetry, event envelope |
| Backend → device | `coldchain/{device_id}/registration/ack` | QoS 1; retain false | registration result |
| Backend → device | `coldchain/{device_id}/config` | QoS 1; retain true | authoritative current config |
| Backend subscription | `coldchain/+/messages` | QoS 1 | receive device messages |

The backend uses Paho MQTT 2.x callback API version 2. Mosquitto relays the JSON payload; it does not rewrite the envelope.

### Common envelope (all device messages)

Exactly these fields are required; unknown fields are rejected:

```json
{
  "schema_version":1,
  "message_type":"telemetry",
  "device_id":"tracker-001",
  "firmware_version":"0.1.0",
  "boot_id":"7f3a9c21",
  "message_seq":42,
  "config_version":3,
  "observed_at":"2026-10-28T01:15:15Z",
  "uptime_ms":15000,
  "payload":{}
}
```

- `schema_version`: integer exactly `1`.
- `message_type`: `register`, `telemetry`, or `event`.
- `device_id`: 1–64 ASCII letters, digits, `_` or `-`; must match the topic segment.
- `firmware_version`, `boot_id`: non-empty strings up to 128 chars.
- `message_seq`: non-negative integer within PostgreSQL BIGINT range.
- `config_version`: non-negative integer within PostgreSQL INTEGER range.
- `uptime_ms`: non-negative integer.
- `observed_at`: timezone-aware ISO/RFC3339 string.
- `payload`: JSON object.

Use a new random `boot_id` after each reboot and restart `message_seq` at zero. Increase sequence across all outbound device message types within that boot. The backend deduplicates messages by `(device_id, boot_id, message_seq)` for register/telemetry/event. A new device must register with config version `0`; a device cannot claim a config version newer than the backend's current version. Messages from unregistered devices are rejected except registration.

### `register` payload

```json
{
  "hardware_model":"esp32-devkit-v1",
  "capabilities":{
    "temperature":true,
    "accelerometer":true,
    "gnss":true,
    "epaper":false
  }
}
```

Exactly the two keys `hardware_model` and `capabilities` are accepted. `hardware_model` can be null or a non-empty string. `capabilities` is an object mapping strings to booleans. Thresholds are not accepted at registration.

Registration is idempotent and does not overwrite package assignment or package thresholds. Accepted registration updates device diagnostics, sends a registration ACK and republishes the current retained config to recover a missed config after reconnect.

### `telemetry` payload

```json
{
  "temperature":{"valid":true,"celsius":5.4},
  "motion":{
    "valid":true,
    "peak_acceleration_magnitude_g":1.08,
    "shock_count_since_last_report":0
  },
  "location":{
    "fix_valid":false,
    "latitude":null,
    "longitude":null,
    "accuracy_m":null,
    "satellites_used":0
  }
}
```

All nested objects and exact keys are required. `temperature.valid`/`motion.valid` must be booleans. A valid sensor value must be present; an invalid value must be `null`. Temperature range is `-100..100` Celsius. Acceleration magnitude is non-negative and in **g**. `shock_count_since_last_report` and `satellites_used` are non-negative integers that fit PostgreSQL INTEGER. Coordinates must be in valid geographic ranges. If `location.fix_valid` is false, latitude, longitude and accuracy must be null; if true, latitude and longitude are required. The backend snapshots current package/shipment assignment from PostgreSQL at ingest time; it does not trust IDs in telemetry because none are provided by the tracker.

### `event` payload: temperature breach

```json
{
  "type":"temperature_breach",
  "temperature_c":9.1,
  "breach_direction":"above_maximum",
  "min_temperature_c":2.0,
  "max_temperature_c":8.0
}
```

Allowed fields are exactly those shown. Reading and min/max must be finite; min <= max. Direction is `below_minimum` or `above_maximum`.

### `event` payload: shock

```json
{
  "type":"shock_detected",
  "peak_acceleration_magnitude_g":5.7,
  "threshold_g":4.0,
  "duration_ms":35
}
```

Allowed fields are `type`, `peak_acceleration_magnitude_g`, `threshold_g`, and optional `duration_ms`. Peak value is non-negative, threshold is greater than zero and no more than 1000 g, and duration (if present) is a non-negative integer. These fields must reflect the same calibrated metric firmware uses for its local threshold comparison.

### Registration ACK

Topic `coldchain/{device_id}/registration/ack`. Success example:

```json
{
  "message_type":"registration_ack",
  "device_id":"tracker-001",
  "status":"registered",
  "reason":null
}
```

Status is `registered`, `already_registered`, or `rejected`. Rejected registration can include a reason. Subscribe before publishing the registration message so the tracker cannot miss the ACK.

### Retained `device_config`

Topic `coldchain/{device_id}/config`. Assigned example (values illustrative):

```json
{
  "schema_version":1,
  "message_type":"device_config",
  "device_id":"tracker-001",
  "config_version":3,
  "issued_at":"2026-10-28T01:15:00Z",
  "assignment":{
    "assigned":true,
    "package_id":"PKG-2026-0042",
    "shipment_id":"SHP-2026-0001",
    "package_status":"in_transit",
    "shipment_status":"in_transit"
  },
  "package":{"product_sku":"MED-COLD-001","product_name":"Temperature-sensitive medicine"},
  "monitoring":{
    "enabled":true,
    "reporting_interval_seconds":15,
    "temperature":{"min_temperature_c":2.0,"max_temperature_c":8.0},
    "shock":{"threshold_g":4.0}
  }
}
```

For an unassigned device, `assignment.assigned` is false, package/shipment IDs and statuses are null, `package` is null, `monitoring.enabled` is false, and `monitoring.temperature`/`monitoring.shock` are null. If assigned package/shipment is delivered, monitoring is disabled. The backend includes `issued_at` and `config_version`; firmware should validate and atomically apply only well-formed, non-stale configs. The current reporting interval is hard-coded to 15 seconds in this backend, not editable through an API.

### Ingestion and alert rules

- Validate outer envelope, topic/ID match, timestamp and message-specific payload; reject malformed messages and unknown fields.
- Register may create/update device metadata and diagnostics, not package thresholds/assignment.
- Telemetry/event from an unregistered device is rejected.
- Record deduplication key and telemetry/alert row in the same SQL transaction.
- Duplicate key: ignore duplicate effects. QoS 1 redelivery must not make duplicate telemetry or alert rows.
- Telemetry creates telemetry only. An alert is created only from a valid explicit `event` message.
- For `temperature_breach`, firmware should emit an event when entering an excursion episode rather than on every sampling loop. Shock is a discrete event; a subsequent normal telemetry sample does not automatically resolve an alert.
- Commit database changes before MQTT config publication. PostgreSQL remains authoritative if publishing fails.

## 15. Source-of-truth checklist

- Thresholds live on `packages`, not `devices` or a device-settings table.
- Device registration contains identity/capabilities only.
- Only a registered device can send telemetry or events.
- Configuration uses a monotonically increasing `config_version` per device; firmware reports the version it actually applied.
- Backend does not classify telemetry into an alert; firmware event messages are the source of alert creation.
- Public tracking only reads a hash-verified token and returns sanitized data.
- Delivery is an authenticated operator/admin action, never a QR scan side effect.
- Account creation is invitation-only after the first CLI-provisioned admin.
- Every authenticated mutation sends the CSRF header.
- `CREATE TABLE IF NOT EXISTS` is not schema migration; back up and migrate/reset deliberately when switching from an older schema.
