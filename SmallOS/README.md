# smallOS

`smallOS` is a lightweight cooperative runtime for priority-oriented task
management.

It is designed around three ideas:
- write tasks with modern `async` / `await` syntax
- keep scheduling policy owned by `smallOS`, not `asyncio`
- stay portable enough to run on desktop Python today and MicroPython boards
  later

## Status

The project is currently experimental but usable. The runtime core supports:
- priority-based cooperative scheduling
- task spawning, `join`, and `join_all`
- signal-based wakeups
- time-based sleeping
- readiness-based socket/I/O waiting
- generic TCP/TLS kernel hooks for higher-level protocols
- smallOS-native HTTP, Redis, MQTT, SSE, and WebSocket helper clients

## Why smallOS?

Python's `asyncio` gives great syntax, but it also brings its own scheduler and
event-loop policy. `smallOS` keeps the syntax while swapping in a custom
runtime, so tasks can be scheduled with project-specific priority rules and a
smaller portability surface.

That makes it a good fit for:
- robotics or device-control projects
- embedded experiments on MicroPython boards
- custom runtimes where task priority matters
- learning how coroutine scheduling works under the hood

## Project Layout

- [SmallPackage/SmallOS.py](SmallPackage/SmallOS.py):
  cooperative scheduler
- [SmallPackage/SmallTask.py](SmallPackage/SmallTask.py):
  task lifecycle, coroutine stepping, join bookkeeping
- [SmallPackage/Kernel.py](SmallPackage/Kernel.py):
  desktop and MicroPython kernel abstractions
- [SmallPackage/SmallIO.py](SmallPackage/SmallIO.py):
  buffered app/shell output routing and terminal-mode helpers
- [SmallPackage/clients](SmallPackage/clients):
  protocol client package for cooperative network integrations
- [SmallPackage/clients/README.md](SmallPackage/clients/README.md):
  detailed client-specific guide and API notes
- [SmallPackage/clients/SmallHTTP.py](SmallPackage/clients/SmallHTTP.py):
  dependency-free HTTP and SSE clients for smallOS tasks
- [SmallPackage/clients/SmallStream.py](SmallPackage/clients/SmallStream.py):
  cooperative socket stream helper for protocol clients
- [SmallPackage/clients/SmallRedis.py](SmallPackage/clients/SmallRedis.py):
  dependency-free Redis client for smallOS tasks
- [SmallPackage/clients/SmallMQTT.py](SmallPackage/clients/SmallMQTT.py):
  dependency-free MQTT client for smallOS tasks
- [SmallPackage/clients/SmallWebSocket.py](SmallPackage/clients/SmallWebSocket.py):
  dependency-free WebSocket client for bidirectional messaging
- [SmallPackage/SmallConfig.py](SmallPackage/SmallConfig.py):
  runtime configuration loader/container
- [smallos.config.json](smallos.config.json):
  repo-level runtime defaults
- [SmallPackage/shells.py](SmallPackage/shells.py):
  command shell helpers for runtime inspection and demos
- [demos](demos):
  desktop and board-specific demo entry points
- [tests](tests):
  unit tests for scheduler, kernel, config, and supporting structures

## Installation

Desktop development:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Run the test suite:

```bash
python3 -m unittest discover -s tests -v
```

## Quick Start

Minimal desktop runtime:

```python
from SmallPackage.Kernel import Unix
from SmallPackage.SmallConfig import SmallOSConfig
from SmallPackage.SmallOS import SmallOS
from SmallPackage.SmallTask import SmallTask


async def hello(task):
    task.OS.print("hello from smallOS\n")
    await task.sleep(0.1)
    return "done"


config = SmallOSConfig.from_json_file("smallos.config.json")
runtime = SmallOS(config=config).setKernel(Unix())
runtime.setErrorHandler(
    lambda event: print(
        "[smallOS] task failure in {} (PID {}): {}".format(
            event["task_name"] or "unnamed task",
            event["task_id"],
            event["exception_repr"],
        )
    )
)
runtime.fork([SmallTask(2, hello, name="hello")])
runtime.startOS()
```

## Runtime Error Handling

`smallOS` now supports a runtime-level error observer through
`runtime.setErrorHandler(handler, include_cancelled=False)`.

Use it when you want:
- readable debug output for uncaught task failures
- lightweight cleanup or bookkeeping at the runtime boundary
- a single place to surface task errors without crashing the scheduler

The handler is synchronous and receives a failure-event dictionary after the
task has been finalized. Current event fields include:
- `task_id`
- `task_name`
- `parent_id`
- `exception`
- `exception_type`
- `exception_repr`
- `is_cancelled`
- `blocked_reason`
- `waiting_signal`
- `io_wait_mode`
- `join_target_id`
- `join_pending_ids`
- `traceback_text`

By default, `TaskCancelledError` does not trigger the handler. Pass
`include_cancelled=True` if you want cancellation events too.

Example:

```python
from SmallPackage.Kernel import Unix
from SmallPackage.SmallOS import SmallOS


def log_runtime_error(event):
    print(
        "[smallOS] task failure in {} (PID {}): {}".format(
            event["task_name"] or "unnamed task",
            event["task_id"],
            event["exception_repr"],
        )
    )
    if event["traceback_text"]:
        print(event["traceback_text"], end="")


runtime = SmallOS().setKernel(Unix())
runtime.setErrorHandler(log_runtime_error)
```

### Closed or Invalid File Descriptors

Closed or invalid file descriptors used in `wait_readable(...)` or
`wait_writable(...)` no longer crash the whole scheduler through the platform
poll/select layer.

Instead:
- the kernel validates the watched object before polling
- the waiting task receives a normal exception such as `ValueError`
- the runtime finalizes that task cleanly
- your runtime error handler can log or clean up the failure gracefully

If you do not install an error handler, the task still fails cleanly and the
runtime keeps its internal state consistent, but adding `setErrorHandler(...)`
is the recommended way to make these failures visible in applications.

## Configuration

The runtime now uses a first-class config object backed by
[smallos.config.json](smallos.config.json).

Current config fields:
- `task_capacity`: maximum tracked tasks / PID slots
- `priority_levels`: number of ready-queue categories
- `io_buffer_length`: buffered app output length when the terminal view is hidden
- `eternal_watchers`: keep the runtime alive when only watcher tasks remain
- `client_defaults`: shared defaults for cooperative clients and streams

Example:

```json
{
  "task_capacity": 1024,
  "priority_levels": 10,
  "io_buffer_length": 1024,
  "eternal_watchers": false,
  "client_defaults": {
    "stream": {
      "max_buffer_size": 16777216
    },
    "http": {
      "max_response_size": 16777216
    },
    "redis": {
      "max_response_size": 16777216,
      "max_nesting_depth": 32
    },
    "mqtt": {
      "keepalive": 60,
      "max_packet_size": 262144,
      "max_queued_messages": 1024
    }
  }
}
```

The config loader also accepts the aliases `oslist_length` and
`num_categories` so older notes and experiments can map cleanly onto the
current runtime.

Client constructors still accept explicit overrides, but when you create them
inside a task they now inherit these defaults from `task.OS.config` unless you
pass a value directly.

## Kernels and Board Profiles

Desktop kernel:
- `Unix`

MicroPython kernels:
- `MicroPythonKernel`
- `ESP32`
- `PicoW` / `RaspberryPiPicoW`
- `ESP8266` compatibility profile

You can either pick a board profile explicitly or let the runtime choose a
built-in profile from the firmware machine string:

```python
from SmallPackage.Kernel import ESP32, PicoW, build_micropython_kernel

kernel = ESP32(hostname="smallos-esp32")
kernel = PicoW(country="US", hostname="smallos-pico")
kernel = build_micropython_kernel()
```

## Demos

The new demos live in [demos](demos):
- [demos/unix_demo.py](demos/unix_demo.py):
  desktop scheduler demo
- [demos/esp32_demo.py](demos/esp32_demo.py):
  ESP32-oriented startup and optional Wi-Fi bring-up example
- [demos/pico_w_demo.py](demos/pico_w_demo.py):
  Pico W oriented startup and Wi-Fi configuration example
- [demos/micropython_autodetect_demo.py](demos/micropython_autodetect_demo.py):
  automatic MicroPython kernel selection
- [demos/runtime_demo.py](demos/runtime_demo.py):
  migrated home for the original root-level runtime showcase
- [demos/shell_demo.py](demos/shell_demo.py):
  scripted shell session running alongside other cooperative tasks
- [demos/redis_demo.py](demos/redis_demo.py):
  Redis example built on the native cooperative client
- [demos/http_demo.py](demos/http_demo.py):
  HTTP example built on the native cooperative client
- [demos/web_app_demo.py](demos/web_app_demo.py):
  cooperative single-thread web app demo with HTTP routes, live browser UI, and shell-driven server shutdown
- [demos/mqtt_demo.py](demos/mqtt_demo.py):
  MQTT example built on the native cooperative client

All of the shared demo entry points now install a default runtime error handler
through [demos/common.py](demos/common.py). That means network failures,
invalid I/O wait objects, and other uncaught task exceptions are reported as
readable task-failure diagnostics instead of looking like abrupt scheduler
crashes or silent exits.

The original root demo remains available in
[demo.py](demo.py) as a compatibility wrapper around
[demos/runtime_demo.py](demos/runtime_demo.py).

## Runtime Model

`smallOS` is intentionally small in scope:
- tasks are `async def` coroutines wrapped in `SmallTask`
- task code awaits smallOS-owned awaitables such as `task.sleep(...)`,
  `task.wait_signal(...)`, `task.wait_readable(...)`, and `task.join(...)`
- the scheduler steps coroutines directly and decides when each task becomes
  runnable again
- kernels provide timing, output, and readiness-based transport primitives

This means arbitrary `asyncio` libraries are not drop-in compatible with the
runtime, but it also means scheduling policy and portability stay under your
control.

## Running on MicroPython

For MicroPython targets, the intended flow is:
1. choose `ESP32`, `PicoW`, or `build_micropython_kernel()`
2. optionally connect Wi-Fi through the kernel helper
3. build `SmallOS(config=...)`
4. fork tasks and start the runtime

The kernel layer is deliberately generic. Protocol clients such as HTTPS,
Redis, MQTT, RabbitMQ/AMQP, and Kafka should be built on top of the shared
TCP/TLS socket surface rather than requiring protocol-specific kernel methods.

## Clients

The current setup now includes first-party smallOS-native helpers for HTTP,
Redis, and MQTT, so users can stay inside the smallOS scheduler instead of
dropping down to raw sockets or depending on `asyncio`-owned clients.

Available helpers:
- `SmallHTTPClient`
- `SmallRedisClient`
- `SmallMQTTClient`

Current scope:
- HTTP: request/response helper with query params, JSON bodies, TLS, and
  chunked/content-length response parsing
- Redis: RESP command execution plus helpers like `ping`, `get`, `set`,
  `delete`, `publish`, and `subscribe`
- MQTT: MQTT 3.1.1 connect/disconnect, publish at QoS 0/1/2, subscribe at
  QoS 0/1/2, and inbound message receive with PUBACK/PUBREC/PUBREL/PUBCOMP
  handling as required by the protocol
- Both clients: optional username/password auth and TLS transport setup, with
  Unix support for custom CA and client certificate paths through
  `tls_ca_file`, `tls_cert_file`, and `tls_key_file`

For detailed examples, constructor options, response helpers, and transport
notes, see
[SmallPackage/clients/README.md](SmallPackage/clients/README.md).

## Contributing

Contributions, issues, experiments, and board-port notes are welcome. Good
areas for contribution include:
- new MicroPython port validation
- higher-level protocol clients built on the transport layer
- shell and debugging tools
- more board demos and deployment examples
- additional scheduler tests and edge-case coverage

## License

This project is licensed under the MIT License. See
[LICENSE](LICENSE).
