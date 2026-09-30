# Implementation guide

Everything needed to get this running from nothing, including the two steps
that catch people out: getting the XPENG app onto a Mac at all, and the macOS
permission the bridge needs to read it.

Budget about half an hour.

---

## 1. What you need

| | |
|---|---|
| **An Apple Silicon Mac** | M1 or newer. This is not a preference — see below. |
| **The XPENG app, signed in** | From the Mac App Store, as an iPhone/iPad app. |
| **A second XPENG account** | Recommended. See §3. |
| **An MQTT broker** | With Home Assistant's MQTT integration, or any broker. |
| **Car visible in the app** | The app must have fetched the car at least once. |

Everything else is in the Python standard library. There is nothing to `pip
install`.

### Why Apple Silicon is required

The XPENG app is an **iPad app**, not a Mac app. Apple only allows iOS and iPadOS
apps to run on Macs with Apple Silicon, where the same architecture lets them run
natively; Intel Macs cannot run them at all.

That matters because this bridge has no other data source. It does not talk to
XPENG's servers and it does not talk to the car. It reads the app's own cache —
the SQLite database and preferences file the app writes while running. No app,
no data.

---

## 2. Install the XPENG app on the Mac

1. Open **App Store**.
2. Search for `XPENG`.
3. Select the **iPhone & iPad Apps** tab in the results. A Mac-native app will
   not exist; you are looking for the iOS listing.
4. Install it, then open it.

If nothing appears under iPhone & iPad Apps, the developer has withdrawn
macOS availability for your region and this approach will not work for you.
There is no workaround — a sideloaded IPA cannot be signed for macOS by you.

Sign in and confirm the app shows your car with live data (battery, range) before
going further. **Until the app has fetched the car once, there is nothing to
read.**

---

## 3. Use a second XPENG account (recommended)

It is tempting to sign the Mac in with your normal account. Don't, for two
reasons:

* The Mac's app session holds a **long-lived refresh token**. If you ever want to
  revoke it, revoking means changing the password on your main account.
* The bridge reads **vehicle location**. Keeping that on an account you can
  delete independently limits the blast radius.

Instead, give a second account access to the car, the same way you would lend
someone the car:

1. On your **main (owner) account**, in the XPENG app, open the vehicle and find
   the sharing / **add driver** section — in the international app this is
   usually under the vehicle's settings or "shared users".
2. Invite the second account by its email address or phone number.
3. Install the XPENG app on the Mac and sign in as the **second account**.
4. Accept the invitation from the second account.

The app's own API confirms this is a supported flow — the binary contains
`/vehicleuser/app/v3/addDriver`, `/vehicleuser/app/v3/vehicleDrivers` and
`/business-center/p/dv_auth/car/user/set`, which is exactly owner-side driver
grant management.

**You do not need to share the digital key.** Sharing a BLE key is a separate
authorisation (`OWNER_SHARE_BLE_KEY`) needed for unlocking and driving, and this
bridge is read-only — it never sends a command. Grant the account access to *see*
the vehicle and you are done.

> If the app updates and changes the sharing flow, note that the bridge depends
> only on the second account being able to see the car's state. As long as the
> app renders live data, the cache is populated.

---

## 4. Confirm the data is there

Before configuring anything, check that the app has written its cache:

```bash
ls ~/Library/Containers/*/Data/Library/XPVehicleSDK/*/XPVehicle.db
```

You should see one path, with the middle directory named after the account's
numeric uid. If you get `No such file or directory`, the app has not synced yet —
open it, look at the car, and let it refresh.

Now read it:

```bash
cd xpeng-mac-bridge
python3 xpeng_bridge.py --print
```

That needs no broker and no configuration. You should get your VIN, the model,
the data age, and all 157 fields. **If that works, the hard part is done.**

If it fails with a permission error, that is expected in some terminals — see
§6.

---

## 5. Configure and run

```bash
mkdir -p ~/.config/xpeng-mac-bridge
cp config.example.json ~/.config/xpeng-mac-bridge/config.json
chmod 600 ~/.config/xpeng-mac-bridge/config.json
$EDITOR ~/.config/xpeng-mac-bridge/config.json
```

The two settings you must set are `mqtt.host` and `mqtt.username` /
`mqtt.password`. `mqtt.discovery_prefix` must match your Home Assistant MQTT
integration's discovery prefix — the default is `homeassistant`, which is also
HA's default. You can confirm yours under **Settings → Devices & Services →
MQTT → Configure**.

Then, in order:

```bash
python3 xpeng_bridge.py --check      # config valid, broker reachable, login accepted
python3 xpeng_bridge.py --dry-run    # see every topic and payload, send nothing
python3 xpeng_bridge.py --once       # create the entities and publish once
```

`--dry-run` is worth actually reading. It prints the discovery payloads, so you
can see exactly what will be created before it exists.

After `--once`, the device appears in Home Assistant under **Settings → Devices &
Services → MQTT** as an entry named with your VIN.

### Run it as a service

```bash
./build-app.sh        # creates XPENGBridge.app
./install-launchd.sh  # substitutes this checkout's path and loads the agent
```

`install-launchd.sh` is what makes the plist work: launchd does not expand `~` or
environment variables, so the agent's paths must be absolute and correct for
where you put the checkout. The file in `launchd/` is a template with
`__CHECKOUT__` and `__HOME__` placeholders.

Logs: `~/Library/Logs/xpeng-mac-bridge.log`.

---

## 6. Grant Full Disk Access

**This is the step that most often goes wrong.** macOS protects one app from
reading another app's container, and the bridge reads the XPENG app's container.
From a `launchd` service:

| Operation | Result |
|---|---|
| list the container's `Data` directory | `Operation not permitted` |
| open the SQLite database read-only | `authorization denied` |
| open the preferences plist | `Operation not permitted` |
| `stat()` either file | **succeeds** |

The last row is the trap. The file *exists* and is *visible*, so the failure looks
like a missing file rather than a denied one — and a library call like `glob`
returns "no matches" for a permission error instead of raising, which sends the
diagnosis in entirely the wrong direction. (That is a real bug this project hit;
`reader.py` now walks the tree by hand and reports the denial.)

Running from your terminal works because your terminal already has the grant. A
service does not inherit it.

**To fix it:**

1. `./build-app.sh`
2. **System Settings → Privacy & Security → Full Disk Access**
3. Click **+**, and add `XPENGBridge.app` from this folder.
4. Reload the service:

```bash
./install-launchd.sh
```

The daemon does **not** exit when access is denied — it logs the fix once, then
one line per retry, so granting access takes effect without reloading.

Re-run `build-app.sh` if you move the folder or edit these files: the signature
pins a content hash, and a modified bundle loses its grant.

---

## 7. Keeping the data fresh

**The bridge is only as fresh as the app.** It reads a cache, so:

* The app must stay signed in.
* The app must refresh periodically. It has background fetch and background
  processing modes, and a scheduled background task, so it does wake on its own —
  but background refresh is at iOS/macOS's discretion, and a Mac that has been
  asleep will not have polled.
* Opening the app is a reliable way to force a refresh.

That is why every entity carries an **availability** topic: past
`behaviour.stale_after_seconds` (default 30 minutes) the whole device flips to
`unavailable` rather than presenting old numbers as current. The `Data age` and
`Data timestamp` diagnostic sensors tell you exactly how old the reading is.

If you want a longer or shorter tolerance, change `stale_after_seconds`.

---

## 8. Security

Read this before pointing the bridge at anything other than a broker on your own
network.

* **The config file holds your MQTT password in plain text.** `chmod 600` it. You
  can keep the password out of the file entirely by setting
  `XPENG_BRIDGE_MQTT_PASSWORD` in the environment or in the launchd plist's
  `EnvironmentVariables` instead.
* **The topics reveal where your car is.** `xpeng/<vin>/location` is a retained
  GPS fix, and `xpeng/<vin>/state` contains every field including the position.
  On a broker reachable by anyone else that is a live location feed of your car
  and, by extension, your home.
* **Use a dedicated broker user** limited by ACL to the topic prefix this bridge
  writes, rather than an admin account. In Mosquitto that is a `topic write
  xpeng/#` rule for one user.
* **Do not expose the broker to the internet** without TLS and per-user ACLs.
* Nothing in this tool can unlock, start or move the car. It has no command path
  at all, and the account it uses needs no digital key.

---

## 9. Troubleshooting

**`no XPVehicle.db found` / permission error**
You are hitting §6. Confirm by running `python3 xpeng_bridge.py --print` in your
terminal: if that works and the service does not, it is the Full Disk Access
grant, not the app.

**The device appears in HA but every entity is `unavailable`**
The availability topic says `offline`, which means the cached state is older than
`stale_after_seconds`. Open the XPENG app and let it refresh. Check the `Data age`
sensor for how stale it was.

**No entities appear at all**
Check `mqtt.discovery` is `true`, that `mqtt.discovery_prefix` matches your HA
MQTT integration, and that HA's MQTT integration has discovery enabled. Publish
`--dry-run` output to confirm configs are being generated. Home Assistant ignores
a discovery payload whose `device_class` is invalid for its component, so check
the log with `logging.level = "DEBUG"`.

**An entity's ID is not what I expected**
Home Assistant derives entity IDs from the device name on first sight, and pins
them to the `unique_id` afterwards — so renaming the device does not rename
entities. `default_entity_id` is set on every payload to make them deterministic
on a fresh install; to rename existing ones, remove them in HA or use `--reset`.

**Changed the device in config and nothing moved**
`--reset` retires every entity and re-creates it, waiting
`reset_settle_seconds` in between. That wait is load-bearing: 8 seconds was not
enough on HA 2026.9.4, 15 was.

**It worked from my terminal and not as a service**
Almost always §6. The other possibility is that the plist's absolute paths are
wrong after moving the folder.

---

## 10. Supporting another platform

The interesting part of this design is that it needs no credentials, and that
generalises. Anywhere the official XPENG app runs and caches state, the same
approach works — you only need to replace `xpengmac/reader.py`, which is the
only platform-specific module. Everything downstream (`signals.py`, the registry,
the discovery payloads) is platform-independent.

On Android, for comparison, the prior art (`schwoi/xpeng-mqtt-bridge`) reads the
*rendered screen* through an accessibility service, which needs a dedicated
phone that can never be locked. Reading the app's own database, as here, has
neither problem — but it does need the container to be readable.
