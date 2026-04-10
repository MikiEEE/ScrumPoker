import asyncio
import os
import sys
import unittest


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


from smallos_websocket_server import SmallWebSocketServerConnection


def _masked_client_frame(opcode, payload=b"", fin=True, mask=b"\x01\x02\x03\x04"):
    payload = bytes(payload)
    first_byte = (0x80 if fin else 0x00) | (opcode & 0x0F)
    length = len(payload)
    header = bytearray([first_byte])

    if length < 126:
        header.append(0x80 | length)
    elif length <= 0xFFFF:
        header.append(0x80 | 126)
        header.extend(length.to_bytes(2, "big"))
    else:
        header.append(0x80 | 127)
        header.extend(length.to_bytes(8, "big"))

    header.extend(mask)
    masked = bytearray(payload)
    for index in range(length):
        masked[index] ^= mask[index % 4]
    header.extend(masked)
    return bytes(header)


class FakeSocket:
    def __init__(self, incoming=b""):
        self.incoming = bytearray(incoming)
        self.sent = []


class FakeKernel:
    def socket_send(self, sock, data):
        sock.sent.append(bytes(data))
        return len(data)

    def socket_recv(self, sock, size):
        if not sock.incoming:
            return b""
        chunk = bytes(sock.incoming[:size])
        del sock.incoming[:size]
        return chunk

    def socket_needs_read(self, exc):
        return False

    def socket_needs_write(self, exc):
        return False


class FakeOS:
    def __init__(self):
        self.kernel = FakeKernel()


class FakeTask:
    def __init__(self):
        self.OS = FakeOS()

    async def wait_readable(self, sock):
        raise AssertionError("wait_readable should not be called in this test")

    async def wait_writable(self, sock):
        raise AssertionError("wait_writable should not be called in this test")


class TestSmallOSWebSocketServer(unittest.TestCase):
    def test_accept_sends_upgrade_response(self):
        async def scenario():
            task = FakeTask()
            sock = FakeSocket()
            headers = {
                "connection": "Upgrade",
                "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==",
                "sec-websocket-version": "13",
                "upgrade": "websocket",
            }

            connection = await SmallWebSocketServerConnection.accept(task, sock, headers)

            self.assertTrue(connection.connected)
            self.assertEqual(1, len(sock.sent))
            self.assertIn(b"HTTP/1.1 101 Switching Protocols", sock.sent[0])
            self.assertIn(b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=", sock.sent[0])

        asyncio.run(scenario())

    def test_receive_auto_replies_to_ping_then_returns_text(self):
        async def scenario():
            task = FakeTask()
            incoming = (
                _masked_client_frame(0x9, b"ok")
                + _masked_client_frame(0x1, b"hello")
            )
            sock = FakeSocket(incoming=incoming)
            connection = SmallWebSocketServerConnection(task, sock)

            message = await connection.receive()

            self.assertEqual({"type": "text", "data": "hello"}, message)
            self.assertEqual(b"\x8a\x02ok", sock.sent[0])

        asyncio.run(scenario())

    def test_send_text_emits_unmasked_server_frame(self):
        async def scenario():
            task = FakeTask()
            sock = FakeSocket()
            connection = SmallWebSocketServerConnection(task, sock)

            await connection.send_text("hi")

            self.assertEqual([b"\x81\x02hi"], sock.sent)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
