"""
Configuration: JSON file, overridden by environment, overridden by flags.

JSON rather than TOML, deliberately. ``tomllib`` is 3.11+, and the Python on
this machine is Homebrew 3.14 whose ``plistlib`` is already broken against the
system libexpat — so a config format that needs the newest interpreter is a
dependency this tool does not want. ``json`` is in every Python 3 and cannot
fail that way. The cost is no comments in the file, so every key is documented
in ``README.md`` instead.

Precedence is file < environment < command line, which is what lets the
password live in an environment variable (or a launchd ``EnvironmentVariables``
block) instead of on disk.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path.home() / ".config/xpeng-mac-bridge/config.json"

ENV_PREFIX = "XPENG_BRIDGE_"

# Anything in this set may be overridden by XPENG_BRIDGE_<PATH> with nested
# keys joined by a single underscore, e.g. XPENG_BRIDGE_MQTT_HOST.
_ENV_ALIASES = {
    f"{ENV_PREFIX}MQTT_HOST": ("mqtt", "host"),
    f"{ENV_PREFIX}MQTT_PORT": ("mqtt", "port"),
    f"{ENV_PREFIX}MQTT_USERNAME": ("mqtt", "username"),
    f"{ENV_PREFIX}MQTT_PASSWORD": ("mqtt", "password"),
    f"{ENV_PREFIX}MQTT_TLS": ("mqtt", "tls"),
    f"{ENV_PREFIX}MQTT_TOPIC_PREFIX": ("mqtt", "topic_prefix"),
    f"{ENV_PREFIX}MQTT_DISCOVERY_PREFIX": ("mqtt", "discovery_prefix"),
    f"{ENV_PREFIX}SOURCE_DATABASE": ("source", "database"),
    f"{ENV_PREFIX}SOURCE_UID": ("source", "uid"),
    f"{ENV_PREFIX}SOURCE_VIN": ("source", "vin"),
    f"{ENV_PREFIX}LOG_LEVEL": ("logging", "level"),
}


@dataclass
class MqttConfig:
    host: str = "127.0.0.1"
    port: int = 1883
    username: str | None = None
    password: str | None = None
    tls: bool = False
    tls_insecure: bool = False
    ca_file: str | None = None
    client_id: str = "xpeng-mac-bridge"
    keepalive: int = 60
    topic_prefix: str = "xpeng"
    discovery_prefix: str = "homeassistant"
    discovery: bool = True
    publish_interval: float = 30.0


@dataclass
class SourceConfig:
    database: str | None = None
    uid: str | None = None
    vin: str | None = None
    enrich_from_plist: bool = True
    plist_path: str | None = None


@dataclass
class BehaviourConfig:
    retain: bool = True
    qos: int = 1
    stale_after_seconds: float = 1800.0
    publish_raw_state: bool = True
    prune_removed_entities: bool = True
    state_file: str | None = None
    reset_settle_seconds: float = 20.0
    """How long ``--reset`` waits between retiring entities and re-creating them.

    Home Assistant gives an entity the ID derived from its device name the first
    time it sees it, and thereafter pins that ID to the ``unique_id`` — so
    retiring and immediately re-announcing does not rename anything. The empty
    retained payload has to be processed as a deletion, with nothing
    re-creating it in the same moment, or Home Assistant applies "last message
    wins" and the entity never goes away.

    Measured 2026-09-30 against HA 2026.9.4: 8 seconds was not enough (all 32
    entities survived a ``--reset`` unchanged), while retiring as its own step
    followed by a 15-second wait and then announcing produced all 32 under their
    new IDs. The default carries margin over that measurement.
    """


@dataclass
class LoggingConfig:
    level: str = "INFO"


@dataclass
class Config:
    mqtt: MqttConfig = field(default_factory=MqttConfig)
    source: SourceConfig = field(default_factory=SourceConfig)
    behaviour: BehaviourConfig = field(default_factory=BehaviourConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    path: Path | None = None

    def default_state_file(self) -> Path:
        if self.behaviour.state_file:
            return Path(self.behaviour.state_file).expanduser()
        base = self.path.parent if self.path else DEFAULT_CONFIG_PATH.parent
        return base / "published_entities.json"


# --------------------------------------------------------------------------- #


def _section(cls: type, raw: Any, name: str) -> Any:
    if raw is None:
        return cls()
    if not isinstance(raw, dict):
        raise ValueError(f"config section {name!r} must be an object")
    known = {f.name for f in cls.__dataclass_fields__.values()}  # type: ignore[attr-defined]
    unknown = set(raw) - known
    if unknown:
        raise ValueError(
            f"unknown key(s) in {name!r}: {', '.join(sorted(unknown))}. "
            f"Known keys: {', '.join(sorted(known))}"
        )
    return cls(**raw)


def _coerce(value: str, target: Any) -> Any:
    """Turn an environment string into the type the default declares."""
    if isinstance(target, bool):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"expected a boolean, got {value!r}")
    if isinstance(target, int):
        return int(value)
    if isinstance(target, float):
        return float(value)
    return value


def _apply_env(config: Config) -> Config:
    for variable, (section, key) in _ENV_ALIASES.items():
        raw = os.environ.get(variable)
        if raw is None:
            continue
        current = getattr(getattr(config, section), key)
        try:
            value = _coerce(raw, current) if current is not None else raw
        except ValueError as exc:
            raise ValueError(f"{variable}: {exc}") from exc
        setattr(getattr(config, section), key, value)
        logger.debug("config %s.%s overridden from environment", section, key)
    return config


def load(
    path: Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    require_file: bool = False,
) -> Config:
    """Load configuration, applying environment then explicit overrides."""
    resolved = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH

    raw: dict[str, Any] = {}
    if resolved.exists():
        try:
            raw = json.loads(resolved.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"{resolved} is not valid JSON: {exc}") from exc
        if not isinstance(raw, dict):
            raise ValueError(f"{resolved} must contain a JSON object")
    elif require_file:
        raise FileNotFoundError(f"no config file at {resolved}")

    known_sections = {"mqtt", "source", "behaviour", "logging"}
    unknown = set(raw) - known_sections
    if unknown:
        raise ValueError(
            f"unknown top-level key(s): {', '.join(sorted(unknown))}. "
            f"Known sections: {', '.join(sorted(known_sections))}"
        )

    config = Config(
        mqtt=_section(MqttConfig, raw.get("mqtt"), "mqtt"),
        source=_section(SourceConfig, raw.get("source"), "source"),
        behaviour=_section(BehaviourConfig, raw.get("behaviour"), "behaviour"),
        logging=_section(LoggingConfig, raw.get("logging"), "logging"),
        path=resolved,
    )

    _apply_env(config)

    for dotted, value in (overrides or {}).items():
        section, _, key = dotted.partition(".")
        if value is None:
            continue
        setattr(getattr(config, section), key, value)

    _warn_if_password_exposed(resolved, config)
    return config


def _warn_if_password_exposed(path: Path, config: Config) -> None:
    """A plaintext password in a group/world-readable file is worth saying out loud."""
    if not config.mqtt.password or not path.exists():
        return
    try:
        mode = path.stat().st_mode
    except OSError:
        return
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        logger.warning(
            "%s is readable by other users and holds an MQTT password; "
            "run: chmod 600 %s",
            path,
            path,
        )


def with_overrides(config: Config, **dotted: Any) -> Config:
    """Return a copy with dotted-path overrides applied — used by the tests."""
    updated = replace(config)
    for key, value in dotted.items():
        section, _, leaf = key.partition(".")
        target = getattr(updated, section)
        setattr(target, leaf, value)
    return updated
