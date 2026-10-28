
import os
import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("PORT", 1883))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "coldchain/+/messages")
MQTT_USERNAME = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None

def on_connect(client, userdata, reason_code, properties):
    if reason_code == 0:
        print("Connected to MQTT broker")

        # Subscribe after connecting
        client.subscribe(MQTT_TOPIC, qos=1)
        print(f"Subscribed to: {MQTT_TOPIC}")
    else:
        print(f"Failed to connect: {reason_code}")

def on_subscribe(client, userdata, mid, reason_code_list, properties):
    print(f"Subscribtion result: ", reason_code_list)

def on_message(client, userdata, msg):
    print(f"Topic: {msg.topic}")
    print(f"Payload: {msg.payload.decode('utf-8', errors='replace')}")



client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="coldchain_user"
)

client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

client.on_connect = on_connect
client.on_subscribe = on_subscribe
client.on_message = on_message

print(f"Connecting to {MQTT_HOST}:{MQTT_PORT}...")

client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
client.loop_forever()


