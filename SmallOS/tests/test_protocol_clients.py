import sys
import unittest

sys.path.append("..")

from SmallPackage.Kernel import Kernel
from SmallPackage.clients import HTTPProtocolError, SmallHTTPClient
from SmallPackage.clients import SmallMQTTClient
from SmallPackage.SmallOS import SmallOS
from SmallPackage.clients import SmallRedisClient
from SmallPackage.clients import SmallSSEClient, SmallWebSocketClient
from SmallPackage.clients.SmallMQTT import MQTTProtocolError
from SmallPackage.clients.SmallRedis import RedisError
from SmallPackage.clients.SmallHTTP import SSEProtocolError
from SmallPackage.clients.SmallWebSocket import WebSocketProtocolError
from SmallPackage.SmallTask import SmallTask


class ScriptedSocket:
    def __init__(self, recv_chunks=None):
        self.recv_chunks = [bytes(chunk) for chunk in (recv_chunks or [])]
        self.sent = []
        self.closed = False
        self.blocking = True


class ScriptedKernel(Kernel):
    def __init__(self, sockets):
        super().__init__()
        self.now = 0
        self.output = []
        self.sockets = list(sockets)
        self.opened = []
        self.tls_wrap_calls = []

    def write(self, msg):
        self.output.append(msg)

    def time_epoch(self):
        return self.now / 1000

    def time_monotonic(self):
        return self.now / 1000

    def ticks_ms(self):
        return self.now

    def ticks_add(self, base, delta_ms):
        return base + delta_ms

    def ticks_diff(self, end, start):
        return end - start

    def sleep_ms(self, delay_ms):
        if delay_ms > 0:
            self.now += int(delay_ms)

    def io_wait(self, readables, writables, timeout_ms=None):
        ready_read = [obj for obj in readables if obj.recv_chunks]
        ready_write = list(writables)
        if timeout_ms is not None and timeout_ms > 0 and not ready_read and not ready_write:
            self.now += int(timeout_ms)
        return ready_read, ready_write

    def resolve_address(self, host, port):
        return (0, 0, 0, "", (host, port))

    def socket_open(self, address_info):
        sock = self.sockets.pop(0)
        self.opened.append(sock)
        return sock

    def socket_setblocking(self, sock, flag):
        sock.blocking = flag

    def socket_connect(self, sock, sockaddr):
        return False

    def socket_connection_error(self, sock):
        return 0

    def socket_send(self, sock, data):
        payload = bytes(data)
        sock.sent.append(payload)
        return len(payload)

    def socket_recv(self, sock, buffer_size):
        if not sock.recv_chunks:
            raise BlockingIOError()
        chunk = sock.recv_chunks.pop(0)
        if len(chunk) > buffer_size:
            sock.recv_chunks.insert(0, chunk[buffer_size:])
            chunk = chunk[:buffer_size]
        return chunk

    def socket_close(self, sock):
        sock.closed = True

    def socket_wrap_tls_client(
        self,
        sock,
        server_hostname=None,
        tls_ca_file=None,
        tls_cert_file=None,
        tls_key_file=None,
        tls_verify=True,
    ):
        self.tls_wrap_calls.append(
            {
                "sock": sock,
                "server_hostname": server_hostname,
                "tls_ca_file": tls_ca_file,
                "tls_cert_file": tls_cert_file,
                "tls_key_file": tls_key_file,
                "tls_verify": tls_verify,
            }
        )
        return sock


def _decode_client_frame(frame):
    first = frame[0]
    second = frame[1]
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    if not masked:
        raise ValueError("expected masked client frame")

    length = second & 0x7F
    cursor = 2
    if length == 126:
        length = int.from_bytes(frame[cursor:cursor + 2], "big")
        cursor += 2
    elif length == 127:
        length = int.from_bytes(frame[cursor:cursor + 8], "big")
        cursor += 8

    mask = frame[cursor:cursor + 4]
    cursor += 4
    payload = bytearray(frame[cursor:cursor + length])
    for idx in range(length):
        payload[idx] ^= mask[idx % 4]

    return {
        "opcode": opcode,
        "payload": bytes(payload),
    }


class TestProtocolClients(unittest.TestCase):
    def build_os(self, socket, task, config=None):
        kernel = ScriptedKernel([socket])
        runtime = SmallOS(config=config).setKernel(kernel)
        runtime.fork([task])
        runtime.startOS()
        return runtime, kernel

    def test_redis_client_supports_ping_set_and_get(self):
        redis_socket = ScriptedSocket(
            [
                b"+PONG\r\n",
                b"+OK\r\n",
                b"$5\r\nvalue\r\n",
            ]
        )

        async def redis_job(task):
            client = SmallRedisClient(task, host="redis.local")
            await client.connect()
            pong = await client.ping()
            ok = await client.set("key", "value")
            value = await client.get("key")
            client.close()
            return (pong, ok, value)

        root = SmallTask(2, redis_job, name="redis_job")
        _, kernel = self.build_os(redis_socket, root)

        self.assertEqual(("PONG", "OK", "value"), root.result)
        self.assertTrue(redis_socket.closed)
        self.assertEqual(
            [
                b"*1\r\n$4\r\nPING\r\n",
                b"*3\r\n$3\r\nSET\r\n$3\r\nkey\r\n$5\r\nvalue\r\n",
                b"*2\r\n$3\r\nGET\r\n$3\r\nkey\r\n",
            ],
            redis_socket.sent,
        )
        self.assertIs(kernel.opened[0], redis_socket)

    def test_http_client_supports_get_with_base_url_params_and_tls(self):
        http_socket = ScriptedSocket(
            [
                b"HTTP/1.1 200 OK\r\n",
                b"Content-Type: application/json\r\n",
                b"Content-Length: 12\r\n",
                b"\r\n",
                b"{\"ok\": true}",
            ]
        )

        async def http_job(task):
            client = SmallHTTPClient(
                task,
                base_url="https://api.example.com:8443/v1",
                default_headers={"Accept": "application/json"},
                tls_ca_file="tests/fixtures/ca.pem",
                tls_verify=False,
            )
            response = await client.get("status", params={"check": 1, "verbose": True}, headers={"X-Test": "1"})
            return (response.status_code, response.json()["ok"], response.ok)

        root = SmallTask(2, http_job, name="http_get_job")
        _, kernel = self.build_os(http_socket, root)

        self.assertEqual((200, True, True), root.result)
        self.assertTrue(http_socket.closed)
        self.assertEqual(
            [
                {
                    "sock": http_socket,
                    "server_hostname": "api.example.com",
                    "tls_ca_file": "tests/fixtures/ca.pem",
                    "tls_cert_file": None,
                    "tls_key_file": None,
                    "tls_verify": False,
                }
            ],
            kernel.tls_wrap_calls,
        )
        self.assertEqual(
            [
                (
                    b"GET /v1/status?check=1&verbose=true HTTP/1.1\r\n"
                    b"Accept: application/json\r\n"
                    b"X-Test: 1\r\n"
                    b"Host: api.example.com:8443\r\n"
                    b"User-Agent: smallOS/0.1\r\n"
                    b"Connection: close\r\n"
                    b"\r\n"
                )
            ],
            http_socket.sent,
        )

    def test_http_client_supports_post_json_and_chunked_response(self):
        http_socket = ScriptedSocket(
            [
                b"HTTP/1.1 201 Created\r\n",
                b"Transfer-Encoding: chunked\r\n",
                b"Content-Type: application/json; charset=utf-8\r\n",
                b"\r\n",
                b"B\r\n",
                b"{\"id\": 123}\r\n",
                b"0\r\n",
                b"\r\n",
            ]
        )

        async def http_job(task):
            client = SmallHTTPClient(task, host="api.example.com", port=8080)
            response = await client.post(
                "/items",
                headers={"Accept": "application/json"},
                json_body={"name": "smallos"},
            )
            return (response.status_code, response.reason, response.json()["id"], response.text())

        root = SmallTask(2, http_job, name="http_post_job")
        self.build_os(http_socket, root)

        self.assertEqual((201, "Created", 123, '{"id": 123}'), root.result)
        self.assertTrue(http_socket.closed)
        self.assertEqual(
            [
                (
                    b"POST /items HTTP/1.1\r\n"
                    b"Accept: application/json\r\n"
                    b"Content-Type: application/json\r\n"
                    b"Host: api.example.com:8080\r\n"
                    b"User-Agent: smallOS/0.1\r\n"
                    b"Connection: close\r\n"
                    b"Content-Length: 18\r\n"
                    b"\r\n"
                    b"{\"name\":\"smallos\"}"
                )
            ],
            http_socket.sent,
        )

    def test_http_client_inherits_limits_from_runtime_config(self):
        http_socket = ScriptedSocket(
            [
                b"HTTP/1.1 200 OK\r\n",
                b"Content-Length: 12\r\n",
                b"\r\n",
                b"{\"ok\": true}",
            ]
        )

        async def http_job(task):
            client = SmallHTTPClient(task, host="api.example.com")
            return await client.get("/")

        root = SmallTask(2, http_job, name="http_config_limits")
        self.build_os(
            http_socket,
            root,
            config={
                "client_defaults": {
                    "http": {"max_response_size": 4},
                }
            },
        )

        self.assertIsInstance(root.exception, HTTPProtocolError)
        self.assertIn("max_response_size", str(root.exception))

    def test_explicit_client_arguments_override_runtime_config(self):
        http_socket = ScriptedSocket(
            [
                b"HTTP/1.1 200 OK\r\n",
                b"Content-Length: 12\r\n",
                b"\r\n",
                b"{\"ok\": true}",
            ]
        )

        async def http_job(task):
            client = SmallHTTPClient(
                task,
                host="api.example.com",
                max_response_size=32,
                max_buffer_size=128,
            )
            response = await client.get("/")
            return response.json()["ok"]

        root = SmallTask(2, http_job, name="http_override_limits")
        self.build_os(
            http_socket,
            root,
            config={
                "client_defaults": {
                    "stream": {"max_buffer_size": 4},
                    "http": {"max_response_size": 4},
                }
            },
        )

        self.assertTrue(root.result)
        self.assertIsNone(root.exception)

    def test_redis_client_supports_auth_and_tls_options(self):
        redis_socket = ScriptedSocket(
            [
                b"+OK\r\n",
                b"+PONG\r\n",
            ]
        )

        async def redis_job(task):
            client = SmallRedisClient(
                task,
                host="redis.local",
                use_tls=True,
                server_hostname="redis.local",
                tls_ca_file="tests/fixtures/ca.pem",
                tls_verify=False,
                username="smallos",
                password="testpassword",
            )
            await client.connect()
            pong = await client.ping()
            client.close()
            return pong

        root = SmallTask(2, redis_job, name="redis_auth_tls_job")
        _, kernel = self.build_os(redis_socket, root)

        self.assertEqual("PONG", root.result)
        self.assertEqual(
            [
                {
                    "sock": redis_socket,
                    "server_hostname": "redis.local",
                    "tls_ca_file": "tests/fixtures/ca.pem",
                    "tls_cert_file": None,
                    "tls_key_file": None,
                    "tls_verify": False,
                }
            ],
            kernel.tls_wrap_calls,
        )
        self.assertEqual(
            [
                b"*3\r\n$4\r\nAUTH\r\n$7\r\nsmallos\r\n$12\r\ntestpassword\r\n",
                b"*1\r\n$4\r\nPING\r\n",
            ],
            redis_socket.sent,
        )

    def test_redis_client_inherits_limits_from_runtime_config(self):
        redis_socket = ScriptedSocket(
            [
                b"$6\r\nvalue!\r\n",
            ]
        )

        async def redis_job(task):
            client = SmallRedisClient(task, host="redis.local")
            return await client.get("key")

        root = SmallTask(2, redis_job, name="redis_config_limits")
        self.build_os(
            redis_socket,
            root,
            config={
                "client_defaults": {
                    "redis": {"max_response_size": 4},
                }
            },
        )

        self.assertIsInstance(root.exception, RedisError)
        self.assertIn("max_response_size", str(root.exception))

    def test_mqtt_client_supports_connect_subscribe_receive_and_publish(self):
        mqtt_socket = ScriptedSocket(
            [
                b"\x20\x02\x00\x00",
                b"\x90\x03\x00\x01\x00",
                b"\x30\x08\x00\x04demohi",
            ]
        )

        async def mqtt_job(task):
            client = SmallMQTTClient(task, host="broker.local", client_id="demo-client")
            await client.connect()
            suback = await client.subscribe("demo")
            message = await client.receive_message()
            await client.publish("demo", "ok")
            await client.disconnect()
            return (suback["granted_qos"], message["topic"], message["payload"])

        root = SmallTask(2, mqtt_job, name="mqtt_job")
        self.build_os(mqtt_socket, root)

        self.assertEqual((0, "demo", "hi"), root.result)
        self.assertTrue(mqtt_socket.closed)
        self.assertEqual(
            [
                b"\x10\x17\x00\x04MQTT\x04\x02\x00<\x00\x0bdemo-client",
                b"\x82\x09\x00\x01\x00\x04demo\x00",
                b"\x30\x08\x00\x04demook",
                b"\xE0\x00",
            ],
            mqtt_socket.sent,
        )

    def test_mqtt_client_supports_qos1_publish(self):
        mqtt_socket = ScriptedSocket(
            [
                b"\x20\x02\x00\x00",
                b"\x40\x02\x00\x01",
            ]
        )

        async def mqtt_job(task):
            client = SmallMQTTClient(task, host="broker.local", client_id="demo-client")
            await client.connect()
            publish_info = await client.publish("demo", "ok", qos=1)
            await client.disconnect()
            return publish_info

        root = SmallTask(2, mqtt_job, name="mqtt_qos1_publish")
        self.build_os(mqtt_socket, root)

        self.assertEqual({"qos": 1, "packet_id": 1}, root.result)
        self.assertTrue(mqtt_socket.closed)
        self.assertEqual(
            [
                b"\x10\x17\x00\x04MQTT\x04\x02\x00<\x00\x0bdemo-client",
                b"\x32\x0A\x00\x04demo\x00\x01ok",
                b"\xE0\x00",
            ],
            mqtt_socket.sent,
        )

    def test_mqtt_client_supports_auth_and_tls_options(self):
        mqtt_socket = ScriptedSocket(
            [
                b"\x20\x02\x00\x00",
            ]
        )

        async def mqtt_job(task):
            client = SmallMQTTClient(
                task,
                host="broker.local",
                client_id="demo-client",
                use_tls=True,
                server_hostname="broker.local",
                tls_ca_file="tests/fixtures/ca.pem",
                username="smallos",
                password="testpassword",
            )
            await client.connect()
            await client.disconnect()
            return "connected"

        root = SmallTask(2, mqtt_job, name="mqtt_auth_tls_connect")
        _, kernel = self.build_os(mqtt_socket, root)

        self.assertEqual("connected", root.result)
        self.assertEqual(
            [
                {
                    "sock": mqtt_socket,
                    "server_hostname": "broker.local",
                    "tls_ca_file": "tests/fixtures/ca.pem",
                    "tls_cert_file": None,
                    "tls_key_file": None,
                    "tls_verify": True,
                }
            ],
            kernel.tls_wrap_calls,
        )
        self.assertEqual(
            [
                b"\x10.\x00\x04MQTT\x04\xc2\x00<\x00\x0bdemo-client\x00\x07smallos\x00\x0ctestpassword",
                b"\xE0\x00",
            ],
            mqtt_socket.sent,
        )

    def test_mqtt_client_acks_inbound_qos1_messages(self):
        mqtt_socket = ScriptedSocket(
            [
                b"\x20\x02\x00\x00",
                b"\x90\x03\x00\x01\x01",
                b"\x32\x0A\x00\x04demo\x00\x07hi",
            ]
        )

        async def mqtt_job(task):
            client = SmallMQTTClient(task, host="broker.local", client_id="demo-client")
            await client.connect()
            suback = await client.subscribe("demo", qos=1)
            message = await client.receive_message()
            await client.disconnect()
            return (suback["granted_qos"], message["topic"], message["payload"], message["qos"])

        root = SmallTask(2, mqtt_job, name="mqtt_qos1_inbound")
        self.build_os(mqtt_socket, root)

        self.assertEqual((1, "demo", "hi", 1), root.result)
        self.assertTrue(mqtt_socket.closed)
        self.assertEqual(
            [
                b"\x10\x17\x00\x04MQTT\x04\x02\x00<\x00\x0bdemo-client",
                b"\x82\x09\x00\x01\x00\x04demo\x01",
                b"\x40\x02\x00\x07",
                b"\xE0\x00",
            ],
            mqtt_socket.sent,
        )

    def test_mqtt_client_supports_qos2_publish_and_receive(self):
        mqtt_socket = ScriptedSocket(
            [
                b"\x20\x02\x00\x00",
                b"\x90\x03\x00\x01\x02",
                b"\x50\x02\x00\x02",
                b"\x70\x02\x00\x02",
                b"\x34\x0A\x00\x04demo\x00\x0Bhi",
                b"\x62\x02\x00\x0B",
            ]
        )

        async def mqtt_job(task):
            client = SmallMQTTClient(task, host="broker.local", client_id="demo-client")
            await client.connect()
            suback = await client.subscribe("demo", qos=2)
            publish_info = await client.publish("demo", "ok", qos=2)
            message = await client.receive_message()
            await client.disconnect()
            return (suback["granted_qos"], publish_info["packet_id"], message["topic"], message["payload"], message["qos"])

        root = SmallTask(2, mqtt_job, name="mqtt_qos2_flow")
        self.build_os(mqtt_socket, root)

        self.assertEqual((2, 2, "demo", "hi", 2), root.result)
        self.assertTrue(mqtt_socket.closed)
        self.assertEqual(
            [
                b"\x10\x17\x00\x04MQTT\x04\x02\x00<\x00\x0bdemo-client",
                b"\x82\x09\x00\x01\x00\x04demo\x02",
                b"\x34\x0A\x00\x04demo\x00\x02ok",
                b"\x62\x02\x00\x02",
                b"\x50\x02\x00\x0B",
                b"\x70\x02\x00\x0B",
                b"\xE0\x00",
            ],
            mqtt_socket.sent,
        )

    def test_mqtt_client_inherits_keepalive_and_packet_limits_from_runtime_config(self):
        mqtt_socket = ScriptedSocket(
            [
                b"\x20\x02\x00\x00",
                b"\x30\x08\x00\x04demohi",
            ]
        )

        async def mqtt_job(task):
            client = SmallMQTTClient(task, host="broker.local", client_id="demo-client")
            await client.connect()
            return await client.receive_message()

        root = SmallTask(2, mqtt_job, name="mqtt_config_limits")
        self.build_os(
            mqtt_socket,
            root,
            config={
                "client_defaults": {
                    "mqtt": {
                        "keepalive": 7,
                        "max_packet_size": 4,
                    }
                }
            },
        )

        self.assertEqual(
            b"\x10\x17\x00\x04MQTT\x04\x02\x00\x07\x00\x0bdemo-client",
            mqtt_socket.sent[0],
        )
        self.assertIsInstance(root.exception, MQTTProtocolError)
        self.assertIn("max_packet_size", str(root.exception))

    def test_sse_client_supports_connect_and_event_parsing(self):
        sse_socket = ScriptedSocket(
            [
                b"HTTP/1.1 200 OK\r\n",
                b"Content-Type: text/event-stream\r\n",
                b"Cache-Control: no-cache\r\n",
                b"\r\n",
                b"id: 7\n",
                b"event: tick\n",
                b"data: one\n",
                b"data: two\n",
                b"\n",
            ]
        )

        async def sse_job(task):
            client = SmallSSEClient(task, host="events.local", port=8080)
            await client.connect("/stream", params={"topic": "demo"})
            event = await client.read_event()
            client.close()
            return event

        root = SmallTask(2, sse_job, name="sse_job")
        self.build_os(sse_socket, root)

        self.assertEqual(
            {"event": "tick", "data": "one\ntwo", "id": "7"},
            root.result,
        )
        self.assertTrue(sse_socket.closed)
        self.assertIn(b"GET /stream?topic=demo HTTP/1.1", sse_socket.sent[0])
        self.assertIn(b"Accept: text/event-stream\r\n", sse_socket.sent[0])

    def test_sse_client_inherits_max_event_size_from_runtime_config(self):
        sse_socket = ScriptedSocket(
            [
                b"HTTP/1.1 200 OK\r\n",
                b"Content-Type: text/event-stream\r\n",
                b"\r\n",
                b"data: 123456\n",
                b"\n",
            ]
        )

        async def sse_job(task):
            client = SmallSSEClient(task, host="events.local")
            await client.connect("/events")
            try:
                await client.read_event()
            finally:
                client.close()

        root = SmallTask(2, sse_job, name="sse_limit_job")
        self.build_os(
            sse_socket,
            root,
            config={
                "client_defaults": {
                    "sse": {"max_event_size": 5},
                }
            },
        )

        self.assertIsInstance(root.exception, SSEProtocolError)
        self.assertIn("max_event_size", str(root.exception))

    def test_websocket_client_supports_handshake_send_and_receive(self):
        ws_socket = ScriptedSocket(
            [
                b"HTTP/1.1 101 Switching Protocols\r\n",
                b"Upgrade: websocket\r\n",
                b"Connection: Upgrade\r\n",
                b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n",
                b"\r\n",
                b"\x81\x05hello",
            ]
        )

        async def ws_job(task):
            client = SmallWebSocketClient(
                task,
                host="ws.local",
                port=9000,
                client_key="dGhlIHNhbXBsZSBub25jZQ==",
            )
            await client.connect("/chat", params={"room": "demo"})
            await client.send_text("ping")
            message = await client.receive()
            await client.disconnect()
            return message

        root = SmallTask(2, ws_job, name="ws_job")
        self.build_os(ws_socket, root)

        self.assertEqual({"type": "text", "data": "hello"}, root.result)
        self.assertTrue(ws_socket.closed)
        self.assertIn(b"GET /chat?room=demo HTTP/1.1", ws_socket.sent[0])
        self.assertIn(b"Upgrade: websocket\r\n", ws_socket.sent[0])

        text_frame = _decode_client_frame(ws_socket.sent[1])
        close_frame = _decode_client_frame(ws_socket.sent[2])
        self.assertEqual(0x1, text_frame["opcode"])
        self.assertEqual(b"ping", text_frame["payload"])
        self.assertEqual(0x8, close_frame["opcode"])

    def test_websocket_client_auto_replies_to_ping_frames(self):
        ws_socket = ScriptedSocket(
            [
                b"HTTP/1.1 101 Switching Protocols\r\n",
                b"Upgrade: websocket\r\n",
                b"Connection: Upgrade\r\n",
                b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n",
                b"\r\n",
                b"\x89\x01z",
                b"\x81\x02ok",
            ]
        )

        async def ws_job(task):
            client = SmallWebSocketClient(
                task,
                host="ws.local",
                client_key="dGhlIHNhbXBsZSBub25jZQ==",
            )
            await client.connect("/ws")
            message = await client.receive()
            await client.disconnect()
            return message

        root = SmallTask(2, ws_job, name="ws_ping_job")
        self.build_os(ws_socket, root)

        self.assertEqual({"type": "text", "data": "ok"}, root.result)
        pong_frame = _decode_client_frame(ws_socket.sent[1])
        self.assertEqual(0xA, pong_frame["opcode"])
        self.assertEqual(b"z", pong_frame["payload"])

    def test_websocket_client_inherits_max_frame_size_from_runtime_config(self):
        payload = b"x" * 130
        ws_socket = ScriptedSocket(
            [
                b"HTTP/1.1 101 Switching Protocols\r\n",
                b"Upgrade: websocket\r\n",
                b"Connection: Upgrade\r\n",
                b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n",
                b"\r\n",
                b"\x81\x7E\x00\x82" + payload,
            ]
        )

        async def ws_job(task):
            client = SmallWebSocketClient(
                task,
                host="ws.local",
                client_key="dGhlIHNhbXBsZSBub25jZQ==",
            )
            await client.connect("/big")
            try:
                await client.receive()
            finally:
                client.close()

        root = SmallTask(2, ws_job, name="ws_limit_job")
        self.build_os(
            ws_socket,
            root,
            config={
                "client_defaults": {
                    "websocket": {"max_frame_size": 64},
                }
            },
        )

        self.assertIsInstance(root.exception, WebSocketProtocolError)
        self.assertIn("max_frame_size", str(root.exception))


if __name__ == "__main__":
    unittest.main()
