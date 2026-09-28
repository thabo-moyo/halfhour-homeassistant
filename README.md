# Halfhour for Home Assistant

Sends your home's half-hourly energy use (house load, and optionally grid,
solar and battery) to [Halfhour](https://halfhour.energy), so its forecasts,
battery plans and tariff comparisons use your real usage rather than a model.

## What it does

Home Assistant's recorder already keeps statistics for your power and energy
sensors. This integration reads those statistics, turns them into half-hour
averages and uploads them to your Halfhour account. Home Assistant only ever
connects out; Halfhour never connects in, so there is nothing to open on your
router.

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

## Supported devices

Any Home Assistant power sensor or energy counter with long-term statistics,
from any integration (inverters, smart plugs, energy monitors, template
sensors, and so on). A preset pre-fills the form for Victron GX systems.

## Supported functions

The integration adds one **Halfhour** device with three diagnostic entities:

| Entity | Shows |
|---|---|
| `sensor.halfhour_last_upload` | When the last upload got through. |
| `sensor.halfhour_synced_up_to` | Everything before this time is final at Halfhour. |
| `binary_sensor.halfhour_connected` | On while uploads succeed; off while Halfhour can't be reached or refuses this home's key. |

It provides no actions, triggers or conditions of its own.

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

## Troubleshooting

- **Connected is off.** Home Assistant can't reach Halfhour. Check the
  internet connection; uploads resume by themselves and nothing is lost. The
  log notes it once at info level when it starts, and again when it recovers.
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
  sync state with the key removed; attach it to an
  [issue](https://github.com/thabo-moyo/halfhour-homeassistant/issues).

## Removal

1. In Home Assistant, go to **Settings → Devices & services → Halfhour**,
   open **⋮** and choose **Delete**. This stops uploads and forgets the sync
   positions.
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
