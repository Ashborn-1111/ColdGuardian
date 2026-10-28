
import os
import json
import paho.mqtt.client as mqtt

MQTT_HOST = os.getenv("MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("MQTT_PORT", 1883))
MQTT_TOPIC = os.getenv("MQTT_TOPIC", "coldchain/+/messages")
MQTT_USERNAME = os.getenv("MQTT_USERNAME") or None
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD") or None

def on_connect(client, userdata,flags,reason_code,properties):
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

    #step:1 decode the incoming message
    payload = msg.payload.decode("utf-8")
    print(f"Data: {payload}")

    #Step:2 parse JSON
    try:
        payload_json = json.loads(payload)
    except json.JSONDecodeError:
        print("Invalid JSON; ACK not sent")
        return
    #step:3 Handle registration messages only
    if payload_json.get("message_type") != "register":
        return
     # Step 4: Extract device ID from the topic
    topic_parts = msg.topic.split("/")

    if len(topic_parts) != 3 or topic_parts[0] != "coldchain":
        print("Invalid topic format; ACK not sent")
        return
    device_id= topic_parts[1]
    ack_topic = f"coldchain/{device_id}/registration/ack"

    # Step 5: Get and validate required settings
    settings = payload_json.get("payload", {}).get("settings", {})

    required_settings = [
        "max_temperature_c",
        "min_temperature_c",
        "shock_threshold_g",
    ]

    missing = [
        key for key in required_settings
        if key not in settings
    ]

    if missing:
        print(f"Missing settings: {missing}")
        return

  
    # Step 6: Build the ACK response
    ack_payload = {
        "status": "registered",
        "settings": {
            "max_temperature_c": settings["max_temperature_c"],
            "min_temperature_c": settings["min_temperature_c"],
            "shock_threshold_g": settings["shock_threshold_g"],
        },
    }

    # Step 7: Publish the ACK
    result = client.publish(
        ack_topic,
        json.dumps(ack_payload),
        qos=1,
    )

    if result.rc == mqtt.MQTT_ERR_SUCCESS:
        print(f"ACK queued for: {ack_topic}")
    else:
        print(f"Failed to publish ACK: {result.rc}")



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






