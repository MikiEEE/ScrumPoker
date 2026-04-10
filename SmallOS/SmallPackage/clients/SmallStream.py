"""
Shared non-blocking byte-stream helper for smallOS protocol clients.

The scheduler already knows how to suspend a task until a socket becomes
readable or writable. This module packages that pattern into one reusable
stream abstraction so higher-level clients like Redis and MQTT can focus on
their wire protocols instead of open/connect/send/read bookkeeping.
"""

from ._client_config import MISSING, resolve_client_setting


class StreamClosedError(Exception):
    """Raised when a peer closes the stream unexpectedly."""


class StreamBufferOverflow(Exception):
    """Raised when the internal read buffer exceeds its configured limit."""


class SmallStream:
    """
    Cooperative socket stream built on top of the active smallOS kernel.

    The stream owns one socket plus an internal read buffer. All public methods
    are async because they may need to wait for socket readiness before making
    progress.
    """

    def __init__(
        self,
        task,
        host,
        port,
        use_tls=False,
        server_hostname=None,
        tls_ca_file=None,
        tls_cert_file=None,
        tls_key_file=None,
        tls_verify=True,
        max_buffer_size=MISSING,
    ):
        self.task = task
        self.host = host
        self.port = port
        self.use_tls = use_tls
        self.server_hostname = server_hostname or host
        self.tls_ca_file = tls_ca_file
        self.tls_cert_file = tls_cert_file
        self.tls_key_file = tls_key_file
        self.tls_verify = tls_verify
        self.max_buffer_size = resolve_client_setting(
            task,
            "stream",
            "max_buffer_size",
            max_buffer_size,
            16 * 1024 * 1024,
        )
        self.sock = None
        self._connected = False
        self._buffer = bytearray()

    @property
    def kernel(self):
        """Return the kernel owned by the task's runtime."""
        if not self.task or not getattr(self.task, "OS", None) or not self.task.OS.kernel:
            raise RuntimeError("SmallStream requires a task attached to a runtime with a kernel.")
        return self.task.OS.kernel

    async def connect(self):
        """Open the socket, wait for connect completion, and optionally start TLS."""
        if self._connected and self.sock is not None:
            return self

        kernel = self.kernel
        address_info = kernel.resolve_address(self.host, self.port)
        sockaddr = address_info[4]
        sock = kernel.socket_open(address_info)
        kernel.socket_setblocking(sock, False)

        try:
            connected = kernel.socket_connect(sock, sockaddr)
            if not connected:
                await self.task.wait_writable(sock)
                pending_error = kernel.socket_connection_error(sock)
                if pending_error != 0:
                    raise OSError(pending_error, "socket connect failed")

            if self.use_tls:
                sock = kernel.socket_wrap_tls_client(
                    sock,
                    server_hostname=self.server_hostname,
                    tls_ca_file=self.tls_ca_file,
                    tls_cert_file=self.tls_cert_file,
                    tls_key_file=self.tls_key_file,
                    tls_verify=self.tls_verify,
                )
                while True:
                    try:
                        kernel.socket_do_handshake(sock)
                        break
                    except Exception as exc:
                        if kernel.socket_needs_read(exc):
                            await self.task.wait_readable(sock)
                            continue
                        if kernel.socket_needs_write(exc):
                            await self.task.wait_writable(sock)
                            continue
                        raise
        except Exception:
            try:
                kernel.socket_close(sock)
            except Exception:
                pass
            raise

        self.sock = sock
        self._connected = True
        return self

    def close(self):
        """Close the underlying socket and clear buffered state."""
        if self.sock is not None:
            try:
                self.kernel.socket_close(self.sock)
            except Exception:
                pass
        self.sock = None
        self._connected = False
        self._buffer = bytearray()
        return

    async def send_all(self, data):
        """Send all bytes in ``data`` before returning."""
        if not self._connected or self.sock is None:
            await self.connect()

        view = memoryview(bytes(data))
        while view:
            try:
                sent = self.kernel.socket_send(self.sock, view)
                if sent == 0:
                    raise StreamClosedError("socket closed while sending data")
                view = view[sent:]
            except Exception as exc:
                if self.kernel.socket_needs_read(exc):
                    await self.task.wait_readable(self.sock)
                    continue
                if self.kernel.socket_needs_write(exc):
                    await self.task.wait_writable(self.sock)
                    continue
                raise
        return

    async def recv_some(self, size=4096):
        """Read and return at least one chunk from the peer."""
        if not self._connected or self.sock is None:
            await self.connect()

        while True:
            try:
                chunk = self.kernel.socket_recv(self.sock, size)
                if not chunk:
                    raise StreamClosedError("socket closed while reading data")
                return bytes(chunk)
            except Exception as exc:
                if self.kernel.socket_needs_read(exc):
                    await self.task.wait_readable(self.sock)
                    continue
                if self.kernel.socket_needs_write(exc):
                    await self.task.wait_writable(self.sock)
                    continue
                raise

    async def _fill_buffer(self, minimum):
        """Read from the socket until at least ``minimum`` buffered bytes exist."""
        if self.max_buffer_size and minimum > self.max_buffer_size:
            raise StreamBufferOverflow(
                "requested read of {} bytes exceeds max_buffer_size ({})".format(
                    minimum, self.max_buffer_size
                )
            )
        while len(self._buffer) < minimum:
            self._buffer.extend(await self.recv_some())
            if self.max_buffer_size and len(self._buffer) > self.max_buffer_size:
                raise StreamBufferOverflow(
                    "read buffer exceeded max_buffer_size ({})".format(
                        self.max_buffer_size
                    )
                )
        return

    async def read_exactly(self, size):
        """Read exactly ``size`` bytes from the stream."""
        if size < 0:
            raise ValueError("size must be non-negative")
        if size == 0:
            return b""

        await self._fill_buffer(size)
        data = bytes(self._buffer[:size])
        del self._buffer[:size]
        return data

    async def read_until(self, delimiter, max_length=0):
        """Read until and including ``delimiter``.

        If ``max_length`` is positive the search is capped at that many bytes.
        When neither ``max_length`` nor ``max_buffer_size`` is set the buffer
        can still grow without bound, but the default ``max_buffer_size``
        provides a safety net.
        """
        if not delimiter:
            raise ValueError("delimiter must not be empty")

        effective_limit = max_length or self.max_buffer_size

        while True:
            index = self._buffer.find(delimiter)
            if index != -1:
                end = index + len(delimiter)
                data = bytes(self._buffer[:end])
                del self._buffer[:end]
                return data
            if effective_limit and len(self._buffer) >= effective_limit:
                raise StreamBufferOverflow(
                    "delimiter not found within {} bytes".format(effective_limit)
                )
            self._buffer.extend(await self.recv_some())
