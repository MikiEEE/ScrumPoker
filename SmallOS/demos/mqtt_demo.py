"""SmallOS MQTT example using the native cooperative MQTT client."""

from common import build_runtime

from SmallPackage import SmallMQTTClient, SmallTask, Unix


MQTT_HOST = "127.0.0.1"
MQTT_PORT = 1883
MQTT_USE_TLS = False
MQTT_TOPIC = "smallos/demo"
MQTT_QOS = 1


async def mqtt_demo(task):
    client = SmallMQTTClient(
        task,
        host=MQTT_HOST,
        port=MQTT_PORT,
        use_tls=MQTT_USE_TLS,
        client_id="smallos-demo-client",
    )
    await client.connect()
    suback = await client.subscribe(MQTT_TOPIC, qos=MQTT_QOS)
    publish_info = await client.publish(MQTT_TOPIC, "hello from smallOS", qos=MQTT_QOS)
    task.OS.print("mqtt subscribed to {} with granted QoS {}\n".format(MQTT_TOPIC, suback["granted_qos"]))
    task.OS.print("mqtt publish sent to {} with QoS {}\n".format(MQTT_TOPIC, publish_info["qos"]))
    task.OS.print("mqtt waiting for one inbound message on {}\n".format(MQTT_TOPIC))
    message = await client.receive_message()
    task.OS.print(
        "mqtt received {} -> {} at QoS {}\n".format(message["topic"], message["payload"], message["qos"])
    )
    await client.disconnect()
    return message


def main():
    runtime = build_runtime(Unix())
    runtime.fork([SmallTask(2, mqtt_demo, name="mqtt_demo")])
    runtime.startOS()


if __name__ == "__main__":
    main()
