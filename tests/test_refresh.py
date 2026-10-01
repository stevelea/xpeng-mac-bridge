"""
Tests for the scheduled app refresh.

Every test here stubs the app control in `xpengmac.reader`, so the suite never
launches or quits a real application. That matters beyond tidiness: the whole
point of the feature is not to leave the app running, so a test that did would
contradict what it is checking.
"""

from __future__ import annotations

import logging
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# The refresh path logs warnings on its failure branches, which these tests
# deliberately exercise. Silenced so a passing run stays readable.
logging.getLogger("xpeng_bridge").setLevel(logging.CRITICAL)

from tests.support import test_config  # noqa: E402
from xpeng_bridge import Bridge, RecordingPublisher  # noqa: E402
from xpengmac import config as config_module  # noqa: E402
from xpengmac import reader  # noqa: E402


def make_vehicle(age_seconds: float) -> reader.Vehicle:
    return reader.Vehicle(
        vin="L1NNSGHA0SB000000",
        uid="10000001",
        state={"charge": {"battery_soc": 64}},
        meta={"typeName": "G6"},
        timestamp=datetime.now(timezone.utc) - timedelta(seconds=age_seconds),
    )


class RefreshTestCase(unittest.TestCase):
    def setUp(self):
        import tempfile

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = test_config(Path(self.tmp.name))
        self.config.source.refresh_app = True
        self.config.source.refresh_after_seconds = 900
        self.config.source.refresh_wait_seconds = 5
        self.publisher = RecordingPublisher(echo=False)
        self.bridge = Bridge(self.config, self.publisher)

        patcher = mock.patch.object(reader, "is_app_running", return_value=False)
        self.app_running = patcher.start()
        self.addCleanup(patcher.stop)

        patcher = mock.patch.object(reader, "launch_app", return_value=True)
        self.launch = patcher.start()
        self.addCleanup(patcher.stop)

        patcher = mock.patch.object(reader, "quit_app", return_value=True)
        self.quit = patcher.start()
        self.addCleanup(patcher.stop)


class TestRefreshDecisions(RefreshTestCase):
    def test_disabled_does_nothing(self):
        self.config.source.refresh_app = False
        self.bridge.maybe_refresh([make_vehicle(99999)])
        self.launch.assert_not_called()

    def test_fresh_cache_does_nothing(self):
        self.bridge.maybe_refresh([make_vehicle(30)])
        self.launch.assert_not_called()

    def test_running_app_is_left_alone(self):
        """Someone is using it; quitting out from under them is worse."""
        self.app_running.return_value = True
        self.bridge.maybe_refresh([make_vehicle(99999)])
        self.launch.assert_not_called()
        self.quit.assert_not_called()

    def test_empty_vehicle_list_does_nothing(self):
        self.bridge.maybe_refresh([])
        self.launch.assert_not_called()

    def test_vehicle_without_timestamp_does_nothing(self):
        vehicle = make_vehicle(0)
        vehicle.timestamp = None
        self.bridge.maybe_refresh([vehicle])
        self.launch.assert_not_called()


class TestRefreshCycle(RefreshTestCase):
    def test_stale_cache_launches_waits_and_quits(self):
        self.bridge._await_cache_advance = mock.Mock(return_value=True)
        with mock.patch.object(self.bridge, "read", return_value=[make_vehicle(1)]):
            result = self.bridge.maybe_refresh([make_vehicle(99999)])

        self.launch.assert_called_once_with("XPENG")
        self.quit.assert_called_once_with("XPENG")
        # The re-read is what gets returned, so fresh values are published.
        self.assertEqual(result[0].age_seconds < 60, True)

    def test_app_is_closed_even_when_the_cache_never_moves(self):
        """An app left running is exactly what this feature exists to prevent."""
        self.bridge._await_cache_advance = mock.Mock(return_value=False)
        with mock.patch.object(self.bridge, "read", return_value=[make_vehicle(99999)]):
            self.bridge.maybe_refresh([make_vehicle(99999)])
        self.quit.assert_called_once_with("XPENG")

    def test_app_is_closed_even_if_the_wait_raises(self):
        self.bridge._await_cache_advance = mock.Mock(side_effect=RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            self.bridge.maybe_refresh([make_vehicle(99999)])
        self.quit.assert_called_once_with("XPENG")

    def test_failed_launch_is_survived(self):
        self.launch.return_value = False
        vehicles = [make_vehicle(99999)]
        self.assertEqual(self.bridge.maybe_refresh(vehicles), vehicles)
        self.quit.assert_not_called()

    def test_original_vehicles_returned_when_reread_fails(self):
        self.bridge._await_cache_advance = mock.Mock(return_value=True)
        original = [make_vehicle(99999)]
        with mock.patch.object(self.bridge, "read", side_effect=FileNotFoundError()):
            self.assertEqual(self.bridge.maybe_refresh(original), original)


class TestRefreshDoesNotBreakTheBroker(RefreshTestCase):
    """The wait must pump the connection, or the broker drops us mid-refresh.

    Measured on the first run of this feature: a ~50 s wait with no traffic is
    longer than the keepalive, and the next publish failed with "broker closed
    the connection".
    """

    def test_tick_is_called_while_waiting(self):
        ticks = []
        bridge = Bridge(
            self.config,
            self.publisher,
            tick=lambda: ticks.append(1),
        )
        # A real (very short) wait, against a stubbed reader, so the loop runs.
        with mock.patch.object(bridge, "read", return_value=[make_vehicle(1)]):
            advanced = bridge._await_cache_advance(None, timeout=0.1)

        self.assertTrue(advanced)
        self.assertGreaterEqual(len(ticks), 1)

    def test_a_failing_tick_does_not_abort_the_refresh(self):
        def boom() -> None:
            raise RuntimeError("broker went away")

        bridge = Bridge(self.config, self.publisher, tick=boom)
        with mock.patch.object(bridge, "read", return_value=[make_vehicle(1)]):
            self.assertTrue(bridge._await_cache_advance(None, timeout=0.1))


if __name__ == "__main__":
    unittest.main()


class TestAdaptiveInterval(RefreshTestCase):
    """One interval cannot suit a parked, a charging and a driving car."""

    def test_parked_uses_the_slow_interval(self):
        self.config.source.refresh_after_seconds = 3600
        vehicle = make_vehicle(1)  # no drive/charge groups at all
        threshold, why = self.bridge._refresh_threshold([vehicle])
        self.assertEqual((threshold, why), (3600, "parked"))

    def test_charging_uses_the_middle_interval(self):
        self.config.source.refresh_charging_after_seconds = 300
        vehicle = make_vehicle(1)
        vehicle.state["charge"] = {"power": 6.9, "battery_soc": 64}
        threshold, why = self.bridge._refresh_threshold([vehicle])
        self.assertEqual((threshold, why), (300, "charging"))

    def test_driving_uses_the_fast_interval(self):
        self.config.source.refresh_driving_after_seconds = 60
        vehicle = make_vehicle(1)
        vehicle.state["drive"] = {"shift_state": 1, "speed": 48}
        threshold, why = self.bridge._refresh_threshold([vehicle])
        self.assertEqual((threshold, why), (60, "driving"))

    def test_driving_wins_over_charging(self):
        """A car cannot really be both, but driving is the more urgent reading."""
        self.config.source.refresh_driving_after_seconds = 60
        self.config.source.refresh_charging_after_seconds = 300
        vehicle = make_vehicle(1)
        vehicle.state["charge"] = {"power": 6.9}
        vehicle.state["drive"] = {"shift_state": 1, "speed": 48}
        self.assertEqual(self.bridge._refresh_threshold([vehicle])[1], "driving")

    def test_parked_still_refreshes_once_past_the_slow_interval(self):
        self.config.source.refresh_after_seconds = 3600
        self.bridge._await_cache_advance = mock.Mock(return_value=True)
        with mock.patch.object(self.bridge, "read", return_value=[make_vehicle(1)]):
            self.bridge.maybe_refresh([make_vehicle(3700)])
        self.launch.assert_called_once()

    def test_parked_does_not_refresh_before_it(self):
        self.config.source.refresh_after_seconds = 3600
        self.bridge.maybe_refresh([make_vehicle(1800)])
        self.launch.assert_not_called()


class TestStalenessNeverFlaps(RefreshTestCase):
    """A parked car must not read `unavailable` between its own refreshes."""

    def test_raised_above_the_parked_interval(self):
        self.config.source.refresh_app = True
        self.config.source.refresh_after_seconds = 3600
        self.config.behaviour.stale_after_seconds = 1800
        self.assertGreater(self.bridge.effective_stale_after(), 3600)

    def test_configured_value_kept_when_it_is_already_long_enough(self):
        self.config.source.refresh_after_seconds = 300
        self.config.behaviour.stale_after_seconds = 7200
        self.assertEqual(self.bridge.effective_stale_after(), 7200)

    def test_not_applied_when_the_bridge_does_not_open_the_app(self):
        """With refresh_app off, the configured window is what the user asked for."""
        self.config.source.refresh_app = False
        self.config.source.refresh_after_seconds = 3600
        self.config.behaviour.stale_after_seconds = 1800
        self.assertEqual(self.bridge.effective_stale_after(), 1800)

    def test_published_availability_uses_the_raised_window(self):
        self.config.source.refresh_app = True
        self.config.source.refresh_after_seconds = 3600
        self.config.behaviour.stale_after_seconds = 1800
        self.bridge.publish_states([make_vehicle(2400)])
        availability = [
            payload
            for topic, payload, _ in self.publisher.messages
            if topic.endswith("/availability")
        ]
        # 40 minutes old, past the configured 30 but inside the raised window.
        self.assertEqual(availability, ["online"])
