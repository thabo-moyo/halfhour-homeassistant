"""Constants for the Halfhour integration."""

from datetime import timedelta

DOMAIN = "halfhour"
DEFAULT_URL = "https://halfhour.energy"

CONF_URL = "url"
CONF_CODE = "code"
CONF_TOKEN = "token"
CONF_HUB_ID = "hub_id"
CONF_MAPPING = "mapping"  # {role: {"entity_id": str, "invert": bool, "unit": "W" | "%"}}
CONF_ROLES = "roles"  # the gateway's role list, kept so options need no network

SAMPLE_INTERVAL = timedelta(seconds=60)
UPLOAD_INTERVAL = timedelta(minutes=5)
DRAIN_DELAY = 11  # s between backlog batches; the gateway allows 1 per 10 s
MAX_BATCH = 2000
MAX_AGE = timedelta(days=7)
BACKOFF_START = 30  # s
BACKOFF_MAX = 1800  # s
STORAGE_VERSION = 1
