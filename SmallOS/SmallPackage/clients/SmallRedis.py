"""
SmallOS-native Redis client helpers.

The goal here is not to replace a full desktop Redis library. Instead, this
module gives smallOS tasks a simple, dependency-free way to speak Redis over
the runtime's cooperative socket layer on both Unix and MicroPython targets.
"""

import warnings

from ._client_config import MISSING, resolve_client_setting
from .SmallStream import SmallStream


class RedisError(Exception):
    """Raised when Redis returns an ``-ERR`` style response."""


class SmallRedisClient:
    """
    Minimal Redis RESP client for smallOS tasks.

    Supported usage is intentionally focused:
    - connect/auth/select
    - generic command execution
    - common helpers like ``ping``, ``get``, ``set``, and ``delete``
    - pub/sub helpers for lightweight message flows
    - optional TLS with custom CA/client certificate paths on Unix targets
    """

    def __init__(
        self,
        task,
        host="127.0.0.1",
        port=6379,
        use_tls=False,
        server_hostname=None,
        tls_ca_file=None,
        tls_cert_file=None,
        tls_key_file=None,
        tls_verify=True,
        username=None,
        password=None,
        db=None,
        decode_responses=True,
        max_response_size=MISSING,
        max_nesting_depth=MISSING,
        max_buffer_size=MISSING,
    ):
        self.task = task
        self.username = username
        self.password = password
        self.db = db
        self.decode_responses = decode_responses
        self.max_response_size = resolve_client_setting(
            task,
            "redis",
            "max_response_size",
            max_response_size,
            16 * 1024 * 1024,
        )
        self.max_nesting_depth = resolve_client_setting(
            task,
            "redis",
            "max_nesting_depth",
            max_nesting_depth,
            32,
        )
        self.max_buffer_size = resolve_client_setting(
            task,
            "redis",
            "max_buffer_size",
            max_buffer_size,
            16 * 1024 * 1024,
        )

        if (username is not None or password is not None) and not use_tls:
            warnings.warn(
                "Redis credentials are being sent without TLS. "
                "Set use_tls=True to encrypt the connection.",
                stacklevel=2,
            )

        self.stream = SmallStream(
            task,
            host=host,
            port=port,
            use_tls=use_tls,
            server_hostname=server_hostname,
            tls_ca_file=tls_ca_file,
            tls_cert_file=tls_cert_file,
            tls_key_file=tls_key_file,
            tls_verify=tls_verify,
            max_buffer_size=self.max_buffer_size,
        )

    @staticmethod
    def _encode_part(value):
        """Normalize one command argument into bytes."""
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if isinstance(value, memoryview):
            return bytes(value)
        if isinstance(value, str):
            return value.encode("utf-8")
        if isinstance(value, (int, float, bool)):
            return str(value).encode("utf-8")
        raise TypeError("Redis command arguments must be bytes, str, int, float, or bool.")

    @classmethod
    def encode_command(cls, *parts):
        """Return a RESP-encoded Redis command."""
        encoded_parts = [cls._encode_part(part) for part in parts]
        chunks = ["*{}\r\n".format(len(encoded_parts)).encode("ascii")]
        for part in encoded_parts:
            chunks.append("${}\r\n".format(len(part)).encode("ascii"))
            chunks.append(part)
            chunks.append(b"\r\n")
        return b"".join(chunks)

    def _decode_bulk(self, value):
        """Decode bulk string payloads when text responses are requested."""
        if value is None or not self.decode_responses:
            return value
        return value.decode("utf-8", errors="replace")

    def _response_tag(self, value):
        """Return a normalized lowercase tag for Redis array-style responses."""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace").lower()
        if isinstance(value, str):
            return value.lower()
        return value

    async def connect(self):
        """Connect and optionally authenticate/select a database."""
        await self.stream.connect()
        if self.password is not None:
            if self.username is None:
                await self.command("AUTH", self.password)
            else:
                await self.command("AUTH", self.username, self.password)
        if self.db is not None:
            await self.command("SELECT", self.db)
        return self

    def close(self):
        """Close the underlying stream immediately."""
        self.stream.close()
        return

    async def command(self, *parts):
        """Send any Redis command and return its parsed response."""
        await self.stream.send_all(self.encode_command(*parts))
        return await self._read_response()

    async def ping(self, message=None):
        """Convenience helper for Redis ``PING``."""
        if message is None:
            return await self.command("PING")
        return await self.command("PING", message)

    async def get(self, key):
        """Return the current value for ``key``."""
        return await self.command("GET", key)

    async def set(self, key, value):
        """Set ``key`` to ``value`` and return the Redis response."""
        return await self.command("SET", key, value)

    async def delete(self, *keys):
        """Delete one or more keys and return the number removed."""
        return await self.command("DEL", *keys)

    async def publish(self, channel, message):
        """Publish a message on a Redis pub/sub channel."""
        return await self.command("PUBLISH", channel, message)

    async def subscribe(self, *channels):
        """Subscribe to one or more channels and return the ack events."""
        if not channels:
            return []
        await self.stream.send_all(self.encode_command("SUBSCRIBE", *channels))
        responses = []
        while len(responses) < len(channels):
            responses.append(await self.read_pubsub_event())
        return responses

    async def read_pubsub_event(self):
        """Read and normalize one Redis pub/sub event."""
        response = await self._read_response()
        if not isinstance(response, list) or not response:
            return {"type": "unknown", "data": response}

        event_type = self._response_tag(response[0])
        if event_type == "message" and len(response) >= 3:
            return {
                "type": "message",
                "channel": response[1],
                "data": response[2],
            }
        if event_type == "subscribe" and len(response) >= 3:
            return {
                "type": "subscribe",
                "channel": response[1],
                "count": response[2],
            }
        return {"type": event_type, "data": response[1:]}

    async def _read_response(self, _depth=0):
        """Parse one RESP value from the stream."""
        if self.max_nesting_depth and _depth > self.max_nesting_depth:
            raise RedisError(
                "RESP nesting depth exceeded max_nesting_depth ({}).".format(
                    self.max_nesting_depth
                )
            )

        line = await self.stream.read_until(b"\r\n")
        prefix = line[:1]
        payload = line[1:-2]

        if prefix == b"+":
            return payload.decode("utf-8", errors="replace")

        if prefix == b"-":
            raise RedisError(payload.decode("utf-8", errors="replace"))

        if prefix == b":":
            return int(payload.decode("ascii"))

        if prefix == b"$":
            length = int(payload.decode("ascii"))
            if length == -1:
                return None
            if length < 0:
                raise RedisError("Invalid negative bulk string length.")
            if self.max_response_size and length > self.max_response_size:
                raise RedisError(
                    "Bulk string of {} bytes exceeds max_response_size ({}).".format(
                        length, self.max_response_size
                    )
                )
            data = await self.stream.read_exactly(length)
            trailer = await self.stream.read_exactly(2)
            if trailer != b"\r\n":
                raise RedisError("Malformed bulk string response.")
            return self._decode_bulk(data)

        if prefix == b"*":
            length = int(payload.decode("ascii"))
            if length == -1:
                return None
            items = []
            for _ in range(length):
                items.append(await self._read_response(_depth=_depth + 1))
            return items

        raise RedisError("Unsupported Redis response prefix {!r}".format(prefix))
