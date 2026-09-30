"""
An in-process MQTT 3.1.1 broker, just enough to test our client against.

Not a mock: it is a real socket server that parses the wire format and replies
with real CONNACK/PUBACK/SUBACK packets, so a framing mistake in the client
fails the test instead of being agreed with by a stub.
"""

from __future__ import annotations

import socket
import struct
import threading
from dataclasses import dataclass, field

CONNECT, CONNACK = 0x10, 0x20
PUBLISH, PUBACK = 0x30, 0x40
SUBSCRIBE, SUBACK = 0x80, 0x90
PINGREQ, PINGRESP = 0xC0, 0xD0
DISCONNECT = 0xE0


@dataclass
class RecordedMessage:
    topic: str
    payload: bytes
    retain: bool
    qos: int


@dataclass
class TestBroker:
    """Accepts one client at a time and records what it publishes."""

    accept_code: int = 0
    require_auth: bool = False
    messages: list[RecordedMessage] = field(default_factory=list)
    subscriptions: list[str] = field(default_factory=list)
    connect_fields: dict = field(default_factory=dict)

    _server: socket.socket | None = field(default=None, repr=False)
    _thread: threading.Thread | None = field(default=None, repr=False)
    _client: socket.socket | None = field(default=None, repr=False)
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _ready: threading.Event = field(default_factory=threading.Event, repr=False)

    # -- lifecycle ---------------------------------------------------------- #

    def __enter__(self) -> "TestBroker":
        self._server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server.bind(("127.0.0.1", 0))
        self._server.listen(1)
        self._server.settimeout(0.2)
        self.port = self._server.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        for sock in (self._client, self._server):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- server loop -------------------------------------------------------- #

    def _serve(self) -> None:
        assert self._server is not None
        while not self._stop.is_set():
            try:
                client, _ = self._server.accept()
            except (socket.timeout, OSError):
                continue
            self._client = client
            client.settimeout(0.5)
            try:
                self._handle(client)
            except (OSError, ValueError):
                pass
            finally:
                try:
                    client.close()
                except OSError:
                    pass
                self._client = None

    def _handle(self, client: socket.socket) -> None:
        buffer = bytearray()
        while not self._stop.is_set():
            packet = _read_packet(client, buffer)
            if packet is None:
                continue
            packet_type, flags, body = packet

            if packet_type == CONNECT:
                self.connect_fields = _parse_connect(body)
                if self.require_auth and not self.connect_fields.get("username"):
                    client.sendall(bytes([CONNACK, 2, 0, 4]))
                    return
                client.sendall(bytes([CONNACK, 2, 0, self.accept_code]))
                if self.accept_code != 0:
                    return

            elif packet_type & 0xF0 == PUBLISH:
                qos = (packet_type >> 1) & 0x03
                retain = bool(packet_type & 0x01)
                topic, payload, packet_id = _parse_publish(body, qos)
                self.messages.append(
                    RecordedMessage(topic, payload, retain, qos)
                )
                if qos == 1:
                    client.sendall(bytes([PUBACK, 2]) + struct.pack("!H", packet_id))
                self._deliver(topic, payload)

            elif packet_type & 0xF0 == SUBSCRIBE:
                # Masked, because SUBSCRIBE's low nibble is the mandatory 0x02.
                packet_id = struct.unpack("!H", body[:2])[0]
                topic = _read_string(body, 2)
                self.subscriptions.append(topic)
                client.sendall(bytes([SUBACK, 3]) + struct.pack("!H", packet_id) + b"\x00")

            elif packet_type == PINGREQ:
                client.sendall(bytes([PINGRESP, 0]))

            elif packet_type == DISCONNECT:
                return

    def _deliver(self, topic: str, payload: bytes) -> None:
        """Echo a publish back to subscribers, so the client's read path is exercised."""
        if self._client is None or not self.subscriptions:
            return
        for subscription in self.subscriptions:
            if _matches(subscription, topic):
                body = _mqtt_string(topic) + payload
                packet = bytes([PUBLISH]) + _remaining_length(len(body)) + body
                try:
                    self._client.sendall(packet)
                except OSError:
                    pass
                return

    # -- helpers ------------------------------------------------------------ #

    def wait_for_messages(self, count: int, timeout: float = 5.0) -> bool:
        deadline = threading.Event()
        waited = 0.0
        while waited < timeout:
            if len(self.messages) >= count:
                return True
            deadline.wait(0.05)
            waited += 0.05
        return len(self.messages) >= count

    def topics(self) -> list[str]:
        return [m.topic for m in self.messages]


# --------------------------------------------------------------------------- #
# wire format helpers


def _read_packet(sock: socket.socket, buffer: bytearray) -> tuple[int, int, bytes] | None:
    """Read one packet, or None when nothing arrived within the socket timeout."""
    try:
        header = _need(sock, buffer, 1)
    except (socket.timeout, TimeoutError):
        return None
    if header is None:
        return None

    remaining = 0
    multiplier = 1
    for _ in range(4):
        byte = _need(sock, buffer, 1)
        if byte is None:
            return None
        remaining += (byte[0] & 0x7F) * multiplier
        if not byte[0] & 0x80:
            break
        multiplier *= 128

    body = _need(sock, buffer, remaining) if remaining else b""
    if body is None:
        return None
    # Return the whole first byte, not just the 4-bit type: the callers compare
    # against CONNECT/PUBLISH constants like 0x10 and 0x30, which are full bytes.
    return header[0], header[0] & 0x0F, bytes(body)


def _need(sock: socket.socket, buffer: bytearray, count: int) -> bytes | None:
    while len(buffer) < count:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            raise
        if not chunk:
            return None
        buffer.extend(chunk)
    data = bytes(buffer[:count])
    del buffer[:count]
    return data


def _parse_connect(body: bytes) -> dict:
    length = struct.unpack("!H", body[:2])[0]
    offset = 2 + length
    level = body[offset]
    flags = body[offset + 1]
    offset += 4  # level, flags, keepalive(2)
    client_id = _read_string(body, offset)
    offset += 2 + len(client_id.encode())
    out = {"protocol_level": level, "clean": bool(flags & 0x02), "client_id": client_id}
    if flags & 0x80:
        username = _read_string(body, offset)
        out["username"] = username
        offset += 2 + len(username.encode())
    if flags & 0x40:
        password = _read_string(body, offset)
        out["password"] = password
    return out


def _parse_publish(body: bytes, qos: int) -> tuple[str, bytes, int]:
    topic = _read_string(body, 0)
    offset = 2 + len(topic.encode())
    packet_id = 0
    if qos:
        packet_id = struct.unpack("!H", body[offset : offset + 2])[0]
        offset += 2
    return topic, body[offset:], packet_id


def _read_string(data: bytes, offset: int) -> str:
    length = struct.unpack("!H", data[offset : offset + 2])[0]
    return data[offset + 2 : offset + 2 + length].decode()


def _mqtt_string(value: str) -> bytes:
    encoded = value.encode()
    return struct.pack("!H", len(encoded)) + encoded


def _remaining_length(length: int) -> bytes:
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        out.append(byte)
        if not length:
            return bytes(out)


def _matches(subscription: str, topic: str) -> bool:
    if subscription == topic:
        return True
    sub, top = subscription.split("/"), topic.split("/")
    for index, part in enumerate(sub):
        if part == "#":
            return True
        if index >= len(top):
            return False
        if part != "+" and part != top[index]:
            return False
    return len(sub) == len(top)
