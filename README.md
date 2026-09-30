# xpeng-mac-bridge

Publish your XPENG car's live state to MQTT, with Home Assistant auto-discovery,
by reading **what the official XPENG app has already cached on your Mac**.

No XPENG developer account, no API key, no reverse-engineered cloud protocol,
and no third-party service in the middle. The app is already logged in and
already polling your car; this reads what it stored and republishes it.

It is **read-only in both directions**:

* it never writes to the app's container, and
* it never contacts XPENG — there is no HTTP client in this tool at all.

```
XPENG iPad app  ──polls──▶  car
      │
      │ caches state in its container + the widget plist
      ▼
xpeng-mac-bridge  ──MQTT + HA discovery──▶  your broker  ──▶  Home Assistant
```

## What you get

One Home Assistant device per car, named with the full VIN, and **32 entities**
created automatically — 19 sensors, 6 binary sensors, a GPS `device_tracker`,
and diagnostics. No YAML.

| | |
|---|---|
| Energy | battery %, range, odometer |
| Charging | power, voltage, current limit, time remaining, energy added, charge limit, last-charge energy and range |
| Climate | inside temperature, target temperature |
| Driving | speed, heading, parked, gear |
| Tyres | all four pressures (kPa) |
| Body | any door open, any window open, tailgate, charging, climate on |
| Location | GPS `device_tracker` with course |
| Diagnostics | data timestamp, data age, Bluetooth last connected, raw enum values |

Every entity is listed in **[docs/ENTITIES.md](docs/ENTITIES.md)**, and every
field the app caches — 157 of them — in
**[docs/STATE-FIELDS.md](docs/STATE-FIELDS.md)**. Both are generated from a live
read by `generate_reference.py`, so they cannot drift from the code.

## Quick start

Requirements: an **Apple Silicon Mac**, the XPENG app signed in, and an MQTT
broker. Nothing to install — this is stdlib-only and runs on the system Python.

```bash
mkdir -p ~/.config/xpeng-mac-bridge
cp config.example.json ~/.config/xpeng-mac-bridge/config.json
chmod 600 ~/.config/xpeng-mac-bridge/config.json   # it holds the MQTT password
$EDITOR ~/.config/xpeng-mac-bridge/config.json     # your broker
```

```bash
python3 xpeng_bridge.py --print     # what can be read; no broker needed
python3 xpeng_bridge.py --dry-run   # every topic and payload; nothing sent
python3 xpeng_bridge.py --once      # create the entities and publish once
python3 xpeng_bridge.py             # run as a daemon
```

Then open Home Assistant — the device appears on its own under Settings →
Devices & Services → MQTT.

To run it as a service:

```bash
./build-app.sh        # creates XPENGBridge.app for the Full Disk Access grant
./install-launchd.sh  # installs and starts the agent
```

**Full instructions — including the two things that trip people up, setting up
the XPENG app on the Mac and granting Full Disk Access — are in
[docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md).**

## Why Full Disk Access

macOS protects other apps' containers with TCC, and the bridge reads two of
them. Everything below was measured on macOS 26 from a `launchd` agent:

| Operation | Result without Full Disk Access |
|---|---|
| `os.listdir` on `<container>/Data` | `EPERM` |
| `sqlite3` open, `mode=ro` | `authorization denied` |
| `open()` on the group-container plist | `EPERM` |
| `stat()` on either file | **allowed** — metadata only |

That last row is the trap: "the file exists" is not evidence you can read it.
Running from your own terminal works only because your terminal already holds
the grant, which is why a command that succeeds interactively can fail under
`launchd`.

`./build-app.sh` produces `XPENGBridge.app` so the grant can be scoped to this
tool. Running `/usr/bin/python3 xpeng_bridge.py` under `launchd` would identify
as Python, and granting *that* Full Disk Access hands the permission to every
Python script on the machine.

## Design notes

Three things here are deliberate and worth not undoing:

**Only measured values are published.** A passthrough is the car's own number,
relabelled, so it is safe. A *derived* value ships only with its evidence beside
it in `xpengmac/signals.py`:

* `charging` — charging power above **1 kW**, which co-occurred with a stationary
  car in 100% of matched samples. The sign of the current cannot be used instead:
  regen also drives it negative.
* `parked` — gear **4 is Park** on this platform. Measured over a month of 1 Hz
  data, the car reads 4 in 95% of samples and 1 whenever moving, so the familiar
  1-2-3-4 = P-R-N-D reading would call the whole month a drive.
* `any_window_open` — position below 100 (closed reads exactly 100), with
  sunshades excluded because they use 255 as a "not reporting" sentinel.

**`plugged_in` is deliberately absent.** The connector-status field has only ever
been seen as `1`, and only while charging. One observed value of an enum is not
enough to name a state, so the raw value is published as a diagnostic instead.
A wrong binary in a dashboard reads as fact.

**The data age is published, and acted on.** The app stores the latest state, not
a history, so stale values are the failure mode. Every entity carries an
`availability` topic that flips to `offline` past `stale_after_seconds`, and
`Data age` / `Data timestamp` are exposed so you can tell "the bridge is running"
from "the car last spoke at".

That distinction is not academic. Measured 2026-09-30: the XPENG app was running
and using ~172% CPU while the cached state had not changed in **30 minutes** — a
5-minute sample every 10 seconds saw zero updates. An app can be alive, busy and
not polling. Watch `Data age`, not the process.

## Documentation

| Document | Contents |
|---|---|
| [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) | Setting up the app, a second XPENG account, permissions, config, running as a service, troubleshooting |
| [docs/ENTITIES.md](docs/ENTITIES.md) | Every discovery entity, its source field, unit and device class |
| [docs/STATE-FIELDS.md](docs/STATE-FIELDS.md) | All 157 cached fields, with types and observed values |
| [docs/EVCONDUIT.md](docs/EVCONDUIT.md) | Forwarding positions back to EVConduit for trip maps, and why that is harder than it sounds |

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

64 tests, no dependencies. `tests/test_broker.py` is a real in-process MQTT 3.1.1
broker rather than a stub, so a framing mistake fails the test instead of being
agreed with. The registry tests enforce the rules Home Assistant applies at
runtime — a valid `device_class` for the component, a unit valid for the
`device_class`, no `state_class` on a timestamp — which would otherwise only show
up as an entity silently failing to appear.

## Licence

MIT.
