"""Tests for the hand-rolled MQTT client, against a real in-process broker."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.test_broker import TestBroker  # noqa: E402
from xpengmac.mqtt import (  # noqa: E402
    MQTTClient,
    MQTTConnectError,
    MQTTError,
    encode_remaining_length,
    encode_string,
    _topic_matches,
)


class TestEncoding(unittest.TestCase):
    def test_remaining_length_single_byte(self):
        self.assertEqual(encode_remaining_length(0), b"\x00")
        self.assertEqual(encode_remaining_length(127), b"\x7f")

    def test_remaining_length_multi_byte(self):
        # The canonical example from the MQTT 3.1.1 spec.
        self.assertEqual(encode_remaining_length(128), b"\x80\x01")
        self.assertEqual(encode_remaining_length(16383), b"\xff\x7f")
        self.assertEqual(encode_remaining_length(2097151), b"\xff\xff\x7f")

    def test_remaining_length_rejects_out_of_range(self):
        with self.assertRaises(ValueError):
            encode_remaining_length(268_435_456)

    def test_encode_string_is_length_prefixed(self):
        self.assertEqual(encode_string("MQTT"), b"\x00\x04MQTT")

    def test_encode_string_rejects_overlong(self):
        with self.assertRaises(ValueError):
            encode_string("x" * 65536)


class TestTopicMatching(unittest.TestCase):
    def test_exact(self):
        self.assertTrue(_topic_matches("a/b", "a/b"))
        self.assertFalse(_topic_matches("a/b", "a/c"))

    def test_single_level_wildcard(self):
        self.assertTrue(_topic_matches("a/+/c", "a/b/c"))
        self.assertFalse(_topic_matches("a/+/c", "a/b/d"))
        self.assertFalse(_topic_matches("a/+", "a/b/c"))

    def test_multi_level_wildcard(self):
        self.assertTrue(_topic_matches("a/#", "a/b/c/d"))
        self.assertTrue(_topic_matches("#", "anything/at/all"))

    def test_length_must_match_without_wildcard(self):
        self.assertFalse(_topic_matches("a/b", "a/b/c"))


class TestAgainstBroker(unittest.TestCase):
    def test_connect_sends_credentials_and_reads_connack(self):
        with TestBroker() as broker:
            client = MQTTClient(
                host="127.0.0.1",
                port=broker.port,
                username="mqtt",
                password="secret",
                client_id="test-client",
            )
            client.connect()
            self.assertTrue(client.connected)
            client.close()

            self.assertEqual(broker.connect_fields["client_id"], "test-client")
            self.assertEqual(broker.connect_fields["username"], "mqtt")
            self.assertEqual(broker.connect_fields["password"], "secret")
            self.assertEqual(broker.connect_fields["protocol_level"], 4)
            self.assertTrue(broker.connect_fields["clean"])

    def test_refused_connection_raises_with_the_reason(self):
        with TestBroker(accept_code=5) as broker:
            client = MQTTClient(host="127.0.0.1", port=broker.port, username="mqtt")
            with self.assertRaises(MQTTConnectError) as caught:
                client.connect()
            self.assertEqual(caught.exception.code, 5)
            self.assertIn("not authorized", str(caught.exception))
            self.assertFalse(client.connected)

    def test_connect_hint_mentions_missing_username(self):
        with TestBroker(accept_code=4) as broker:
            client = MQTTClient(host="127.0.0.1", port=broker.port)
            with self.assertRaises(MQTTConnectError) as caught:
                client.connect()
            self.assertIn("username", str(caught.exception))

    def test_publish_qos1_is_retained_and_acknowledged(self):
        with TestBroker() as broker:
            with MQTTClient(host="127.0.0.1", port=broker.port) as client:
                client.publish("some/topic", "hello", qos=1, retain=True)
            self.assertTrue(broker.wait_for_messages(1))
            message = broker.messages[0]
            self.assertEqual(message.topic, "some/topic")
            self.assertEqual(message.payload, b"hello")
            self.assertTrue(message.retain)
            self.assertEqual(message.qos, 1)

    def test_publish_empty_payload_round_trips(self):
        """An empty retained payload is how a discovery entity is deleted."""
        with TestBroker() as broker:
            with MQTTClient(host="127.0.0.1", port=broker.port) as client:
                client.publish("homeassistant/sensor/x/y/config", "", qos=1, retain=True)
            self.assertTrue(broker.wait_for_messages(1))
            self.assertEqual(broker.messages[0].payload, b"")
            self.assertTrue(broker.messages[0].retain)

    def test_utf8_payload_survives(self):
        payload = "XPENG G6 — 26.5°C"
        with TestBroker() as broker:
            with MQTTClient(host="127.0.0.1", port=broker.port) as client:
                client.publish("t", payload)
            self.assertTrue(broker.wait_for_messages(1))
            self.assertEqual(broker.messages[0].payload.decode(), payload)

    def test_subscribe_then_receive_echoed_message(self):
        with TestBroker() as broker:
            received: list[tuple[str, bytes]] = []
            with MQTTClient(host="127.0.0.1", port=broker.port) as client:
                client.subscribe(
                    "homeassistant/status",
                    lambda topic, payload: received.append((topic, payload)),
                )
                self.assertIn("homeassistant/status", broker.subscriptions)

                client.publish("homeassistant/status", "online", qos=0)
                for _ in range(20):
                    client.poll(timeout=0.25)
                    if received:
                        break

            self.assertEqual(received, [("homeassistant/status", b"online")])

    def test_publish_to_unreachable_port_raises(self):
        client = MQTTClient(host="127.0.0.1", port=1)  # nothing listens on port 1
        with self.assertRaises(MQTTError):
            client.connect()
        self.assertFalse(client.connected)


if __name__ == "__main__":
    unittest.main()
