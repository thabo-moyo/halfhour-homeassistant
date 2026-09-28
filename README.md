# Halfhour for Home Assistant

Sends your home's power readings (house load, and optionally grid, solar and
battery) to [Halfhour](https://halfhour.energy), so its forecasts and tariff
comparisons use your real half-hourly usage.

Home Assistant only ever connects out. Halfhour never connects in, so there is
nothing to open on your router. Readings are taken every minute and uploaded
every 5 minutes. If Halfhour can't be reached, up to 7 days of readings are
kept and sent later.

## Install with HACS

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
   → Halfhour**, and type the code. Keep the server that Halfhour shows.
3. Pick your sensors. Only house load is required. Suggested sensors are
   filled in when your hardware matches a known setup. Tick **invert** for any
   sensor whose sign is the wrong way round.

To change sensors later, open the integration's **Configure**. If you unlink
Home Assistant in Halfhour, Home Assistant asks you for a new code.

## What it adds

Three diagnostic entities: **Last upload**, **Queued samples** and
**Connected**.

## Development

```sh
uv venv --python 3.13 .venv
uv pip install --python .venv -r requirements_test.txt
.venv/bin/pytest
```

Verified against Home Assistant 2026.2.3.
