"""
SmallOS-native HTTP helpers.

This module gives smallOS tasks compact, dependency-light HTTP and SSE clients
built on the same cooperative socket stream used by the Redis and MQTT
helpers. The goal is convenience, not full browser parity:
- HTTP/1.1 requests over plain TCP or TLS
- simple `get` / `post` / `put` / `patch` / `delete` helpers
- query string, form body, and JSON body support
- response parsing for content-length, chunked transfer, and connection-close
- long-lived Server-Sent Events (`text/event-stream`) consumption

The client intentionally closes the transport after each request. That keeps
the implementation easy to reason about and works well for the common
"make one request and keep running other tasks" workflow.
"""

import json

from ._client_config import MISSING, resolve_client_setting
from .SmallStream import SmallStream, StreamClosedError


class HTTPProtocolError(Exception):
    """Raised when the peer sends malformed or unsupported HTTP data."""


class SSEProtocolError(Exception):
    """Raised when an SSE endpoint returns malformed or unsupported data."""


def _sanitize_header_value(value):
    """Strip characters that could enable header injection."""
    return str(value).replace("\r", "").replace("\n", "").replace("\x00", "")


def _sanitize_header_name(name):
    """Strip characters that could enable header injection."""
    cleaned = str(name).replace("\r", "").replace("\n", "").replace("\x00", "").replace(":", "")
    if not cleaned:
        raise ValueError("HTTP header name must not be empty.")
    return cleaned


def _percent_encode(value):
    """Percent-encode one query-string key or value."""
    if isinstance(value, bytes):
        data = value
    else:
        data = str(value).encode("utf-8")

    safe = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    encoded = []
    for byte in data:
        if byte in safe:
            encoded.append(chr(byte))
        else:
            encoded.append("%{:02X}".format(byte))
    return "".join(encoded)


def _normalize_query_value(value):
    """Convert one query-string value into a string-friendly representation."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return value


def _iter_pairs(value):
    """Return a stable iterable of key/value pairs from common mapping inputs."""
    if value is None:
        return []
    if hasattr(value, "items"):
        return list(value.items())
    return list(value)


def _encode_query(params):
    """Encode a dict-or-pairs structure into a URL query string."""
    parts = []
    for key, value in _iter_pairs(params):
        if isinstance(value, (list, tuple)):
            values = value
        else:
            values = [value]
        for item in values:
            parts.append(
                "{}={}".format(
                    _percent_encode(key),
                    _percent_encode(_normalize_query_value(item)),
                )
            )
    return "&".join(parts)


def _split_header_line(line):
    """Split one raw HTTP header line into name/value pieces."""
    if b":" not in line:
        raise HTTPProtocolError("Malformed HTTP header line.")
    name, value = line.split(b":", 1)
    return (
        name.decode("utf-8", errors="replace").strip(),
        value.decode("utf-8", errors="replace").strip(),
    )


def _parse_base_url(base_url):
    """Parse a compact `http://` or `https://` base URL without asyncio helpers."""
    if "://" not in base_url:
        raise ValueError("base_url must include http:// or https://")

    scheme, remainder = base_url.split("://", 1)
    scheme = scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError("Only http and https base URLs are supported.")

    if "/" in remainder:
        authority, base_path = remainder.split("/", 1)
        base_path = "/" + base_path
    else:
        authority = remainder
        base_path = ""

    if not authority:
        raise ValueError("base_url must include a host.")

    if "@" in authority:
        authority = authority.rsplit("@", 1)[1]

    if ":" in authority and not authority.startswith("["):
        host, port_text = authority.rsplit(":", 1)
        port = int(port_text)
    else:
        host = authority
        port = 443 if scheme == "https" else 80

    base_path = base_path.split("#", 1)[0]
    return {
        "host": host,
        "port": port,
        "use_tls": scheme == "https",
        "base_path": base_path.rstrip("/"),
    }


class SmallHTTPResponse:
    """Container for one parsed HTTP response."""

    def __init__(self, http_version, status_code, reason, headers, body):
        self.http_version = http_version
        self.status_code = int(status_code)
        self.reason = reason
        self.raw_headers = list(headers)
        self.headers = {}
        for name, value in self.raw_headers:
            self.headers[name.lower()] = value
        self.body = bytes(body)

    @property
    def ok(self):
        """Return `True` for 2xx responses."""
        return 200 <= self.status_code < 300

    def header(self, name, default=None):
        """Return one response header case-insensitively."""
        return self.headers.get(str(name).lower(), default)

    def text(self, encoding=None):
        """Decode the response body into text."""
        if encoding is None:
            content_type = self.header("content-type", "")
            encoding = "utf-8"
            if "charset=" in content_type:
                encoding = content_type.split("charset=", 1)[1].split(";", 1)[0].strip()
        return self.body.decode(encoding, errors="replace")

    def json(self):
        """Decode the response body as JSON."""
        return json.loads(self.text())


class SmallHTTPClient:
    """
    Minimal cooperative HTTP client for smallOS tasks.

    Typical usage:
    - bind the client to a `base_url`
    - make one or more requests with `await client.get(...)` or `await client.post(...)`
    - inspect `status_code`, `headers`, `body`, `text()`, or `json()`

    The client closes each request stream after reading the response, which
    keeps the behavior predictable on both desktop Python and future embedded
    targets.
    """

    def __init__(
        self,
        task,
        base_url=None,
        host=None,
        port=None,
        use_tls=False,
        server_hostname=None,
        default_headers=None,
        tls_ca_file=None,
        tls_cert_file=None,
        tls_key_file=None,
        tls_verify=True,
        max_response_size=MISSING,
        max_buffer_size=MISSING,
    ):
        self.task = task
        self.default_headers = dict(default_headers or {})
        self.tls_ca_file = tls_ca_file
        self.tls_cert_file = tls_cert_file
        self.tls_key_file = tls_key_file
        self.tls_verify = tls_verify
        self.max_response_size = resolve_client_setting(
            task,
            "http",
            "max_response_size",
            max_response_size,
            16 * 1024 * 1024,
        )
        self.max_buffer_size = resolve_client_setting(
            task,
            "http",
            "max_buffer_size",
            max_buffer_size,
            16 * 1024 * 1024,
        )
        self.base_path = ""

        if base_url is not None:
            parsed = _parse_base_url(base_url)
            host = parsed["host"]
            port = parsed["port"]
            use_tls = parsed["use_tls"]
            self.base_path = parsed["base_path"]

        if host is None:
            raise ValueError("SmallHTTPClient requires either host=... or base_url=...")

        self.host = host
        self.use_tls = bool(use_tls)
        self.port = int(port or (443 if self.use_tls else 80))
        self.server_hostname = server_hostname or self.host

    def _make_stream(self):
        """Build a fresh transport for one request."""
        return SmallStream(
            self.task,
            host=self.host,
            port=self.port,
            use_tls=self.use_tls,
            server_hostname=self.server_hostname,
            tls_ca_file=self.tls_ca_file,
            tls_cert_file=self.tls_cert_file,
            tls_key_file=self.tls_key_file,
            tls_verify=self.tls_verify,
            max_buffer_size=self.max_buffer_size,
        )

    def _default_port(self):
        """Return the conventional port for the current scheme."""
        return 443 if self.use_tls else 80

    def _host_header(self):
        """Return the value for the HTTP Host header."""
        if self.port == self._default_port():
            return self.host
        return "{}:{}".format(self.host, self.port)

    def _build_target(self, path, params=None):
        """Build the request target path plus optional query string."""
        path = (path or "").split("#", 1)[0]

        if path.startswith("/"):
            target = path
        elif path:
            prefix = self.base_path.rstrip("/")
            if prefix:
                target = prefix + "/" + path.lstrip("/")
            else:
                target = "/" + path.lstrip("/")
        elif self.base_path:
            target = self.base_path or "/"
        else:
            target = "/"

        if "?" in target:
            path_only, existing_query = target.split("?", 1)
        else:
            path_only = target
            existing_query = ""

        extra_query = _encode_query(params)
        if existing_query and extra_query:
            return "{}?{}&{}".format(path_only, existing_query, extra_query)
        if existing_query:
            return "{}?{}".format(path_only, existing_query)
        if extra_query:
            return "{}?{}".format(path_only, extra_query)
        return path_only

    @staticmethod
    def _set_header(headers, name, value):
        """Set or replace a header case-insensitively while preserving order."""
        for index, (header_name, _) in enumerate(headers):
            if header_name.lower() == name.lower():
                headers[index] = (name, value)
                return
        headers.append((name, value))

    @staticmethod
    def _ensure_header(headers, name, value):
        """Set a header only when the caller has not already provided one."""
        for header_name, _ in headers:
            if header_name.lower() == name.lower():
                return
        headers.append((name, value))

    def _prepare_body(self, data=None, json_body=None, headers=None):
        """Normalize outgoing request body bytes and matching headers."""
        if data is not None and json_body is not None:
            raise ValueError("Pass either data=... or json_body=..., not both.")

        body = b""
        if json_body is not None:
            body = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
            self._set_header(headers, "Content-Type", "application/json")
        elif data is None:
            body = b""
        elif isinstance(data, bytes):
            body = data
        elif isinstance(data, bytearray):
            body = bytes(data)
        elif isinstance(data, memoryview):
            body = bytes(data)
        elif isinstance(data, str):
            body = data.encode("utf-8")
        else:
            body = _encode_query(data).encode("utf-8")
            self._set_header(headers, "Content-Type", "application/x-www-form-urlencoded")
        return body

    async def request(self, method, path="", headers=None, params=None, data=None, json_body=None):
        """Send one HTTP request and return a parsed response object."""
        request_headers = []
        for name, value in self.default_headers.items():
            request_headers.append((str(name), str(value)))
        for name, value in dict(headers or {}).items():
            self._set_header(request_headers, str(name), str(value))

        body = self._prepare_body(data=data, json_body=json_body, headers=request_headers)
        target = self._build_target(path, params=params)

        self._set_header(request_headers, "Host", self._host_header())
        self._ensure_header(request_headers, "User-Agent", "smallOS/0.1")
        self._ensure_header(request_headers, "Accept", "*/*")
        self._ensure_header(request_headers, "Connection", "close")
        if body:
            self._set_header(request_headers, "Content-Length", str(len(body)))

        lines = ["{} {} HTTP/1.1".format(method.upper(), target)]
        for name, value in request_headers:
            lines.append("{}: {}".format(
                _sanitize_header_name(name),
                _sanitize_header_value(value),
            ))
        request_bytes = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body

        stream = self._make_stream()
        try:
            await stream.connect()
            await stream.send_all(request_bytes)
            return await self._read_response(stream, method=method)
        finally:
            stream.close()

    async def get(self, path="", headers=None, params=None):
        """Convenience wrapper for `GET`."""
        return await self.request("GET", path=path, headers=headers, params=params)

    async def post(self, path="", headers=None, params=None, data=None, json_body=None):
        """Convenience wrapper for `POST`."""
        return await self.request("POST", path=path, headers=headers, params=params, data=data, json_body=json_body)

    async def put(self, path="", headers=None, params=None, data=None, json_body=None):
        """Convenience wrapper for `PUT`."""
        return await self.request("PUT", path=path, headers=headers, params=params, data=data, json_body=json_body)

    async def patch(self, path="", headers=None, params=None, data=None, json_body=None):
        """Convenience wrapper for `PATCH`."""
        return await self.request("PATCH", path=path, headers=headers, params=params, data=data, json_body=json_body)

    async def delete(self, path="", headers=None, params=None):
        """Convenience wrapper for `DELETE`."""
        return await self.request("DELETE", path=path, headers=headers, params=params)

    async def _read_response(self, stream, method):
        """Parse one HTTP response from the stream."""
        status_line = (await stream.read_until(b"\r\n"))[:-2].decode("utf-8", errors="replace")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            raise HTTPProtocolError("Malformed HTTP status line.")

        http_version = parts[0]
        status_code = int(parts[1])
        reason = parts[2] if len(parts) > 2 else ""

        headers = []
        while True:
            line = await stream.read_until(b"\r\n")
            if line == b"\r\n":
                break
            headers.append(_split_header_line(line[:-2]))

        header_map = {name.lower(): value for name, value in headers}
        method = str(method).upper()

        if method == "HEAD" or status_code in (204, 304) or 100 <= status_code < 200:
            body = b""
        elif "chunked" in header_map.get("transfer-encoding", "").lower():
            body = await self._read_chunked_body(stream)
        elif "content-length" in header_map:
            content_length = int(header_map["content-length"])
            if content_length < 0:
                raise HTTPProtocolError("Negative Content-Length.")
            if self.max_response_size and content_length > self.max_response_size:
                raise HTTPProtocolError(
                    "Content-Length {} exceeds max_response_size ({}).".format(
                        content_length, self.max_response_size
                    )
                )
            body = await stream.read_exactly(content_length)
        else:
            body = await self._read_to_close(stream)

        return SmallHTTPResponse(
            http_version=http_version,
            status_code=status_code,
            reason=reason,
            headers=headers,
            body=body,
        )

    async def _read_chunked_body(self, stream):
        """Read an HTTP chunked-transfer body."""
        chunks = []
        total = 0
        while True:
            line = (await stream.read_until(b"\r\n"))[:-2]
            size_text = line.split(b";", 1)[0]
            chunk_size = int(size_text.decode("ascii"), 16)
            if chunk_size == 0:
                while True:
                    trailer = await stream.read_until(b"\r\n")
                    if trailer == b"\r\n":
                        return b"".join(chunks)
            if self.max_response_size and total + chunk_size > self.max_response_size:
                raise HTTPProtocolError(
                    "Chunked body exceeds max_response_size ({}).".format(
                        self.max_response_size
                    )
                )
            chunks.append(await stream.read_exactly(chunk_size))
            total += chunk_size
            trailer = await stream.read_exactly(2)
            if trailer != b"\r\n":
                raise HTTPProtocolError("Malformed chunk terminator.")

    async def _read_to_close(self, stream):
        """Read until the peer closes the HTTP stream."""
        chunks = []
        total = 0
        while True:
            try:
                chunk = await stream.recv_some()
                total += len(chunk)
                if self.max_response_size and total > self.max_response_size:
                    raise HTTPProtocolError(
                        "Response body exceeds max_response_size ({}).".format(
                            self.max_response_size
                        )
                    )
                chunks.append(chunk)
            except StreamClosedError:
                return b"".join(chunks)


class SmallSSEClient:
    """
    Minimal cooperative SSE client for smallOS tasks.

    Typical flow:
    - `await client.connect(...)`
    - repeatedly `await client.read_event()`
    - `client.close()` when finished
    """

    def __init__(
        self,
        task,
        base_url=None,
        host=None,
        port=None,
        use_tls=False,
        server_hostname=None,
        default_headers=None,
        tls_ca_file=None,
        tls_cert_file=None,
        tls_key_file=None,
        tls_verify=True,
        max_event_size=MISSING,
        max_line_size=MISSING,
        max_buffer_size=MISSING,
    ):
        self.task = task
        self.default_headers = dict(default_headers or {})
        self.tls_ca_file = tls_ca_file
        self.tls_cert_file = tls_cert_file
        self.tls_key_file = tls_key_file
        self.tls_verify = tls_verify
        self.max_event_size = resolve_client_setting(
            task,
            "sse",
            "max_event_size",
            max_event_size,
            1024 * 1024,
        )
        self.max_line_size = resolve_client_setting(
            task,
            "sse",
            "max_line_size",
            max_line_size,
            64 * 1024,
        )
        self.max_buffer_size = resolve_client_setting(
            task,
            "sse",
            "max_buffer_size",
            max_buffer_size,
            16 * 1024 * 1024,
        )
        self.base_path = ""
        self._event_state = None
        self.stream = None
        self.connected = False

        if base_url is not None:
            parsed = _parse_base_url(base_url)
            host = parsed["host"]
            port = parsed["port"]
            use_tls = parsed["use_tls"]
            self.base_path = parsed["base_path"]

        if host is None:
            raise ValueError("SmallSSEClient requires either host=... or base_url=...")

        self.host = host
        self.use_tls = bool(use_tls)
        self.port = int(port or (443 if self.use_tls else 80))
        self.server_hostname = server_hostname or self.host

    def _default_port(self):
        return 443 if self.use_tls else 80

    def _host_header(self):
        if self.port == self._default_port():
            return self.host
        return "{}:{}".format(self.host, self.port)

    def _build_target(self, path, params=None):
        path = (path or "").split("#", 1)[0]

        if path.startswith("/"):
            target = path
        elif path:
            prefix = self.base_path.rstrip("/")
            target = (prefix + "/" + path.lstrip("/")) if prefix else ("/" + path.lstrip("/"))
        elif self.base_path:
            target = self.base_path or "/"
        else:
            target = "/"

        query = _encode_query(params)
        if not query:
            return target
        if "?" in target:
            return "{}&{}".format(target, query)
        return "{}?{}".format(target, query)

    def _make_stream(self):
        return SmallStream(
            self.task,
            host=self.host,
            port=self.port,
            use_tls=self.use_tls,
            server_hostname=self.server_hostname,
            tls_ca_file=self.tls_ca_file,
            tls_cert_file=self.tls_cert_file,
            tls_key_file=self.tls_key_file,
            tls_verify=self.tls_verify,
            max_buffer_size=self.max_buffer_size,
        )

    async def connect(self, path="", headers=None, params=None, last_event_id=None):
        """Open the event stream and validate the HTTP response headers."""
        if self.connected and self.stream is not None:
            return self

        target = self._build_target(path, params=params)

        request_headers = []
        for name, value in self.default_headers.items():
            request_headers.append((str(name), str(value)))
        for name, value in dict(headers or {}).items():
            request_headers.append((str(name), str(value)))
        request_headers.append(("Host", self._host_header()))
        request_headers.append(("Accept", "text/event-stream"))
        request_headers.append(("Cache-Control", "no-cache"))
        request_headers.append(("Connection", "keep-alive"))
        if last_event_id is not None:
            request_headers.append(("Last-Event-ID", str(last_event_id)))

        lines = ["GET {} HTTP/1.1".format(target)]
        for name, value in request_headers:
            lines.append("{}: {}".format(
                _sanitize_header_name(name),
                _sanitize_header_value(value),
            ))
        request_bytes = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")

        stream = self._make_stream()
        await stream.connect()
        await stream.send_all(request_bytes)

        status_line = (await stream.read_until(b"\r\n", max_length=self.max_line_size + 2))[:-2]
        parts = status_line.decode("utf-8", errors="replace").split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            stream.close()
            raise SSEProtocolError("Malformed HTTP status line.")

        try:
            status_code = int(parts[1])
        except ValueError:
            stream.close()
            raise SSEProtocolError("Malformed HTTP status code.")

        headers_map = {}
        while True:
            line = await stream.read_until(b"\r\n", max_length=self.max_line_size + 2)
            if line == b"\r\n":
                break
            try:
                name, value = _split_header_line(line[:-2])
            except HTTPProtocolError as exc:
                stream.close()
                raise SSEProtocolError(str(exc))
            headers_map[name.lower()] = value

        if status_code != 200:
            stream.close()
            raise SSEProtocolError("SSE endpoint returned HTTP {}.".format(status_code))

        content_type = headers_map.get("content-type", "").lower()
        if "text/event-stream" not in content_type:
            stream.close()
            raise SSEProtocolError("SSE endpoint did not return text/event-stream.")

        self.stream = stream
        self.connected = True
        self._reset_event_state()
        return self

    def _reset_event_state(self):
        self._event_state = {
            "event": None,
            "id": None,
            "retry": None,
            "data_lines": [],
            "data_size": 0,
        }

    def _append_data_line(self, value):
        data_len = len(value.encode("utf-8"))
        projected = self._event_state["data_size"] + data_len
        if self._event_state["data_lines"]:
            projected += 1
        if self.max_event_size and projected > self.max_event_size:
            raise SSEProtocolError(
                "SSE event data exceeded max_event_size ({}).".format(self.max_event_size)
            )
        self._event_state["data_lines"].append(value)
        self._event_state["data_size"] = projected

    def _dispatch_event(self):
        if self._event_state["event"] is None and not self._event_state["data_lines"] and self._event_state["id"] is None:
            return None

        event = {
            "event": self._event_state["event"] or "message",
            "data": "\n".join(self._event_state["data_lines"]),
            "id": self._event_state["id"],
        }
        if self._event_state["retry"] is not None:
            event["retry"] = self._event_state["retry"]
        self._reset_event_state()
        return event

    async def read_event(self):
        """Read and return the next SSE event."""
        if not self.connected or self.stream is None:
            raise RuntimeError("SSE stream is not connected. Call connect() first.")

        while True:
            raw_line = await self.stream.read_until(b"\n", max_length=self.max_line_size)
            line = raw_line.decode("utf-8", errors="replace").rstrip("\n")
            if line.endswith("\r"):
                line = line[:-1]

            if line == "":
                event = self._dispatch_event()
                if event is not None:
                    return event
                continue

            if line.startswith(":"):
                continue

            if ":" in line:
                field, value = line.split(":", 1)
                if value.startswith(" "):
                    value = value[1:]
            else:
                field, value = line, ""

            if field == "data":
                self._append_data_line(value)
            elif field == "event":
                self._event_state["event"] = value
            elif field == "id":
                if "\x00" not in value:
                    self._event_state["id"] = value
            elif field == "retry":
                if value.isdigit():
                    self._event_state["retry"] = int(value)

    def close(self):
        """Close the SSE stream immediately."""
        if self.stream is not None:
            self.stream.close()
        self.stream = None
        self.connected = False
        self._event_state = None
