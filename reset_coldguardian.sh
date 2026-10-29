#!/usr/bin/env bash
set -Eeuo pipefail

# Destructive development reset for ColdGuardian only.
# Clears retained messages beneath coldchain/# and drops/recreates ONLY database "coldchain".
# Run from WSL in the project root, with main.py stopped and ESP32 unplugged.

cd "$(dirname "${BASH_SOURCE[0]}")"
PG_CONTAINER="${PG_CONTAINER:-coldchain-postgres}"
MQTT_CONTAINER="${MQTT_CONTAINER:-coldchain-mosquitto}"
PG_USER="${PG_USER:-coldchain}"
DB_NAME="coldchain" # Hard whitelist; intentionally not configurable.
ADMIN_EMAIL="${ADMIN_EMAIL:-admin@admin.admin}"

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

[[ -f main.py ]] || fail "main.py not found. Put this script in the ColdGuardian project root."
[[ -f .env ]] || fail ".env not found. Keep your existing .env in the project root."
command -v podman >/dev/null || fail "podman not found. Run this in WSL, not Windows Git Bash."
command -v uv >/dev/null || fail "uv not found in WSL."
command -v python3 >/dev/null || fail "python3 not found."
command -v curl >/dev/null || fail "curl not found."

python3 - <<'PY'
from pathlib import Path
from urllib.parse import urlparse
values = {}
for raw in Path('.env').read_text(encoding='utf-8').splitlines():
    line = raw.strip()
    if line and not line.startswith('#') and '=' in line:
        key, value = line.split('=', 1)
        values[key.strip()] = value.strip().strip('"\'')
url = values.get('DATABASE_URL', '')
if not url:
    raise SystemExit('ERROR: DATABASE_URL missing from .env')
if urlparse(url).path.lstrip('/') != 'coldchain':
    raise SystemExit('ERROR: DATABASE_URL does not target database coldchain. Refusing to reset.')
print('Database guard passed: only PostgreSQL database coldchain will be reset.')
PY

if curl -fsS --max-time 1 http://127.0.0.1:8000/openapi.json >/dev/null 2>&1; then
  fail "Backend is running on port 8000. Stop main.py with Ctrl+C before resetting."
fi
podman container exists "$PG_CONTAINER" || fail "PostgreSQL container '$PG_CONTAINER' not found."
podman container exists "$MQTT_CONTAINER" || fail "Mosquitto container '$MQTT_CONTAINER' not found."

# Start required containers if stopped; don't delete or recreate volumes.
if [[ "$(podman inspect -f '{{.State.Running}}' "$PG_CONTAINER")" != "true" ]]; then podman start "$PG_CONTAINER"; fi
if [[ "$(podman inspect -f '{{.State.Running}}' "$MQTT_CONTAINER")" != "true" ]]; then podman start "$MQTT_CONTAINER"; fi
podman exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d postgres >/dev/null || fail "PostgreSQL isn't ready."

cat <<'TEXT'

THIS RESET IS DESTRUCTIVE.
It will:
  • delete all records in PostgreSQL database "coldchain" (users, devices,
    packages, shipments, telemetry, alerts, audit records and sessions);
  • clear retained MQTT messages under coldchain/# (including retained device configs).

It will NOT delete Podman containers or volumes, any other database, source files,
or your .env. It cannot erase non-retained messages already delivered to clients.

Safety steps:
  • Stop main.py (it must not be running).
  • Unplug the physical ESP32 and stop simulator scripts, so they don't re-register
    or republish config while cleanup is happening.
TEXT
read -r -p 'Proceed with reset? [y/N] ' answer
case "$answer" in y|Y|yes|YES) ;; *) echo 'Cancelled. No data was reset.'; exit 0;; esac

# Keep the existing token secret stable. Generate one only if missing/too short.
if ! grep -Eq '^TRACKING_TOKEN_SECRET=.{32,}$' .env; then
  TRACKING_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
  python3 - "$TRACKING_KEY" <<'PY'
from pathlib import Path
import sys
p=Path('.env'); lines=p.read_text(encoding='utf-8').splitlines(); key='TRACKING_TOKEN_SECRET='
value=key+sys.argv[1]; found=False
for i,line in enumerate(lines):
    if line.startswith(key): lines[i]=value; found=True
if not found: lines.append(value)
p.write_text('\n'.join(lines)+'\n', encoding='utf-8')
PY
  echo 'Created a new TRACKING_TOKEN_SECRET. Keep it stable and private.'
fi

# Ensure paho-mqtt is available in the WSL Linux venv (use copy mode on /mnt/d).
echo
echo 'Checking Python dependencies in WSL…'
UV_LINK_MODE=copy uv sync

# Discover retained topics and delete them using a zero-byte retained publish.
echo
echo 'Scanning and clearing retained MQTT messages under coldchain/#…'
UV_LINK_MODE=copy uv run --env-file .env python - <<'PY'
import os, sys, time, uuid, threading
import paho.mqtt.client as mqtt

host=os.getenv('MQTT_HOST','127.0.0.1')
port=int(os.getenv('MQTT_PORT','1883'))
username=os.getenv('MQTT_USERNAME') or None
password=os.getenv('MQTT_PASSWORD') or None

def client(label):
    c=mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                  client_id=f'cg-reset-{label}-{uuid.uuid4().hex[:8]}',
                  protocol=mqtt.MQTTv311)
    if username: c.username_pw_set(username,password)
    return c

topics=set(); connected=threading.Event(); suback=threading.Event(); errors=[]
c=client('scan')
def on_connect(client, userdata, flags, reason_code, properties):
    if getattr(reason_code,'is_failure',False): errors.append(f'connect rejected: {reason_code}'); connected.set(); return
    connected.set(); rc,_=client.subscribe('coldchain/#',qos=1)
    if rc != mqtt.MQTT_ERR_SUCCESS: errors.append(f'subscribe failed rc={rc}'); suback.set()
def on_subscribe(client, userdata, mid, reason_code_list, properties):
    if any(getattr(code,'is_failure',False) for code in reason_code_list): errors.append(f'subscribe rejected: {reason_code_list}')
    suback.set()
def on_message(client, userdata, msg):
    if msg.retain and msg.topic.startswith('coldchain/'): topics.add(msg.topic)
c.on_connect=on_connect; c.on_subscribe=on_subscribe; c.on_message=on_message
try:
    c.connect(host,port,keepalive=20); c.loop_start()
    if not connected.wait(8): raise RuntimeError(f'cannot connect to MQTT broker at {host}:{port}')
    if errors: raise RuntimeError(errors[0])
    if not suback.wait(8): raise RuntimeError('timeout subscribing to coldchain/#')
    if errors: raise RuntimeError(errors[0])
    # Retained deliveries arrive immediately after SUBACK; allow them to drain.
    time.sleep(1.5)
finally:
    try: c.disconnect(); c.loop_stop()
    except Exception: pass

print(f'Found {len(topics)} retained app topic(s).')
if topics:
    connected=threading.Event(); errors=[]; clearer=client('clear')
    def on_clear_connect(client, userdata, flags, reason_code, properties):
        if getattr(reason_code,'is_failure',False): errors.append(f'clear connection rejected: {reason_code}')
        connected.set()
    clearer.on_connect=on_clear_connect
    clearer.connect(host,port,keepalive=20); clearer.loop_start()
    try:
        if not connected.wait(8): raise RuntimeError('timeout connecting to clear retained topics')
        if errors: raise RuntimeError(errors[0])
        for topic in sorted(topics):
            info=clearer.publish(topic,payload=b'',qos=1,retain=True)
            if info.rc != mqtt.MQTT_ERR_SUCCESS: raise RuntimeError(f'publish failed for {topic}: rc={info.rc}')
            info.wait_for_publish(timeout=5)
            if not info.is_published(): raise RuntimeError(f'PUBACK timeout clearing {topic}')
            print(f'  cleared {topic}')
    finally:
        clearer.disconnect(); clearer.loop_stop()

# Verify no retained application messages remain.
remaining=set(); connected=threading.Event(); suback=threading.Event(); errors=[]; verify=client('verify')
def on_verify_connect(client, userdata, flags, reason_code, properties):
    if getattr(reason_code,'is_failure',False): errors.append(f'verify connection rejected: {reason_code}')
    connected.set()
    if not errors:
        rc,_=client.subscribe('coldchain/#',qos=1)
        if rc != mqtt.MQTT_ERR_SUCCESS: errors.append(f'verify subscribe failed rc={rc}'); suback.set()
def on_verify_subscribe(client, userdata, mid, reason_code_list, properties):
    if any(getattr(code,'is_failure',False) for code in reason_code_list): errors.append(f'verify subscribe rejected: {reason_code_list}')
    suback.set()
def on_verify_message(client, userdata, msg):
    if msg.retain and msg.topic.startswith('coldchain/'): remaining.add(msg.topic)
verify.on_connect=on_verify_connect; verify.on_subscribe=on_verify_subscribe; verify.on_message=on_verify_message
try:
    verify.connect(host,port,keepalive=20); verify.loop_start()
    if not connected.wait(8): raise RuntimeError('verify connect timeout')
    if not suback.wait(8): raise RuntimeError('verify subscribe timeout')
    time.sleep(1.5)
finally:
    try: verify.disconnect(); verify.loop_stop()
    except Exception: pass
if errors: raise RuntimeError('; '.join(errors))
if remaining: raise RuntimeError('retained topics remain: '+', '.join(sorted(remaining)))
print('PASS: no retained messages remain under coldchain/#.')
PY

# Clear retained messages first, then reset only the whitelisted database.
echo
echo "Dropping and recreating only database '$DB_NAME'…"
podman exec "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres -c 'DROP DATABASE IF EXISTS coldchain WITH (FORCE)'
podman exec "$PG_CONTAINER" psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres -c 'CREATE DATABASE coldchain OWNER coldchain'

echo
echo "Database and retained MQTT state cleared. Creating the initial administrator: $ADMIN_EMAIL"
uv run --env-file .env python main.py create-admin --email "$ADMIN_EMAIL"

echo
echo 'RESET COMPLETE.'
echo 'Start the backend with: uv run --env-file .env python main.py'
echo 'Reconnect the ESP32; it should re-register automatically.'
echo 'The 5-tracker simulator scripts are separate; run create_five_trackers.py before simulate_five_trackers.py.'
