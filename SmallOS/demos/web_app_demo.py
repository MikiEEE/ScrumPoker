"""SmallOS demo web app using a cooperative single-threaded HTTP server."""

from datetime import datetime
import json

from common import build_runtime

from SmallPackage import SmallTask, Unix
from SmallPackage.shells import BaseShell
from SmallPackage.SmallErrors import TaskCancelledError


HOST = "127.0.0.1"
PORT = 8081
LISTEN_BACKLOG = 16
REQUEST_HEADER_LIMIT = 8 * 1024
METRICS_INTERVAL_SECONDS = 5
STATE_TICK_INTERVAL_SECONDS = 1


def _http_reason(status_code):
    """Return a short reason phrase for the selected status code."""
    reasons = {
        200: "OK",
        404: "Not Found",
        405: "Method Not Allowed",
        413: "Payload Too Large",
        500: "Internal Server Error",
    }
    return reasons.get(status_code, "OK")


def _json_bytes(value):
    """Encode a Python object into compact JSON bytes."""
    return json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _http_response(status_code, body, content_type="text/plain; charset=utf-8"):
    """Build one complete HTTP/1.1 response payload."""
    if isinstance(body, str):
        body_bytes = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray, memoryview)):
        body_bytes = bytes(body)
    else:
        raise TypeError("HTTP response body must be str or bytes-like.")

    lines = [
        "HTTP/1.1 {} {}".format(status_code, _http_reason(status_code)),
        "Content-Type: {}".format(content_type),
        "Content-Length: {}".format(len(body_bytes)),
        "Connection: close",
        "Cache-Control: no-store",
        "",
        "",
    ]
    return "\r\n".join(lines).encode("utf-8") + body_bytes


def _home_page():
    """Render a tiny browser UI that polls the API routes."""
    return """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>smallOS Web App Demo</title>
  <style>
    :root {
      --bg: #0f172a;
      --card: #111827;
      --line: #1f2937;
      --text: #e5e7eb;
      --muted: #9ca3af;
      --accent: #22d3ee;
    }
    body {
      margin: 0;
      font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, "Liberation Mono", monospace;
      background: radial-gradient(circle at top right, #111827 0%, var(--bg) 55%);
      color: var(--text);
      min-height: 100vh;
      display: grid;
      place-items: center;
      padding: 24px;
      box-sizing: border-box;
    }
    .app {
      width: min(760px, 100%);
      border: 1px solid var(--line);
      border-radius: 14px;
      background: color-mix(in srgb, var(--card) 92%, black);
      box-shadow: 0 20px 50px rgba(0, 0, 0, 0.35);
      overflow: hidden;
    }
    .header {
      border-bottom: 1px solid var(--line);
      padding: 16px 20px;
    }
    .header h1 {
      margin: 0;
      font-size: 1.1rem;
      color: var(--accent);
    }
    .header p {
      margin: 6px 0 0;
      color: var(--muted);
      font-size: 0.92rem;
    }
    .grid {
      display: grid;
      gap: 12px;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      padding: 16px;
    }
    .card {
      border: 1px solid var(--line);
      border-radius: 10px;
      padding: 12px;
      background: rgba(15, 23, 42, 0.6);
    }
    .label {
      color: var(--muted);
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.05em;
      margin-bottom: 4px;
    }
    .value {
      font-size: 1.2rem;
      word-break: break-word;
    }
    .footer {
      border-top: 1px solid var(--line);
      padding: 10px 16px;
      color: var(--muted);
      font-size: 0.8rem;
    }
    @media (max-width: 620px) {
      .grid {
        grid-template-columns: 1fr;
      }
    }
  </style>
</head>
<body>
  <section class="app">
    <header class="header">
      <h1>smallOS Cooperative Web App</h1>
      <p>Live stats served by a one-thread smallOS scheduler.</p>
    </header>
    <main class="grid">
      <article class="card"><div class="label">Requests</div><div class="value" id="requests">-</div></article>
      <article class="card"><div class="label">Active Connections</div><div class="value" id="active">-</div></article>
      <article class="card"><div class="label">Uptime (s)</div><div class="value" id="uptime">-</div></article>
      <article class="card"><div class="label">Demo Value</div><div class="value" id="demo">-</div></article>
      <article class="card"><div class="label">Last Path</div><div class="value" id="lastPath">-</div></article>
      <article class="card"><div class="label">Server Time</div><div class="value" id="time">-</div></article>
    </main>
    <footer class="footer">Routes: <code>/</code>, <code>/api/stats</code>, <code>/api/time</code>, <code>/healthz</code></footer>
  </section>
  <script>
    async function refresh() {
      const [statsResponse, timeResponse] = await Promise.all([
        fetch('/api/stats'),
        fetch('/api/time'),
      ]);
      const stats = await statsResponse.json();
      const time = await timeResponse.json();
      document.getElementById('requests').textContent = stats.requests_total;
      document.getElementById('active').textContent = stats.active_connections;
      document.getElementById('uptime').textContent = stats.uptime_seconds;
      document.getElementById('demo').textContent = stats.demo_value;
      document.getElementById('lastPath').textContent = stats.last_path;
      document.getElementById('time').textContent = time.iso_utc;
    }
    refresh();
    setInterval(refresh, 1000);
  </script>
</body>
</html>
"""


async def _send_all(task, sock, data):
    """Write all bytes to a non-blocking socket using smallOS waits."""
    kernel = task.OS.kernel
    remaining = memoryview(bytes(data))

    while remaining:
        try:
            sent = kernel.socket_send(sock, remaining)
        except Exception as exc:
            if kernel.socket_needs_read(exc):
                await task.wait_readable(sock)
                continue
            if kernel.socket_needs_write(exc):
                await task.wait_writable(sock)
                continue
            raise

        if sent == 0:
            return
        remaining = remaining[sent:]


async def _read_request_head(task, sock):
    """Read HTTP headers until CRLFCRLF or until the safety cap is hit."""
    kernel = task.OS.kernel
    data = bytearray()

    while b"\r\n\r\n" not in data:
        try:
            chunk = kernel.socket_recv(sock, 1024)
        except Exception as exc:
            if kernel.socket_needs_read(exc):
                await task.wait_readable(sock)
                continue
            if kernel.socket_needs_write(exc):
                await task.wait_writable(sock)
                continue
            raise

        if not chunk:
            break

        data.extend(chunk)
        if len(data) > REQUEST_HEADER_LIMIT:
            raise ValueError("request headers exceeded {} bytes".format(REQUEST_HEADER_LIMIT))

    return bytes(data)


def _parse_request_line(raw_request):
    """Extract the HTTP method and target from the first request line."""
    if not raw_request:
        return None, None

    line = raw_request.split(b"\r\n", 1)[0].decode("iso-8859-1", errors="replace")
    parts = line.split(" ")
    if len(parts) < 2:
        return None, None
    return parts[0].upper(), parts[1]


def _build_stats(task, state):
    """Return one snapshot of app metrics for `/api/stats`."""
    uptime_ms = max(0, task.OS.kernel.scheduler_now_ms() - state["started_ms"])
    return {
        "uptime_seconds": round(uptime_ms / 1000, 3),
        "requests_total": state["requests_total"],
        "active_connections": state["active_connections"],
        "last_path": state["last_path"],
        "last_client": state["last_client"],
        "demo_value": state["demo_value"],
    }


def _pid_for_name(os_ref, name):
    """Return the PID for the first live task that matches ``name``."""
    for item in os_ref.tasks.list():
        if item.name == name:
            return item.getID()
    return None


async def web_client_handler(task, client_sock, client_addr, state):
    """Handle one inbound HTTP client request and close the connection."""
    kernel = task.OS.kernel
    state["active_connections"] += 1
    task.OS.print("Accepted connection from {} (active connections: {})\n".format(client_addr, state["active_connections"]))

    try:
        try:
            request_head = await _read_request_head(task, client_sock)
        except ValueError:
            await _send_all(task, client_sock, _http_response(413, "request header too large\n"))
            return

        method, target = _parse_request_line(request_head)
        if method is None:
            return

        path = target.split("?", 1)[0]
        state["requests_total"] += 1
        state["last_path"] = path
        state["last_client"] = str(client_addr)

        if method != "GET":
            payload = _http_response(405, "only GET is supported in this demo\n")
        elif path == "/":
            payload = _http_response(200, _home_page(), "text/html; charset=utf-8")
        elif path == "/api/stats":
            payload = _http_response(
                200,
                _json_bytes(_build_stats(task, state)),
                "application/json; charset=utf-8",
            )
        elif path == "/api/time":
            payload = _http_response(
                200,
                _json_bytes(
                    {
                        "epoch": task.OS.kernel.time_epoch(),
                        "iso_utc": datetime.utcnow().isoformat() + "Z",
                    }
                ),
                "application/json; charset=utf-8",
            )
        elif path == "/healthz":
            payload = _http_response(200, "ok\n")
        else:
            payload = _http_response(404, "route not found\n")

        await _send_all(task, client_sock, payload)
    finally:
        state["active_connections"] = max(0, state["active_connections"] - 1)
        kernel.socket_close(client_sock)
        task.OS.print("Closed connection from {} (active connections: {})\n".format(client_addr, state["active_connections"]))


async def app_state_task(task, state):
    """Update a little app state in the background to show concurrency."""
    while True:
        state["demo_value"] = (state["demo_value"] + 7) % 101
        await task.sleep(STATE_TICK_INTERVAL_SECONDS)


async def metrics_task(task, state):
    """Print periodic server metrics to the terminal."""
    while True:
        stats = _build_stats(task, state)
        task.OS.print(
            "[web_app] requests={} active={} uptime={}s demo_value={}\n".format(
                stats["requests_total"],
                stats["active_connections"],
                stats["uptime_seconds"],
                stats["demo_value"],
            )
        )
        await task.sleep(METRICS_INTERVAL_SECONDS)


async def web_server_task(task, state):
    """Run a small cooperative HTTP server on one non-blocking listener."""
    kernel = task.OS.kernel
    if kernel is None:
        raise RuntimeError("web_server_task requires a kernel-enabled runtime.")

    address_info = kernel.resolve_address(HOST, PORT)
    listener = kernel.socket_open(address_info)

    # Reuse address when available so restarting the demo is less annoying.
    if hasattr(listener, "setsockopt") and hasattr(listener, "SOL_SOCKET") and hasattr(listener, "SO_REUSEADDR"):
        try:
            listener.setsockopt(listener.SOL_SOCKET, listener.SO_REUSEADDR, 1)
        except Exception:
            pass

    listener.bind(address_info[4])
    listener.listen(LISTEN_BACKLOG)
    kernel.socket_setblocking(listener, False)

    task.OS.print("smallOS web app running on http://{}:{}/\n".format(HOST, PORT))
    task.OS.print("routes: /  /api/stats  /api/time  /healthz\n")
    task.OS.print("web_server PID: {}\n".format(task.getID()))

    try:
        while True:
            try:
                client_sock, client_addr = listener.accept()
            except Exception as exc:
                if kernel.socket_needs_read(exc):
                    await task.wait_readable(listener)
                    continue
                if kernel.socket_needs_write(exc):
                    await task.wait_writable(listener)
                    continue
                raise

            kernel.socket_setblocking(client_sock, False)
            task.spawn(
                web_client_handler,
                priority=max(1, task.priority - 1),
                name="http_client",
                args=(client_sock, client_addr, state),
            )
            await task.yield_now()
    finally:
        kernel.socket_close(listener)


def main():
    """Start the demo runtime and keep serving until interrupted."""
    runtime = build_runtime(Unix())
    shell = BaseShell(prompt="webapp> ", allow_python=False)
    runtime.shells.append(shell.setOS(runtime))

    state = {
        "started_ms": runtime.kernel.scheduler_now_ms(),
        "requests_total": 0,
        "active_connections": 0,
        "last_path": "/",
        "last_client": "n/a",
        "demo_value": 0,
    }

    web_server = SmallTask(2, web_server_task, name="web_server", args=(state,))
    shell_stdin = shell.make_task(
        priority=3,
        name="shell_stdin",
        is_watcher=True,
        poll_interval=0.1,
        banner_text=(
            "\nInteractive shell enabled while web app runs.\n"
            "Type commands like: count, ps, stat <pid>, kill <pid>, toggle, help\n"
        ),
        force_output=True,
    )
    app_state = SmallTask(5, app_state_task, name="app_state", args=(state,), isWatcher=True)
    metrics = SmallTask(7, metrics_task, name="metrics", args=(state,), isWatcher=True)

    runtime.fork([web_server, shell_stdin, app_state, metrics])
    runtime.startOS()

    if web_server.exception is not None and not isinstance(web_server.exception, TaskCancelledError):
        raise web_server.exception
    if isinstance(web_server.exception, TaskCancelledError):
        runtime.print("web_server was stopped by shell command\n")

    if shell_stdin.exception is not None:
        raise shell_stdin.exception

    web_pid = _pid_for_name(runtime, "web_server")
    if web_pid is None:
        runtime.print("web app demo ended (web_server no longer running)\n")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nweb app demo stopped")
