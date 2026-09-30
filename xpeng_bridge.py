#!/usr/bin/env python3
"""
xpeng-mac-bridge — publish the XPENG app's own cached vehicle state to MQTT.

Reads what the XPENG app on this Mac already knows (see ``xpengmac.reader``)
and republishes it to a broker of your choosing, with Home Assistant MQTT
discovery so the entities appear on their own.

Strictly read-only in both directions:

  * It never writes to the app's container — the SQLite handle is ``mode=ro``
    and the only other read is ``plutil`` printing a key to stdout.
  * It never contacts XPENG. There is no HTTP client in this tool at all.

Usage::

    xpeng_bridge.py --print                 # show what it can read, no broker
    xpeng_bridge.py --dry-run               # show what it would publish
    xpeng_bridge.py --once                  # one poll, then exit
    xpeng_bridge.py                         # run as a daemon

See ``README.md`` for configuration.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any, Callable, Protocol

from xpengmac import __version__, config as config_module
from xpengmac import reader, signals
from xpengmac.reader import AccessDenied, ACCESS_HELP
from xpengmac.mqtt import MQTTClient, MQTTConnectError, MQTTError

logger = logging.getLogger("xpeng_bridge")

BIRTH_TOPIC = "homeassistant/status"


class Publisher(Protocol):
    """The slice of the MQTT client the bridge uses — so tests can stand in."""

    def publish(
        self, topic: str, payload: str | bytes, *, qos: int = 1, retain: bool = True
    ) -> None: ...


# --------------------------------------------------------------------------- #
# Publishing


class RecordingPublisher:
    """Collects messages instead of sending them. Backs ``--dry-run``."""

    def __init__(self, echo: bool = True) -> None:
        self.messages: list[tuple[str, str, bool]] = []
        self.echo = echo

    def publish(
        self, topic: str, payload: str | bytes, *, qos: int = 1, retain: bool = True
    ) -> None:
        text = payload.decode() if isinstance(payload, bytes) else payload
        self.messages.append((topic, text, retain))
        if self.echo:
            shown = text if len(text) <= 240 else f"{text[:240]}… ({len(text)} chars)"
            print(f"{'R' if retain else ' '} {topic}\n    {shown}")


class Bridge:
    """Builds payloads from the registry and pushes them at a publisher."""

    def __init__(
        self,
        config: config_module.Config,
        publisher: Publisher,
        *,
        state_file: Path | None = None,
        tick: Callable[[], None] | None = None,
    ) -> None:
        self.config = config
        self.publisher = publisher
        self.state_file = state_file or config.default_state_file()
        self._reannounce = False
        self._tick_fn = tick

    def _tick(self) -> None:
        """Pump whatever needs pumping during a long wait.

        Used by `maybe_refresh`, which can sit for two minutes waiting for the
        app to fetch. Without it the MQTT connection goes silent for longer than
        the keepalive and the broker drops us — measured, on the first run of
        this feature. A failure here is swallowed on purpose: the refresh must
        still reach its `finally` and close the app, because an app left running
        is the whole thing this feature exists to avoid.
        """
        if self._tick_fn is None:
            return
        try:
            self._tick_fn()
        except Exception:  # noqa: BLE001
            logger.debug("tick failed", exc_info=True)

    @property
    def availability_file(self) -> Path:
        return self.state_file.with_name("availability_topics.json")

    # -- remembering the car when we cannot read it -------------------------- #

    def remember_availability_topics(self, vehicles: list[reader.Vehicle]) -> None:
        """Persist availability topics so a later failed read can clear them.

        Without this, a read that fails leaves Home Assistant holding the last
        values with availability still `online` — stale data presented as
        current, which is the exact failure the data-age sensor exists to
        prevent. It matters most in the case that actually happens: the app is
        not running or not signed in, so there is no VIN to read and the topic
        has to have been remembered beforehand.
        """
        topics = sorted({self.availability_topic(v) for v in vehicles})
        try:
            self.availability_file.parent.mkdir(parents=True, exist_ok=True)
            self.availability_file.write_text(json.dumps(topics, indent=1))
        except OSError as exc:
            logger.warning("could not write %s: %s", self.availability_file, exc)

    def mark_unavailable(self) -> int:
        """Mark every previously seen car offline. Used when a read fails."""
        if not self.availability_file.exists():
            return 0
        try:
            topics = json.loads(self.availability_file.read_text())
        except (json.JSONDecodeError, OSError):
            return 0
        if not isinstance(topics, list):
            return 0

        marked = 0
        for topic in topics:
            if isinstance(topic, str) and topic:
                self._publish(topic, "offline")
                marked += 1
        return marked

    # -- source ------------------------------------------------------------- #

    def read(self) -> list[reader.Vehicle]:
        return reader.read_vehicles(
            db_path=Path(self.config.source.database).expanduser()
            if self.config.source.database
            else None,
            uid=self.config.source.uid,
            enrich_from_plist=self.config.source.enrich_from_plist,
            plist_path=Path(self.config.source.plist_path).expanduser()
            if self.config.source.plist_path
            else None,
            vin=self.config.source.vin,
        )

    # -- topics ------------------------------------------------------------- #

    def availability_topic(self, vehicle: reader.Vehicle) -> str:
        return f"{signals.base_topic(vehicle, self.config.mqtt.topic_prefix)}/availability"

    def raw_state_topic(self, vehicle: reader.Vehicle) -> str:
        return f"{signals.base_topic(vehicle, self.config.mqtt.topic_prefix)}/state"

    def _publish(self, topic: str, payload: str | None) -> None:
        if payload is None:
            return
        self.publisher.publish(
            topic,
            payload,
            qos=self.config.behaviour.qos,
            retain=self.config.behaviour.retain,
        )

    # -- discovery ---------------------------------------------------------- #

    def announce(self, vehicles: list[reader.Vehicle]) -> list[str]:
        """Publish every discovery config. Returns the entity keys announced."""
        if not self.config.mqtt.discovery:
            logger.info("discovery disabled in config; publishing state only")
            return []

        prefix = self.config.mqtt.discovery_prefix
        announced: list[str] = []

        for vehicle in vehicles:
            availability = self.availability_topic(vehicle)

            for signal in signals.SIGNALS:
                topic = signals.discovery_topic(
                    prefix, signal.component, vehicle, signal.object_id
                )
                payload = signals.signal_discovery(
                    signal, vehicle, self.config.mqtt.topic_prefix, availability
                )
                self._publish(topic, json.dumps(payload))
                announced.append(f"{signal.component}/{signal.object_id}")

            for derived in signals.DERIVED:
                topic = signals.discovery_topic(
                    prefix, derived.component, vehicle, derived.object_id
                )
                payload = signals.derived_discovery(
                    derived, vehicle, self.config.mqtt.topic_prefix, availability
                )
                self._publish(topic, json.dumps(payload))
                announced.append(f"{derived.component}/{derived.object_id}")

            tracker_topic = signals.discovery_topic(
                prefix, signals.COMPONENT_TRACKER, vehicle, "location"
            )
            self._publish(
                tracker_topic,
                json.dumps(
                    signals.tracker_discovery(
                        vehicle, self.config.mqtt.topic_prefix, availability
                    )
                ),
            )
            announced.append(f"{signals.COMPONENT_TRACKER}/location")

        logger.info(
            "announced %d entities for %d vehicle(s)", len(announced), len(vehicles)
        )
        return announced

    def prune(self, current: list[str], *, retire_all: bool = False) -> int:
        """Clear discovery configs this registry no longer publishes.

        Removing an entity is done by publishing an empty retained payload to
        its config topic — Home Assistant treats that as a deletion and drops
        the registry entry, so there is nothing else to clean up on its side.

        ``retire_all`` retires everything previously announced rather than only
        what is missing, which is what ``--reset`` needs: changing a payload in
        a way Home Assistant resolves at first sight (the device name, and
        therefore the generated entity ID) only takes effect on a fresh entity.
        """
        if not self.config.mqtt.discovery or not self.config.behaviour.prune_removed_entities:
            return 0
        if not self.state_file.exists():
            self._save_published(current)
            return 0

        try:
            previous = json.loads(self.state_file.read_text())
        except (json.JSONDecodeError, OSError):
            logger.warning("could not read %s; skipping prune", self.state_file)
            previous = []
        if not isinstance(previous, list):
            previous = []

        if retire_all:
            removed = list(previous)
        else:
            removed = [key for key in previous if key not in current]

        if not removed:
            self._save_published(current)
            return 0

        # The VIN is not recoverable from the key alone, so the topic is rebuilt
        # from the vehicles in play. Anything we cannot address is left alone.
        vehicles = self.read()
        for key in removed:
            component, _, object_id = key.partition("/")
            for vehicle in vehicles:
                topic = signals.discovery_topic(
                    self.config.mqtt.discovery_prefix, component, vehicle, object_id
                )
                self.publisher.publish(
                    topic, "", qos=self.config.behaviour.qos, retain=True
                )
                logger.info("retired discovery entity %s", key)

        self._save_published(current)
        return len(removed)

    def _save_published(self, keys: list[str]) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps(sorted(set(keys)), indent=1))
        except OSError as exc:
            logger.warning("could not write %s: %s", self.state_file, exc)

    # -- state -------------------------------------------------------------- #

    def publish_states(self, vehicles: list[reader.Vehicle]) -> int:
        published = 0
        stale_after = self.config.behaviour.stale_after_seconds

        # Remembered before publishing, so a failure on the *next* cycle can
        # still mark these cars offline.
        self.remember_availability_topics(vehicles)

        for vehicle in vehicles:
            age = vehicle.age_seconds
            fresh = age is not None and age <= stale_after
            availability = self.availability_topic(vehicle)

            for signal in signals.SIGNALS:
                value = signals.signal_state(signal, vehicle)
                if value is None:
                    continue
                self._publish(
                    f"{signals.base_topic(vehicle, self.config.mqtt.topic_prefix)}"
                    f"/{signal.object_id}",
                    value,
                )
                published += 1

            for derived in signals.DERIVED:
                value = signals.derived_state(derived, vehicle)
                if value is None:
                    continue
                self._publish(
                    f"{signals.base_topic(vehicle, self.config.mqtt.topic_prefix)}"
                    f"/{derived.object_id}",
                    value,
                )
                published += 1

            location = signals.location_payload(vehicle)
            if location:
                self._publish(
                    f"{signals.base_topic(vehicle, self.config.mqtt.topic_prefix)}/location",
                    location,
                )
                published += 1

            if self.config.behaviour.publish_raw_state:
                self._publish(
                    self.raw_state_topic(vehicle),
                    json.dumps(
                        {
                            "vin": vehicle.vin,
                            "timestamp": vehicle.timestamp.isoformat()
                            if vehicle.timestamp
                            else None,
                            "age_seconds": int(age) if age is not None else None,
                            "uid": vehicle.uid,
                            "meta": vehicle.meta,
                            "capabilities": vehicle.capabilities,
                            "state": vehicle.state,
                        },
                        default=str,
                    ),
                )

            # Availability goes last, so entities flip to unavailable only after
            # the values behind them have already been refreshed.
            self._publish(availability, "online" if fresh else "offline")
            logger.info(
                "%-17s soc=%-4s range=%-5s power=%-6s age=%s -> %s",
                vehicle.vin,
                vehicle.get("charge.battery_soc"),
                vehicle.get("odometer.avalible_driving_distance"),
                vehicle.get("charge.power"),
                reader.format_age(age),
                "online" if fresh else "STALE",
            )

        return published

    # -- lifecycle ---------------------------------------------------------- #

    def on_birth(self, topic: str, payload: bytes) -> None:
        """Home Assistant restarted (or a broker lost its retained messages)."""
        text = payload.decode("utf-8", "replace").strip().lower()
        if topic == BIRTH_TOPIC and text == "online":
            logger.info("Home Assistant birth message seen; re-announcing")
            self._reannounce = True

    def take_reannounce(self) -> bool:
        flag, self._reannounce = self._reannounce, False
        return flag

    # -- keeping the cache fresh without leaving the app running ------------ #

    def _await_cache_advance(self, before, timeout: float) -> bool:
        """Poll until the cached timestamp moves past ``before``."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            # Bounded by what is left, so a short timeout is not silently padded
            # to a full poll interval.
            time.sleep(max(0.05, min(3.0, deadline - time.monotonic())))
            self._tick()
            try:
                vehicles = self.read()
            except (AccessDenied, FileNotFoundError):
                continue
            newest = max((v.timestamp for v in vehicles if v.timestamp), default=None)
            if newest and (before is None or newest > before):
                return True
        return False

    def maybe_refresh(self, vehicles: list[reader.Vehicle]) -> list[reader.Vehicle]:
        """Launch the app briefly if the cache is stale, then quit it again.

        The XPENG app uses about two cores the entire time it is open — measured
        191% within a minute of launch, and 12.3 CPU-hours over 7.5 hours of wall
        clock on one run — and it does not need to be open, because this reads a
        cache that survives the app quitting. A ~45 second launch is enough to
        refresh it.

        So the default shape of a healthy install is: the app is closed, and this
        opens it on a schedule, waits for the cache to move, and closes it again.
        That trades an always-on two cores for a duty cycle of a couple of
        percent.

        Deliberately does nothing when the app is already running — at that point
        somebody is using it, and quitting it out from under them would be worse
        than stale data.
        """
        source = self.config.source
        if not source.refresh_app or not vehicles:
            return vehicles

        ages = [v.age_seconds for v in vehicles if v.age_seconds is not None]
        if not ages or min(ages) <= source.refresh_after_seconds:
            return vehicles

        if reader.is_app_running():
            logger.info(
                "cache is %s old but %s is already open; leaving it alone",
                reader.format_age(min(ages)),
                source.app_name,
            )
            return vehicles

        before = max((v.timestamp for v in vehicles if v.timestamp), default=None)
        logger.info(
            "cache is %s old; opening %s to refresh, then closing it",
            reader.format_age(min(ages)),
            source.app_name,
        )
        if not reader.launch_app(source.app_name):
            logger.warning("could not open %s", source.app_name)
            return vehicles

        try:
            advanced = self._await_cache_advance(before, source.refresh_wait_seconds)
            if advanced:
                logger.info("cache refreshed")
            else:
                logger.warning(
                    "%s did not refresh the cache within %.0fs",
                    source.app_name,
                    source.refresh_wait_seconds,
                )
        finally:
            # Always, even on failure: leaving it open is the thing this exists
            # to avoid, and a launched-but-unquit app would burn two cores until
            # someone noticed.
            if not reader.quit_app(source.app_name):
                logger.warning(
                    "could not close %s; it will keep using CPU until closed",
                    source.app_name,
                )

        try:
            return self.read()
        except (AccessDenied, FileNotFoundError):
            return vehicles

    def cycle(self, *, announce: bool = False) -> list[reader.Vehicle]:
        vehicles = self.read()
        if announce:
            self.prune(self.announce(vehicles))
        vehicles = self.maybe_refresh(vehicles)
        self.publish_states(vehicles)
        return vehicles


# --------------------------------------------------------------------------- #
# CLI


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="xpeng_bridge.py",
        description=(
            "Publish the XPENG app's cached vehicle state from this Mac to MQTT, "
            "with Home Assistant discovery."
        ),
    )
    parser.add_argument("--config", type=Path, help="path to config.json")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    mode = parser.add_argument_group("modes")
    mode.add_argument(
        "--print",
        dest="print_state",
        action="store_true",
        help="print the state read from the app and exit; no broker needed",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="print every topic and payload that would be published, then exit",
    )
    mode.add_argument(
        "--once", action="store_true", help="publish one cycle, then exit"
    )
    mode.add_argument(
        "--check", action="store_true", help="validate config and broker login, then exit"
    )
    mode.add_argument(
        "--announce-only",
        action="store_true",
        help="publish discovery configs only, no state, then exit",
    )
    mode.add_argument(
        "--reset",
        action="store_true",
        help=(
            "retire every previously announced entity first, then re-create it. "
            "Needed when a payload change alters the entity ID Home Assistant "
            "generated (for example the device name)"
        ),
    )

    overrides = parser.add_argument_group("overrides")
    overrides.add_argument("--mqtt-host")
    overrides.add_argument("--mqtt-port", type=int)
    overrides.add_argument("--mqtt-username")
    overrides.add_argument("--mqtt-password")
    overrides.add_argument("--topic-prefix")
    overrides.add_argument("--discovery-prefix")
    overrides.add_argument("--uid", help="XPENG account uid to read")
    overrides.add_argument("--vin", help="only publish this VIN")
    overrides.add_argument("--interval", type=float, help="seconds between polls")
    overrides.add_argument("--verbose", "-v", action="store_true")
    return parser


def _configure_logging(level: str, verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        config = config_module.load(
            args.config,
            overrides={
                "mqtt.host": args.mqtt_host,
                "mqtt.port": args.mqtt_port,
                "mqtt.username": args.mqtt_username,
                "mqtt.password": args.mqtt_password,
                "mqtt.topic_prefix": args.topic_prefix,
                "mqtt.discovery_prefix": args.discovery_prefix,
                "mqtt.publish_interval": args.interval,
                "source.uid": args.uid,
                "source.vin": args.vin,
            },
        )
    except (ValueError, FileNotFoundError) as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2

    _configure_logging(config.logging.level, args.verbose)

    # --print needs neither config nor broker.
    if args.print_state:
        return _print_state(config)

    if args.dry_run:
        bridge = Bridge(config, RecordingPublisher())
        try:
            vehicles = bridge.read()
        except AccessDenied:
            print(ACCESS_HELP, file=sys.stderr)
            return 1
        except FileNotFoundError as exc:
            print(f"cannot read vehicle state: {exc}", file=sys.stderr)
            return 1
        bridge.prune(bridge.announce(vehicles))
        bridge.publish_states(vehicles)
        print(
            f"\n{len(vehicles)} vehicle(s); "
            f"{len(bridge.publisher.messages)} messages (nothing was sent)"
        )
        return 0

    if not config.mqtt.host:
        print("config error: mqtt.host is required to publish", file=sys.stderr)
        return 2

    # A one-shot run must not evict the daemon. MQTT has one session per
    # client id, so a second connection with the same id disconnects the first —
    # measured here as "broker closed the connection" when `--once` was run while
    # the launchd agent was up, which looked like a keepalive bug and was not.
    # The daemon keeps the configured id (stable, so the broker replaces a dead
    # session after a crash); the exiting modes get a unique one.
    ephemeral = args.once or args.announce_only or args.check
    client_id = f"{config.mqtt.client_id}-{os.getpid()}" if ephemeral else config.mqtt.client_id

    client = MQTTClient(
        host=config.mqtt.host,
        port=config.mqtt.port,
        username=config.mqtt.username,
        password=config.mqtt.password,
        client_id=client_id,
        keepalive=config.mqtt.keepalive,
        use_tls=config.mqtt.tls,
        tls_insecure=config.mqtt.tls_insecure,
        ca_file=config.mqtt.ca_file,
    )
    # `tick` keeps the broker connection alive while `maybe_refresh` waits on
    # the app; see Bridge._tick.
    bridge = Bridge(config, client, tick=lambda: client.poll(timeout=0.0))

    try:
        client.connect()
    except MQTTConnectError as exc:
        print(f"broker refused: {exc}", file=sys.stderr)
        return 3
    except MQTTError as exc:
        print(f"cannot reach broker: {exc}", file=sys.stderr)
        return 3

    logger.info(
        "connected to %s:%s as %s", config.mqtt.host, config.mqtt.port,
        config.mqtt.username or "anonymous",
    )

    if args.check:
        client.close()
        print("config OK, broker reachable and credentials accepted")
        return 0

    try:
        client.subscribe(BIRTH_TOPIC, bridge.on_birth)
    except MQTTError as exc:
        # Not fatal: discovery configs are retained, so HA still picks them up.
        logger.warning("could not subscribe to %s: %s", BIRTH_TOPIC, exc)

    stopping = {"now": False}

    def _stop(signum: int, _frame: Any) -> None:
        logger.info("signal %d received; shutting down", signum)
        stopping["now"] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    announced: list[str] = []
    daemon_mode = not (args.once or args.announce_only)
    try:
        if args.reset:
            retired = bridge.prune([], retire_all=True)
            logger.info("reset: retired %d previously announced entities", retired)
            if retired:
                # See BehaviourConfig.reset_settle_seconds: re-announcing in the
                # same instant leaves the old entity ID in place.
                logger.info(
                    "reset: waiting %.0fs for Home Assistant to process the deletions",
                    config.behaviour.reset_settle_seconds,
                )
                time.sleep(config.behaviour.reset_settle_seconds)

        one_shot = args.once or args.announce_only
        announced: list[str] = []

        # The Full Disk Access instructions are long and the daemon retries every
        # interval, so say it once and then keep the log to one line per attempt.
        access_reported = {"done": False}

        def report_access_denied() -> None:
            if not access_reported["done"]:
                logger.error(
                    "cannot read vehicle state: macOS denied access.\n%s", ACCESS_HELP
                )
                access_reported["done"] = True
            else:
                logger.warning(
                    "still denied access to the XPENG container; waiting for the "
                    "Full Disk Access grant"
                )

        try:
            announced = bridge.announce(bridge.read())
            bridge.prune(announced)
        except AccessDenied:
            # Keep a daemon alive rather than exiting: the fix is a Full Disk
            # Access grant the user makes while it is running, and it should
            # start working without a reload. A one-shot run has nothing to wait
            # for, so it reports and exits non-zero.
            report_access_denied()
            if one_shot:
                return 1
        except FileNotFoundError as exc:
            logger.error("cannot read vehicle state: %s", exc)
            if one_shot:
                return 1

        if args.announce_only:
            return 0

        if args.once:
            try:
                bridge.cycle()
            except AccessDenied:
                report_access_denied()
                return 1
            logger.info("one-shot publish complete")
            return 0

        while not stopping["now"]:
            started = time.monotonic()
            try:
                if bridge.take_reannounce():
                    bridge.prune(bridge.announce(bridge.read()))
                bridge.cycle()
            except AccessDenied:
                report_access_denied()
                bridge.mark_unavailable()
            except FileNotFoundError as exc:
                # The app is not signed in, or its container was removed. Say so
                # and keep the daemon alive — this is recoverable.
                logger.error("cannot read vehicle state: %s", exc)
                bridge.mark_unavailable()
            except MQTTError as exc:
                logger.error("broker connection lost: %s; reconnecting", exc)
                _reconnect(client, BIRTH_TOPIC, bridge)

            elapsed = time.monotonic() - started
            sleep_for = max(1.0, config.mqtt.publish_interval - elapsed)
            deadline = time.monotonic() + sleep_for
            while time.monotonic() < deadline and not stopping["now"]:
                try:
                    client.poll(timeout=min(1.0, deadline - time.monotonic()))
                except MQTTError as exc:
                    logger.error("broker connection lost: %s; reconnecting", exc)
                    _reconnect(client, BIRTH_TOPIC, bridge)
                    break
                if bridge.take_reannounce():
                    bridge.prune(bridge.announce(bridge.read()))
    finally:
        # Only a daemon that is genuinely stopping should mark the car offline.
        # A one-shot run has just published fresh values; flipping availability
        # to offline on the way out would make every entity unavailable for no
        # reason.
        if daemon_mode:
            for vehicle in _safe_read(bridge):
                bridge._publish(bridge.availability_topic(vehicle), "offline")
        client.close()
        logger.info("stopped")

    return 0


def _reconnect(client: MQTTClient, birth_topic: str, bridge: Bridge) -> None:
    """Reconnect with a bounded backoff, re-announcing once it is back."""
    delay = 1.0
    while delay <= 60.0:
        time.sleep(delay)
        try:
            client.connect()
            client.subscribe(birth_topic, bridge.on_birth)
            logger.info("reconnected")
            bridge.announce(bridge.read())
            return
        except (MQTTError, FileNotFoundError) as exc:
            logger.warning("reconnect failed (%s); retrying in %.0fs", exc, delay)
            delay *= 2
    logger.error("giving up on reconnect after 60s; exiting so launchd restarts us")
    raise SystemExit(4)


def _safe_read(bridge: Bridge) -> list[reader.Vehicle]:
    try:
        return bridge.read()
    except Exception:  # noqa: BLE001 - shutdown must not raise
        return []


def _print_state(config: config_module.Config) -> int:
    bridge = Bridge(config, RecordingPublisher(echo=False))
    try:
        vehicles = bridge.read()
    except AccessDenied:
        print(ACCESS_HELP, file=sys.stderr)
        return 1
    except FileNotFoundError as exc:
        print(f"cannot read vehicle state: {exc}", file=sys.stderr)
        return 1

    if not vehicles:
        print("no vehicles found in the app database", file=sys.stderr)
        return 1

    for vehicle in vehicles:
        print(f"VIN          {vehicle.vin}")
        print(f"account uid  {vehicle.uid}")
        print(f"database     {vehicle.db_path}")
        model = vehicle.meta.get("typeName") or vehicle.meta.get("vehicleTypeName")
        print(f"model        {model} "
              f"(project code {vehicle.meta.get('vehicleTypeName')}, "
              f"type {vehicle.meta.get('vehicleTypeCode')}, "
              f"region {vehicle.meta.get('saleRegionCode')})")
        print(f"data age     {reader.format_age(vehicle.age_seconds)} "
              f"(car timestamp {vehicle.timestamp})")
        print(f"capabilities {len(vehicle.capabilities)}")
        print()
        for group in sorted(vehicle.state):
            block = vehicle.state[group]
            if isinstance(block, dict):
                print(f"  {group}")
                for key in sorted(block):
                    print(f"      {key:<34} {block[key]}")
            else:
                print(f"  {group:<12} {block}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
