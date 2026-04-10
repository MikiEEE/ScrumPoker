# smallOS Clients

The client helpers in this folder are the "batteries included" networking layer
for `smallOS`.

They are all built on the same cooperative transport model:
- one thread
- no `asyncio` event loop ownership
- sockets suspended and resumed by the smallOS scheduler
- the same kernel surface used on Unix today and targeted for MicroPython

If you already know the runtime and just want protocol-level usage, this is the
best entry point.

## Design Goals

These clients aim to be:
- easy to use from `async def` smallOS tasks
- portable across desktop Python and future MicroPython kernels
- dependency-light
- explicit about transport and scheduling behavior

They do not try to be drop-in replacements for full desktop networking stacks.
The tradeoff is intentional: the runtime stays simple, predictable, and under
smallOS control.

## Shared Transport Model

All protocol helpers are built on top of
[SmallStream.py](SmallStream.py), which provides:
- non-blocking connect
- optional TLS handshake layered through the active kernel
- cooperative `send_all`
- cooperative `read_exactly`
- cooperative `read_until`

That means every client here inherits the same benefits:
- tasks yield when a socket is not ready
- the scheduler can keep running higher-priority work
- no background threads are required

All of the clients also honor `SmallOSConfig.client_defaults` when they are
constructed from an attached task. That lets you set project-wide defaults for
things like stream buffer limits, HTTP response caps, Redis parser limits, and
MQTT keepalive / packet limits in one place.

## Available Clients

### SmallStream

File:
[SmallStream.py](SmallStream.py)

This is the low-level byte-stream helper used internally by the higher-level
clients. You would reach for `SmallStream` directly when:
- you are implementing a new protocol client
- you want exact control over the wire format
- Redis / HTTP / MQTT are too high-level for your use case

Core methods:
- `await stream.connect()`
- `await stream.send_all(data)`
- `await stream.recv_some(size=4096)`
- `await stream.read_exactly(size)`
- `await stream.read_until(delimiter)`
- `stream.close()`

TLS knobs:
- `use_tls`
- `server_hostname`
- `tls_ca_file`
- `tls_cert_file`
- `tls_key_file`
- `tls_verify`
- `max_buffer_size`

## SmallHTTPClient

File:
[SmallHTTP.py](SmallHTTP.py)

`SmallHTTPClient` is the easiest way to make outbound web requests from
smallOS tasks.

Current scope:
- HTTP/1.1 over TCP or TLS
- `GET`, `POST`, `PUT`, `PATCH`, and `DELETE`
- query-string building
- string, bytes, form, and JSON request bodies
- response parsing for:
  - `Content-Length`
  - `Transfer-Encoding: chunked`
  - connection-close bodies

Important behavior:
- one request uses one stream
- the stream is closed after the response is read
- this keeps behavior simple and portable

Typical example:

```python
from SmallPackage import SmallHTTPClient, SmallOS, SmallTask, Unix


async def fetch_status(task):
    client = SmallHTTPClient(
        task,
        base_url="https://example.com",
        tls_ca_file="certs/ca.crt",   # optional for private CAs on Unix
    )
    response = await client.get("/", params={"demo": True})
    task.OS.print("{} {}\n".format(response.status_code, response.reason))
    task.OS.print(response.text()[:120] + "\n")
    return response.ok


runtime = SmallOS().setKernel(Unix())
runtime.fork([SmallTask(2, fetch_status, name="fetch_status")])
runtime.startOS()
```

Constructor options:
- `base_url`
- `host`
- `port`
- `use_tls`
- `server_hostname`
- `default_headers`
- `tls_ca_file`
- `tls_cert_file`
- `tls_key_file`
- `tls_verify`
- `max_response_size`
- `max_buffer_size`

Common request methods:
- `await client.get(path="", headers=None, params=None)`
- `await client.post(path="", headers=None, params=None, data=None, json_body=None)`
- `await client.put(...)`
- `await client.patch(...)`
- `await client.delete(...)`

Response object:
- `response.status_code`
- `response.reason`
- `response.headers`
- `response.body`
- `response.ok`
- `response.text()`
- `response.json()`

Notes:
- if you pass `json_body=...`, the client sets `Content-Type: application/json`
- if you pass mapping-style `data=...`, the client sends
  `application/x-www-form-urlencoded`
- `Connection: close` is sent by default

## SmallRedisClient

File:
[SmallRedis.py](SmallRedis.py)

`SmallRedisClient` is a focused RESP client for smallOS tasks.

Current scope:
- connect
- optional `AUTH`
- optional `SELECT`
- generic command execution
- convenience helpers:
  - `ping`
  - `get`
  - `set`
  - `delete`
  - `publish`
  - `subscribe`

Typical example:

```python
from SmallPackage import SmallRedisClient


async def redis_job(task):
    client = SmallRedisClient(
        task,
        host="127.0.0.1",
        port=6379,
        password="testpassword",      # optional
        use_tls=True,                 # optional
        server_hostname="localhost",  # optional
        tls_ca_file="certs/ca.crt",   # optional for private CAs on Unix
    )
    await client.connect()
    await client.set("smallos:key", "hello")
    value = await client.get("smallos:key")
    client.close()
    return value
```

Useful methods:
- `await client.connect()`
- `await client.command(*parts)`
- `await client.ping(message=None)`
- `await client.get(key)`
- `await client.set(key, value)`
- `await client.delete(*keys)`
- `await client.publish(channel, message)`
- `await client.subscribe(*channels)`
- `await client.read_pubsub_event()`
- `client.close()`

Constructor tuning:
- `max_response_size`
- `max_nesting_depth`
- `max_buffer_size`

## SmallMQTTClient

File:
[SmallMQTT.py](SmallMQTT.py)

`SmallMQTTClient` is a smallOS-native MQTT 3.1.1 client aimed at common device
and hobbyist messaging workflows.

Current scope:
- connect / disconnect
- username/password auth
- TLS transport
- publish QoS 0 / 1 / 2
- subscribe QoS 0 / 1 / 2
- inbound message receive with the required ack flows

Typical example:

```python
from SmallPackage import SmallMQTTClient


async def mqtt_job(task):
    client = SmallMQTTClient(
        task,
        host="127.0.0.1",
        port=1883,
        client_id="smallos-demo",
        username="smallos",           # optional
        password="testpassword",      # optional
        use_tls=True,                 # optional
        server_hostname="localhost",  # optional
        tls_ca_file="certs/ca.crt",   # optional for private CAs on Unix
    )
    await client.connect()
    await client.subscribe("smallos/demo", qos=1)
    await client.publish("smallos/demo", "hello from smallOS", qos=1)
    message = await client.receive_message()
    await client.disconnect()
    return message
```

Useful methods:
- `await client.connect()`
- `await client.disconnect()`
- `await client.ping()`
- `await client.publish(topic, payload, qos=0, retain=False)`
- `await client.subscribe(topic, qos=0)`
- `await client.receive_message()`

Constructor tuning:
- `keepalive`
- `max_packet_size`
- `max_queued_messages`
- `max_buffer_size`

## SmallSSEClient

File:
[SmallHTTP.py](SmallHTTP.py)

`SmallSSEClient` keeps a cooperative `text/event-stream` connection open and
parses SSE events without threads.

Current scope:
- long-lived HTTP GET stream connection
- `id`, `event`, `data`, and `retry` SSE fields
- per-event size caps and per-line size caps

Typical example:

```python
from SmallPackage import SmallSSEClient


async def sse_job(task):
    client = SmallSSEClient(task, base_url="http://127.0.0.1:9000")
    await client.connect("/events", params={"topic": "smallos"})
    event = await client.read_event()
    client.close()
    return event
```

Useful methods:
- `await client.connect(path="", headers=None, params=None, last_event_id=None)`
- `await client.read_event()`
- `client.close()`

Constructor tuning:
- `max_event_size`
- `max_line_size`
- `max_buffer_size`

## SmallWebSocketClient

File:
[SmallWebSocket.py](SmallWebSocket.py)

`SmallWebSocketClient` provides cooperative WebSocket messaging on top of
`SmallStream`.

Current scope:
- ws/wss handshake (RFC 6455, version 13)
- text and binary sends
- receive text/binary messages with fragmentation support
- automatic pong replies to server ping frames
- close/disconnect helpers

Typical example:

```python
from SmallPackage import SmallWebSocketClient


async def ws_job(task):
    client = SmallWebSocketClient(task, base_url="ws://127.0.0.1:9001")
    await client.connect("/chat")
    await client.send_text("hello")
    message = await client.receive()
    await client.disconnect()
    return message
```

Useful methods:
- `await client.connect(path=None, params=None, headers=None, subprotocols=None, origin=None)`
- `await client.send_text(text)`
- `await client.send_binary(data)`
- `await client.ping(payload=b"")`
- `await client.receive()`
- `await client.disconnect(code=1000, reason="")`
- `client.close()`

Constructor tuning:
- `max_frame_size`
- `max_message_size`
- `max_line_size`
- `max_buffer_size`

## TLS and Auth Notes

All current client helpers share the same transport-level TLS pattern.

Common TLS-related options:
- `use_tls=True`
- `server_hostname="localhost"`
- `tls_ca_file="path/to/ca.crt"`
- `tls_cert_file="path/to/client.crt"`
- `tls_key_file="path/to/client.key"`
- `tls_verify=False` for controlled local testing only

Current behavior:
- Unix supports custom CA paths through the Python `ssl` context
- client certificate and key paths are accepted at the transport layer
- MicroPython ports vary more by TLS implementation, so availability may differ

Current auth support by client:
- HTTP: protocol-level auth headers are left to the caller
- Redis: username/password via `AUTH`
- MQTT: username/password in `CONNECT`

## When To Use Which Layer

Use `SmallHTTPClient` when:
- you want easy request/response web calls
- you do not want to handcraft sockets and request lines

Use `SmallRedisClient` when:
- you want a lightweight Redis integration inside the smallOS scheduler

Use `SmallMQTTClient` when:
- you want broker messaging without threads or `asyncio`

Use `SmallSSEClient` when:
- you want long-lived server push over HTTP event streams

Use `SmallWebSocketClient` when:
- you want bidirectional low-latency messaging over one socket

Use `SmallStream` when:
- you are implementing your own protocol
- you need exact byte-level control

## Testing

The repository includes unit coverage for the protocol helpers in:
[tests/test_protocol_clients.py](../../tests/test_protocol_clients.py)

There is also a local-only integration compose setup under `.local/` that can
be used to bring up Redis and Mosquitto for live manual testing on desktop
Python without committing those test fixtures to git.
