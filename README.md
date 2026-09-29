# Halfhour for Home Assistant

Sends your home's half-hourly energy use (house load, and optionally grid,
solar and battery) to [Halfhour](https://halfhour.energy), so its forecasts,
battery plans and tariff comparisons use your real usage rather than a model,
and shows Halfhour's live battery plan in Home Assistant.

The integration never writes to your inverter, battery or any other hardware
in this version: the plan is shown, read-only.

## What it does

Home Assistant's recorder already keeps statistics for your power and energy
sensors. This integration reads those statistics, turns them into half-hour
averages and uploads them to your Halfhour account. It also keeps a live
connection to Halfhour, over which each new plan arrives as soon as it is made.
Home Assistant only ever connects out; Halfhour never connects in, so there
is nothing to open on your router.

Use cases:

- **Better forecasts.** Halfhour learns your house load from real half-hours,
  including the history your recorder already holds.
- **Fairer tariff comparisons.** "What would I have paid on another tariff?"
  is answered from what you actually used, slot by slot.
- **Battery planning.** With battery and solar mapped, Halfhour's plans start
  from how your home really behaves.

## Install

Needs Home Assistant 2026.3 or later.

### With HACS

1. In HACS, open the menu (⋮) → **Custom repositories**, add
   `https://github.com/thabo-moyo/halfhour-homeassistant` with type
   **Integration**.
2. Search HACS for **Halfhour**, download it, and restart Home Assistant.

[![Open your Home Assistant instance and open this repository in HACS.](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=thabo-moyo&repository=halfhour-homeassistant&category=integration)

### Manual install

Copy `custom_components/halfhour` into your Home Assistant config's
`custom_components/` folder and restart.

## Connect

1. In Halfhour, go to **Devices → Link a device → Smart home hub → Home
   Assistant**. It shows a pairing code, valid for 10 minutes.
2. In Home Assistant, go to **Settings → Devices & services → Add integration
   → Halfhour**.
3. Fill in the two installation parameters:
   - **Halfhour address**: leave as shown unless Halfhour's pairing screen
     shows a different address.
   - **Pairing code**: the 8-character code from step 1.
4. Pick your sensors (see below). Only house load is required. Suggested
   sensors are filled in when your hardware matches a known setup.

## Configuration options

The sensor choices can be changed at any time from the integration's
**Configure**. Each reading takes one sensor:

| Field | What to pick |
|---|---|
| House load | Required. What the house uses. |
| Grid power (+ import) | One sensor that is positive importing, negative exporting. |
| Grid import / Grid export | Or two separate sensors, both positive. |
| Solar generation | Solar output. |
| Battery power (+ charging) | Positive while charging, negative while discharging. |
| Battery state of charge | Battery level in %. |

House load, grid import, grid export and solar each accept either kind of
sensor:

- an **energy counter** (kWh, device class `energy`, like the ones on the
  Energy dashboard). Preferred: half-hour totals from a counter are exact.
- a **power sensor** (W, device class `power`). Its 5-minute averages are
  used.

Grid power and battery power take a **power sensor only**: they are signed
(+ import or + charging), and a counter only rises, so it can't say which way
the power flows. For grid energy counters, use grid import and grid export
instead.

Every reading except state of charge has a **reversed** switch. Turn it on
if the sensor counts the other way (for example a battery sensor that is
negative while charging).

A sensor must have long-term statistics: the form refuses one that doesn't.
Sensors get statistics when they have a `state_class` (`measurement` for
power, `total` or `total_increasing` for energy).

To move to a different Halfhour address, use **⋮ → Reconfigure** on the
integration. A home's key belongs to one Halfhour server, so this asks for a
fresh pairing code; your sensor choices and entities are kept.

## Devices behind Home Assistant

Besides the house readings, Halfhour can follow individual devices that Home
Assistant already reads: a home battery, solar, an EV charger or a load (an
immersion heater, a dishwasher). Each one is its own entry under Halfhour on
the integration's page (a config subentry), with a device in Home Assistant
holding its readings and a diagnostic **Controlled by Halfhour** sensor.

**Monitor only.** In this version Halfhour only reads these devices. Some
forms ask for a switch or setpoint entity (a battery's grid setpoint, a
charger's charge switch, a load's switch): Halfhour stores it for when
control arrives, but never switches or sets anything yet.

On the integration's page (**Settings → Devices & services → Halfhour**):

- **Add device**: pick the kind, then an entity for each reading and the
  facts Halfhour needs (a battery's capacity and charge limits, a load's
  rated power and run time, and so on). The kinds, fields, ranges and
  units all come from Halfhour, so the form always matches what Halfhour
  accepts. The pickers list entities of the right domain; Halfhour checks
  the device class and unit when you save and marks the field it refuses.
- **⋮ → Reconfigure** on a device: change its name, entities or facts. A
  field left empty is removed. If the device was changed in Halfhour while
  you were editing, the form shows the latest version to check and save
  again.
- **⋮ → Delete** on a device: removes it from Halfhour too. If Halfhour
  can't be reached, the device stays hidden here and the removal is retried
  until Halfhour confirms it.
- A **load** also takes an optional label saying what it is (Dishwasher,
  Immersion heater); on a load's days, leaving them all empty means every day.
- A device that **needs attention** (an entity it uses no longer exists in
  Home Assistant) can only be saved from Home Assistant once that entity is
  re-picked, even to change just its facts: the form always sends the
  entities, and Halfhour refuses one it can't find.

A new device's latest finished half-hour is sent within a minute of adding
it; its history (up to 360 days) follows, oldest first.

Devices can also be added from Halfhour (**Devices → Home Assistant → Add
device**); both work on the same list. A change appears in Home Assistant
within seconds, over the live connection.

Only entities Home Assistant has already told Halfhour about can be picked:
it sends its entity list (names and units, never states) on setup and a
minute after any change, so a brand-new entity can take a minute. Each
device counts towards your Halfhour plan's device limit.

## How data is updated

- Every **5 minutes** (a minute after the recorder compiles its 5-minute
  statistics) the integration reads each mapped sensor's new statistics and
  uploads **half-hour slots**, including the one in progress. A slot is final
  10 minutes after it ends.
- On first setup, or when you map a new sensor (or flip its **reversed**
  switch), it **backfills up to 360 days** from the recorder's statistics,
  oldest first, in batches of up to 5 days about 11 seconds apart. A full
  year takes about **15 minutes**.
- If Halfhour can't be reached, nothing is lost: each sensor's position is
  saved, and the next successful sync picks up where it stopped. A restart of
  Home Assistant is the same.
- **Plans** arrive over the live connection (MQTT) as soon as Halfhour makes
  them, with no polling. The newest plan is kept, so an older one delivered
  late never replaces it, and it is saved, so it survives a restart. The plan
  entities move to the next slot at every half-hour boundary. If the live
  connection drops, it reconnects by itself, backing off up to 5 minutes;
  uploads carry on over HTTPS meanwhile.

## Supported devices

Any Home Assistant power sensor or energy counter with long-term statistics,
from any integration (inverters, smart plugs, energy monitors, template
sensors, and so on). A preset pre-fills the form for Victron GX systems.

## Supported functions

The integration adds one **Halfhour** device with these diagnostic entities
for uploads:

| Entity | Shows |
|---|---|
| `sensor.halfhour_last_upload` | When the last upload got through. |
| `sensor.halfhour_synced_up_to` | Everything before this time is final at Halfhour, for the home's own sensors. A device added later backfills apart; its progress is in the diagnostics download (`devices_synced_until`). |
| `binary_sensor.halfhour_connected` | On while uploads succeed; off while Halfhour can't be reached or refuses this home's key. |

and the live plan, below. It provides no actions, triggers or conditions of
its own.

## Live plan (read-only)

Halfhour's optimiser plans your battery in half-hour slots. Each plan it makes
is pushed to Home Assistant, and these entities show the slot covering now:

| Entity | Shows |
|---|---|
| `sensor.halfhour_plan_grid_power` | Planned grid power for this slot, W (+ importing, − exporting). |
| `sensor.halfhour_plan_battery_power` | Planned battery power for this slot, W (+ charging, − discharging). |
| `sensor.halfhour_plan_soc_target` | The battery level the plan aims for in this slot, %. |
| `sensor.halfhour_plan_made` | When the plan in use was made. |
| `binary_sensor.halfhour_plan_stale` | On (a problem) when the plan shouldn't be trusted. |
| `binary_sensor.halfhour_live` | On while the live connection to Halfhour is up. |

The three plan targets (grid power, battery power, SoC target) are ordinary
measurement sensors, so they chart and keep long-term statistics like any
other; Plan made, Plan stale and Live are diagnostic. The slot values are
unknown when there is no plan yet, or when no slot of the plan covers the
current time.

**When a plan counts as stale.** Plan stale is on when there is no plan, when
the plan is older than Halfhour's freshness limit (90 minutes unless Halfhour
says otherwise), or when the plan has no slot covering now. Age is measured
from when the plan was made, by Home Assistant's clock, so keep that clock
right: a plan that looks more than 5 minutes ahead of it is noted in the log.
A stale plan's values stay visible, so you can see what it last said.

These entities are for display and your own automations. The integration
itself never writes to hardware in this version.

## Automation example

Get a notification when uploads have been failing for an hour:

```yaml
automation:
  - alias: "Halfhour disconnected"
    triggers:
      - trigger: state
        entity_id: binary_sensor.halfhour_connected
        to: "off"
        for:
          hours: 1
    actions:
      - action: notify.notify
        data:
          title: Halfhour
          message: >-
            Home Assistant hasn't reached Halfhour for an hour. Readings are
            kept and will be sent when it's back.
```

## Known limitations

- The recorder keeps 5-minute statistics for about **10 days**. History older
  than that is hourly, so each half-hour in it is the average of its hour.
- Sensors need a `state_class`, or the recorder keeps no statistics for them.
  Sensors excluded from the recorder can't be used.
- One home per Halfhour account hub: pairing links this Home Assistant as one
  device on your Halfhour account.
- A gap in a sensor's statistics is a gap at Halfhour; it is never filled
  with zeros.
- The plan is read-only: this version never controls a battery or inverter.
- Devices behind Home Assistant are monitor only: their switch and setpoint
  entities are stored, never used, in this version.
- The live connection must be encrypted unless the broker is on your local
  network.

## Troubleshooting

- **Connected is off.** Home Assistant can't reach Halfhour. Check the
  internet connection; uploads resume by themselves and nothing is lost. The
  log notes it once at info level when it starts, and again when it recovers.
- **Live is off.** Home Assistant can't reach Halfhour's live connection.
  It reconnects by itself; uploads are separate and carry on. The log notes
  it once at info level when Live goes down, and again when it is back. If
  Live stays off, check the log. When the live connection refuses this
  home's key, Home Assistant first checks the key with Halfhour: only if
  Halfhour refuses it too are you asked to pair again; otherwise it's
  treated as an outage and retried at least a minute apart. A repair **"Halfhour's live connection needs TLS"**
  means Halfhour offered an unencrypted address outside your local network,
  which Home Assistant refuses. Live is also off when your Halfhour server has
  no live connection configured. In both cases Home Assistant asks Halfhour
  again every 15 minutes and connects by itself once a secure (`mqtts://` or
  `wss://`) or local address is offered; no restart is needed.
- **No plan (the plan entities are unknown).** Halfhour hasn't made a plan for
  this home yet, or the latest plan has no slot for now. A new home shows no
  plan until the optimiser's next run; check that Live is on. For now
  Halfhour's optimiser plans for one home only (its owner's): every other
  home stays on no plan until the optimiser serves more than one home, even
  with Live on.
- **Plan stale is on.** The plan is older than the freshness limit or has run
  out of slots: Halfhour hasn't sent a new one. Check Live, and Plan made for
  when the last one arrived. If Plan made looks wrong by more than a few
  minutes, check Home Assistant's clock.
- **Synced up to is falling behind.** After first setup this is the backfill
  working through history; give it about 15 minutes. If it stays behind,
  check Connected and the log.
- **Repair: "Halfhour can't read …".** A mapped sensor has no statistics or
  no longer exists. Give it a `state_class`, or choose another sensor under
  **Configure**.
- **Asked to pair again.** Halfhour no longer accepts this home's key (for
  example Home Assistant was unlinked in Halfhour). Get a new code from
  **Devices → Link a device → Smart home hub → Home Assistant** and enter it
  when Home Assistant asks.
- **Diagnostics.** **⋮ → Download diagnostics** on the integration gives the
  sync state, the live connection, the plan in use and the last few commands
  from Halfhour, with the key removed; attach it to an
  [issue](https://github.com/thabo-moyo/halfhour-homeassistant/issues).

## Removal

1. In Home Assistant, go to **Settings → Devices & services → Halfhour**,
   open **⋮** and choose **Delete**. This stops uploads, closes the live
   connection and forgets the sync positions and the saved plan.
2. In Halfhour, go to **Devices** and **Unlink** Home Assistant. This revokes
   the key Home Assistant held.
3. If you installed with HACS, remove **Halfhour** in HACS and restart Home
   Assistant. For a manual install, delete `custom_components/halfhour`.

## Development

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements_test.txt
.venv/bin/mypy
.venv/bin/pytest -q --cov=custom_components.halfhour --cov-report=json
.venv/bin/python scripts/check_coverage.py
docker run --rm --platform linux/amd64 -v "$PWD":/github/workspace ghcr.io/home-assistant/hassfest
```

Tests run against Home Assistant 2026.2.3. `custom_components/halfhour/quality_scale.yaml`
records each quality-scale rule as done or exempt.
