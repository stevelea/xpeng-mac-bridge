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
