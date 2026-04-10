"""
SmallOS-native MQTT helpers.

This module implements a compact MQTT 3.1.1 client aimed at the common hobbyist
workflow: connect to a broker, publish messages at QoS 0/1/2, subscribe, and
receive messages cooperatively without threads or ``asyncio``.
"""

import warnings

from ._client_config import MISSING, resolve_client_setting
from .SmallStream import SmallStream


def _random_client_suffix(length=8):
    """Generate a short hex suffix for the default MQTT client id."""
    try:
        import os
        return os.urandom(length // 2).hex()
    except Exception:
        import time
        return "{:08x}".format(int(time.time() * 1000) & 0xFFFFFFFF)


class MQTTProtocolError(Exception):
    """Raised when the broker sends a packet the client cannot accept."""


def _encode_utf8(value):
    """Encode a string-like MQTT field with its two-byte length prefix."""
    if isinstance(value, bytes):
        data = value
    else:
        data = str(value).encode("utf-8")
    return len(data).to_bytes(2, "big") + data


def _encode_remaining_length(length):
    """Encode the MQTT variable-length remaining-length field."""
    if length < 0:
        raise ValueError("remaining length must be non-negative")

    encoded = bytearray()
    while True:
        digit = length % 128
        length //= 128
        if length:
            digit |= 0x80
        encoded.append(digit)
        if not length:
            return bytes(encoded)


class SmallMQTTClient:
    """
    Lightweight MQTT 3.1.1 client for smallOS.

    The client stays intentionally focused, but it supports the full MQTT QoS
    ladder:
    - connect / disconnect
    - publish with QoS 0, 1, or 2
    - subscribe at QoS 0, 1, or 2
    - inbound message receive with the required QoS acknowledgements
    - optional username/password auth and TLS on transports that support it
    """

    def __init__(
        self,
        task,
        host,
        port=None,
        client_id=None,
        use_tls=False,
        server_hostname=None,
        tls_ca_file=None,
        tls_cert_file=None,
        tls_key_file=None,
        tls_verify=True,
        username=None,
        password=None,
        keepalive=MISSING,
        clean_session=True,
        decode_responses=True,
        max_packet_size=MISSING,
        max_queued_messages=MISSING,
        max_buffer_size=MISSING,
    ):
        if client_id is None:
            client_id = "smallos-" + _random_client_suffix()
        self.task = task
        self.client_id = client_id
        self.username = username
        self.password = password
        self.keepalive = resolve_client_setting(
            task,
            "mqtt",
            "keepalive",
            keepalive,
            60,
        )
        self.clean_session = clean_session
        self.decode_responses = decode_responses
        self.max_packet_size = resolve_client_setting(
            task,
            "mqtt",
            "max_packet_size",
            max_packet_size,
            256 * 1024,
        )
        self.max_queued_messages = resolve_client_setting(
            task,
            "mqtt",
            "max_queued_messages",
            max_queued_messages,
            1024,
        )
        self.max_buffer_size = resolve_client_setting(
            task,
            "mqtt",
            "max_buffer_size",
            max_buffer_size,
            16 * 1024 * 1024,
        )
        self._packet_id = 0
        self._message_queue = []
        self._pending_incoming_qos2 = {}

        effective_tls = use_tls
        if (username is not None or password is not None) and not use_tls:
            warnings.warn(
                "MQTT credentials are being sent without TLS. "
                "Set use_tls=True to encrypt the connection.",
                stacklevel=2,
            )

        self.stream = SmallStream(
            task,
            host=host,
            port=port or (8883 if effective_tls else 1883),
            use_tls=effective_tls,
            server_hostname=server_hostname,
            tls_ca_file=tls_ca_file,
            tls_cert_file=tls_cert_file,
            tls_key_file=tls_key_file,
            tls_verify=tls_verify,
            max_buffer_size=self.max_buffer_size,
        )

    def _next_packet_id(self):
        """Return the next MQTT packet identifier, skipping zero."""
        self._packet_id = (self._packet_id % 65535) + 1
        return self._packet_id

    def _decode_payload(self, data):
        """Decode bytes to text when response decoding is enabled."""
        if not self.decode_responses:
            return data
        return data.decode("utf-8", errors="replace")

    def _enqueue_message(self, message):
        """Append a message to the internal queue, enforcing the size limit."""
        if self.max_queued_messages and len(self._message_queue) >= self.max_queued_messages:
            raise MQTTProtocolError(
                "Inbound message queue exceeded max_queued_messages ({}).".format(
                    self.max_queued_messages
                )
            )
        self._message_queue.append(message)

    @staticmethod
    def _packet_id_bytes(packet_id):
        """Encode one MQTT packet identifier."""
        return int(packet_id).to_bytes(2, "big")

    def _read_packet_id(self, payload, packet_name):
        """Extract the two-byte MQTT packet identifier from an ack packet."""
        if len(payload) != 2:
            raise MQTTProtocolError("{} must carry exactly one packet identifier.".format(packet_name))
        return int.from_bytes(payload[:2], "big")

    async def _send_packet_id_ack(self, fixed_header, packet_id):
        """Send a two-byte-identifier MQTT control packet."""
        packet = bytes([fixed_header, 0x02]) + self._packet_id_bytes(packet_id)
        await self.stream.send_all(packet)

    async def _wait_for_packet(self, expected_type, packet_id=None, packet_name=None):
        """
        Wait for a specific MQTT control packet while still servicing inbound work.

        Publish acknowledgements may arrive while the broker is also delivering
        other messages. This loop keeps the connection healthy by handling those
        side packets instead of stalling until the exact ack shows up.
        """
        packet_name = packet_name or "packet type {}".format(expected_type)
        while True:
            packet_type, flags, payload = await self._read_packet()
            if packet_type == expected_type:
                if packet_id is None:
                    return flags, payload
                received_id = self._read_packet_id(payload, packet_name)
                if received_id == packet_id:
                    return flags, payload

            message = await self._handle_incoming_packet(packet_type, flags, payload)
            if message is not None:
                self._enqueue_message(message)

    async def connect(self):
        """Open the transport and complete the MQTT CONNECT handshake."""
        await self.stream.connect()

        connect_flags = 0
        if self.clean_session:
            connect_flags |= 0x02
        if self.username is not None:
            connect_flags |= 0x80
        if self.password is not None:
            connect_flags |= 0x40

        variable_header = (
            _encode_utf8("MQTT")
            + bytes([0x04, connect_flags])
            + int(self.keepalive).to_bytes(2, "big")
        )
        payload = _encode_utf8(self.client_id)
        if self.username is not None:
            payload += _encode_utf8(self.username)
        if self.password is not None:
            payload += _encode_utf8(self.password)

        packet = b"\x10" + _encode_remaining_length(len(variable_header) + len(payload)) + variable_header + payload
        await self.stream.send_all(packet)

        packet_type, _, payload = await self._read_packet()
        if packet_type != 2 or len(payload) != 2:
            raise MQTTProtocolError("Expected CONNACK packet from broker.")
        if payload[1] != 0:
            raise MQTTProtocolError("Broker rejected CONNECT with code {}.".format(payload[1]))
        return self

    async def disconnect(self):
        """Send MQTT DISCONNECT and close the stream."""
        await self.stream.send_all(b"\xE0\x00")
        self.stream.close()
        return

    async def ping(self):
        """Send PINGREQ and wait for PINGRESP."""
        await self.stream.send_all(b"\xC0\x00")
        while True:
            packet_type, flags, payload = await self._read_packet()
            if packet_type == 13:
                return payload
            message = await self._handle_incoming_packet(packet_type, flags, payload)
            if message is not None:
                self._enqueue_message(message)

    async def publish(self, topic, payload, qos=0, retain=False):
        """Publish one message at QoS 0, 1, or 2."""
        if qos not in (0, 1, 2):
            raise ValueError("Only QoS 0, 1, and 2 publishes are supported.")

        payload_bytes = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        variable_header = _encode_utf8(topic)
        packet_id = None
        if qos > 0:
            packet_id = self._next_packet_id()
            variable_header += self._packet_id_bytes(packet_id)

        fixed_header = 0x30 | (qos << 1) | (0x01 if retain else 0x00)
        packet = bytes([fixed_header]) + _encode_remaining_length(len(variable_header) + len(payload_bytes))
        packet += variable_header + payload_bytes
        await self.stream.send_all(packet)

        if qos == 0:
            return {"qos": 0, "packet_id": None}

        if qos == 1:
            await self._wait_for_packet(4, packet_id=packet_id, packet_name="PUBACK")
            return {"qos": 1, "packet_id": packet_id}

        await self._wait_for_packet(5, packet_id=packet_id, packet_name="PUBREC")
        await self._send_packet_id_ack(0x62, packet_id)
        await self._wait_for_packet(7, packet_id=packet_id, packet_name="PUBCOMP")
        return {"qos": 2, "packet_id": packet_id}

    async def subscribe(self, topic, qos=0):
        """Subscribe to one topic and wait for SUBACK."""
        if qos not in (0, 1, 2):
            raise ValueError("Only QoS 0, 1, and 2 subscriptions are supported.")

        packet_id = self._next_packet_id()
        variable_header = self._packet_id_bytes(packet_id)
        payload = _encode_utf8(topic) + bytes([qos])
        packet = b"\x82" + _encode_remaining_length(len(variable_header) + len(payload)) + variable_header + payload
        await self.stream.send_all(packet)

        while True:
            packet_type, flags, packet_payload = await self._read_packet()
            if packet_type == 9:
                if len(packet_payload) < 3:
                    raise MQTTProtocolError("Malformed SUBACK packet.")
                ack_id = int.from_bytes(packet_payload[:2], "big")
                if ack_id != packet_id:
                    raise MQTTProtocolError("Received SUBACK for unexpected packet id {}.".format(ack_id))
                granted_qos = packet_payload[2]
                if granted_qos == 0x80:
                    raise MQTTProtocolError("Broker rejected subscription to {!r}.".format(topic))
                if granted_qos not in (0, 1, 2):
                    raise MQTTProtocolError("Broker returned unsupported SUBACK QoS {}.".format(granted_qos))
                return {"topic": topic, "granted_qos": granted_qos, "packet_id": packet_id}
            message = await self._handle_incoming_packet(packet_type, flags, packet_payload)
            if message is not None:
                self._enqueue_message(message)

    async def receive_message(self):
        """Return the next inbound MQTT publish event."""
        if self._message_queue:
            return self._message_queue.pop(0)

        while True:
            packet_type, flags, payload = await self._read_packet()
            message = await self._handle_incoming_packet(packet_type, flags, payload)
            if message is not None:
                return message

    async def _read_packet(self):
        """Read one complete MQTT packet."""
        fixed_header = await self.stream.read_exactly(1)
        first = fixed_header[0]
        packet_type = first >> 4
        flags = first & 0x0F

        multiplier = 1
        remaining_length = 0
        while True:
            digit = (await self.stream.read_exactly(1))[0]
            remaining_length += (digit & 0x7F) * multiplier
            if not (digit & 0x80):
                break
            multiplier *= 128
            if multiplier > 128**4:
                raise MQTTProtocolError("Malformed MQTT remaining length field.")

        if self.max_packet_size and remaining_length > self.max_packet_size:
            raise MQTTProtocolError(
                "MQTT packet payload of {} bytes exceeds max_packet_size ({}).".format(
                    remaining_length, self.max_packet_size
                )
            )

        payload = await self.stream.read_exactly(remaining_length)
        return packet_type, flags, payload

    async def _handle_incoming_packet(self, packet_type, flags, payload):
        """Handle one inbound packet and return a message event when applicable."""
        if packet_type == 3:
            return await self._parse_publish_packet_async(payload, flags=flags)
        if packet_type == 6:
            return await self._handle_pubrel(payload, flags=flags)
        if packet_type in (13, 9):
            return None
        return None

    def _parse_publish_packet(self, flags, payload):
        """Parse an inbound MQTT PUBLISH packet without side effects."""
        qos = (flags >> 1) & 0x03
        topic_length = int.from_bytes(payload[:2], "big")
        cursor = 2
        topic = payload[cursor:cursor + topic_length]
        cursor += topic_length
        packet_id = None
        if qos > 0:
            packet_id = int.from_bytes(payload[cursor:cursor + 2], "big")
            cursor += 2
        body = payload[cursor:]
        return {
            "topic": self._decode_payload(topic),
            "payload": self._decode_payload(body),
            "qos": qos,
            "packet_id": packet_id,
        }

    async def _parse_publish_packet_async(self, payload, flags=0):
        """Parse a publish packet and perform the required receiver-side QoS flow."""
        message = self._parse_publish_packet(flags, payload)
        if message["qos"] == 1 and message["packet_id"] is not None:
            await self._send_packet_id_ack(0x40, message["packet_id"])
            return message

        if message["qos"] == 2 and message["packet_id"] is not None:
            packet_id = message["packet_id"]
            if packet_id not in self._pending_incoming_qos2:
                if self.max_queued_messages and len(self._pending_incoming_qos2) >= self.max_queued_messages:
                    raise MQTTProtocolError(
                        "Pending QoS 2 messages exceeded max_queued_messages ({}).".format(
                            self.max_queued_messages
                        )
                    )
                # QoS 2 delivery is completed only after PUBREL, so we stage
                # the message here and release it when the broker confirms.
                self._pending_incoming_qos2[packet_id] = message
            await self._send_packet_id_ack(0x50, packet_id)
            return None

        if message["qos"] > 2:
            raise MQTTProtocolError("Inbound publish used unsupported QoS {}.".format(message["qos"]))
        return message

    async def _handle_pubrel(self, payload, flags=0):
        """Finish an inbound QoS 2 publish exchange and release the stored message."""
        if flags != 0x02:
            raise MQTTProtocolError("PUBREL packets must use flags 0x02.")

        packet_id = self._read_packet_id(payload, "PUBREL")
        await self._send_packet_id_ack(0x70, packet_id)
        return self._pending_incoming_qos2.pop(packet_id, None)
