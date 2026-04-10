"""
SmallOS-native WebSocket client.

The implementation targets RFC 6455 text/binary messaging on a cooperative
smallOS runtime without depending on asyncio.
"""

from ._client_config import MISSING, resolve_client_setting
from .SmallStream import SmallStream


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketProtocolError(Exception):
    """Raised when a WebSocket handshake or frame is invalid."""


def _import_hashlib():
    for module_name in ("hashlib", "uhashlib"):
        try:
            return __import__(module_name)
        except ImportError:
            continue
    raise ImportError("WebSocket client requires hashlib or uhashlib.")


def _sha1_digest(data):
    hashlib_mod = _import_hashlib()
    hasher = hashlib_mod.sha1(data)
    return hasher.digest()


def _b64encode(data):
    try:
        import base64
    except ImportError:
        base64 = None

    if base64 is not None:
        return base64.b64encode(data).decode("ascii")

    import ubinascii

    encoded = ubinascii.b2a_base64(data).strip()
    if isinstance(encoded, bytes):
        return encoded.decode("ascii")
    return str(encoded)


def _random_bytes(length):
    try:
        import os

        return os.urandom(length)
    except Exception:
        import random

        return bytes(random.getrandbits(8) for _ in range(length))


def _percent_encode(value):
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


def _iter_pairs(value):
    if value is None:
        return []
    if hasattr(value, "items"):
        return list(value.items())
    return list(value)


def _encode_query(params):
    parts = []
    for key, value in _iter_pairs(params):
        if isinstance(value, (list, tuple)):
            values = value
        else:
            values = [value]
        for item in values:
            parts.append("{}={}".format(_percent_encode(key), _percent_encode("" if item is None else item)))
    return "&".join(parts)


def _split_header_line(line):
    if b":" not in line:
        raise WebSocketProtocolError("Malformed HTTP header line.")
    name, value = line.split(b":", 1)
    return (
        name.decode("utf-8", errors="replace").strip().lower(),
        value.decode("utf-8", errors="replace").strip(),
    )


def _parse_base_url(base_url):
    if "://" not in base_url:
        raise ValueError("base_url must include ws:// or wss://")

    scheme, remainder = base_url.split("://", 1)
    scheme = scheme.lower()
    if scheme not in ("ws", "wss"):
        raise ValueError("Only ws and wss base URLs are supported.")

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
        port = 443 if scheme == "wss" else 80

    return {
        "host": host,
        "port": port,
        "use_tls": scheme == "wss",
        "base_path": base_path.rstrip("/"),
    }


class SmallWebSocketClient:
    """
    Cooperative WebSocket client for smallOS.

    Supported operations:
    - handshake connect (ws:// or wss://)
    - text and binary sends
    - receive text/binary frames with fragmentation support
    - automatic pong replies for server pings
    - close/disconnect support
    """

    def __init__(
        self,
        task,
        base_url=None,
        host=None,
        port=None,
        use_tls=False,
        path="/",
        params=None,
        server_hostname=None,
        default_headers=None,
        tls_ca_file=None,
        tls_cert_file=None,
        tls_key_file=None,
        tls_verify=True,
        client_key=None,
        max_frame_size=MISSING,
        max_message_size=MISSING,
        max_line_size=MISSING,
        max_buffer_size=MISSING,
    ):
        self.task = task
        self.default_headers = dict(default_headers or {})
        self.path = path
        self.params = params
        self.tls_ca_file = tls_ca_file
        self.tls_cert_file = tls_cert_file
        self.tls_key_file = tls_key_file
        self.tls_verify = tls_verify
        self.client_key = client_key
        self.max_frame_size = resolve_client_setting(
            task,
            "websocket",
            "max_frame_size",
            max_frame_size,
            1024 * 1024,
        )
        self.max_message_size = resolve_client_setting(
            task,
            "websocket",
            "max_message_size",
            max_message_size,
            4 * 1024 * 1024,
        )
        self.max_line_size = resolve_client_setting(
            task,
            "websocket",
            "max_line_size",
            max_line_size,
            16 * 1024,
        )
        self.max_buffer_size = resolve_client_setting(
            task,
            "websocket",
            "max_buffer_size",
            max_buffer_size,
            16 * 1024 * 1024,
        )
        self.base_path = ""
        self.stream = None
        self.connected = False
        self.negotiated_subprotocol = None
        self._handshake_key = None
        self._close_sent = False
        self._close_received = False
        self._fragment_opcode = None
        self._fragment_data = bytearray()

        if base_url is not None:
            parsed = _parse_base_url(base_url)
            host = parsed["host"]
            port = parsed["port"]
            use_tls = parsed["use_tls"]
            self.base_path = parsed["base_path"]

        if host is None:
            raise ValueError("SmallWebSocketClient requires either host=... or base_url=...")

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

    def _expected_accept(self, key):
        return _b64encode(_sha1_digest((key + WS_GUID).encode("ascii")))

    async def connect(self, path=None, params=None, headers=None, subprotocols=None, origin=None):
        """Perform the HTTP Upgrade handshake."""
        if self.connected and self.stream is not None:
            return self

        ws_path = self.path if path is None else path
        ws_params = self.params if params is None else params
        target = self._build_target(ws_path, params=ws_params)

        key = self.client_key or _b64encode(_random_bytes(16))
        self._handshake_key = key

        request_headers = []
        for name, value in self.default_headers.items():
            request_headers.append((str(name), str(value)))
        for name, value in dict(headers or {}).items():
            request_headers.append((str(name), str(value)))

        request_headers.extend(
            [
                ("Host", self._host_header()),
                ("Upgrade", "websocket"),
                ("Connection", "Upgrade"),
                ("Sec-WebSocket-Version", "13"),
                ("Sec-WebSocket-Key", key),
            ]
        )
        if origin is not None:
            request_headers.append(("Origin", str(origin)))
        if subprotocols:
            request_headers.append(("Sec-WebSocket-Protocol", ", ".join(subprotocols)))

        lines = ["GET {} HTTP/1.1".format(target)]
        for name, value in request_headers:
            if "\r" in name or "\n" in name or "\r" in value or "\n" in value:
                raise ValueError("HTTP headers must not contain CR/LF.")
            lines.append("{}: {}".format(name, value))
        request_bytes = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")

        stream = self._make_stream()
        await stream.connect()
        await stream.send_all(request_bytes)

        status_line = (await stream.read_until(b"\r\n", max_length=self.max_line_size + 2))[:-2]
        parts = status_line.decode("utf-8", errors="replace").split(" ", 2)
        if len(parts) < 2 or not parts[0].startswith("HTTP/"):
            stream.close()
            raise WebSocketProtocolError("Malformed HTTP status line.")
        status_code = int(parts[1])
        if status_code != 101:
            stream.close()
            raise WebSocketProtocolError("WebSocket upgrade failed with HTTP {}.".format(status_code))

        headers_map = {}
        while True:
            line = await stream.read_until(b"\r\n", max_length=self.max_line_size + 2)
            if line == b"\r\n":
                break
            name, value = _split_header_line(line[:-2])
            headers_map[name] = value

        if "websocket" not in headers_map.get("upgrade", "").lower():
            stream.close()
            raise WebSocketProtocolError("Missing or invalid Upgrade header.")
        if "upgrade" not in headers_map.get("connection", "").lower():
            stream.close()
            raise WebSocketProtocolError("Missing or invalid Connection header.")

        expected_accept = self._expected_accept(key)
        if headers_map.get("sec-websocket-accept", "") != expected_accept:
            stream.close()
            raise WebSocketProtocolError("Invalid Sec-WebSocket-Accept response.")

        self.negotiated_subprotocol = headers_map.get("sec-websocket-protocol")
        self.stream = stream
        self.connected = True
        self._close_sent = False
        self._close_received = False
        self._fragment_opcode = None
        self._fragment_data = bytearray()
        return self

    async def _read_frame(self):
        if not self.connected or self.stream is None:
            raise RuntimeError("WebSocket is not connected.")

        head = await self.stream.read_exactly(2)
        first, second = head[0], head[1]
        fin = bool(first & 0x80)
        opcode = first & 0x0F
        masked = bool(second & 0x80)
        length = second & 0x7F

        if length == 126:
            length = int.from_bytes(await self.stream.read_exactly(2), "big")
        elif length == 127:
            length = int.from_bytes(await self.stream.read_exactly(8), "big")

        if self.max_frame_size and length > self.max_frame_size:
            raise WebSocketProtocolError(
                "Frame payload of {} bytes exceeded max_frame_size ({}).".format(
                    length, self.max_frame_size
                )
            )

        if masked:
            mask = await self.stream.read_exactly(4)
        else:
            mask = None

        payload = await self.stream.read_exactly(length)
        if masked:
            # Per RFC 6455 servers must not mask payloads. We still decode the
            # payload so debugging malformed peers is easier.
            decoded = bytearray(payload)
            for idx in range(length):
                decoded[idx] ^= mask[idx % 4]
            payload = bytes(decoded)
            raise WebSocketProtocolError("Server frames must not be masked.")

        return fin, opcode, payload

    async def _send_frame(self, opcode, payload=b"", fin=True):
        if not self.connected or self.stream is None:
            raise RuntimeError("WebSocket is not connected.")

        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        elif isinstance(payload, (bytearray, memoryview)):
            payload = bytes(payload)
        elif not isinstance(payload, bytes):
            raise TypeError("WebSocket payload must be bytes-like or str.")

        if self.max_frame_size and len(payload) > self.max_frame_size:
            raise WebSocketProtocolError(
                "Outgoing frame payload of {} bytes exceeded max_frame_size ({}).".format(
                    len(payload), self.max_frame_size
                )
            )

        first = (0x80 if fin else 0x00) | (opcode & 0x0F)
        mask = _random_bytes(4)
        length = len(payload)

        frame = bytearray([first])
        if length < 126:
            frame.append(0x80 | length)
        elif length <= 0xFFFF:
            frame.append(0x80 | 126)
            frame.extend(length.to_bytes(2, "big"))
        else:
            frame.append(0x80 | 127)
            frame.extend(length.to_bytes(8, "big"))

        frame.extend(mask)
        if length:
            masked = bytearray(payload)
            for idx in range(length):
                masked[idx] ^= mask[idx % 4]
            frame.extend(masked)

        await self.stream.send_all(bytes(frame))

    async def send_text(self, text):
        """Send one text frame."""
        await self._send_frame(0x1, str(text).encode("utf-8"), fin=True)

    async def send_binary(self, data):
        """Send one binary frame."""
        await self._send_frame(0x2, data, fin=True)

    async def ping(self, payload=b""):
        """Send one ping frame."""
        await self._send_frame(0x9, payload, fin=True)

    async def receive(self):
        """Receive the next application frame or close frame."""
        while True:
            fin, opcode, payload = await self._read_frame()

            if opcode == 0x9:
                await self._send_frame(0xA, payload, fin=True)
                continue

            if opcode == 0xA:
                return {"type": "pong", "data": payload}

            if opcode == 0x8:
                code = None
                reason = ""
                if len(payload) >= 2:
                    code = int.from_bytes(payload[:2], "big")
                    reason = payload[2:].decode("utf-8", errors="replace")
                self._close_received = True
                if not self._close_sent:
                    await self._send_close(code=1000)
                self.close()
                return {"type": "close", "code": code, "reason": reason}

            if opcode in (0x1, 0x2):
                if self._fragment_opcode is not None:
                    raise WebSocketProtocolError("Received new data frame during fragmented message.")

                if fin:
                    return self._build_message(opcode, payload)

                self._fragment_opcode = opcode
                self._fragment_data = bytearray(payload)
                if self.max_message_size and len(self._fragment_data) > self.max_message_size:
                    raise WebSocketProtocolError(
                        "Fragmented message exceeded max_message_size ({}).".format(
                            self.max_message_size
                        )
                    )
                continue

            if opcode == 0x0:
                if self._fragment_opcode is None:
                    raise WebSocketProtocolError("Unexpected continuation frame.")
                self._fragment_data.extend(payload)
                if self.max_message_size and len(self._fragment_data) > self.max_message_size:
                    raise WebSocketProtocolError(
                        "Fragmented message exceeded max_message_size ({}).".format(
                            self.max_message_size
                        )
                    )
                if fin:
                    opcode = self._fragment_opcode
                    data = bytes(self._fragment_data)
                    self._fragment_opcode = None
                    self._fragment_data = bytearray()
                    return self._build_message(opcode, data)
                continue

            raise WebSocketProtocolError("Unsupported opcode {}.".format(opcode))

    def _build_message(self, opcode, payload):
        if self.max_message_size and len(payload) > self.max_message_size:
            raise WebSocketProtocolError(
                "Message payload exceeded max_message_size ({}).".format(
                    self.max_message_size
                )
            )
        if opcode == 0x1:
            return {"type": "text", "data": payload.decode("utf-8", errors="replace")}
        if opcode == 0x2:
            return {"type": "binary", "data": payload}
        raise WebSocketProtocolError("Unsupported message opcode {}.".format(opcode))

    async def _send_close(self, code=1000, reason=""):
        if self._close_sent or not self.connected or self.stream is None:
            return
        payload = b""
        if code is not None:
            payload = int(code).to_bytes(2, "big")
            if reason:
                payload += str(reason).encode("utf-8")
        await self._send_frame(0x8, payload, fin=True)
        self._close_sent = True

    async def disconnect(self, code=1000, reason=""):
        """Send close frame and close the stream."""
        await self._send_close(code=code, reason=reason)
        self.close()

    def close(self):
        """Close the transport immediately without waiting for peer close."""
        if self.stream is not None:
            self.stream.close()
        self.stream = None
        self.connected = False
