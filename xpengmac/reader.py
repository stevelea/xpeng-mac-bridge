"""
Read the vehicle state the XPENG app keeps on this Mac.

The app (``com.xiaopeng.XiaoPengQiChe.International``) runs here as an iPad app
on Apple Silicon. It caches live vehicle state two ways, and this module reads
the better one.

Why SQLite and not the widget plist
-----------------------------------
The app writes the same poll to both:

  * ``Library/XPVehicleSDK/<uid>/XPVehicle.db`` — one ``VehicleState`` row per
    VIN, 33 JSON signal groups, plus ``Vehicle``/``VehicleExtInfo`` metadata.
  * ``Group Containers/group.xiaopeng.intl.cookie/.../group.xiaopeng.intl.cookie.plist``
    — ``kXPAppGroupCookiesKeyWidgetCarStateData.<VIN>``, which the widget reads.

Measured 2026-09-30, both land on the same instant (13:44:14 local), so neither
is fresher than the other. SQLite wins because it is richer: the plist carries
about 25 groups, and ``local``/``monaPower``/``extDoor`` are null in SQLite
while ``bleTimestamp`` is null in the plist, so the two are *complementary*
rather than ranked. We take SQLite as the spine and merge the plist on top for
the handful of fields it alone has.

Reading it safely
-----------------
``mode=ro`` is the right mode and ``immutable=1`` is the wrong one: the app is
actively writing, and ``immutable`` would let SQLite return stale pages from
before the write. A read-only connection participates in WAL normally and sees
committed data without taking a write lock, so polling costs the app nothing.
``busy_timeout`` covers the brief window during a checkpoint.

SQLite timestamps here are **UTC and naive** — ``"2026-09-30 03:44:14.646"``
alongside a plist that says ``2026-09-30T13:44:14+1000`` for the same event.
Assuming local time silently shifts everything by the UTC offset.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CONTAINER_ROOT = Path.home() / "Library/Containers"

GROUP_CONTAINER_ROOT = Path.home() / "Library/Group Containers"
WIDGET_PLIST = (
    GROUP_CONTAINER_ROOT
    / "group.xiaopeng.intl.cookie/Library/Preferences/group.xiaopeng.intl.cookie.plist"
)

# Groups the plist carries that SQLite leaves NULL. Only these are merged, so a
# stale or absent plist can never overwrite live SQLite data.
PLIST_ONLY_GROUPS = (
    "local",
    "monaPower",
    "extDoor",
    "bleTimestamp",
)

# Signal groups, in dashboard order. Missing groups are simply absent.
STATE_GROUPS = (
    "charge",
    "chargeStatistics",
    "odometer",
    "drive",
    "hvac",
    "door",
    "window",
    "tpms",
    "power",
    "seat",
    "steerWheel",
    "trunkPower",
    "tail",
    "fridge",
    "ext",
    "protectMode",
    "fuelTank",
    "hook",
    "local",
    "monaPower",
    "extDoor",
    "bleTimestamp",
)

_LEADING_BRACE = re.compile(r"^\s*[\[{]")


@dataclass
class Vehicle:
    """One VIN's current state, as last cached by the app."""

    vin: str
    uid: str
    state: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)
    capabilities: list[str] = field(default_factory=list)
    timestamp: datetime | None = None
    db_path: Path | None = None
    db_modified: float = 0.0

    @property
    def age_seconds(self) -> float | None:
        """Seconds since the app's cached state was captured, or None."""
        if self.timestamp is None:
            return None
        return (datetime.now(timezone.utc) - self.timestamp).total_seconds()

    @property
    def slug(self) -> str:
        """Topic-safe identifier. VINs are already ``[A-Z0-9]``."""
        return re.sub(r"[^A-Za-z0-9]", "", self.vin).upper()

    def get(self, dotted: str, default: Any = None) -> Any:
        """Look up ``group.signal``, returning default when either is missing."""
        group, _, signal = dotted.partition(".")
        if not signal:
            return self.state.get(group, default)
        block = self.state.get(group)
        if not isinstance(block, dict):
            return default
        value = block.get(signal, default)
        return default if value is None else value


# --------------------------------------------------------------------------- #
# timestamps


def parse_timestamp(value: Any) -> datetime | None:
    """Parse any timestamp shape the app writes, always returning aware UTC.

    Deliberately does not call ``datetime.fromisoformat`` on the raw string.
    That function is only safe from Python 3.11 — before that it accepts three
    or six fractional digits and nothing else, so a trimmed ``...T22:06:57.49``
    returns None with no error rather than raising. Measured: 11 of 121 real
    timestamps were unparseable on 3.10 and all 121 parsed on 3.12, which is
    how the same data produced different answers in two environments.
    """
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        # chargeStatistics.completeTime is epoch milliseconds. Anything past
        # year 5138 is implausible for seconds, so treat large values as ms.
        seconds = value / 1000.0 if abs(value) > 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()

    # "2026-09-30 03:44:14.646" and "2026-09-30 03:44:14" — both UTC here.
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    iso = text.replace("Z", "+00:00")
    # "+1000" -> "+10:00"; fromisoformat rejects the compact form before 3.11.
    compact = re.search(r"([+-]\d{2})(\d{2})$", iso)
    if compact:
        iso = f"{iso[: compact.start()]}{compact.group(1)}:{compact.group(2)}"
    # Pad/truncate the fractional part to exactly 6 digits.
    iso = re.sub(
        r"\.(\d+)",
        lambda m: "." + (m.group(1) + "000000")[:6],
        iso,
    )
    try:
        parsed = datetime.fromisoformat(iso)
    except ValueError:
        logger.debug("unparseable timestamp %r", value)
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class AccessDenied(RuntimeError):
    """macOS refused to let us into the app's container.

    Raised instead of FileNotFoundError because the two are easily confused and
    only one of them is the user's fault to fix. Measured 2026-09-30 on this
    machine, from a ``launchd`` agent with no Full Disk Access:

      * ``os.listdir`` on ``<container>/Data``            -> EPERM
      * ``sqlite3.connect("file:...?mode=ro")``           -> "authorization denied"
      * ``open()`` on the group container plist           -> EPERM
      * ``os.listdir`` on ``~/Library/Group Containers``  -> allowed
      * ``stat()`` on either file                         -> allowed (metadata only)

    So both data sources are gated, a directory listing can succeed while the
    file inside it does not, and ``glob`` reports zero matches for a permission
    error rather than raising — which is exactly how this masqueraded as "the app
    has never been signed in".
    """


ACCESS_HELP = (
    "macOS is blocking access to the XPENG app's container.\n"
    "\n"
    "Both data sources live behind the same protection, so there is no path\n"
    "that avoids it: a small script run from your terminal normally works\n"
    "because the terminal already has Full Disk Access, but a launchd agent\n"
    "does not inherit that.\n"
    "\n"
    "To fix it, grant Full Disk Access to the program that runs the bridge:\n"
    "  System Settings -> Privacy & Security -> Full Disk Access -> +\n"
    "then add XPENGBridge.app (recommended — it scopes the permission to this\n"
    "one tool rather than to every Python script), or add the Python\n"
    "interpreter directly for a quick test.\n"
    "\n"
    "After granting it, reload the agent:\n"
    "  ./install-launchd.sh\n"
)


@dataclass
class SourceProbe:
    """What we could and could not reach, for diagnostics."""

    database: Path | None = None
    database_error: str | None = None
    plist_readable: bool | None = None
    plist_error: str | None = None

    @property
    def any_source(self) -> bool:
        return self.database is not None or self.plist_readable is True


def probe_sources() -> SourceProbe:
    """Check each data source independently, without raising.

    Used by the CLI so a failure names the blocked thing instead of guessing.
    """
    probe = SourceProbe()

    try:
        probe.database = select_database()
    except AccessDenied as exc:
        probe.database_error = str(exc).splitlines()[0]
    except (FileNotFoundError, sqlite3.Error) as exc:
        probe.database_error = f"{type(exc).__name__}: {exc}"

    try:
        with open(WIDGET_PLIST, "rb") as handle:
            handle.read(16)
        probe.plist_readable = True
    except PermissionError:
        probe.plist_readable = False
        probe.plist_error = "Operation not permitted"
    except OSError as exc:
        probe.plist_readable = False
        probe.plist_error = f"{type(exc).__name__}: {exc}"

    return probe


# --------------------------------------------------------------------------- #
# database discovery


def find_databases() -> list[Path]:
    """Every ``XPVehicle.db`` under any app container, newest first.

    Walks the tree explicitly rather than using ``glob``. ``glob`` swallows
    ``OSError`` while descending, so a permission denial returns an
    empty list — indistinguishable from "no such file" — and that is the wrong
    answer to give someone whose real problem is a missing Full Disk Access
    grant. Here a denial is counted, and reported when nothing was found.

    The parent directory of the database is the account uid, and an account that
    has been switched away from can leave an empty directory behind, so an empty
    directory is not an error.
    """
    if not CONTAINER_ROOT.is_dir():
        raise FileNotFoundError(f"{CONTAINER_ROOT} does not exist")

    try:
        containers = list(CONTAINER_ROOT.iterdir())
    except PermissionError as exc:
        raise AccessDenied(ACCESS_HELP) from exc

    found: list[Path] = []
    denied = 0

    for container in containers:
        sdk_root = container / "Data/Library/XPVehicleSDK"
        try:
            uid_dirs = list(sdk_root.iterdir())
        except PermissionError:
            # Every container's Data directory is protected until Full Disk
            # Access is granted, so this is the normal unprivileged outcome.
            denied += 1
            continue
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError:
            continue

        for uid_dir in uid_dirs:
            candidate = uid_dir / "XPVehicle.db"
            try:
                if candidate.is_file():
                    found.append(candidate)
            except OSError:
                continue

    if not found and denied:
        raise AccessDenied(ACCESS_HELP)

    return sorted(found, key=_database_freshness, reverse=True)


def _database_freshness(db_path: Path) -> float:
    """Newest mtime across the db and its WAL.

    The main db file can sit untouched for hours while every write lands in the
    WAL, so the db file's own mtime is not the freshness signal.
    """
    stamps = [db_path.stat().st_mtime] if db_path.exists() else []
    for suffix in ("-wal", "-shm"):
        sidecar = Path(str(db_path) + suffix)
        if sidecar.exists():
            stamps.append(sidecar.stat().st_mtime)
    return max(stamps) if stamps else 0.0


def select_database(uid: str | None = None, db_path: Path | None = None) -> Path:
    """Resolve the database to read, or raise with an actionable message."""
    if db_path is not None:
        if not db_path.exists():
            raise FileNotFoundError(f"no database at {db_path}")
        return db_path

    candidates = find_databases()
    if not candidates:
        raise FileNotFoundError(
            "no XPVehicle.db found under ~/Library/Containers. The XPENG app "
            "has to have been signed in on this Mac at least once, and to have "
            "fetched vehicle state — the database is created on first sync."
        )

    if uid:
        matches = [p for p in candidates if p.parent.name == str(uid)]
        if not matches:
            known = ", ".join(sorted(p.parent.name for p in candidates))
            raise FileNotFoundError(f"no database for uid {uid!r}; found: {known}")
        return matches[0]

    return candidates[0]


# --------------------------------------------------------------------------- #
# plist enrichment


def _plutil_json(key: str, path: Path) -> Any | None:
    """Extract one key from a plist and parse it as JSON, or return None.

    Tries ``json`` output then ``raw``, because which one works depends on the
    value and could not be predicted — both cases measured on this machine:

      * ``kXPAppGroupCookiesKeyWidgetCarStateData.<VIN>`` is a **String** holding
        JSON text. ``json`` fails on it with "Invalid object in plist for JSON
        format" (the plist's ``NSData`` token blobs cannot be rendered as JSON,
        and plutil appears to validate the container it walked), while ``raw``
        prints the string's contents verbatim.
      * ``kXPAppGroupCookiesKeyWidgetVehicleInfo`` is an **array of dicts**, where
        ``json`` works and ``raw`` is useless (it prints ``1``).

    Both branches end up parsed by ``json.loads``, so the caller sees one shape.

    The widget keys contain a literal dot, which ``plutil`` would read as a
    key-path separator, so every dot is escaped.
    """
    escaped = key.replace(".", "\\.")
    for fmt in ("json", "raw"):
        try:
            result = subprocess.run(
                ["plutil", "-extract", escaped, fmt, "-o", "-", str(path)],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.debug("plutil unavailable: %s", exc)
            return None

        if result.returncode != 0 or not result.stdout.strip():
            continue
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            continue

    logger.debug("no usable value for %s in %s", key, path)
    return None


def read_plist_widget(
    vin: str, plist_path: Path | None = None
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the widget plist for ``vin``.

    Returns ``(extra_groups, vehicle_info)``. The groups are the handful SQLite
    leaves NULL; the info block is the app's own description of the car, which
    is where the commercial model name and the app's own VIN mask come from.
    """
    path = plist_path or WIDGET_PLIST
    try:
        if not path.exists():
            return {}, {}
    except PermissionError:
        # exists() can itself be denied; fall through and let the read report it.
        pass

    extra: dict[str, Any] = {}
    state = _plutil_json(f"kXPAppGroupCookiesKeyWidgetCarStateData.{vin}", path)
    if isinstance(state, dict):
        extra = {g: state[g] for g in PLIST_ONLY_GROUPS if g in state}

    info: dict[str, Any] = {}
    raw_info = _plutil_json("kXPAppGroupCookiesKeyWidgetVehicleInfo", path)
    if isinstance(raw_info, list):
        for entry in raw_info:
            if isinstance(entry, dict) and entry.get("vin") == vin:
                info = entry
                break
    return extra, info


# --------------------------------------------------------------------------- #
# reading


def _jsonish(value: Any) -> Any:
    """Decode a column that holds JSON, passing anything else through."""
    if not isinstance(value, str) or not _LEADING_BRACE.match(value):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def read_vehicles(
    db_path: Path | None = None,
    uid: str | None = None,
    *,
    enrich_from_plist: bool = True,
    plist_path: Path | None = None,
    vin: str | None = None,
) -> list[Vehicle]:
    """Return the cached state for every VIN in the database.

    Raises FileNotFoundError when there is no database to read, and sqlite3
    errors when the schema is not what we expect — both are the operator's
    problem to see, not something to paper over with an empty list.
    """
    path = select_database(uid=uid, db_path=db_path)
    resolved_uid = path.parent.name

    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.OperationalError as exc:
        # "authorization denied" is TCC talking, not a corrupt database. Even a
        # path we can stat is unreadable until Full Disk Access is granted.
        if "authorization denied" in str(exc) or "unable to open" in str(exc):
            raise AccessDenied(ACCESS_HELP) from exc
        raise
    try:
        connection.execute("PRAGMA busy_timeout = 5000")
        vehicles = _read_vehicles(connection, path, resolved_uid)
    finally:
        connection.close()

    if vin:
        wanted = vin.upper()
        vehicles = [v for v in vehicles if v.vin.upper() == wanted]
        if not vehicles:
            raise FileNotFoundError(f"VIN {vin} not present in {path}")

    if enrich_from_plist:
        for vehicle in vehicles:
            extra, info = read_plist_widget(vehicle.vin, plist_path)
            for group, value in extra.items():
                # Only fills what SQLite could not give us.
                if vehicle.state.get(group) in (None, {}, ""):
                    vehicle.state[group] = value
            if info:
                # The app's own names. `typeName` is the commercial model ("G6")
                # where the database's `vehicleTypeName` is the OEM project code
                # ("F30"); `disp` is the mask the app itself displays. Taking
                # them means this tool never has to hold a code->model map, which
                # would be a second source of truth for something the app
                # already knows and we would get wrong first.
                vehicle.meta.setdefault("typeName", info.get("typeName"))
                vehicle.meta.setdefault("disp", info.get("disp"))
                vehicle.meta.setdefault("colour", info.get("color"))
            ble = vehicle.state.get("bleTimestamp")
            if ble and vehicle.timestamp is None:
                vehicle.timestamp = parse_timestamp(ble)

    return vehicles


def _read_vehicles(
    connection: sqlite3.Connection, path: Path, uid: str
) -> list[Vehicle]:
    connection.row_factory = sqlite3.Row

    meta_by_vin = _read_metadata(connection)
    modified = _database_freshness(path)

    vehicles: list[Vehicle] = []
    for row in connection.execute("SELECT * FROM VehicleState"):
        columns = set(row.keys())
        vin = row["vin"]
        state: dict[str, Any] = {}
        for group in STATE_GROUPS:
            if group not in columns:
                continue
            value = _jsonish(row[group])
            if value is not None:
                state[group] = value

        meta = meta_by_vin.get(vin, {})
        vehicles.append(
            Vehicle(
                vin=vin,
                uid=uid,
                state=state,
                meta=meta.get("meta", {}),
                capabilities=meta.get("capabilities", []),
                timestamp=parse_timestamp(row["timestamp"]) if "timestamp" in columns else None,
                db_path=path,
                db_modified=modified,
            )
        )
    return vehicles


def _read_metadata(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Vehicle identity and capability list, keyed by VIN."""
    out: dict[str, dict[str, Any]] = {}

    try:
        for row in connection.execute("SELECT * FROM Vehicle"):
            data = dict(row)
            vin = data.pop("vin", None)
            if not vin:
                continue
            # These three are large JSON blobs of server-side detail; keep them
            # out of the published metadata rather than shipping them verbatim.
            for noisy in ("ownerInfo", "permissions", "equipment", "function",
                          "vehicleAppearance", "vehicleCdu"):
                data.pop(noisy, None)
            out.setdefault(vin, {})["meta"] = data
    except sqlite3.DatabaseError as exc:
        logger.debug("Vehicle table unreadable: %s", exc)

    try:
        for row in connection.execute("SELECT vin, capabilities FROM VehicleExtInfo"):
            caps = _jsonish(row["capabilities"])
            if row["vin"] and isinstance(caps, list):
                out.setdefault(row["vin"], {})["capabilities"] = caps
    except sqlite3.DatabaseError as exc:
        logger.debug("VehicleExtInfo table unreadable: %s", exc)

    return out


def _app_pids() -> list[int]:
    """Pids of the running XPENG app, if any."""
    try:
        listing = subprocess.run(
            ["pgrep", "-f", "XPENG.app/XPENG"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if listing.returncode != 0:
        return []
    return [int(p) for p in listing.stdout.split() if p.isdigit()]


def is_app_running() -> bool:
    """Whether the XPENG app process is up.

    Informational only. The database keeps its last values whether or not the
    app is running, so a stopped app means stale data, not absent data — which
    is exactly what the published data-age sensor is for.
    """
    return _app_pids() != []


def launch_app(name: str = "XPENG") -> bool:
    """Open the app by name. False if it could not be started."""
    try:
        result = subprocess.run(
            ["open", "-a", name], capture_output=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("could not launch %s: %s", name, exc)
        return False
    if result.returncode != 0:
        logger.debug("open -a %s failed: %s", name, result.stderr.decode().strip())
        return False
    return True


def quit_app(name: str = "XPENG", wait_seconds: float = 15.0) -> bool:
    """Quit the app gracefully, then insist. True once it is gone.

    Graceful first, because the app owns the charging schedule and a polite
    request is worth trying before a hard kill. SIGTERM after the grace period:
    it is a viewer, so nothing is lost by closing it.
    """
    try:
        subprocess.run(
            ["osascript", "-e", f'quit app "{name}"'],
            capture_output=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        pass

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not _app_pids():
            return True
        time.sleep(0.5)

    for pid in _app_pids():
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if not _app_pids():
            return True
        time.sleep(0.5)
    return not _app_pids()


def format_age(seconds: float | None) -> str:
    """Human-readable age, for logs."""
    if seconds is None:
        return "unknown"
    if seconds < 0:
        return "in the future"
    return str(timedelta(seconds=int(seconds)))
