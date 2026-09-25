"""Coordinator for the AstralPool Halo Cloud integration."""

from __future__ import annotations

import asyncio
import datetime
import enum
import logging
import random
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .pychlorinator_cloud.exceptions import (
    SignallingAuthenticationError,
    SignallingBusyError,
    SignallingUnavailableError,
)
from .pychlorinator_cloud.websocket_client import (
    ChlorinatorLiveData,
    HaloWebSocketClient,
    MEASUREMENTS_CMD_ID,
)

from .const import (
    CONF_CONNECTION_PAUSE_MINUTES,
    CONF_PASSWORD,
    CONF_POST_PAIR_CLOUD_SETTLE_UNTIL,
    CONF_SERIAL_NUMBER,
    CONF_USERNAME,
    DOMAIN,
)

# Default cloud pause duration when CONF_CONNECTION_PAUSE_MINUTES is unset.
_DEFAULT_CONNECTION_PAUSE_MINUTES = 15

_LOGGER = logging.getLogger(__name__)

_EXPECTED_RECONNECT_BACKOFF_START = 60
_POST_DISCONNECT_RECONNECT_BACKOFF_START = 5
_POST_DISCONNECT_RECONNECT_BACKOFF_MAX = 60
_UNEXPECTED_RECONNECT_BACKOFF_START = 30
_RECONNECT_BACKOFF_MAX = 900
_JITTER_MAX_SECONDS = 15
_SHORT_SESSION_SECONDS = 30
_SHORT_SESSION_LIMIT = 3
_SHORT_SESSION_WINDOW_SECONDS = 300
_SHORT_SESSION_CIRCUIT_BREAKER_BACKOFF = 300

# Escalating reconnect cooldown after server-initiated disconnects.
#
# Evidence (2026-05-26 A/B soak, both run orders): the AstralPool cloud relay
# holds a per-credential / per-IP penalty state. A 5s reconnect after every
# kick keeps us pinned in that penalty tier — sessions stay short and the
# relay starts refusing connects (chlorinator_unavailable). A fresh connection
# into a clean/cooled-down slot instead held 960s, 985s and 1402s across runs
# (and a vendor capture held 1055s); session lengths are bimodal — either
# <~12s (penalty spiral) or >~900s (clean slot), nothing between. The 2s-vs-4s
# poll cadence had no effect on session length, so the lever is churn, not
# cadence. Therefore: a one-off kick after a healthy session recovers fast,
# but repeated kicks escalate the cooldown toward a long quiet window that
# lets the relay clear the penalty, then reset once a healthy session lands.
#
# Ladder is indexed by consecutive non-healthy disconnects (0-based). The 900s
# top rung is the proven quiet gap; the lower rungs (whether a shorter recovery
# gap clears the penalty just as well, to minimise downtime) remain to be
# calibrated by a follow-up soak.
_HEALTHY_SESSION_SECONDS = 240
_DISCONNECT_COOLDOWN_LADDER = (15, 60, 180, 600, 900)


class ConnectionState(enum.Enum):
    """Explicit coordinator connection states."""

    IDLE = "idle"
    CONNECTING = "connecting"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    PAUSED = "paused"
    SHUTTING_DOWN = "shutting_down"


class HaloCloudCoordinator(DataUpdateCoordinator[ChlorinatorLiveData]):
    """Manage a persistent Halo cloud WebSocket connection."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialise the coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,
        )
        self.client = HaloWebSocketClient(
            serial_number=entry.data[CONF_SERIAL_NUMBER],
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
        )
        # Diagnostic flags remain available via environment variables in the
        # client, but the default coordinator path should stay near-release.
        self._entry = entry
        self._shutdown_event = asyncio.Event()
        self._connection_task: asyncio.Task | None = None
        self._startup_task: asyncio.Task | None = None
        self._started_listener: CALLBACK_TYPE | None = None
        self._connect_lock = asyncio.Lock()
        self._wake_event = asyncio.Event()
        self._last_connection_issue: str | None = None
        self._pause_until: datetime.datetime | None = None
        self._pending_publish: asyncio.TimerHandle | None = None
        self._reconnect_backoff_override: int | None = None
        self._next_connect_at: datetime.datetime | None = None
        self._short_session_disconnects: list[datetime.datetime] = []
        self._consecutive_disconnects: int = 0
        self._connection_state: ConnectionState = ConnectionState.IDLE
        # Lazy import keeps the coordinator module importable without HA storage
        # stubs (the shape-check tests import this module without HA installed).
        from .acid import AcidReservoirTracker

        self.acid = AcidReservoirTracker(hass, entry.entry_id)
        self._acid_last_secs: int | None = None
        self._acid_poll_unsub: CALLBACK_TYPE | None = None
        self.client.on_data = self._handle_client_data
        self.client.on_disconnect = self._handle_client_disconnect
        self.data = self.client.data
        self._apply_post_pair_settle_delay()

    async def async_load_persistent_state(self) -> None:
        """Load HA-side persistent state (acid reservoir). Called during setup."""
        await self.acid.async_load()

    @callback
    def _maybe_ingest_acid(self) -> None:
        """Feed a fresh acid-dosing reading into the reservoir tracker."""
        dosing_today = self.client.data.acid_dosing_seconds_today

        if dosing_today is None or dosing_today == self._acid_last_secs:
            return

        firmware = self.client.data.firmware_version
        pump = self.client.data.acid_pump_size_ml_per_min

        self._acid_last_secs = dosing_today
    
        self.hass.async_create_task(
            self.acid.async_ingest(
                dosing_today,
                pump,
                firmware,
            )
        )

    @callback
    def _cancel_pending_publish(self) -> None:
        """Cancel any scheduled publish callback."""
        if self._pending_publish is None:
            return
        self._pending_publish.cancel()
        self._pending_publish = None

    @callback
    def _async_flush_client_data(self) -> None:
        """Push the latest coalesced WebSocket data into Home Assistant."""
        self._pending_publish = None
        try:
            self.async_set_updated_data(self.client.data)
        except Exception:
            _LOGGER.exception("Error pushing data update to Home Assistant")

    @callback
    def _handle_client_data(self, _: dict[str, Any]) -> None:
        """Coalesce packet bursts before publishing into Home Assistant."""
        self._maybe_ingest_acid()
        if self._pending_publish is not None:
            return
        self._pending_publish = self.hass.loop.call_later(0.2, self._async_flush_client_data)

    @callback
    def _short_session_reconnect_delay(self) -> int | None:
        """Return a circuit-breaker delay after repeated short-lived sessions."""
        duration = self.client.last_session_duration_seconds
        if duration is None or duration >= _SHORT_SESSION_SECONDS:
            self._short_session_disconnects.clear()
            return None

        now = datetime.datetime.now(datetime.timezone.utc)
        cutoff = now - datetime.timedelta(seconds=_SHORT_SESSION_WINDOW_SECONDS)
        self._short_session_disconnects = [
            seen_at for seen_at in self._short_session_disconnects if seen_at >= cutoff
        ]
        self._short_session_disconnects.append(now)

        if len(self._short_session_disconnects) >= _SHORT_SESSION_LIMIT:
            return _SHORT_SESSION_CIRCUIT_BREAKER_BACKOFF
        return None

    @callback
    def _post_disconnect_cooldown(self) -> int:
        """Return the reconnect cooldown (s) after a server disconnect.

        Escalates with consecutive non-healthy disconnects so a repeated-kick
        spiral backs off toward a long quiet window (letting the cloud relay
        clear its penalty state), and resets after a healthy long-lived
        session so a one-off kick still recovers quickly. See
        ``_DISCONNECT_COOLDOWN_LADDER`` for the rationale and evidence.
        """
        duration = self.client.last_session_duration_seconds
        if duration is not None and duration >= _HEALTHY_SESSION_SECONDS:
            self._consecutive_disconnects = 0
        else:
            self._consecutive_disconnects = min(
                self._consecutive_disconnects + 1,
                len(_DISCONNECT_COOLDOWN_LADDER) - 1,
            )
        return _DISCONNECT_COOLDOWN_LADDER[self._consecutive_disconnects]

    @callback
    def _notify_cloud_unstable(self) -> None:
        """Surface a persistent notification when the cloud relay is misbehaving.

        Triggered when the coordinator's short-lived-session circuit breaker
        fires: ≥3 server-initiated disconnects within 300s. This usually
        means the AstralPool cloud relay is degraded/unreachable for the
        controller (not a problem with this integration). Confirmed pattern
        2026-05-24: the official AstralPool / Halo Chlor mobile app also
        could not maintain a cloud session during a relay outage; only BLE
        worked. The integration keeps retrying with backoff in the
        background.
        """
        try:
            serial = (
                self._entry.data.get(CONF_SERIAL_NUMBER)
                or self._entry.unique_id
                or "unknown"
            )
            persistent_notification.async_create(
                self.hass,
                title="AstralPool Halo Cloud unreachable",
                message=(
                    "Home Assistant cannot maintain a connection to your "
                    f"AstralPool Halo chlorinator (serial {serial}) via the "
                    "AstralPool cloud relay. The integration has paused "
                    "reconnect attempts for 5 minutes and will keep retrying "
                    "automatically.\n\n"
                    "This is usually a problem with the AstralPool cloud "
                    "service itself, not with Home Assistant. To confirm: "
                    "open the official **AstralPool Halo Chlor** mobile app "
                    "and try connecting from there. If the official app also "
                    "can't maintain a connection (or only Bluetooth works), "
                    "the AstralPool cloud relay is down or your controller "
                    "is in a per-account/per-controller rate-limit window. "
                    "Wait it out — the integration will reconnect on its own "
                    "once the cloud relay recovers.\n\n"
                    "If the official app DOES work and Home Assistant doesn't, "
                    "reload this integration entry from Settings › Devices & "
                    "services."
                ),
                notification_id=f"astralpool_halo_cloud_unreachable_{serial}",
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to create cloud-unreachable notification: %s", err)

    @callback
    def _dismiss_cloud_unstable_notification(self) -> None:
        """Clear the cloud-unreachable notification on a healthy connect."""
        try:
            serial = (
                self._entry.data.get(CONF_SERIAL_NUMBER)
                or self._entry.unique_id
                or "unknown"
            )
            persistent_notification.async_dismiss(
                self.hass,
                notification_id=f"astralpool_halo_cloud_unreachable_{serial}",
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to dismiss cloud-unreachable notification: %s", err)

    @callback
    def _schedule_next_connect(self, delay_seconds: int) -> None:
        """Schedule the earliest next connection attempt."""
        self._next_connect_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            seconds=max(0, int(delay_seconds))
        )

    @callback
    def _apply_post_pair_settle_delay(self) -> None:
        """Delay the first cloud connect for newly BLE-paired entries."""
        raw_until = self._entry.data.get(CONF_POST_PAIR_CLOUD_SETTLE_UNTIL)
        if not raw_until:
            return

        try:
            settle_until = datetime.datetime.fromisoformat(str(raw_until))
        except ValueError:
            _LOGGER.debug("Ignoring malformed post-pair cloud settle timestamp: %r", raw_until)
            return

        if settle_until.tzinfo is None:
            settle_until = settle_until.replace(tzinfo=datetime.timezone.utc)

        now = datetime.datetime.now(datetime.timezone.utc)
        if settle_until <= now:
            return

        self._next_connect_at = settle_until
        delay = max(1, int((settle_until - now).total_seconds()))
        _LOGGER.info(
            "BLE pairing completed recently; delaying initial Halo cloud connect for %ss "
            "while the controller releases its pairing session",
            delay,
        )

    @callback
    def _handle_client_disconnect(self) -> None:
        """Handle disconnects without pushing stale updates back into HA."""
        try:
            self._cancel_pending_publish()
            if self._shutdown_event.is_set():
                _LOGGER.info("Cloud WebSocket disconnected during shutdown, not scheduling reconnect")
                self.async_set_updated_data(self.client.data)
                return

            # If the disconnect is the result of a user-requested Connection
            # Hold pause, do not log/schedule a reconnect — the pause guard
            # will keep us idle until resume. This avoids the cosmetic
            # "scheduling quiet reconnect" line during pause activation.
            if self.is_connection_paused:
                self._connection_state = ConnectionState.PAUSED
                self.async_set_updated_data(self.client.data)
                return

            self._connection_state = ConnectionState.DISCONNECTED
            self.client._quiet_reconnect = True
            # Escalating cooldown: a one-off kick recovers fast, repeated kicks
            # back off toward a long quiet window so the relay clears its
            # per-credential/per-IP penalty state instead of us churning.
            reconnect_delay = self._post_disconnect_cooldown()
            # The short-session circuit breaker can still raise the delay
            # further on a fast sub-30s spiral.
            circuit_delay = self._short_session_reconnect_delay()
            if circuit_delay is not None:
                reconnect_delay = max(reconnect_delay, circuit_delay)
            if reconnect_delay >= _SHORT_SESSION_CIRCUIT_BREAKER_BACKOFF:
                _LOGGER.warning(
                    "Cloud WebSocket repeatedly disconnected (%s consecutive, "
                    "%s short-lived within %ss); holding reconnect for %ss to let "
                    "the cloud relay clear its penalty state",
                    self._consecutive_disconnects,
                    len(self._short_session_disconnects),
                    _SHORT_SESSION_WINDOW_SECONDS,
                    reconnect_delay,
                )
                self._notify_cloud_unstable()
            else:
                _LOGGER.info(
                    "Cloud WebSocket disconnected, scheduling quiet reconnect in %ss",
                    reconnect_delay,
                )

            self._schedule_next_connect(reconnect_delay)
            self._reconnect_backoff_override = reconnect_delay
            self._wake_event.set()
            self.async_set_updated_data(self.client.data)
        except Exception:
            _LOGGER.exception("Error handling WebSocket disconnect")

    @property
    def connection_state(self) -> ConnectionState:
        """Return the current coordinator connection state."""
        return self._connection_state

    @property
    def connection_state_label(self) -> str:
        """Return a human-readable connection state label."""
        return self._connection_state.value

    @property
    def default_pause_minutes(self) -> int:
        """Return the configured default cloud-pause duration in minutes.

        Public accessor used by the Pause Cloud Connection button so it can
        read the user's preferred duration without poking into the config
        entry's options dict directly.
        """
        return int(
            self._entry.options.get(
                CONF_CONNECTION_PAUSE_MINUTES, _DEFAULT_CONNECTION_PAUSE_MINUTES
            )
        )

    @property
    def is_connection_paused(self) -> bool:
        """Return whether reconnect attempts are intentionally paused."""
        return self.connection_paused_until is not None

    @property
    def connection_paused_until(self) -> datetime.datetime | None:
        """Return when reconnect attempts will resume, if paused."""
        if self._pause_until is None:
            return None
        now = datetime.datetime.now(datetime.timezone.utc)
        if now >= self._pause_until:
            return None
        return self._pause_until

    @callback
    def _clear_expired_pause(self) -> bool:
        """Clear an expired pause window and report whether it changed."""
        if self._pause_until is None:
            return False
        now = datetime.datetime.now(datetime.timezone.utc)
        if now < self._pause_until:
            return False
        self._pause_until = None
        return True

    async def async_pause_connection(self, minutes: int) -> None:
        """Pause reconnect attempts and release the controller slot.

        minutes=0 means indefinite pause, staying paused until
        async_resume_connection() is called explicitly.
        """
        if minutes <= 0:
            self._pause_until = datetime.datetime(
                9999, 12, 31, tzinfo=datetime.timezone.utc
            )
            _LOGGER.info("Pausing Halo cloud connection indefinitely (until explicit resume)")
        else:
            bounded_minutes = max(1, int(minutes))
            self._pause_until = datetime.datetime.now(
                datetime.timezone.utc
            ) + datetime.timedelta(minutes=bounded_minutes)
            _LOGGER.info("Pausing Halo cloud connection for %s minute(s)", bounded_minutes)
        self._wake_event.set()
        await self.client.disconnect()
        self.async_set_updated_data(self.client.data)

    async def async_resume_connection(self) -> None:
        """Resume reconnect attempts immediately."""
        _LOGGER.info("Resuming Halo cloud connection")
        self._pause_until = None
        self._next_connect_at = None
        self._wake_event.set()
        self.async_set_updated_data(self.client.data)
        await self._ensure_connection_task()

    @callback
    def async_schedule_start(self) -> None:
        """Schedule cloud startup only after Home Assistant is fully running."""
        if self._shutdown_event.is_set():
            return

        if self.hass.is_running or self.hass.state is CoreState.running:
            _LOGGER.info("Home Assistant already running, scheduling Halo cloud startup now")
            self._async_schedule_background_start()
            return

        if self._started_listener is not None:
            return

        _LOGGER.info("Deferring Halo cloud startup until Home Assistant has fully started")

        @callback
        def _handle_hass_started(_: Any) -> None:
            self._started_listener = None
            _LOGGER.info("Home Assistant started, scheduling Halo cloud startup")
            self._async_schedule_background_start()

        self._started_listener = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STARTED,
            _handle_hass_started,
        )

    @callback
    def _async_schedule_background_start(self) -> None:
        """Start the coordinator bootstrap in the background if needed."""
        if self._shutdown_event.is_set():
            return
        if self._startup_task is not None and not self._startup_task.done():
            return
        self._startup_task = self.hass.async_create_task(self._async_start_background())

    async def _async_start_background(self) -> None:
        """Start the background connection manager outside setup/bootstrap."""
        try:
            await self.async_start()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("Error starting Halo cloud connection manager")

    async def async_start(self) -> None:
        """Start the background connection manager without waiting for live data."""
        _LOGGER.info("Starting Halo cloud coordinator")
        self._ensure_acid_poll()
        await self._ensure_connection_task()

    @callback
    def _ensure_acid_poll(self) -> None:
        """Periodically re-read 0x0259 so acid-dosing-today stays fresh.

        The startup/quiet-reconnect refresh reads 0x0259 at most once per session
        (and skips it once state settles), so the acid reservoir source would
        otherwise be sampled ~once a day and under-count everything dosed before
        midnight. A single read every 10 min while connected is negligible next
        to the 4s keepalive+0x0002 cadence and keeps the estimate accurate.
        """
        if self._acid_poll_unsub is not None:
            return
        self._acid_poll_unsub = async_track_time_interval(
            self.hass, self._async_poll_acid, datetime.timedelta(minutes=10)
        )

    async def _async_poll_acid(self, _now: datetime.datetime) -> None:
        if not self.client.data.connected:
            return
        try:
            await self.client.request_data(MEASUREMENTS_CMD_ID, source="acid_poll")
        except Exception:  # noqa: BLE001
            _LOGGER.debug("Acid-poll 0x0259 read failed", exc_info=True)

    async def _ensure_connection_task(self) -> None:
        """Start the background connection manager if needed."""
        if self._connection_task is None or self._connection_task.done():
            self._shutdown_event.clear()
            self._wake_event.clear()
            _LOGGER.info("Creating Halo cloud connection task")
            self._connection_task = self.hass.async_create_task(
                self._connection_manager()
            )
        else:
            _LOGGER.debug("Halo cloud connection task already running")
            self._wake_event.set()

    def _compute_backoff(self, backoff: int) -> float:
        """Return a reconnect delay with small jitter."""
        jitter_ceiling = min(_JITTER_MAX_SECONDS, max(1, int(backoff * 0.1)))
        return min(
            _RECONNECT_BACKOFF_MAX,
            backoff + random.uniform(0, jitter_ceiling),
        )

    async def _wait_for_retry(self, delay: float) -> None:
        """Sleep until the next retry, unless shutdown or wake is requested."""
        shutdown_wait = asyncio.create_task(self._shutdown_event.wait())
        wake_wait = asyncio.create_task(self._wake_event.wait())
        try:
            done, pending = await asyncio.wait(
                {shutdown_wait, wake_wait},
                timeout=delay,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if wake_wait in done:
                self._wake_event.clear()
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        finally:
            for task in (shutdown_wait, wake_wait):
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

    def _log_connection_issue(self, issue_key: str, level: int, message: str, *args: Any) -> None:
        """Log a connection issue, suppressing consecutive duplicates."""
        if self._last_connection_issue == issue_key:
            return
        self._last_connection_issue = issue_key
        _LOGGER.log(level, message, *args)

    async def _connection_manager(self) -> None:
        """Maintain a persistent connection and reconnect with backoff."""
        _LOGGER.info("Halo cloud connection manager started")
        backoff = _EXPECTED_RECONNECT_BACKOFF_START

        while not self._shutdown_event.is_set():
            if self._clear_expired_pause():
                self.async_set_updated_data(self.client.data)

            pause_until = self.connection_paused_until
            if pause_until is not None:
                self._connection_state = ConnectionState.PAUSED
                delay = min(
                    5,
                    max(
                        1,
                        (pause_until - datetime.datetime.now(datetime.timezone.utc)).total_seconds(),
                    ),
                )
                await self._wait_for_retry(delay)
                continue

            if self.client.data.connected:
                await self._wait_for_retry(5)
                continue

            next_connect_at = self._next_connect_at
            if next_connect_at is not None:
                now = datetime.datetime.now(datetime.timezone.utc)
                if now < next_connect_at:
                    await self._wait_for_retry(
                        max(1, (next_connect_at - now).total_seconds())
                    )
                    continue
                self._next_connect_at = None

            async with self._connect_lock:
                if self._shutdown_event.is_set() or self.client.data.connected:
                    continue

                try:
                    _LOGGER.info("Attempting Halo cloud connect")
                    self._connection_state = ConnectionState.CONNECTING
                    await self.client.connect()
                    self._connection_state = ConnectionState.CONNECTED
                    self._last_connection_issue = None
                    self._next_connect_at = None
                    backoff = _EXPECTED_RECONNECT_BACKOFF_START
                    _LOGGER.info("Chlorinator cloud connected")
                    # Clear the cloud-unreachable notification (if any).
                    # We only dismiss on a real connect; a session may still
                    # be short-lived, in which case the disconnect handler
                    # will re-fire the notification on next circuit trip.
                    self._dismiss_cloud_unstable_notification()
                except asyncio.CancelledError:
                    raise
                except (SignallingBusyError, SignallingUnavailableError) as err:
                    backoff_seed = self._reconnect_backoff_override or backoff
                    self._reconnect_backoff_override = None
                    quiet_reconnect_recovery = self.client._quiet_reconnect
                    backoff_cap = (
                        _POST_DISCONNECT_RECONNECT_BACKOFF_MAX
                        if quiet_reconnect_recovery
                        else _RECONNECT_BACKOFF_MAX
                    )
                    effective_seed = min(backoff_seed, backoff_cap)
                    delay = self._compute_backoff(effective_seed)
                    issue_key = (
                        f"expected:{type(err).__name__}:{err}:seed={effective_seed}:"
                        f"quiet={quiet_reconnect_recovery}"
                    )
                    if quiet_reconnect_recovery:
                        retry_message = (
                            "Cloud connection deferred (%s: %s); keeping quiet reconnect "
                            "retries capped, retrying in %.0fs"
                        )
                    else:
                        retry_message = "Cloud connection deferred (%s: %s); retrying in %.0fs"
                    self._log_connection_issue(
                        issue_key,
                        logging.INFO,
                        retry_message,
                        type(err).__name__,
                        err,
                        delay,
                    )
                    await self._wait_for_retry(delay)
                    backoff = min(effective_seed * 2, backoff_cap)
                except SignallingAuthenticationError as err:
                    delay = self._compute_backoff(_RECONNECT_BACKOFF_MAX)
                    issue_key = f"auth:{err}"
                    self._log_connection_issue(
                        issue_key,
                        logging.WARNING,
                        "Cloud authentication failed (%s: %s); retrying in %.0fs",
                        type(err).__name__,
                        err,
                        delay,
                    )
                    await self._wait_for_retry(delay)
                except Exception as err:
                    delay = self._compute_backoff(backoff)
                    issue_key = f"unexpected:{type(err).__name__}:{err}"
                    self._log_connection_issue(
                        issue_key,
                        logging.WARNING,
                        "Unexpected cloud connection failure (%s): %s; retrying in %.0fs",
                        type(err).__name__,
                        err,
                        delay,
                    )
                    _LOGGER.debug("Unexpected Halo cloud connection traceback", exc_info=True)
                    await self._wait_for_retry(delay)
                    backoff = min(max(backoff, _UNEXPECTED_RECONNECT_BACKOFF_START) * 2, _RECONNECT_BACKOFF_MAX)

        if self._connection_state not in (ConnectionState.SHUTTING_DOWN,):
            self._connection_state = ConnectionState.IDLE
        _LOGGER.info("Halo cloud connection manager stopped")

    async def _async_update_data(self) -> ChlorinatorLiveData:
        """Ensure the persistent connection manager is running."""
        await self._ensure_connection_task()
        return self.client.data

    async def async_shutdown(self) -> None:
        """Disconnect the WebSocket client and stop reconnect attempts."""
        _LOGGER.info("Shutting down Halo cloud coordinator")
        self._connection_state = ConnectionState.SHUTTING_DOWN
        if self._acid_poll_unsub is not None:
            self._acid_poll_unsub()
            self._acid_poll_unsub = None
        self._shutdown_event.set()
        self._wake_event.set()

        self._cancel_pending_publish()

        if self._started_listener is not None:
            self._started_listener()
            self._started_listener = None

        if self._startup_task is not None:
            self._startup_task.cancel()
            try:
                await self._startup_task
            except (asyncio.CancelledError, Exception):
                pass
            self._startup_task = None

        try:
            await self.client.disconnect()
        except Exception:
            _LOGGER.exception("Error shutting down cloud client")

        if self._connection_task is not None:
            self._connection_task.cancel()
            try:
                await self._connection_task
            except (asyncio.CancelledError, Exception):
                pass
            self._connection_task = None
