# Feeding the position back to EVConduit

The XPENG **data export carries no location of any kind**, so a drive's route on
EVConduit's trip map can only come from a device the owner runs. EVConduit's own
guide therefore asks you to run a GPS logger on your phone.

**This bridge can be that logger.** Its `location` entity is a GPS
`device_tracker`, and EVConduit already documents exactly this pattern, because a
`device_tracker` is what its guide was written against. Nothing needs to change in
EVConduit, and no logger app is needed — you are swapping one entity ID.

> ## Read this first
>
> **It only works if the Mac travels with the car, and even then the fix rate is
> poor.** The bridge reads a cache that the XPENG app refreshes on XPENG's
> schedule, and that schedule is not reliable.
>
> Measured on a Mac left at home, 2026-09-30: the app process was running and
> consuming ~172% CPU, and the cached state had **not changed in 30 minutes** —
> a 5-minute sample taken every 10 seconds saw zero updates. Whatever the app is
> doing in that state, it is not polling the car.
>
> A Mac that stays at home therefore records nothing along a route, because the
> app has no reason to fetch a position it is not moving to. If you want trip
> routes, either run a real GPS logger, or put a MacBook in the car with the app
> open and accept the cadence in §5.

## What you need

* The bridge running and publishing, with the car's `location` entity visible in
  Home Assistant.
* An **EVConduit account with an API key**.
* Outbound HTTPS from Home Assistant. The flow is HA calling EVConduit, so
  nothing needs to reach *into* your network.

## 1. Get your ingest URL

EVConduit's ingest endpoint is:

```
POST https://backend.evconduit.com/api/positions/<subject>
```

`<subject>` is either an internal vehicle id, or **the VIN the export carries**.
For a bridge-only setup the VIN is the right one, and it is the route built for
owners who upload exports without linking a vehicle — you do not need a linked
car.

Fetch yours:

```bash
curl -H "Authorization: Bearer $EVCONDUIT_API_KEY" \
     https://backend.evconduit.com/api/positions/link
```

It returns one path per car and one per VIN. The same list is shown with a copy
button at **Settings → XPENG Data Export → GPS logger** in the EVConduit app.

The VIN in the URL is the same one the bridge publishes under, lower-cased in
topics but in whatever case EVConduit returns — copy it rather than typing it.

## 2. Configuration

Add to `configuration.yaml`:

```yaml
rest_command:
  evconduit_position:
    url: "https://backend.evconduit.com/api/positions/YOUR_VIN"
    method: POST
    headers:
      Authorization: !secret evconduit_authorization
      Content-Type: "application/json"
    payload: >-
      {"lat": {{ lat }}, "lon": {{ lon }},
       "timestamp": {{ ts }}, "accuracy": {{ acc }}}
```

and to `secrets.yaml`:

```yaml
evconduit_authorization: "Bearer YOUR_EVCONDUIT_API_KEY"
```

## 3. The automation

This is EVConduit's own documented automation with the entity ID swapped. The
comments explain the parts that are deliberate and easy to get wrong.

```yaml
# automations.yaml
- alias: "Forward XPENG positions to EVConduit"
  mode: queued
  max: 10
  triggers:
    # The tracker's STATE is home/not_home and does NOT change while driving, so
    # a plain state trigger fires once as the car leaves and then never again —
    # an automation that looks correct and records nothing. Watch the position
    # attributes instead. Each fix fires both, and the second run upserts onto
    # the first's row.
    - trigger: state
      entity_id: device_tracker.xpeng_l1nnsgha0sb000000_location
      attribute: latitude
    - trigger: state
      entity_id: device_tracker.xpeng_l1nnsgha0sb000000_location
      attribute: longitude
  conditions:
    # Optional: skip fixes taken while parked. A stationary car produces fixes
    # all day that no trip window reads. Costs the first ~70 m of a departure.
    - condition: not
      conditions:
        - condition: zone
          entity_id: device_tracker.xpeng_l1nnsgha0sb000000_location
          zone: zone.home
    # An unknown tracker yields null, which the endpoint rejects point by point.
    - "{{ trigger.to_state.attributes.latitude is not none
          and trigger.to_state.attributes.longitude is not none }}"
  actions:
    - action: rest_command.evconduit_position
      data:
        lat: "{{ trigger.to_state.attributes.latitude }}"
        lon: "{{ trigger.to_state.attributes.longitude }}"
        # The fix's OWN time, never now(). A re-sent last-known fix stamped with
        # the current time is filed against whatever drive is happening now,
        # which puts a point on the wrong trip — worse than a missing one.
        ts: "{{ as_timestamp(trigger.to_state.last_updated) | int }}"
        acc: "{{ trigger.to_state.attributes.gps_accuracy | float(0) }}"
```

Replace `device_tracker.xpeng_l1nnsgha0sb000000_location` with your entity. In
Home Assistant, **Developer Tools → States** and search for `xpeng` to find it;
it is named `<your VIN> Location`.

## 4. Verify

Reload Home Assistant, then drive somewhere — or watch the automation's trace
under **Settings → Automations** and confirm it fires.

The endpoint answers with what it stored and, per index, why anything was not:

```json
{"stored": 1, "ignored": []}
```

A point rejected is not a failed request, so an empty `stored` with a reason is
the useful signal. The common reasons:

| Response | Cause |
|---|---|
| `no_coordinates` | latitude or longitude was null — the guard condition should stop this |
| `null_island` | `0, 0`. A receiver reports it before locking on; it is rejected on purpose |
| `implausible_timestamp` | a 13-digit millisecond timestamp read as seconds |

Positions appear on the trip map at `/insights/xpeng/trips/<id>`. The read side
is `GET /api/user/xpeng/trips/{id}/track`, which binds points to a trip by time
window.

## 5. How detailed will the route be?

**This is the honest limitation, and it is about the bridge, not EVConduit.**

EVConduit's guide says the map's detail *is* the logger's interval: one fix a
minute draws the road, one every ten minutes draws the shape of the journey.

A phone GPS logger samples on its own schedule — often every few seconds. This
bridge cannot, and worse, its rate is not merely low, it is **unpredictable**.

What was actually measured, on a Mac left at home with the app running:

| Observation | Value |
|---|---|
| Sampling window | 5 minutes, every 10 s |
| Distinct cached timestamps seen | **1** — the cache never moved |
| Age of the cached state at the end | **29 minutes** and rising |
| App process | running, using ~172% CPU |
| Last write to the app's database | 30 minutes before the check |

So the app can sit there apparently busy without polling the car at all. Earlier
in the same session the cache *was* advancing, so this is a state the app falls
into rather than a fixed interval — which means **you cannot plan around it**.

Two consequences:

* **A Mac that stays at home will not draw a route.** Positions are fetched while
  the app is refreshing, and the app is refreshing at home, not mid-drive. You
  would get a fix at each end and nothing between.
* **A Mac in the car might**, at whatever cadence the app happens to be polling —
  expect the shape of the journey, not the road, and expect gaps.

If you want reliable trip maps, run a real logger. The two can post to the same
URL: the endpoint upserts on each point's own timestamp, so a logger and this
bridge will not conflict or duplicate.

Where the bridge is genuinely strong is **live state while parked** — battery,
charging, charge limit, tyre pressures, doors — because that is what the app
polls for and it is what these entities were built for. Routes are the wrong
thing to ask of it.

Two further limits worth knowing:

* **Fixes stop when the app stops refreshing.** The Mac must be awake and the app
  signed in. The `Data age` sensor tells you how stale the last reading is, and
  past `stale_after_seconds` the device goes unavailable — which the condition
  template above treats as `null` and skips, which is the correct outcome.
* **A parked car reports its last known fix indefinitely.** That is precisely why
  the automation keys on the fix's own `last_updated` rather than the current
  time: a re-sent fix would otherwise be filed against the current drive.

## 6. Is there anything else worth sending back?

The position push is the only one built for this. The bridge's other entities are
already available in EVConduit by other routes — battery and charging state come
through Enode or the export, and the trip and charge history comes from the
export — so pushing them again would duplicate rows rather than add information.

The one genuinely additive thing the bridge knows and the export does not is
**live state**, and EVConduit has no ingest for that. If you want it there, MQTT
is the side that already has it.
