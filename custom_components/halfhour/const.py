"""Constants for the Halfhour integration."""

from datetime import timedelta

DOMAIN = "halfhour"
DEFAULT_URL = "https://halfhour.energy"

CONF_URL = "url"
CONF_CODE = "code"
CONF_TOKEN = "token"
CONF_HUB_ID = "hub_id"
CONF_MAPPING = "mapping"  # {role: {"entity_id": str, "invert": bool, "kind": Kind}}; 0.1.x stored "unit" instead
CONF_ROLES = "roles"  # the gateway's role list, kept so options need no network

SYNC_INTERVAL = timedelta(minutes=5)
SYNC_OFFSET = timedelta(minutes=1)  # after a 5-minute boundary, so its statistics exist
DRAIN_DELAY = 11  # s between backlog windows; the gateway allows 1 per 10 s
SEND_GAP = 11  # s: never two requests closer than this (as DRAIN_DELAY)
MAX_EMPTY_WINDOWS = 100  # windows with nothing to send skipped through in one sync
MAX_AGE = timedelta(days=360)  # how far back a new (or remapped) role backfills; the servers accept 400
WINDOW = timedelta(days=5)  # at most this much history per role per request
MAX_BATCH = 2000  # slots per request, the gateway's limit: WINDOW shrinks when many roles are mapped
FINAL_AFTER = timedelta(minutes=10)  # a slot is final this long after it ends
SHORT_TERM = timedelta(days=11)  # 5-minute statistics are never older than this
BACKOFF_START = 30  # s
BACKOFF_MAX = 1800  # s
STORAGE_VERSION = 1
