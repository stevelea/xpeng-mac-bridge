"""
The signal registry: the single source of truth for what gets published.

One table drives everything — the Home Assistant discovery configs, the state
topics, and the tests. Adding a signal is one entry here and nothing else,
and for the same reason a second list always drifts: two places that must
agree will eventually disagree.

Evidence rule
-------------
This project's convention is to publish what has been *measured* and to leave a
value out rather than guess it. That applies here:

  * A **passthrough** signal is safe — it is the car's own number, relabelled.
  * A **derived** signal ships only with evidence recorded next to it.

So ``charging`` is derived (``charge.power > 1 kW``, the same threshold the CSV
parser uses, where ``ldcu_chrgpwr > 1 kW`` coincided with a stationary car in
100% of matched samples) while ``plugged_in`` is deliberately *not* derived:
``charge.charge_connector_status`` was observed as ``1`` only while charging, so
we know one value of an enum whose other values we have never seen. It is
published raw, as a diagnostic, until someone watches the car unplug.

That rule is why there are fewer entities here than the data would allow. It is
the intended trade.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .reader import Vehicle
from . import __version__

COMPONENT_SENSOR = "sensor"
COMPONENT_BINARY = "binary_sensor"
COMPONENT_TRACKER = "device_tracker"

ORIGIN: dict[str, str] = {
    "name": "xpeng-mac-bridge",
    "sw_version": __version__,
}
"""Required by Home Assistant on MQTT discovery payloads.

It records which integration advertised the entity, so a user can see where an
unexpected entity came from instead of guessing.
"""


@dataclass(frozen=True)
class Signal:
    """One published entity."""

    key: str
    """Dotted path into the vehicle state, ``group.signal``."""

    object_id: str
    """Entity id suffix. Full id is ``xpeng_<slug>_<object_id>``."""

    name: str
    component: str = COMPONENT_SENSOR
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = None
    icon: str | None = None
    entity_category: str | None = None
    precision: int | None = 2
    """Floats are rounded before publishing — the car sends 6.900000095367432."""

    payload_on: str | None = None
    payload_off: str | None = None


# --------------------------------------------------------------------------- #
# Registry

SIGNALS: tuple[Signal, ...] = (
    # --- energy and range ---------------------------------------------------
    Signal(
        "charge.battery_soc", "battery", "Battery",
        unit="%", device_class="battery", state_class="measurement", precision=0,
    ),
    Signal(
        "odometer.avalible_driving_distance", "range", "Range",
        unit="km", device_class="distance", state_class="measurement", precision=0,
    ),
    Signal(
        "odometer.total_mileage", "odometer", "Odometer",
        unit="km", device_class="distance", state_class="total_increasing", precision=0,
    ),
    # --- charging -----------------------------------------------------------
    Signal(
        "charge.power", "charging_power", "Charging power",
        unit="kW", device_class="power", state_class="measurement",
    ),
    Signal(
        "charge.voltage", "charging_voltage", "Charging voltage",
        unit="V", device_class="voltage", state_class="measurement",
    ),
    Signal(
        "charge.electric_current_limit", "charge_current_limit", "Charge current limit",
        unit="A", device_class="current", state_class="measurement", precision=0,
    ),
    Signal(
        "charge.time_to_complete_charge_min", "charge_time_remaining",
        "Charge time remaining",
        unit="min", device_class="duration", state_class="measurement", precision=0,
    ),
    Signal(
        "charge.electricity", "charge_energy_added", "Charge energy added",
        unit="kWh", device_class="energy", state_class="measurement",
    ),
    Signal(
        "charge.max_range_charge", "charge_limit", "Charge limit",
        unit="%", device_class="battery", state_class="measurement", precision=0,
    ),
    Signal(
        "chargeStatistics.usageElectricity", "last_charge_energy",
        "Last charge energy", unit="kWh", device_class="energy", precision=2,
    ),
    Signal(
        "chargeStatistics.addedRange", "last_charge_range_added",
        "Last charge range added", unit="km", device_class="distance", precision=0,
    ),
    # --- climate ------------------------------------------------------------
    Signal(
        "hvac.hvac_inner_temp_float", "inside_temperature", "Inside temperature",
        unit="\u00b0C", device_class="temperature", state_class="measurement", precision=1,
    ),
    Signal(
        "hvac.hvac_temp_float", "target_temperature", "Target temperature",
        unit="\u00b0C", device_class="temperature", state_class="measurement", precision=1,
    ),
    # --- driving ------------------------------------------------------------
    Signal(
        "drive.speed", "speed", "Speed",
        unit="km/h", device_class="speed", state_class="measurement", precision=0,
    ),
    Signal(
        "drive.angle", "heading", "Heading",
        unit="\u00b0", entity_category="diagnostic", precision=1,
    ),
    # --- tyres --------------------------------------------------------------
    # kPa, already in the 100-500 band the export parser treats as plausible.
    Signal(
        "tpms.tpms_pressure_fl", "tyre_pressure_front_left", "Tyre pressure front left",
        unit="kPa", device_class="pressure", state_class="measurement", precision=0,
    ),
    Signal(
        "tpms.tpms_pressure_fr", "tyre_pressure_front_right", "Tyre pressure front right",
        unit="kPa", device_class="pressure", state_class="measurement", precision=0,
    ),
    Signal(
        "tpms.tpms_pressure_rl", "tyre_pressure_rear_left", "Tyre pressure rear left",
        unit="kPa", device_class="pressure", state_class="measurement", precision=0,
    ),
    Signal(
        "tpms.tpms_pressure_rr", "tyre_pressure_rear_right", "Tyre pressure rear right",
        unit="kPa", device_class="pressure", state_class="measurement", precision=0,
    ),
    # --- raw passthroughs whose enum is not evidenced ------------------------
    Signal(
        "charge.charging_state", "charging_state_raw", "Charging state (raw)",
        entity_category="diagnostic", precision=0,
    ),
    Signal(
        "charge.charge_connector_status", "charge_connector_status_raw",
        "Charge connector status (raw)", entity_category="diagnostic", precision=0,
    ),
    Signal(
        "drive.shift_state", "shift_state_raw", "Shift state (raw)",
        entity_category="diagnostic", precision=0,
    ),
    # --- provenance ---------------------------------------------------------
    Signal(
        "__timestamp__", "data_timestamp", "Data timestamp",
        device_class="timestamp", entity_category="diagnostic", precision=None,
    ),
    Signal(
        "__data_age__", "data_age", "Data age",
        unit="s", device_class="duration", state_class="measurement",
        entity_category="diagnostic", precision=0,
    ),
    Signal(
        "bleTimestamp", "ble_last_connected", "Bluetooth last connected",
        device_class="timestamp", entity_category="diagnostic", precision=None,
    ),
    # --- location -----------------------------------------------------------
    Signal(
        "__location_raw__", "location_raw", "Location (raw)",
        icon="mdi:map-marker", precision=None,
    ),
    # The coordinates as a *state*, which the tracker cannot give you: a
    # device_tracker's state is `home` / `not_home` / a zone name, and its
    # coordinates live in attributes — so nothing has the position as a state to
    # graph, template against, or trigger on directly. This is exactly that:
    # `-33.8688,151.2093`.
    #
    # Deliberately not `entity_category: diagnostic`, unlike the other `*_raw`
    # entities. Those expose enums whose meaning is not established, so they are
    # reference material; this one is asked for on purpose, and being visible and
    # usable is its whole value.
)

# --------------------------------------------------------------------------- #
# Derived signals

DOOR_AJAR_KEYS = (
    "door.driver_door_ajar_st",
    "door.passenger_door_ajar_st",
    "door.rear_left_door_ajar_st",
    "door.rear_right_door_ajar_st",
    "door.bonnet_ajar_st",
    "door.trunk_ajar_st",
)

WINDOW_POSITION_KEYS = (
    "window.front_left_window_position",
    "window.front_right_window_position",
    "window.rear_left_window_position",
    "window.rear_right_window_position",
)

CHARGING_POWER_THRESHOLD_KW = 1.0
"""Measured: a charging-power reading above 1 kW co-occurred with a
stationary car in 100% of matched samples, and regen also drives current
negative — so the sign of the current cannot identify charging, and this
threshold can. `charge.power` is the same signal the CAN export calls
`ldcu_chrgpwr`."""


@dataclass(frozen=True)
class Derived:
    """A computed entity: ``fn(vehicle) -> value or None``."""

    object_id: str
    name: str
    component: str
    fn: Callable[[Vehicle], Any]
    device_class: str | None = None
    icon: str | None = None
    entity_category: str | None = None
    evidence: str = ""


def _num(value: Any) -> float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def is_charging(vehicle: Vehicle) -> bool | None:
    power = _num(vehicle.get("charge.power"))
    if power is None:
        return None
    return power > CHARGING_POWER_THRESHOLD_KW


def _derived_any_door_open(vehicle: Vehicle) -> bool | None:
    seen = False
    for key in DOOR_AJAR_KEYS:
        value = _num(vehicle.get(key))
        if value is None:
            continue
        seen = True
        if value == 1:
            return True
    return False if seen else None


def _derived_any_window_open(vehicle: Vehicle) -> bool | None:
    """Closed reads 100 and open reads lower, so anything under 100 is open.

    The sunshade positions are excluded: they use 255 as a sentinel (``behind``
    and ``sky`` both read 255 on this car while closed) and would report every
    car as open.
    """
    seen = False
    for key in WINDOW_POSITION_KEYS:
        value = _num(vehicle.get(key))
        if value is None:
            continue
        seen = True
        if value < 100:
            return True
    return False if seen else None


def _derived_hvac_on(vehicle: Vehicle) -> bool | None:
    value = _num(vehicle.get("hvac.hvac_on"))
    return None if value is None else bool(value)


def _derived_tailgate_open(vehicle: Vehicle) -> bool | None:
    value = _num(vehicle.get("door.trunk_ajar_st"))
    return None if value is None else bool(value)


def is_driving(vehicle: Vehicle) -> bool | None:
    """Whether the car is out of Park.

    Deliberately "not in Park" rather than "in Drive": reverse and neutral are
    also not parked, and a car manoeuvring on a driveway is exactly when a
    position is worth having. Gear 3 is Reverse on this platform, which
    `is_parked` documents.

    Speed is checked as well because it is the more direct signal and does not
    depend on the gear reading being present — a moving car reporting speed is
    not parked whatever the gear says.

    None when neither field is usable, so a caller can tell "not driving" from
    "cannot tell".
    """
    speed = _num(vehicle.get("drive.speed"))
    if speed is not None and speed > 0:
        return True

    shift = _num(vehicle.get("drive.shift_state"))
    if shift is None:
        return None if speed is None else False
    # 1-4 are the gears this platform reports; anything else is not a gear
    # reading and must not be read as "not Park".
    if shift not in (1, 2, 3, 4):
        return None if speed is None else False
    return shift != 4


def is_parked(vehicle: Vehicle) -> bool | None:
    """Gear 4 is Park on this platform, not Drive.

    Measured over a month of 1 Hz CAN data: the car reads gear 4 in 95% of
    samples, and gear 1 whenever it is moving. So the familiar 1-2-3-4 =
    P-R-N-D reading is wrong here, and treating 4 as Drive would call the whole
    month a drive.
    """
    shift = _num(vehicle.get("drive.shift_state"))
    if shift is None:
        return None
    return shift == 4


DERIVED: tuple[Derived, ...] = (
    Derived(
        "charging", "Charging", COMPONENT_BINARY, is_charging,
        device_class="battery_charging",
        evidence="charge.power > 1 kW, matching the CSV parser's threshold",
    ),
    Derived(
        "any_door_open", "Any door open", COMPONENT_BINARY, _derived_any_door_open,
        device_class="door",
        evidence="door.*_ajar_st == 1 for any of the six ajar signals",
    ),
    Derived(
        "any_window_open", "Any window open", COMPONENT_BINARY, _derived_any_window_open,
        device_class="window",
        evidence="window position < 100 (closed reads exactly 100); sunshades excluded",
    ),
    Derived(
        "tailgate_open", "Tailgate open", COMPONENT_BINARY, _derived_tailgate_open,
        device_class="opening",
        evidence="door.trunk_ajar_st",
    ),
    Derived(
        "hvac_on", "Climate on", COMPONENT_BINARY, _derived_hvac_on,
        evidence="hvac.hvac_on",
    ),
    Derived(
        "parked", "Parked", COMPONENT_BINARY, is_parked,
        evidence="shift_state == 4, which trips.py established means Park",
    ),
)


# --------------------------------------------------------------------------- #
# Payload building


def entity_id(vehicle: Vehicle, object_id: str) -> str:
    return f"xpeng_{vehicle.slug.lower()}_{object_id}"


def device_name(vehicle: Vehicle) -> str:
    """The full, unmasked VIN.

    The VIN is the device name rather than a friendly label on purpose. It is
    the one identifier every other tool in this project keys on, and it is
    unambiguous across accounts — "XPENG G6" is not, since an account can hold
    two. The model, make and serial still ride along in the device payload, so
    the device card in Home Assistant shows the car as a G6 without the name
    having to say so.
    """
    return vehicle.vin


def base_topic(vehicle: Vehicle, prefix: str) -> str:
    return f"{prefix.rstrip('/')}/{vehicle.slug.lower()}"


def device_payload(vehicle: Vehicle) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "identifiers": [f"xpeng_{vehicle.slug.lower()}"],
        "name": device_name(vehicle),
        "manufacturer": "XPENG",
        "serial_number": vehicle.vin,
    }
    model = vehicle.meta.get("typeName") or vehicle.meta.get("vehicleTypeName")
    if model:
        payload["model"] = model
    return payload


def _topic_root(discovery_prefix: str, component: str, vehicle: Vehicle) -> str:
    return f"{discovery_prefix.rstrip('/')}/{component}/xpeng_{vehicle.slug.lower()}"


def discovery_topic(
    discovery_prefix: str, component: str, vehicle: Vehicle, object_id: str
) -> str:
    return f"{_topic_root(discovery_prefix, component, vehicle)}/{object_id}/config"


def _common(
    vehicle: Vehicle,
    component: str,
    object_id: str,
    name: str,
    mqtt_prefix: str,
    availability_topic: str,
) -> dict[str, Any]:
    unique = entity_id(vehicle, object_id)
    return {
        "name": name,
        "unique_id": unique,
        # Home Assistant otherwise builds the entity ID from the *device name*,
        # so renaming the device silently changes every entity ID -- and it pins
        # an existing ID to its unique_id, so it will not follow a later rename
        # either (measured 2026-09-30: retiring and re-creating all 32 entities
        # after renaming the device restored the old `xpeng_g6_l1n_783_*` IDs).
        # `default_entity_id` makes the ID deterministic and independent of the
        # display name, and applies when the entity is first added.
        "default_entity_id": f"{component}.{unique}",
        "object_id": unique,
        "state_topic": f"{base_topic(vehicle, mqtt_prefix)}/{object_id}",
        "availability_topic": availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_payload(vehicle),
        "origin": dict(ORIGIN),
    }


def signal_discovery(
    signal: Signal, vehicle: Vehicle, mqtt_prefix: str, availability_topic: str
) -> dict[str, Any]:
    payload = _common(
        vehicle,
        signal.component,
        signal.object_id,
        signal.name,
        mqtt_prefix,
        availability_topic,
    )
    if signal.unit:
        payload["unit_of_measurement"] = signal.unit
    if signal.device_class:
        payload["device_class"] = signal.device_class
    if signal.state_class:
        payload["state_class"] = signal.state_class
    if signal.icon:
        payload["icon"] = signal.icon
    if signal.entity_category:
        payload["entity_category"] = signal.entity_category
    if signal.payload_on:
        payload["payload_on"] = signal.payload_on
    if signal.payload_off:
        payload["payload_off"] = signal.payload_off
    return payload


def derived_discovery(
    derived: Derived, vehicle: Vehicle, mqtt_prefix: str, availability_topic: str
) -> dict[str, Any]:
    payload = _common(
        vehicle,
        derived.component,
        derived.object_id,
        derived.name,
        mqtt_prefix,
        availability_topic,
    )
    if derived.device_class:
        payload["device_class"] = derived.device_class
    if derived.icon:
        payload["icon"] = derived.icon
    if derived.entity_category:
        payload["entity_category"] = derived.entity_category
    return payload


def tracker_discovery(
    vehicle: Vehicle, mqtt_prefix: str, availability_topic: str
) -> dict[str, Any]:
    """A GPS device_tracker.

    Coordinates go on ``json_attributes_topic``, **not** on ``state_topic``.
    Home Assistant's MQTT device tracker documents the valid ``state_topic``
    payloads as ``home``, ``not_home`` or a zone name; ``json_attributes_topic``
    is what carries ``latitude``/``longitude``. Publishing coordinates to
    ``state_topic`` leaves the entity's state as the raw JSON string — which is
    exactly what the first live run produced, and why this has no
    ``state_topic`` at all. With only the attributes topic, Home Assistant
    derives home/not_home from the position against the configured zones.
    """
    unique = entity_id(vehicle, "location")
    return {
        "name": "Location",
        "unique_id": unique,
        "default_entity_id": f"{COMPONENT_TRACKER}.{unique}",
        "object_id": unique,
        "source_type": "gps",
        "json_attributes_topic": f"{base_topic(vehicle, mqtt_prefix)}/location",
        "availability_topic": availability_topic,
        "payload_available": "online",
        "payload_not_available": "offline",
        "device": device_payload(vehicle),
        "origin": dict(ORIGIN),
    }


# --------------------------------------------------------------------------- #
# Values


def format_value(value: Any, precision: int | None) -> str | None:
    """Render one value as an MQTT payload, or None when it cannot be used."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, float):
        if precision is not None:
            value = round(value, precision)
        # 6.0 should publish as "6", not "6.0": HA otherwise shows a decimal
        # place the car never reported.
        if value == int(value):
            return str(int(value))
        return str(value)
    if isinstance(value, int):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip() or None
    return None


def signal_state(signal: Signal, vehicle: Vehicle) -> str | None:
    """The payload for one signal, including the computed pseudo-signals."""
    if signal.key == "__timestamp__":
        return vehicle.timestamp.isoformat() if vehicle.timestamp else None
    if signal.key == "__data_age__":
        age = vehicle.age_seconds
        return None if age is None else str(int(age))
    if signal.key == "__location_raw__":
        return raw_location(vehicle)
    return format_value(vehicle.get(signal.key), signal.precision)


def raw_location(vehicle: Vehicle) -> str | None:
    """``"-33.868800,151.209300"`` — latitude then longitude, 6 decimal places.

    Fixed width rather than `round(...)`, which drops a trailing zero and gives
    `-33.8688` one reading and `-33.868816` the next. A coordinate read by a
    template or a parser should not change shape between fixes. Six places is
    about 11 cm, well past anything the car reports.

    Returns None rather than half a position when a component is missing or out
    of range, so the entity goes unavailable rather than reporting a location
    that is partly invented.
    """
    lat = _num(vehicle.get("drive.latitude"))
    lon = _num(vehicle.get("drive.longitude"))
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return f"{lat:.6f},{lon:.6f}"


def derived_state(derived: Derived, vehicle: Vehicle) -> str | None:
    value = derived.fn(vehicle)
    if value is None:
        return None
    return "ON" if value else "OFF"


def location_payload(vehicle: Vehicle) -> str | None:
    """Position and extras on one topic, for the tracker's attributes topic.

    ``latitude``/``longitude`` are what Home Assistant reads as the position;
    any other key becomes an attribute. ``gps_accuracy`` is deliberately absent
    — the car reports no accuracy figure, and inventing one would draw a
    confidence circle that means nothing.
    """
    lat = _num(vehicle.get("drive.latitude"))
    lon = _num(vehicle.get("drive.longitude"))
    if lat is None or lon is None:
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    payload: dict[str, Any] = {"latitude": round(lat, 6), "longitude": round(lon, 6)}
    heading = _num(vehicle.get("drive.angle"))
    if heading is not None:
        payload["course"] = round(heading, 1)
    if vehicle.timestamp:
        payload["last_seen"] = vehicle.timestamp.isoformat()
    return json.dumps(payload)


def entity_keys(vehicle: Vehicle) -> list[tuple[str, str]]:
    """Every ``(component, object_id)`` this registry publishes.

    Used to retire discovery entities that a previous version created and this
    one no longer does — the correct way to delete one is an empty retained
    payload on its config topic, not leaving it orphaned.
    """
    keys: list[tuple[str, str]] = [(s.component, s.object_id) for s in SIGNALS]
    keys += [(d.component, d.object_id) for d in DERIVED]
    keys.append((COMPONENT_TRACKER, "location"))
    return keys
