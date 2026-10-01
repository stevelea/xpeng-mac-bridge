"""
Registry, reader and bridge tests.

The registry checks are the important ones: they enforce the rules Home Assistant
applies at runtime (a device_class that is not valid for the component makes it
reject the whole discovery payload), which is not something a typo would
otherwise surface until the entity silently failed to appear.
"""

from __future__ import annotations

import json
import os
import tempfile
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.support import test_config  # noqa: E402
from xpeng_bridge import Bridge, RecordingPublisher  # noqa: E402
from xpengmac import config as config_module  # noqa: E402
from xpengmac import reader, signals  # noqa: E402

# Home Assistant's valid device classes. Kept explicit rather than imported so a
# bad value fails here rather than in someone's instance.
SENSOR_DEVICE_CLASSES = {
    "apparent_power", "aqi", "atmospheric_pressure", "battery", "carbon_dioxide",
    "carbon_monoxide", "current", "data_rate", "data_size", "date", "distance",
    "duration", "energy", "energy_distance", "energy_storage", "enum",
    "frequency", "gas", "humidity", "illuminance", "irradiance", "moisture",
    "monetary", "nitrogen_dioxide", "nitrogen_monoxide", "nitrous_oxide",
    "ozone", "ph", "pm1", "pm10", "pm25", "power", "power_factor",
    "precipitation", "precipitation_intensity", "pressure", "reactive_energy",
    "reactive_power", "signal_strength", "speed", "sulphur_dioxide",
    "temperature", "timestamp", "tvoc", "volatile_organic_compounds",
    "volatile_organic_compounds_parts", "voltage", "volume",
    "volume_flow_rate", "volume_storage", "water", "weight", "wind_direction",
    "wind_speed",
}

BINARY_DEVICE_CLASSES = {
    "battery", "battery_charging", "carbon_monoxide", "cold", "connectivity",
    "door", "garage_door", "gas", "heat", "light", "lock", "moisture", "motion",
    "moving", "occupancy", "opening", "plug", "power", "presence", "problem",
    "running", "safety", "smoke", "sound", "tamper", "update", "vibration",
    "window",
}

UNITS_FOR_CLASS = {
    "battery": {"%"},
    "distance": {"km"},
    "power": {"kW", "W"},
    "voltage": {"V"},
    "current": {"A", "mA"},
    "duration": {"s", "min", "h", "d"},
    "energy": {"kWh", "Wh", "MWh"},
    "temperature": {"\u00b0C", "\u00b0F", "K"},
    "speed": {"km/h"},
    "pressure": {"kPa"},
    "timestamp": {None},
}


def make_vehicle(**groups) -> reader.Vehicle:
    """A synthetic vehicle, fresh by default.

    The timestamp defaults to *now* rather than a fixed instant: a hard-coded
    one drifts past ``stale_after_seconds`` as real time passes, which made the
    availability test fail 30 minutes after it was written.
    """
    state = dict(groups)
    return reader.Vehicle(
        vin="L1NNSGHA0SB000000",
        uid="10000001",
        state=state,
        meta={"typeName": "G6", "vehicleTypeName": "F30", "saleRegionCode": "AU"},
        timestamp=datetime.now(timezone.utc),
    )


class TestRegistryIntegrity(unittest.TestCase):
    def test_object_ids_are_unique(self):
        ids = [s.object_id for s in signals.SIGNALS]
        ids += [d.object_id for d in signals.DERIVED]
        ids.append("location")
        self.assertEqual(len(ids), len(set(ids)), "duplicate object_id in the registry")

    def test_keys_are_unique(self):
        keys = [s.key for s in signals.SIGNALS]
        self.assertEqual(len(keys), len(set(keys)))

    def test_entity_keys_matches_the_registry(self):
        vehicle = make_vehicle()
        expected = len(signals.SIGNALS) + len(signals.DERIVED) + 1
        self.assertEqual(len(signals.entity_keys(vehicle)), expected)

    def test_every_sensor_device_class_is_valid(self):
        for signal in signals.SIGNALS:
            if signal.component != signals.COMPONENT_SENSOR or not signal.device_class:
                continue
            self.assertIn(
                signal.device_class,
                SENSOR_DEVICE_CLASSES,
                f"{signal.object_id}: invalid sensor device_class",
            )

    def test_every_binary_device_class_is_valid(self):
        for derived in signals.DERIVED:
            if not derived.device_class:
                continue
            self.assertIn(
                derived.device_class,
                BINARY_DEVICE_CLASSES,
                f"{derived.object_id}: invalid binary_sensor device_class",
            )

    def test_unit_matches_device_class(self):
        for signal in signals.SIGNALS:
            if not signal.unit:
                continue
            allowed = UNITS_FOR_CLASS.get(signal.device_class or "")
            if allowed is None:
                continue
            self.assertIn(
                signal.unit,
                allowed,
                f"{signal.object_id}: unit {signal.unit!r} not valid for "
                f"device_class {signal.device_class!r}",
            )

    def test_timestamp_sensors_have_no_state_class(self):
        for signal in signals.SIGNALS:
            if signal.device_class == "timestamp":
                self.assertIsNone(
                    signal.state_class,
                    f"{signal.object_id}: timestamp sensors must not set state_class",
                )

    def test_state_class_is_one_of_the_valid_values(self):
        valid = {"measurement", "total", "total_increasing"}
        for signal in signals.SIGNALS:
            if signal.state_class:
                self.assertIn(signal.state_class, valid, signal.object_id)

    def test_entity_category_is_valid(self):
        for signal in signals.SIGNALS:
            if signal.entity_category:
                self.assertIn(signal.entity_category, {"config", "diagnostic"})

    def test_derived_entries_document_their_evidence(self):
        for derived in signals.DERIVED:
            self.assertTrue(
                derived.evidence, f"{derived.object_id} has no recorded evidence"
            )


class TestDiscoveryPayloads(unittest.TestCase):
    def setUp(self):
        self.vehicle = make_vehicle()
        self.availability = "xpeng/l1nnsgha0sb000000/availability"

    def _payloads(self):
        for signal in signals.SIGNALS:
            yield (
                signals.discovery_topic(
                    "homeassistant", signal.component, self.vehicle, signal.object_id
                ),
                signals.signal_discovery(
                    signal, self.vehicle, "xpeng", self.availability
                ),
            )
        for derived in signals.DERIVED:
            yield (
                signals.discovery_topic(
                    "homeassistant", derived.component, self.vehicle, derived.object_id
                ),
                signals.derived_discovery(
                    derived, self.vehicle, "xpeng", self.availability
                ),
            )
        yield (
            signals.discovery_topic(
                "homeassistant", signals.COMPONENT_TRACKER, self.vehicle, "location"
            ),
            signals.tracker_discovery(self.vehicle, "xpeng", self.availability),
        )

    def test_required_keys_present(self):
        for topic, payload in self._payloads():
            self.assertTrue(topic.startswith("homeassistant/"), topic)
            self.assertTrue(topic.endswith("/config"), topic)
            for key in ("name", "unique_id", "default_entity_id", "device", "origin"):
                self.assertIn(key, payload, f"{topic} missing {key}")

    def test_origin_is_well_formed(self):
        for topic, payload in self._payloads():
            self.assertIn("name", payload["origin"], topic)

    def test_device_block_is_well_formed(self):
        for topic, payload in self._payloads():
            device = payload["device"]
            self.assertIn("identifiers", device, topic)
            self.assertIn("name", device, topic)
            self.assertEqual(device["name"], self.vehicle.vin)
            self.assertEqual(device["manufacturer"], "XPENG")

    def test_default_entity_id_matches_component(self):
        for topic, payload in self._payloads():
            component = topic.split("/")[1]
            self.assertTrue(
                payload["default_entity_id"].startswith(f"{component}."),
                f"{topic}: default_entity_id {payload['default_entity_id']!r} "
                f"does not start with {component}.",
            )

    def test_unique_ids_are_unique(self):
        ids = [payload["unique_id"] for _, payload in self._payloads()]
        self.assertEqual(len(ids), len(set(ids)))

    def test_payloads_are_json_serialisable(self):
        for topic, payload in self._payloads():
            json.dumps(payload)
            self.assertNotIn("None", json.dumps(payload), topic)

    def test_availability_is_wired_up(self):
        for topic, payload in self._payloads():
            self.assertEqual(payload["availability_topic"], self.availability)
            self.assertEqual(payload["payload_available"], "online")
            self.assertEqual(payload["payload_not_available"], "offline")

    def test_tracker_uses_attributes_topic_not_state_topic(self):
        """Coordinates on state_topic leave the entity showing raw JSON."""
        payload = signals.tracker_discovery(self.vehicle, "xpeng", self.availability)
        self.assertNotIn("state_topic", payload)
        self.assertEqual(payload["source_type"], "gps")
        self.assertEqual(
            payload["json_attributes_topic"], "xpeng/l1nnsgha0sb000000/location"
        )


class TestValues(unittest.TestCase):
    def test_float_noise_is_rounded(self):
        # The car really does send this.
        self.assertEqual(signals.format_value(6.9000000953674316, 2), "6.9")

    def test_whole_floats_lose_the_decimal(self):
        self.assertEqual(signals.format_value(6.0, 2), "6")

    def test_booleans_render_as_ha_payloads(self):
        self.assertEqual(signals.format_value(True, None), "ON")
        self.assertEqual(signals.format_value(False, None), "OFF")

    def test_none_is_published_as_nothing(self):
        self.assertIsNone(signals.format_value(None, 2))

    def test_empty_string_is_nothing(self):
        self.assertIsNone(signals.format_value("   ", None))

    def test_datetime_is_isoformatted(self):
        moment = datetime(2026, 9, 30, 3, 44, 14, tzinfo=timezone.utc)
        self.assertEqual(signals.format_value(moment, None), moment.isoformat())


class TestDerivations(unittest.TestCase):
    def test_charging_uses_the_csv_parsers_threshold(self):
        self.assertTrue(
            signals.is_charging(make_vehicle(**{"charge": {"power": 6.9}}))
        )
        self.assertFalse(
            signals.is_charging(make_vehicle(**{"charge": {"power": 0.4}}))
        )
        self.assertIsNone(signals.is_charging(make_vehicle()))

    def test_any_door_open(self):
        closed = make_vehicle(
            door={"driver_door_ajar_st": 0, "bonnet_ajar_st": 0, "trunk_ajar_st": 0}
        )
        self.assertFalse(signals._derived_any_door_open(closed))
        opened = make_vehicle(door={"driver_door_ajar_st": 0, "trunk_ajar_st": 1})
        self.assertTrue(signals._derived_any_door_open(opened))
        self.assertIsNone(signals._derived_any_door_open(make_vehicle()))

    def test_any_window_open_ignores_sunshade_sentinels(self):
        """255 marks a sunshade that is not reporting; 100 is closed."""
        vehicle = make_vehicle(
            window={
                "front_left_window_position": 100,
                "front_right_window_position": 100,
                "rear_left_window_position": 100,
                "rear_right_window_position": 100,
                "behind_sun_shade_position": 255,
                "sky_sun_shade_position": 255,
            }
        )
        self.assertFalse(signals._derived_any_window_open(vehicle))

        ajar = make_vehicle(window={"front_left_window_position": 60})
        self.assertTrue(signals._derived_any_window_open(ajar))

    def test_parked_is_gear_four(self):
        self.assertTrue(signals.is_parked(make_vehicle(drive={"shift_state": 4})))
        self.assertFalse(signals.is_parked(make_vehicle(drive={"shift_state": 1})))

    def test_location_rejects_impossible_coordinates(self):
        bad = make_vehicle(drive={"latitude": 999.0, "longitude": 0.0})
        self.assertIsNone(signals.location_payload(bad))

    def test_location_payload_shape(self):
        vehicle = make_vehicle(
            drive={"latitude": -33.86880000000000, "longitude": 151.20930000000000,
                   "angle": 193.51}
        )
        payload = json.loads(signals.location_payload(vehicle))
        self.assertAlmostEqual(payload["latitude"], -33.868800)
        self.assertAlmostEqual(payload["longitude"], 151.209300)
        self.assertEqual(payload["course"], 193.5)
        self.assertNotIn("gps_accuracy", payload, "we must not invent an accuracy")


class TestTimestamps(unittest.TestCase):
    def test_sqlite_naive_timestamp_is_utc(self):
        """03:44:14 UTC is 13:44:14 +1000 — assuming local would shift the data."""
        parsed = reader.parse_timestamp("2026-09-30 03:44:14.646")
        self.assertEqual(parsed, datetime(2026, 9, 30, 3, 44, 14, 646000, tzinfo=timezone.utc))

    def test_compact_offset_is_accepted(self):
        parsed = reader.parse_timestamp("2026-09-30T13:44:14+1000")
        self.assertEqual(parsed.astimezone(timezone.utc).hour, 3)

    def test_z_suffix_is_accepted(self):
        self.assertIsNotNone(reader.parse_timestamp("2026-09-30T03:44:14Z"))

    def test_two_digit_fraction_is_padded(self):
        """Postgres trims trailing zeros; fromisoformat before 3.11 then returns None."""
        self.assertIsNotNone(reader.parse_timestamp("2026-09-30T03:44:14.49+00:00"))

    def test_epoch_milliseconds(self):
        parsed = reader.parse_timestamp(1790745827000)
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.tzinfo, timezone.utc)

    def test_garbage_returns_none(self):
        self.assertIsNone(reader.parse_timestamp("not a timestamp"))
        self.assertIsNone(reader.parse_timestamp(None))
        self.assertIsNone(reader.parse_timestamp(""))


class TestBridgePublishing(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = test_config(Path(self.tmp.name))
        self.config.mqtt.discovery_prefix = "homeassistant"
        self.config.mqtt.topic_prefix = "xpeng"
        self.publisher = RecordingPublisher(echo=False)
        self.bridge = Bridge(self.config, self.publisher)
        self.vehicle = make_vehicle(
            charge={"battery_soc": 68, "power": 6.9},
            odometer={"avalible_driving_distance": 379, "total_mileage": 60015},
            drive={"latitude": -33.868800, "longitude": 151.209300, "shift_state": 4},
        )

    def topics(self) -> list[str]:
        return [t for t, _, _ in self.publisher.messages]

    def test_publish_states_covers_every_signal(self):
        self.bridge.publish_states([self.vehicle])
        topics = self.topics()
        self.assertIn("xpeng/l1nnsgha0sb000000/battery", topics)
        self.assertIn("xpeng/l1nnsgha0sb000000/range", topics)
        self.assertIn("xpeng/l1nnsgha0sb000000/charging", topics)
        self.assertIn("xpeng/l1nnsgha0sb000000/location", topics)
        self.assertIn("xpeng/l1nnsgha0sb000000/availability", topics)
        self.assertIn("xpeng/l1nnsgha0sb000000/state", topics)

    def test_availability_online_for_fresh_data(self):
        self.bridge.publish_states([self.vehicle])
        availability = [
            payload
            for topic, payload, _ in self.publisher.messages
            if topic.endswith("/availability")
        ]
        self.assertEqual(availability, ["online"])

    def test_availability_offline_when_stale(self):
        stale = make_vehicle(charge={"battery_soc": 68})
        stale.timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.bridge.publish_states([stale])
        availability = [
            payload
            for topic, payload, _ in self.publisher.messages
            if topic.endswith("/availability")
        ]
        self.assertEqual(availability, ["offline"])

    def test_availability_is_published_last(self):
        """Entities must not flip to unavailable before their values refresh."""
        self.bridge.publish_states([self.vehicle])
        self.assertTrue(self.topics()[-1].endswith("/availability"))

    def test_missing_signals_are_skipped_not_published_as_empty(self):
        self.bridge.publish_states([self.vehicle])
        for topic, payload, _ in self.publisher.messages:
            self.assertNotEqual(payload, "", f"{topic} published an empty payload")

    def test_everything_is_retained(self):
        self.bridge.publish_states([self.vehicle])
        for topic, _, retain in self.publisher.messages:
            self.assertTrue(retain, f"{topic} was not retained")

    def test_announce_returns_the_entity_keys(self):
        keys = self.bridge.announce([self.vehicle])
        self.assertIn("sensor/battery", keys)
        self.assertIn("binary_sensor/charging", keys)
        self.assertIn("device_tracker/location", keys)
        self.assertEqual(len(keys), len(set(keys)))


class TestConfig(unittest.TestCase):
    def test_unknown_top_level_key_is_rejected(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"nope": {}}))
            with self.assertRaises(ValueError) as caught:
                config_module.load(path)
            self.assertIn("nope", str(caught.exception))

    def test_unknown_section_key_is_rejected(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"mqtt": {"hots": "typo"}}))
            with self.assertRaises(ValueError) as caught:
                config_module.load(path)
            self.assertIn("hots", str(caught.exception))

    def test_env_override_wins_over_file(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"mqtt": {"host": "from-file"}}))
            os.environ["XPENG_BRIDGE_MQTT_HOST"] = "from-env"
            try:
                loaded = config_module.load(path)
            finally:
                del os.environ["XPENG_BRIDGE_MQTT_HOST"]
            self.assertEqual(loaded.mqtt.host, "from-env")

    def test_env_port_is_coerced_to_int(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{}")
            os.environ["XPENG_BRIDGE_MQTT_PORT"] = "8883"
            try:
                loaded = config_module.load(path)
            finally:
                del os.environ["XPENG_BRIDGE_MQTT_PORT"]
            self.assertEqual(loaded.mqtt.port, 8883)


if __name__ == "__main__":
    unittest.main()


class TestAvailabilityIsClearedWhenReadFails(unittest.TestCase):
    """A failed read must not leave stale values marked as current."""

    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = test_config(Path(self.tmp.name))
        self.publisher = RecordingPublisher(echo=False)
        self.bridge = Bridge(self.config, self.publisher)
        self.vehicle = make_vehicle(charge={"battery_soc": 68})

    def test_topics_are_remembered_and_cleared(self):
        self.bridge.publish_states([self.vehicle])
        self.publisher.messages.clear()

        marked = self.bridge.mark_unavailable()

        self.assertEqual(marked, 1)
        self.assertEqual(
            self.publisher.messages,
            [("xpeng/l1nnsgha0sb000000/availability", "offline", True)],
        )

    def test_forgetting_nothing_is_not_an_error(self):
        self.assertEqual(self.bridge.mark_unavailable(), 0)
        self.assertEqual(self.publisher.messages, [])

    def test_corrupt_memory_file_is_survived(self):
        self.bridge.availability_file.write_text("{not json")
        self.assertEqual(self.bridge.mark_unavailable(), 0)

    def test_cycle_reads_before_clearing(self):
        """The happy path must not clear availability."""
        self.bridge.publish_states([self.vehicle])
        self.publisher.messages.clear()
        self.bridge.publish_states([self.vehicle])
        availability = [
            payload
            for topic, payload, _ in self.publisher.messages
            if topic.endswith("/availability")
        ]
        self.assertEqual(availability, ["online"])


class TestStateFilesFollowTheConfig(unittest.TestCase):
    """Regression for a bug that reached a live Home Assistant.

    The suite built its config with no path, which falls back to the real
    ~/.config/xpeng-mac-bridge — so publishing from a test overwrote the running
    daemon's availability_topics.json with the fixture VIN. The daemon then
    cleared a topic for a car that does not exist, and every real entity showed
    as unavailable because its availability topic had no message.
    """

    def test_state_files_sit_next_to_the_config(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            self.assertEqual(config.default_state_file().parent, Path(tmp))
            self.assertEqual(
                Bridge(config, RecordingPublisher(echo=False)).availability_file.parent,
                Path(tmp),
            )

    def test_publishing_from_a_test_does_not_write_to_the_real_config_dir(self):
        import tempfile

        real = Path.home() / ".config/xpeng-mac-bridge/availability_topics.json"
        before = real.read_text() if real.exists() else None

        with tempfile.TemporaryDirectory() as tmp:
            config = test_config(Path(tmp))
            bridge = Bridge(config, RecordingPublisher(echo=False))
            bridge.publish_states([make_vehicle(charge={"battery_soc": 60})])
            self.assertTrue(bridge.availability_file.exists())

        after = real.read_text() if real.exists() else None
        self.assertEqual(before, after, "a test wrote to the real config directory")


class TestRawLocationSensor(unittest.TestCase):
    """A device_tracker hides the position in attributes; this makes it a state."""

    def test_formats_fixed_width(self):
        vehicle = make_vehicle(
            drive={"latitude": -33.8688, "longitude": 151.2093}
        )
        self.assertEqual(signals.raw_location(vehicle), "-33.868800,151.209300")

    def test_trailing_zero_does_not_change_the_shape(self):
        """round() would give -33.8688 here and -33.868816 the next fix."""
        a = signals.raw_location(
            make_vehicle(drive={"latitude": -33.8688, "longitude": 1.0})
        )
        b = signals.raw_location(
            make_vehicle(drive={"latitude": -33.868816, "longitude": 1.0})
        )
        self.assertEqual(len(a.split(",")[0].split(".")[1]), 6)
        self.assertEqual(len(b.split(",")[0].split(".")[1]), 6)

    def test_missing_half_is_not_half_a_position(self):
        self.assertIsNone(
            signals.raw_location(make_vehicle(drive={"latitude": -33.9}))
        )
        self.assertIsNone(signals.raw_location(make_vehicle()))

    def test_impossible_coordinates_are_rejected(self):
        self.assertIsNone(
            signals.raw_location(
                make_vehicle(drive={"latitude": 999.0, "longitude": 0.0})
            )
        )

    def test_registered_as_a_visible_sensor(self):
        signal = next(
            s for s in signals.SIGNALS if s.object_id == "location_raw"
        )
        self.assertEqual(signal.component, signals.COMPONENT_SENSOR)
        # Visible on purpose: unlike the other *_raw entities, this one is the
        # only entity whose state is the position.
        self.assertIsNone(signal.entity_category)

    def test_publishes_to_its_own_topic(self):
        vehicle = make_vehicle(drive={"latitude": -33.8688, "longitude": 151.2093})
        with tempfile.TemporaryDirectory() as tmp:
            config = RecordingPublisher(echo=False)
            bridge = Bridge(test_config(Path(tmp)), config)
            bridge.publish_states([vehicle])
        topics = {t: p for t, p, _ in config.messages}
        self.assertEqual(
            topics["xpeng/l1nnsgha0sb000000/location_raw"],
            "-33.868800,151.209300",
        )


class TestNoIdentifyingDataIsCommitted(unittest.TestCase):
    """Runs the leak checker as part of the suite.

    It reads the live car and greps the checkout for its VIN, account uid and
    coordinates, in every encoding that matters — the values differ per machine,
    so a hand-written pattern cannot do this. Skipped where there is no live
    state to compare against, which is every machine but the owner's.
    """

    def test_check_leaks_passes(self):
        import subprocess

        script = Path(__file__).resolve().parent.parent / "check-leaks.py"
        result = subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True
        )
        if "could not read live state" in result.stdout or "nothing to check" in result.stdout:
            self.skipTest("no live XPENG state on this machine")
        self.assertEqual(
            result.returncode,
            0,
            f"identifying data committed:\n{result.stdout}\n{result.stderr}",
        )


class TestDrivingRule(unittest.TestCase):
    """`is_driving` is "not in Park" — it drives the adaptive refresh schedule."""

    def test_gear_four_is_parked(self):
        self.assertFalse(signals.is_driving(make_vehicle(drive={"shift_state": 4})))

    def test_every_other_gear_is_not_parked(self):
        for gear in (1, 2, 3):
            with self.subTest(gear=gear):
                self.assertTrue(
                    signals.is_driving(make_vehicle(drive={"shift_state": gear}))
                )

    def test_speed_alone_is_enough(self):
        """A moving car is not parked whatever the gear reading says."""
        self.assertTrue(signals.is_driving(make_vehicle(drive={"speed": 12})))

    def test_park_with_speed_is_still_moving(self):
        self.assertTrue(
            signals.is_driving(make_vehicle(drive={"shift_state": 4, "speed": 3}))
        )

    def test_unknown_is_not_false(self):
        """"Cannot tell" must not be reported as "not driving"."""
        self.assertIsNone(signals.is_driving(make_vehicle()))

    def test_nonsense_gear_is_not_read_as_not_park(self):
        for gear in (0, 9, 255, -1):
            with self.subTest(gear=gear):
                self.assertIsNone(
                    signals.is_driving(make_vehicle(drive={"shift_state": gear}))
                )

    def test_stationary_with_a_gear_is_not_driving(self):
        self.assertFalse(
            signals.is_driving(make_vehicle(drive={"shift_state": 4, "speed": 0}))
        )
