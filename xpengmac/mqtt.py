"""
A minimal MQTT 3.1.1 client — publish, plus the one subscription we need.

Why not paho
------------
This ships as a ``launchd`` agent that must keep working across macOS and
Homebrew Python upgrades with no virtualenv to rot. Everything else here is
stdlib, and paho is not installed on this machine. The protocol surface the
bridge actually needs is small and stable: CONNECT, PUBLISH (QoS 0/1), and a
SUBSCRIBE to Home Assistant's birth topic. That is a few hundred lines.

Deliberately NOT implemented: QoS 2, retained-message delivery on subscribe,
persistent sessions (``clean_session`` is always true), and MQTT 5. None is
needed, and each would be untested code in a daemon nobody watches.

No threads. The bridge polls on an interval, so incoming packets are drained at
the top of each tick and a PINGREQ goes out when keepalive is due. A reader
thread would buy nothing and add a class of bug that only shows up at 3am.
"""

from __future__ import annotations

import logging
import socket
import ssl
import struct
import time
from dataclasses import dataclass, field
from typing import Callable

logger = logging.getLogger(__name__)

PROTOCOL_NAME = b"MQTT"
PROTOCOL_LEVEL = 4  # 3.1.1

CONNECT, CONNACK = 0x10, 0x20
PUBLISH, PUBACK = 0x30, 0x40
SUBSCRIBE, SUBACK = 0x80, 0x90
PINGREQ, PINGRESP = 0xC0, 0xD0
DISCONNECT = 0xE0

CONNACK_MEANING = {
    0: "connection accepted",
    1: "unacceptable protocol version",
    2: "identifier rejected",
    3: "server unavailable",
    4: "bad user name or password",
    5: "not authorized",
}


class MQTTError(RuntimeError):
    """Any protocol-level or transport-level failure."""


class MQTTConnectError(MQTTError):
    """The broker refused the connection. Not worth retrying unchanged."""

    def __init__(self, code: int, detail: str = "") -> None:
        self.code = code
        meaning = CONNACK_MEANING.get(code, "unknown")
        message = f"broker refused connection: {meaning} (code {code})"
        if detail:
            message = f"{message} — {detail}"
        super().__init__(message)


def encode_remaining_length(length: int) -> bytes:
    """MQTT variable-length integer. At most 4 bytes, max 268435455."""
    if length < 0 or length > 268_435_455:
        raise ValueError(f"remaining length out of range: {length}")
    out = bytearray()
    while True:
        byte = length % 128
        length //= 128
        if length:
            byte |= 0x80
        out.append(byte)
        if not length:
            return bytes(out)


def encode_string(value: str) -> bytes:
    """MQTT UTF-8 string: 2-byte big-endian length, then the bytes."""
    encoded = value.encode("utf-8")
    if len(encoded) > 65535:
        raise ValueError("MQTT string exceeds 65535 bytes")
    return struct.pack("!H", len(encoded)) + encoded


def encode_utf8_payload(value: str | bytes) -> bytes:
    return value if isinstance(value, bytes) else value.encode("utf-8")


@dataclass
class MQTTClient:
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    client_id: str = "xpeng-mac-bridge"
    keepalive: int = 60
    use_tls: bool = False
    tls_insecure: bool = False
    ca_file: str | None = None

    _sock: socket.socket | None = field(default=None, repr=False)
    _buffer: bytearray = field(default_factory=bytearray, repr=False)
    _next_packet_id: int = field(default=1, repr=False)
    _last_outbound: float = field(default=0.0, repr=False)
    _subscriptions: dict[str, Callable[[str, bytes], None]] = field(
        default_factory=dict, repr=False
    )

    # -- lifecycle --------------------------------------------------------- #

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        """Open the socket and complete the MQTT handshake."""
        self.close()
        try:
            raw = socket.create_connection((self.host, self.port), timeout=10.0)
        except OSError as exc:
            raise MQTTError(f"cannot reach {self.host}:{self.port} — {exc}") from exc

        if self.use_tls:
            raw = self._wrap_tls(raw)

        raw.settimeout(None)
        self._sock = raw
        self._buffer.clear()

        try:
            self._sock.sendall(self._build_connect())
            self._last_outbound = time.monotonic()
            packet_type, body = self._read_packet(timeout=10.0)
        except MQTTError:
            self.close()
            raise
        except OSError as exc:
            self.close()
            raise MQTTError(f"handshake failed: {exc}") from exc

        if packet_type != CONNACK:
            self.close()
            raise MQTTError(
                f"expected CONNACK, got packet type 0x{packet_type:02x}"
            )
        if len(body) < 2:
            self.close()
            raise MQTTError("malformed CONNACK")
        code = body[1]
        if code != 0:
            self.close()
            raise MQTTConnectError(code, self._auth_hint(code))

    def _auth_hint(self, code: int) -> str:
        if code == 4 and not self.username:
            return "the broker wants a username; set username/password in the config"
        if code == 5 and self.username:
            return f"credentials for {self.username!r} were rejected"
        return ""

    def _wrap_tls(self, raw: socket.socket) -> ssl.SSLSocket:
        if self.tls_insecure:
            context = ssl._create_unverified_context()
        else:
            context = ssl.create_default_context(cafile=self.ca_file)
        try:
            return context.wrap_socket(raw, server_hostname=self.host)
        except ssl.SSLError as exc:
            raw.close()
            raise MQTTError(f"TLS handshake failed: {exc}") from exc

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.sendall(bytes([DISCONNECT, 0]))
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buffer.clear()

    def __enter__(self) -> "MQTTClient":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- outgoing packets --------------------------------------------------- #

    def _build_connect(self) -> bytes:
        flags = 0x02  # clean session
        payload = encode_string(self.client_id)
        if self.username is not None:
            flags |= 0x80
            payload += encode_string(self.username)
        if self.password is not None:
            flags |= 0x40
            payload += encode_string(self.password)

        variable = (
            encode_string(PROTOCOL_NAME.decode())
            + bytes([PROTOCOL_LEVEL, flags])
            + struct.pack("!H", self.keepalive)
        )
        body = variable + payload
        return bytes([CONNECT]) + encode_remaining_length(len(body)) + body

    def _build_publish(
        self, topic: str, payload: bytes, qos: int, retain: bool, packet_id: int
    ) -> bytes:
        header = PUBLISH | (qos << 1) | (1 if retain else 0)
        body = encode_string(topic)
        if qos:
            body += struct.pack("!H", packet_id)
        body += payload
        return bytes([header]) + encode_remaining_length(len(body)) + body

    def _build_subscribe(self, topic: str, qos: int, packet_id: int) -> bytes:
        body = struct.pack("!H", packet_id) + encode_string(topic) + bytes([qos])
        return bytes([SUBSCRIBE | 0x02]) + encode_remaining_length(len(body)) + body

    def _send(self, data: bytes) -> None:
        if self._sock is None:
            raise MQTTError("not connected")
        try:
            self._sock.sendall(data)
        except OSError as exc:
            raise MQTTError(f"send failed: {exc}") from exc
        self._last_outbound = time.monotonic()

    def _take_packet_id(self) -> int:
        packet_id = self._next_packet_id
        self._next_packet_id = 1 if packet_id >= 65535 else packet_id + 1
        return packet_id

    # -- public operations -------------------------------------------------- #

    def publish(
        self, topic: str, payload: str | bytes, *, qos: int = 1, retain: bool = True
    ) -> None:
        """Publish one message. For QoS 1, waits for the PUBACK."""
        data = encode_utf8_payload(payload)
        packet_id = self._take_packet_id() if qos else 0
        self._send(self._build_publish(topic, data, qos, retain, packet_id))
        if qos:
            self._await_puback(packet_id)

    def subscribe(
        self, topic: str, callback: Callable[[str, bytes], None], *, qos: int = 0
    ) -> None:
        # Registered *before* the SUBSCRIBE goes out, not after the SUBACK
        # arrives. A broker sends retained messages immediately after the
        # SUBACK, and _dispatch drops a message for a topic it has no callback
        # for — so registering late leaves the retained batch one scheduling
        # accident away from being discarded.
        self._subscriptions[topic] = callback

        packet_id = self._take_packet_id()
        self._send(self._build_subscribe(topic, qos, packet_id))
        deadline = time.monotonic() + 10.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._subscriptions.pop(topic, None)
                raise MQTTError(f"no SUBACK for {topic}")
            packet_type, body = self._read_packet(timeout=remaining)
            if packet_type == SUBACK:
                if len(body) >= 3 and body[2] == 0x80:
                    self._subscriptions.pop(topic, None)
                    raise MQTTError(f"broker refused subscription to {topic}")
                return
            self._dispatch(packet_type, body)

    def _await_puback(self, packet_id: int) -> None:
        deadline = time.monotonic() + 10.0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MQTTError(f"no PUBACK for packet {packet_id}")
            packet_type, body = self._read_packet(timeout=remaining)
            if packet_type == PUBACK and len(body) >= 2:
                if struct.unpack("!H", body[:2])[0] == packet_id:
                    return
                continue
            self._dispatch(packet_type, body)

    def poll(self, timeout: float = 0.0) -> None:
        """Drain incoming packets and keep the keepalive satisfied.

        ``timeout`` of 0 returns immediately once the buffer is empty.
        """
        if self._sock is None:
            raise MQTTError("not connected")

        while True:
            try:
                packet_type, body = self._read_packet(timeout=timeout)
            except TimeoutError:
                break
            self._dispatch(packet_type, body)
            timeout = 0.0  # keep draining without blocking again

        if self.keepalive:
            # Half the keepalive is the usual safety margin.
            if time.monotonic() - self._last_outbound >= self.keepalive / 2:
                self._send(bytes([PINGREQ, 0]))

    def _dispatch(self, packet_type: int, body: bytes) -> None:
        if packet_type & 0xF0 == PUBLISH:
            topic, payload = self._parse_publish(packet_type, body)
            for subscription, callback in self._subscriptions.items():
                if _topic_matches(subscription, topic):
                    try:
                        callback(topic, payload)
                    except Exception:  # noqa: BLE001 - a bad callback must not kill the daemon
                        logger.exception("subscription callback failed for %s", topic)
                    return
            logger.debug("no handler for %s", topic)

    @staticmethod
    def _parse_publish(packet_type: int, body: bytes) -> tuple[str, bytes]:
        qos = (packet_type >> 1) & 0x03
        topic_length = struct.unpack("!H", body[:2])[0]
        topic = body[2 : 2 + topic_length].decode("utf-8", "replace")
        offset = 2 + topic_length
        if qos:
            offset += 2
        return topic, body[offset:]

    # -- framing ------------------------------------------------------------ #

    def _read_packet(self, timeout: float) -> tuple[int, bytes]:
        """Read one whole MQTT packet, raising TimeoutError if none arrives."""
        if self._sock is None:
            raise MQTTError("not connected")

        deadline = time.monotonic() + timeout
        header = self._read_exactly(1, deadline)
        remaining = 0
        multiplier = 1
        for _ in range(4):
            byte = self._read_exactly(1, deadline)[0]
            remaining += (byte & 0x7F) * multiplier
            if not byte & 0x80:
                break
            multiplier *= 128
        else:
            raise MQTTError("malformed remaining length")

        body = self._read_exactly(remaining, deadline) if remaining else b""
        return header[0], body

    def _read_exactly(self, count: int, deadline: float) -> bytes:
        if self._sock is None:
            raise MQTTError("not connected")

        while len(self._buffer) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no packet within timeout")
            self._sock.settimeout(max(0.001, remaining))
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout as exc:
                raise TimeoutError("no packet within timeout") from exc
            except OSError as exc:
                raise MQTTError(f"read failed: {exc}") from exc
            if not chunk:
                raise MQTTError("broker closed the connection")
            self._buffer.extend(chunk)

        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data


def _topic_matches(subscription: str, topic: str) -> bool:
    """MQTT topic filter matching, supporting ``+`` and ``#``."""
    if subscription == topic:
        return True
    sub_parts = subscription.split("/")
    topic_parts = topic.split("/")
    for index, part in enumerate(sub_parts):
        if part == "#":
            return True
        if index >= len(topic_parts):
            return False
        if part != "+" and part != topic_parts[index]:
            return False
    return len(sub_parts) == len(topic_parts)
