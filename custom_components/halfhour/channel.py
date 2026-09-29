"""The live MQTT channel to Halfhour's broker.

One connection per paired home: client id and username are the hub id, the
password is the device token. The home says it is online (retained, with a
retained Last Will saying offline), receives the Optimiser's Plan, its config
and commands, and acks each command. paho runs its own network thread; every
callback is moved onto HA's loop before it touches any state here.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import random
import threading
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlsplit

from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later
from homeassistant.loader import async_get_integration
from homeassistant.util import dt as dt_util

from .const import CONF_HUB_ID, CONF_TOKEN, DOMAIN
from .plan import Plan, PlanError, parse_plan

if TYPE_CHECKING:
    import paho.mqtt.client as mqtt
    from paho.mqtt.reasoncodes import ReasonCode

    from . import HalfhourConfigEntry

_LOGGER = logging.getLogger(__name__)

KEEPALIVE = 60  # s
SESSION_EXPIRY = 86400  # s: commands queue at the broker for a day while offline
RECONNECT_MAX = 300  # s
JITTER = (0.7, 1.3)
SEEN_IDS = 100  # command ids remembered for de-duplication
SHOWN_IDS = 10  # command ids exposed to diagnostics
# CONNACK codes that say the credentials are refused: v3.1.1 4/5, v5 0x86/0x87.
# EMQX also answers 135 when its auth hook (the gateway) is down or misconfigured,
# so the key is checked over HTTP before one of these counts as a revocation.
NOT_AUTHORISED = frozenset({4, 5, 134, 135})
OUTAGE_RETRY = 60  # s: the least wait after a refusal that turned out not to be the key
QOS = 1
STABLE_AFTER = 60  # s up before the reconnect backoff starts again from 1 s

_DEFAULT_PORTS = {"mqtt": 1883, "mqtts": 8883, "ws": 80, "wss": 443}
_SECURE = frozenset({"mqtts", "wss"})
_WEBSOCKET = frozenset({"ws", "wss"})


def url_allowed(url: str) -> bool:
    """TLS URLs always; plain ones only to a private, link-local or .local host."""
    try:
        parts = urlsplit(url)
        host = parts.hostname
        _ = parts.port  # raises on an out-of-range or non-numeric port
    except ValueError:
        return False
    if parts.scheme not in _DEFAULT_PORTS or not host:
        return False
    if parts.scheme in _SECURE:
        return True
    if host.endswith(".local"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_private or addr.is_link_local


class Transport(Protocol):
    """The thin seam over paho, so tests can use a fake broker connection.

    connect and disconnect block (call them from an executor); subscribe and
    publish only queue. The callbacks are invoked on the HA loop.
    """

    on_connect: Callable[[int], None]  # reason code: 0 ok, 4/5/134/135 not authorised
    on_disconnect: Callable[[int], None]
    on_message: Callable[[str, bytes], None]
    on_subscribe: Callable[[list[int]], None]  # SUBACK reason codes, in the order subscribed

    def connect(
        self, host: str, port: int, tls: bool, ws: bool, username: str, password: str, will: tuple[str, bytes], keepalive: int, path: str = "/mqtt"
    ) -> None: ...

    def subscribe(self, topics: list[tuple[str, int]]) -> None: ...

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None: ...

    def disconnect(self) -> None: ...


def _noop(*_args: Any) -> None:
    return None


def _detach(client: mqtt.Client) -> None:
    """A closing client must not report into the next connection's state."""
    client.on_connect = None
    client.on_disconnect = None
    client.on_message = None
    client.on_subscribe = None


class PahoTransport:
    """paho-mqtt 2.x, MQTT v5, persistent session; a fresh client per connect."""

    def __init__(self, hass: HomeAssistant, client_id: str) -> None:
        self._hass = hass
        self._client_id = client_id
        self._client: mqtt.Client | None = None
        # connect runs in an executor and disconnect (an unload) can land while
        # it blocks: the lock orders them, and once closed no client may start.
        self._lock = threading.Lock()
        self._closed = False
        self.on_connect: Callable[[int], None] = _noop
        self.on_disconnect: Callable[[int], None] = _noop
        self.on_message: Callable[[str, bytes], None] = _noop
        self.on_subscribe: Callable[[list[int]], None] = _noop

    def connect(
        self, host: str, port: int, tls: bool, ws: bool, username: str, password: str, will: tuple[str, bytes], keepalive: int, path: str = "/mqtt"
    ) -> None:
        import paho.mqtt.client as mqtt  # noqa: PLC0415 - keep paho off the import path of HA's loop
        from paho.mqtt.enums import CallbackAPIVersion  # noqa: PLC0415
        from paho.mqtt.packettypes import PacketTypes  # noqa: PLC0415
        from paho.mqtt.properties import Properties  # noqa: PLC0415

        props = Properties(PacketTypes.CONNECT)  # type: ignore[no-untyped-call]  # paho leaves it unannotated
        props.SessionExpiryInterval = SESSION_EXPIRY
        with self._lock:
            if self._closed:
                return
            self._close()
            client = mqtt.Client(
                CallbackAPIVersion.VERSION2,
                client_id=self._client_id,
                protocol=mqtt.MQTTv5,
                transport="websockets" if ws else "tcp",
                reconnect_on_failure=False,  # the channel owns reconnects and their backoff
            )
            if ws:
                client.ws_set_options(path=path)
            if tls:
                client.tls_set()
            client.username_pw_set(username, password)
            client.will_set(will[0], will[1], qos=QOS, retain=True)
            client.on_connect = self._paho_connect
            client.on_disconnect = self._paho_disconnect
            client.on_message = self._paho_message
            client.on_subscribe = self._paho_subscribe
            self._client = client
        client.connect(host, port, keepalive=keepalive, clean_start=False, properties=props)  # blocks: outside the lock
        with self._lock:
            if self._client is not client:  # closed while connecting: never start its network thread
                _detach(client)
                client.disconnect()
                return
            client.loop_start()

    def subscribe(self, topics: list[tuple[str, int]]) -> None:
        if self._client is not None:
            self._client.subscribe(topics)

    def publish(self, topic: str, payload: bytes, qos: int, retain: bool) -> None:
        if self._client is not None:
            self._client.publish(topic, payload, qos=qos, retain=retain)

    def disconnect(self) -> None:
        """Close for good: the channel only disconnects when it stops."""
        with self._lock:
            self._closed = True
            self._close()

    def _close(self) -> None:
        """Close the current client; the caller holds the lock."""
        client, self._client = self._client, None
        if client is None:
            return
        _detach(client)
        client.disconnect()
        client.loop_stop()

    # paho's thread -> HA's loop

    def _paho_connect(self, client: mqtt.Client, _userdata: Any, _flags: Any, reason: ReasonCode, _props: Any) -> None:
        self._hass.loop.call_soon_threadsafe(self.on_connect, reason.value)

    def _paho_disconnect(self, client: mqtt.Client, _userdata: Any, _flags: Any, reason: ReasonCode, _props: Any) -> None:
        self._hass.loop.call_soon_threadsafe(self.on_disconnect, reason.value)

    def _paho_message(self, client: mqtt.Client, _userdata: Any, message: mqtt.MQTTMessage) -> None:
        self._hass.loop.call_soon_threadsafe(self.on_message, message.topic, message.payload)

    def _paho_subscribe(self, client: mqtt.Client, _userdata: Any, _mid: int, reasons: list[ReasonCode], _props: Any) -> None:
        self._hass.loop.call_soon_threadsafe(self.on_subscribe, [r.value for r in reasons])


class HalfhourChannel:
    """This home's MQTT connection: presence, Plans, config, commands and the device list."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: HalfhourConfigEntry,
        url: str,
        account: str | None,
        on_plan: Callable[[Plan], None],
        on_command: Callable[[str, dict[str, Any]], Awaitable[None]],
        verify_key: Callable[[], Awaitable[bool]],
        on_devices: Callable[[bytes], None] | None = None,
        transport: Transport | None = None,
    ) -> None:
        """verify_key checks the device token over HTTP: False only when it is refused.

        on_devices gets the retained device list's raw payload; it parses it.
        """
        self.hass = hass
        self.entry = entry
        self.url = url
        self.account = account
        self._on_plan = on_plan
        self._on_command = on_command
        self._verify_key = verify_key
        self._on_devices = on_devices
        self._hub: str = entry.data[CONF_HUB_ID]
        self._transport: Transport = transport if transport is not None else PahoTransport(hass, self._hub)
        self._transport.on_connect = self._connected
        self._transport.on_disconnect = self._disconnected
        self._transport.on_message = self._message
        self._transport.on_subscribe = self._subscribed
        self._topics: list[str] = []  # the last subscribe's topics, for naming SUBACK failures
        self._stable_unsub: CALLBACK_TYPE | None = None
        self.live = False
        self._stopped = False
        self._auth_failed = False
        self._verifying = False  # a refused CONNECT's key check is running
        self._down_logged = False  # an outage is logged once, and its end once
        self._attempt = 0  # reconnects since the last good connect
        self._reconnect_unsub: CALLBACK_TYPE | None = None
        self._since = ""
        self._version = ""
        self._acks: OrderedDict[str, dict[str, Any] | None] = OrderedDict()  # id -> ack, None while running
        self._bad_plans: set[str] = set()
        self._listeners: list[Callable[[], None]] = []

    # -- topics ----------------------------------------------------------------

    @property
    def _status_topic(self) -> str:
        return f"homes/{self._hub}/status"

    @property
    def _ack_topic(self) -> str:
        return f"homes/{self._hub}/ack"

    @property
    def _issue_id(self) -> str:
        return f"{self.entry.entry_id}_insecure_broker"

    @property
    def command_ids(self) -> list[str]:
        """The last few command ids seen, oldest first, for diagnostics."""
        return list(self._acks)[-SHOWN_IDS:]

    def _status(self, state: str) -> bytes:
        return json.dumps({"v": 1, "state": state, "since": self._since, "version": self._version}).encode()

    # -- lifecycle ---------------------------------------------------------------

    async def async_start(self) -> None:
        if not url_allowed(self.url):
            _LOGGER.error("Refusing the insecure Halfhour broker %s: use mqtts:// or wss:// for a public host", self.url)
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                self._issue_id,
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key="insecure_broker",
                translation_placeholders={"url": self.url},
            )
            return
        ir.async_delete_issue(self.hass, DOMAIN, self._issue_id)
        self._version = str((await async_get_integration(self.hass, DOMAIN)).version)
        await self._async_connect()

    async def async_stop(self) -> None:
        """Say offline (retained) and close; no reconnects after this."""
        self._stopped = True
        self._cancel_reconnect()
        self._cancel_stable()
        if self.live:
            self._transport.publish(self._status_topic, self._status("offline"), QOS, True)
        await self.hass.async_add_executor_job(self._transport.disconnect)
        self._set_live(False)

    @callback
    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)
        return lambda: self._listeners.remove(cb)

    async def _async_connect(self) -> None:
        if self._stopped or self._auth_failed:
            return
        parts = urlsplit(self.url)
        scheme = parts.scheme
        host = parts.hostname or ""
        port = parts.port or _DEFAULT_PORTS[scheme]
        # since is shared by the Last Will and the online status of one connection.
        self._since = dt_util.utcnow().isoformat()
        try:
            await self.hass.async_add_executor_job(
                self._transport.connect,
                host,
                port,
                scheme in _SECURE,
                scheme in _WEBSOCKET,
                self._hub,
                self.entry.data[CONF_TOKEN],
                (self._status_topic, self._status("offline")),
                KEEPALIVE,
                parts.path or "/mqtt",
            )
        except OSError as err:
            _LOGGER.debug("Halfhour broker %s:%s unreachable: %s", host, port, err)
            self._note_down(f"can't reach Halfhour's broker {host}:{port}: {err}")
            self._schedule_reconnect()
            return
        if self._stopped:
            await self.hass.async_add_executor_job(self._transport.disconnect)

    # -- transport callbacks (on the HA loop) ------------------------------------

    @callback
    def _connected(self, code: int) -> None:
        if self._stopped or self._auth_failed or self._verifying:
            return
        if code in NOT_AUTHORISED:
            _LOGGER.debug("Halfhour's broker refused this home's key (code %s); checking it with Halfhour", code)
            self._verifying = True
            self._cancel_reconnect()
            self._cancel_stable()
            self._set_live(False)
            self.entry.async_create_background_task(self.hass, self._check_key(code), "halfhour check key")
            return
        if code != 0:
            _LOGGER.debug("Halfhour's broker refused the connection (code %s)", code)
            self._set_live(False)
            self._note_down(f"Halfhour's broker refused the connection (code {code})")
            self._schedule_reconnect()
            return
        if self._down_logged:
            self._down_logged = False
            _LOGGER.info("Halfhour's live connection is back")
        self._start_stable()
        self._transport.publish(self._status_topic, self._status("online"), QOS, True)
        topics = [(f"homes/{self._hub}/config", QOS), (f"homes/{self._hub}/cmd", QOS), (f"homes/{self._hub}/devices", QOS)]
        if self.account:
            topics.append((f"accounts/{self.account}/plan", QOS))
        self._topics = [t for t, _ in topics]
        self._transport.subscribe(topics)
        _LOGGER.debug("Connected to Halfhour's broker")
        self._set_live(True)

    async def _check_key(self, code: int) -> None:
        """A not-authorised CONNECT: a revoked key only if Halfhour itself refuses it."""
        try:
            ok = await self._verify_key()
        except Exception as err:  # noqa: BLE001 - can't tell, so not a revocation
            _LOGGER.debug("Couldn't check this home's key with Halfhour: %s", err)
            ok = True
        self._verifying = False
        if self._stopped:
            return
        if not ok:
            _LOGGER.warning("Halfhour refused this home's key (broker code %s); pair again to resume", code)
            self._auth_failed = True
            self._cancel_reconnect()
            self.entry.async_start_reauth(self.hass)
            return
        self._note_down(f"Halfhour's broker refused the connection (code {code}), but Halfhour didn't refuse the key")
        self._schedule_reconnect(OUTAGE_RETRY)

    @callback
    def _disconnected(self, code: int) -> None:
        if self._stopped or self._auth_failed or self._verifying:
            return
        _LOGGER.debug("Disconnected from Halfhour's broker (code %s)", code)
        self._cancel_stable()
        self._set_live(False)
        self._note_down("lost the connection")
        self._schedule_reconnect()

    @callback
    def _subscribed(self, codes: list[int]) -> None:
        for topic, code in zip(self._topics, codes, strict=False):
            if code >= 0x80:
                _LOGGER.warning("Halfhour's broker refused the subscription to %s (code %s)", topic, code)

    @callback
    def _message(self, topic: str, payload: bytes) -> None:
        if self._stopped:
            return
        if topic == f"homes/{self._hub}/cmd":
            self._command(payload)
        elif topic == f"homes/{self._hub}/config":
            self._config(payload)
        elif topic == f"homes/{self._hub}/devices":
            if self._on_devices is not None:
                self._on_devices(payload)
        elif self.account and topic == f"accounts/{self.account}/plan":
            self._plan(payload)

    # -- messages ---------------------------------------------------------------

    @callback
    def _plan(self, payload: bytes) -> None:
        try:
            plan = parse_plan(payload)
        except PlanError as err:
            key = _plan_id(payload)
            if key not in self._bad_plans:
                self._bad_plans.add(key)
                _LOGGER.warning("Ignoring a bad plan %s: %s", key, err)
            return
        self._on_plan(plan)

    @callback
    def _config(self, payload: bytes) -> None:
        try:
            config = json.loads(payload)
        except ValueError:
            _LOGGER.warning("Ignoring bad config from Halfhour")
            return
        self.entry.async_create_background_task(self.hass, self._run_config(config), "halfhour reload config")

    async def _run_config(self, config: dict[str, Any]) -> None:
        try:
            await self._on_command("reload_config", config)
        except Exception:  # noqa: BLE001 - a failed reload must not end the channel
            _LOGGER.exception("Reloading Halfhour's config failed")

    @callback
    def _command(self, payload: bytes) -> None:
        try:
            cmd = json.loads(payload)
        except ValueError:
            cmd = None
        cmd_id = cmd.get("id") if isinstance(cmd, dict) else None
        if not isinstance(cmd_id, str):
            _LOGGER.warning("Ignoring a command without an id")
            return
        if cmd_id in self._acks:
            ack = self._acks[cmd_id]
            if ack is not None:  # still running: its own ack follows
                self._ack(ack)
            return
        self._remember(cmd_id)
        assert isinstance(cmd, dict)
        name, args = cmd.get("name"), cmd.get("args", {})
        if not isinstance(name, str) or not isinstance(args, dict):
            self._finish(cmd_id, {"v": 1, "id": cmd_id, "ok": False, "error": "malformed command"})
            return
        self.entry.async_create_background_task(self.hass, self._run_command(cmd_id, name, args), f"halfhour command {name}")

    async def _run_command(self, cmd_id: str, name: str, args: dict[str, Any]) -> None:
        try:
            await self._on_command(name, args)
        except Exception as err:  # noqa: BLE001 - reported back in the ack
            _LOGGER.warning("Halfhour command %s failed: %s", name, err)
            self._finish(cmd_id, {"v": 1, "id": cmd_id, "ok": False, "error": str(err)})
        else:
            self._finish(cmd_id, {"v": 1, "id": cmd_id, "ok": True})

    @callback
    def _remember(self, cmd_id: str) -> None:
        self._acks[cmd_id] = None
        while len(self._acks) > SEEN_IDS:
            self._acks.popitem(last=False)

    @callback
    def _finish(self, cmd_id: str, ack: dict[str, Any]) -> None:
        if cmd_id in self._acks:
            self._acks[cmd_id] = ack
        self._ack(ack)

    @callback
    def _ack(self, ack: dict[str, Any]) -> None:
        self._transport.publish(self._ack_topic, json.dumps(ack).encode(), QOS, False)

    # -- state and timers ----------------------------------------------------------

    @callback
    def _set_live(self, live: bool) -> None:
        if live == self.live:
            return
        self.live = live
        for cb in list(self._listeners):
            cb()

    @callback
    def _note_down(self, why: str) -> None:
        """Log an outage once at info; each retry after it only at debug."""
        if not self._down_logged:
            self._down_logged = True
            _LOGGER.info("Lost Halfhour's live connection (%s); retrying in the background", why)

    @callback
    def _schedule_reconnect(self, floor: float = 0) -> None:
        if self._stopped or self._auth_failed or self._reconnect_unsub is not None:
            return
        delay = max(floor, min(RECONNECT_MAX, 2**self._attempt) * random.uniform(*JITTER))  # noqa: S311 - jitter, not crypto
        self._attempt = min(self._attempt + 1, 16)

        async def _reconnect(_now: datetime) -> None:
            self._reconnect_unsub = None
            await self._async_connect()

        self._reconnect_unsub = async_call_later(self.hass, delay, _reconnect)

    @callback
    def _start_stable(self) -> None:
        """Reset the backoff only once a connection has stayed up: accept-then-drop keeps backing off."""
        self._cancel_stable()

        @callback
        def _stable(_now: datetime) -> None:
            self._stable_unsub = None
            self._attempt = 0

        self._stable_unsub = async_call_later(self.hass, STABLE_AFTER, _stable)

    @callback
    def _cancel_stable(self) -> None:
        if self._stable_unsub is not None:
            self._stable_unsub()
            self._stable_unsub = None

    @callback
    def _cancel_reconnect(self) -> None:
        if self._reconnect_unsub is not None:
            self._reconnect_unsub()
            self._reconnect_unsub = None


def _plan_id(payload: bytes) -> str:
    """A bad plan's id when it has one, for logging it once."""
    try:
        data = json.loads(payload)
    except ValueError:
        return "(unreadable)"
    plan_id = data.get("id") if isinstance(data, dict) else None
    return plan_id if isinstance(plan_id, str) else "(no id)"
