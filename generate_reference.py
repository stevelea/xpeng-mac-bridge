#!/usr/bin/env python3
"""
Generate the reference documentation from a live read.

``docs/STATE-FIELDS.md`` and ``docs/ENTITIES.md`` are generated rather than
hand-written: a field list transcribed by hand drifts from the code, and a
reference that is almost right is worse than none. ``--check`` exits non-zero
when the committed docs no longer match a live read, which is worth running
before a commit — it needs the app on the same machine, so it is not a CI job.

Identifying values are redacted on the way out — the real VIN becomes
``L1NNSGHA0SB000000`` and the GPS coordinates become an example pair — so the
generated docs can be published without leaking where the car is parked. Field
*names*, types and observed-value sets are kept, because that is the part that
is actually useful to someone implementing this.

    python3 generate_reference.py --snapshot   # refresh the sample from the app
    python3 generate_reference.py              # write the docs
    python3 generate_reference.py --check      # fail if they are out of date

The docs are built from a **committed** sample (`docs/state-sample.json`), not
from a live read. That matters: the fields' values change every time the car
polls, so generating straight from the app made ``--check`` report drift on every
refresh and made the output unreproducible. The sample is redacted when it is
taken, so the fixture is safe to commit, and regenerating the docs needs no app,
no database and no Mac.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from xpengmac import reader, signals  # noqa: E402

DOCS = Path(__file__).resolve().parent / "docs"
SAMPLE = DOCS / "state-sample.json"

EXAMPLE_VIN = "L1NNSGHA0SB000000"
EXAMPLE_LAT = -33.868800
EXAMPLE_LON = 151.209300

# Fields whose real values identify a person or a place.
REDACT = {
    "drive.latitude": EXAMPLE_LAT,
    "drive.longitude": EXAMPLE_LON,
}

# Observed-value sets, filled from the live read where the field is enum-like
# (few distinct small integers) so the doc records what the car actually sends
# rather than a guess at its meaning.
ENUM_LIKE = 6


def redact(path: str, value: Any, vin: str) -> Any:
    """Swap identifying values out. Takes `vin` rather than re-reading it."""
    if path in REDACT:
        return REDACT[path]
    if isinstance(value, str):
        return value.replace(vin, EXAMPLE_VIN)
    return value


def classify(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    return type(value).__name__


def example(value: Any) -> str:
    if isinstance(value, str):
        return f'`"{value}"`' if len(value) <= 40 else f'`"{value[:37]}…"`'
    if isinstance(value, float):
        return f"`{round(value, 4)}`"
    if isinstance(value, bool):
        return f"`{str(value).lower()}`"
    return f"`{value}`"


def flavour(path: str, value: Any) -> str:
    """A short note where the field's meaning is known from this project."""
    notes = {
        "charge.power": "charging only; the export's `ldcu_chrgpwr`",
        "charge.battery_soc": "whole percent",
        "odometer.total_mileage": "whole km",
        "odometer.avalible_driving_distance": "the app's own range estimate",
        "drive.shift_state": "**4 = Park** (see `trips.py`); 1 when moving",
        "drive.angle": "heading, degrees",
        "tpms.tpms_pressure_*": "kPa, not bar or psi",
        "hvac.hvac_inner_temp_float": "float form; the int twin is the display value",
        "window.*_sun_shade_position": "255 is a sentinel, not a position",
        "chargeStatistics.usageElectricity": "kWh, last completed charge",
        "chargeStatistics.addedRange": "km, last completed charge",
        "chargeStatistics.completeTime": "epoch **milliseconds**",
        "bleTimestamp": "last BLE connect, may be months old",
    }
    if path in notes:
        return notes[path]
    for prefix, note in notes.items():
        if prefix.endswith("*") and path.startswith(prefix[:-1]):
            return note
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if value in (0, 1, -1, 2, 3, 4, 5, 99, 255):
            return "enum-like; meaning not established" if value not in (0, 1) else ""
    return ""


def state_fields_doc(vehicle: reader.Vehicle) -> str:
    out: list[str] = []
    out.append("# State fields")
    out.append("")
    out.append(
        "Every field the app caches, with the type and an observed value. "
        "Generated from a live read by `generate_reference.py` — do not edit by hand."
    )
    out.append("")
    out.append(
        "The GPS coordinates and the VIN are redacted in this file. Everything "
        "else is what the car actually reported."
    )
    out.append("")

    total = 0
    for group in sorted(vehicle.state):
        block = vehicle.state[group]
        out.append(f"## `{group}`")
        out.append("")
        if not isinstance(block, dict):
            out.append(f"Scalar value: {example(block)}")
            out.append("")
            total += 1
            continue
        out.append("| Field | Type | Example | Notes |")
        out.append("|---|---|---|---|")
        for key in sorted(block):
            value = redact(f"{group}.{key}", block[key], vehicle.vin)
            out.append(
                f"| `{key}` | {classify(value)} | {example(value)} | "
                f"{flavour(f'{group}.{key}', value)} |"
            )
            total += 1
        out.append("")

    out.append(f"**{total} fields across {len(vehicle.state)} groups.**")
    out.append("")
    return "\n".join(out)


def entities_doc(vehicle: reader.Vehicle, mqtt_prefix: str, discovery_prefix: str) -> str:
    availability = (
        f"{signals.base_topic(vehicle, mqtt_prefix)}/availability"
    )
    out: list[str] = []
    out.append("# Discovery entities")
    out.append("")
    out.append(
        "Every entity the bridge advertises, and the state field behind it. "
        "Generated by `generate_reference.py` — do not edit by hand."
    )
    out.append("")
    out.append(
        "Home Assistant creates all of these from the discovery configs alone; "
        "no YAML is needed. `obj_id` is the suffix of the generated entity ID."
    )
    out.append("")

    out.append("## Sensors")
    out.append("")
    out.append("| Entity | Name | Source field | Unit | device_class | state_class |")
    out.append("|---|---|---|---|---|---|")
    for signal in signals.SIGNALS:
        if signal.component != signals.COMPONENT_SENSOR:
            continue
        source = {
            "__timestamp__": "the cache timestamp",
            "__data_age__": "computed from the timestamp",
            "__location_raw__": "`drive.latitude` + `drive.longitude`, as a state",
        }.get(signal.key, f"`{signal.key}`")
        out.append(
            f"| `{signal.object_id}` | {signal.name} | {source} | "
            f"{signal.unit or '—'} | {signal.device_class or '—'} | "
            f"{signal.state_class or '—'} |"
        )
    out.append("")

    out.append("## Binary sensors")
    out.append("")
    out.append("| Entity | Name | Derived from | device_class |")
    out.append("|---|---|---|---|")
    for derived in signals.DERIVED:
        out.append(
            f"| `{derived.object_id}` | {derived.name} | {derived.evidence} | "
            f"{derived.device_class or '—'} |"
        )
    out.append("")

    out.append("## Tracker")
    out.append("")
    out.append("| Entity | Name | Topics |")
    out.append("|---|---|---|")
    out.append(
        "| `location` | Location | `json_attributes_topic` = "
        f"`{signals.base_topic(vehicle, mqtt_prefix)}/location` |"
    )
    out.append("")
    out.append(
        "Coordinates are published on `json_attributes_topic`, **not** "
        "`state_topic`: Home Assistant's MQTT device tracker accepts only "
        "`home` / `not_home` / a zone name there, and coordinates sent to "
        "`state_topic` leave the entity showing raw JSON. There is no "
        "`state_topic`, so the state is derived from the position against your "
        "zones. `gps_accuracy` is deliberately omitted — the car reports none."
    )
    out.append("")

    out.append("## Topics")
    out.append("")
    out.append("| Topic | Retained | Payload |")
    out.append("|---|---|---|")
    out.append(
        f"| `{signals.base_topic(vehicle, mqtt_prefix)}/availability` | yes | "
        "`online` / `offline` |"
    )
    out.append(
        f"| `{signals.base_topic(vehicle, mqtt_prefix)}/<obj_id>` | yes | the value |"
    )
    out.append(
        f"| `{signals.base_topic(vehicle, mqtt_prefix)}/state` | yes | every field "
        "as one JSON object |"
    )
    out.append(
        f"| `{discovery_prefix}/<component>/xpeng_<vin>\\<obj_id>/config` | yes | "
        "the discovery config |"
    )
    out.append("")
    out.append(
        f"The VIN in topics is lower-cased (`{vehicle.slug.lower()}`). Deleting an "
        "entity is an empty retained payload on its config topic."
    )
    out.append("")
    out.append(
        f"Availability is `offline` once the cached state is older than "
        f"`stale_after_seconds` (default 1800)."
    )
    out.append("")
    return "\n".join(out)


def write_snapshot() -> None:
    """Read the live app state, redact it, and save it as the committed sample."""
    vehicle = reader.read_vehicles()[0]
    real_vin = vehicle.vin

    # The real VIN must not reach the sample. This has to replace `vehicle.vin`
    # itself, not just the display name: `slug`, and therefore every topic and
    # entity ID in the output, is derived from the VIN. The first version of this
    # only masked `disp`, and the docs shipped the real VIN in lower case in five
    # topic names.
    vehicle.vin = EXAMPLE_VIN
    vehicle.meta["vin"] = EXAMPLE_VIN
    vehicle.meta["disp"] = EXAMPLE_VIN
    vehicle.meta["plateNo"] = None

    state = {
        group: {
            key: redact(f"{group}.{key}", value, real_vin)
            for key, value in block.items()
        }
        if isinstance(block, dict)
        else redact(group, block, real_vin)
        for group, block in vehicle.state.items()
    }

    sample = {
        "vin": EXAMPLE_VIN,
        "uid": "10000001",
        "meta": vehicle.meta,
        "capabilities": vehicle.capabilities,
        "state": state,
        "_note": "Redacted sample used to generate ENTITIES.md and STATE-FIELDS.md.",
    }
    serialised = json.dumps(sample, indent=1, sort_keys=True, default=str) + "\n"
    # The guard belongs here, not at generation time: this is the only point at
    # which the real VIN is known, and the sample is what gets committed. Check
    # both cases, because topics lower-case it and an upper-case-only check
    # passed while the real VIN sat in five topic names.
    for form in (real_vin, real_vin.lower()):
        if form in serialised:
            raise SystemExit(f"refusing to write {SAMPLE}: it contains the VIN as {form}")

    SAMPLE.parent.mkdir(parents=True, exist_ok=True)
    SAMPLE.write_text(serialised)
    print(f"wrote {SAMPLE}")


def load_sample() -> reader.Vehicle:
    """Rebuild a Vehicle from the committed sample, with no app access."""
    if not SAMPLE.exists():
        raise SystemExit(
            f"{SAMPLE} is missing. Create it once from a live read with:\n"
            f"  python3 generate_reference.py --snapshot"
        )
    raw = json.loads(SAMPLE.read_text())
    return reader.Vehicle(
        vin=raw["vin"],
        uid=raw["uid"],
        state=raw["state"],
        meta=raw.get("meta", {}),
        capabilities=raw.get("capabilities", []),
        timestamp=None,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit 1 if the generated docs differ from what is on disk",
    )
    parser.add_argument(
        "--snapshot",
        action="store_true",
        help="re-read the app and rewrite docs/state-sample.json",
    )
    args = parser.parse_args()

    if args.snapshot:
        write_snapshot()
        if not args.check:
            return 0

    vehicle = load_sample()
    if vehicle.vin != EXAMPLE_VIN:
        raise SystemExit(
            f"{SAMPLE} carries VIN {vehicle.vin!r}, not the redacted {EXAMPLE_VIN!r}. "
            "Re-create it with --snapshot rather than committing a live dump."
        )
    docs = {
        DOCS / "STATE-FIELDS.md": state_fields_doc(vehicle),
        DOCS / "ENTITIES.md": entities_doc(vehicle, "xpeng", "homeassistant"),
    }

    stale: list[Path] = []
    for path, content in docs.items():
        if args.check:
            if not path.exists() or path.read_text() != content:
                stale.append(path)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            print(f"wrote {path}")

    if args.check and stale:
        print(
            "generated docs are out of date: "
            + ", ".join(str(p) for p in stale)
            + "\nrun: python3 generate_reference.py",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
