#!/usr/bin/env python3
"""
Fail if this checkout contains anything identifying from this machine.

Written after making the same mistake twice. First the real VIN shipped in
lower case across five topic names in the generated docs, because the check was
upper-case only and the generator masked the display name rather than
``vehicle.vin`` — which is what ``slug`` and therefore every topic derives from.
Then the real latitude appeared as a *test value*, copied out of a live reading
without thinking about what it was.

A hand-written grep cannot catch either, because the identifying values are not
known until they are read: they differ per machine, and the next one to leak
would be whichever new field got pasted into a fixture. So this reads them from
the live app and searches for them, in every case and every encoding that
matters.

Deliberately no allowlist. If a value genuinely has to appear — it does not
today — that decision belongs in a visible exclusion here rather than in a
weakened pattern.

    python3 check-leaks.py            # exit 1 and name the files if anything leaks
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path(__file__).resolve().parent

SKIP_DIRS = {".git", "__pycache__", "XPENGBridge.app", "node_modules"}
SKIP_SUFFIXES = {".pyc", ".png", ".jpg", ".jpeg", ".pdf", ".numbers", ".xlsx"}

# Substrings of 6+ characters are searched as-is; anything shorter would match
# far too much (a 4-digit house number, a two-character state code).
MIN_LENGTH = 6


def identifying_strings() -> dict[str, list[str]]:
    """Values from this machine that must never reach the repository."""
    found: dict[str, list[str]] = {}

    def add(label: str, value: object) -> None:
        if value is None:
            return
        text = str(value)
        if len(text) >= MIN_LENGTH:
            found.setdefault(label, []).append(text)

    try:
        from xpengmac import reader

        vehicles = reader.read_vehicles()
    except Exception as exc:  # noqa: BLE001 - a missing app is not a leak
        print(f"note: could not read live state ({exc}); checking paths only")
        vehicles = []

    for vehicle in vehicles:
        label = f"VIN {vehicle.vin}"
        add(label, vehicle.vin)
        add(label, vehicle.uid)

        for key in ("drive.latitude", "drive.longitude"):
            value = vehicle.get(key)
            if value is None:
                continue
            # Several encodings, because a coordinate survives rounding and
            # re-formatting and a substring match on the full float would miss
            # exactly the case that leaked.
            add(key, f"{value}")
            add(key, f"{float(value):.6f}")
            add(key, f"{round(float(value), 4)}")
            add(key, f"{round(float(value), 3)}")

    # Not the username: it is the public GitHub handle, already in the repo URL,
    # the bundle identifier and the launcher, so flagging it is pure noise. The
    # values worth guarding are the ones that describe the car or the account.
    return found


def scan(needles: dict[str, list[str]]) -> list[tuple[Path, int, str, str]]:
    hits: list[tuple[Path, int, str, str]] = []
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        if path.suffix.lower() in SKIP_SUFFIXES:
            continue
        try:
            text = path.read_text(errors="ignore")
        except OSError:
            continue

        lowered = text.lower()
        for label, values in needles.items():
            for value in values:
                for form in {value, value.lower(), value.upper()}:
                    if form.lower() not in lowered:
                        continue
                    for number, line in enumerate(text.splitlines(), 1):
                        if form.lower() in line.lower():
                            hits.append((path.relative_to(ROOT), number, label, form))
    return hits


def main() -> int:
    needles = identifying_strings()
    if not needles:
        print("nothing to check — no live state available")
        return 0

    total = sum(len(v) for v in needles.values())
    print(f"checking {total} value(s) across {len(needles)} field(s)")

    hits = scan(needles)
    if not hits:
        print("clean — nothing identifying is committed")
        return 0

    print(f"\nFAIL: {len(hits)} occurrence(s) of identifying data:\n", file=sys.stderr)
    for path, line, label, value in hits:
        print(f"  {path}:{line}  {label}  <- {value!r}", file=sys.stderr)
    print(
        "\nReplace the value with a public or synthetic one. Test fixtures are "
        "the usual culprit.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
