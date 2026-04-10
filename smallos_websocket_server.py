"""Local SmallOS-compatible WebSocket server helper."""

import base64
import hashlib


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketServerProtocolError(Exception):
    """Raised when a websocket upgrade or frame is invalid."""


async def _send_all(task, sock, data):
    """Write all bytes to a non-blocking socket using cooperative waits."""
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
            raise ConnectionError("socket closed while sending")
        remaining = remaining[sent:]


async def _read_exact(task, sock, length):
    """Read exactly ``length`` bytes or raise EOFError."""
    kernel = task.OS.kernel
    data = bytearray()

    while len(data) < length:
        try:
            chunk = kernel.socket_recv(sock, length - len(data))
        except Exception as exc:
            if kernel.socket_needs_read(exc):
                await task.wait_readable(sock)
                continue
            if kernel.socket_needs_write(exc):
                await task.wait_writable(sock)
                continue
            raise

        if not chunk:
            raise EOFError("socket closed")
        data.extend(chunk)

    return bytes(data)


def _expected_accept(sec_websocket_key):
    """Return the expected ``Sec-WebSocket-Accept`` value."""
    digest = hashlib.sha1((sec_websocket_key + WS_GUID).encode("utf-8")).digest()
    return base64.b64encode(digest).decode("ascii")


def _handshake_response(sec_websocket_key):
    """Build one server-side websocket upgrade response."""
    accept = _expected_accept(sec_websocket_key)
    lines = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        "Sec-WebSocket-Accept: {}".format(accept),
        "",
        "",
    ]
    return "\r\n".join(lines).encode("utf-8")


class SmallWebSocketServerConnection:
    """SmallOS-friendly websocket server transport helper."""

    def __init__(self, task, sock, max_frame_size=1024 * 1024, max_message_size=4 * 1024 * 1024):
        self.task = task
        self.sock = sock
        self.max_frame_size = max_frame_size
        self.max_message_size = max_message_size
        self.connected = True
        self._close_sent = False
        self._close_received = False
        self._fragment_opcode = None
        self._fragment_data = bytearray()

    @classmethod
    async def accept(cls, task, sock, headers, max_frame_size=1024 * 1024, max_message_size=4 * 1024 * 1024):
        """Validate one HTTP upgrade request and return a websocket connection."""
        sec_websocket_key = headers.get("sec-websocket-key")
        if not sec_websocket_key:
            raise WebSocketServerProtocolError("missing Sec-WebSocket-Key header")

        if "websocket" not in headers.get("upgrade", "").lower():
            raise WebSocketServerProtocolError("missing or invalid Upgrade header")
        if "upgrade" not in headers.get("connection", "").lower():
            raise WebSocketServerProtocolError("missing or invalid Connection header")

        version = headers.get("sec-websocket-version")
        if version and version != "13":
            raise WebSocketServerProtocolError("unsupported Sec-WebSocket-Version {!r}".format(version))

        await _send_all(task, sock, _handshake_response(sec_websocket_key))
        return cls(
            task,
            sock,
            max_frame_size=max_frame_size,
            max_message_size=max_message_size,
        )

    async def _read_frame(self):
        """Read one full client-to-server websocket frame."""
        header = await _read_exact(self.task, self.sock, 2)
        first_byte = header[0]
        second_byte = header[1]
        fin = bool(first_byte & 0x80)
        opcode = first_byte & 0x0F
        masked = bool(second_byte & 0x80)
        length = second_byte & 0x7F

        if length == 126:
            length = int.from_bytes(await _read_exact(self.task, self.sock, 2), "big")
        elif length == 127:
            length = int.from_bytes(await _read_exact(self.task, self.sock, 8), "big")

        if self.max_frame_size and length > self.max_frame_size:
            raise WebSocketServerProtocolError(
                "websocket frame exceeded max_frame_size ({})".format(self.max_frame_size)
            )
        if not masked:
            raise WebSocketServerProtocolError("client websocket frames must be masked")

        mask = await _read_exact(self.task, self.sock, 4)
        payload = await _read_exact(self.task, self.sock, length)
        data = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        return fin, opcode, data

    async def _send_frame(self, opcode, payload=b"", fin=True):
        """Send one server-to-client websocket frame."""
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        elif isinstance(payload, (bytearray, memoryview)):
            payload = bytes(payload)
        elif not isinstance(payload, bytes):
            raise TypeError("websocket payload must be bytes-like or str")

        if self.max_frame_size and len(payload) > self.max_frame_size:
            raise WebSocketServerProtocolError(
                "outgoing frame exceeded max_frame_size ({})".format(self.max_frame_size)
            )

        first_byte = (0x80 if fin else 0x00) | (opcode & 0x0F)
        length = len(payload)
        frame = bytearray([first_byte])

        if length < 126:
            frame.append(length)
        elif length <= 0xFFFF:
            frame.append(126)
            frame.extend(length.to_bytes(2, "big"))
        else:
            frame.append(127)
            frame.extend(length.to_bytes(8, "big"))

        frame.extend(payload)
        await _send_all(self.task, self.sock, bytes(frame))

    def _build_message(self, opcode, payload):
        """Build one application-level message from a final payload."""
        if self.max_message_size and len(payload) > self.max_message_size:
            raise WebSocketServerProtocolError(
                "message exceeded max_message_size ({})".format(self.max_message_size)
            )
        if opcode == 0x1:
            return {"type": "text", "data": payload.decode("utf-8", errors="replace")}
        if opcode == 0x2:
            return {"type": "binary", "data": payload}
        raise WebSocketServerProtocolError("unsupported websocket opcode {}".format(opcode))

    async def send_text(self, text):
        """Send one text frame."""
        await self._send_frame(0x1, str(text).encode("utf-8"), fin=True)

    async def send_binary(self, data):
        """Send one binary frame."""
        await self._send_frame(0x2, data, fin=True)

    async def send_close(self, code=1000, reason=""):
        """Send one close frame if it has not already been sent."""
        if self._close_sent:
            return

        payload = b""
        if code is not None:
            payload = int(code).to_bytes(2, "big")
            if reason:
                payload += str(reason).encode("utf-8")
        await self._send_frame(0x8, payload, fin=True)
        self._close_sent = True
        self.connected = False

    async def receive(self):
        """Receive the next application message or close event."""
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
                    await self.send_close(code=code or 1000)
                self.connected = False
                return {"type": "close", "code": code, "reason": reason}

            if opcode in (0x1, 0x2):
                if self._fragment_opcode is not None:
                    raise WebSocketServerProtocolError("received new data frame during fragmented message")
                if fin:
                    return self._build_message(opcode, payload)
                self._fragment_opcode = opcode
                self._fragment_data = bytearray(payload)
                if self.max_message_size and len(self._fragment_data) > self.max_message_size:
                    raise WebSocketServerProtocolError(
                        "fragmented message exceeded max_message_size ({})".format(self.max_message_size)
                    )
                continue

            if opcode == 0x0:
                if self._fragment_opcode is None:
                    raise WebSocketServerProtocolError("unexpected continuation frame")
                self._fragment_data.extend(payload)
                if self.max_message_size and len(self._fragment_data) > self.max_message_size:
                    raise WebSocketServerProtocolError(
                        "fragmented message exceeded max_message_size ({})".format(self.max_message_size)
                    )
                if fin:
                    opcode = self._fragment_opcode
                    data = bytes(self._fragment_data)
                    self._fragment_opcode = None
                    self._fragment_data = bytearray()
                    return self._build_message(opcode, data)
                continue

            raise WebSocketServerProtocolError("unsupported opcode {}".format(opcode))
