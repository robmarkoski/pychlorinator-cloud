"""Coordinator for the AstralPool Halo Cloud integration."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CALLBACK_TYPE, CoreState, HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from pychlorinator_cloud.exceptions import (
    SignallingAuthenticationError,
    SignallingBusyError,
    SignallingUnavailableError,
)
from pychlorinator_cloud.websocket_client import ChlorinatorLiveData, HaloWebSocketClient

from .const import CONF_PASSWORD, CONF_SERIAL_NUMBER, CONF_USERNAME, DOMAIN

_LOGGER = logging.getLogger(__name__)

_EXPECTED_RECONNECT_BACKOFF_START = 60
_UNEXPECTED_RECONNECT_BACKOFF_START = 30
_RECONNECT_BACKOFF_MAX = 900
_JITTER_MAX_SECONDS = 15


class HaloCloudCoordinator(DataUpdateCoordinator[ChlorinatorLiveData]):
    """Manage a persistent Halo cloud WebSocket connection."""

    def __init__(self, hass: HomeAssistant, entry) -> None:
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
        self._entry = entry
        self._shutdown_event = asyncio.Event()
        self._connection_task: asyncio.Task | None = None
        self._startup_task: asyncio.Task | None = None
        self._started_listener: CALLBACK_TYPE | None = None
        self._connect_lock = asyncio.Lock()
        self._last_connection_issue: str | None = None
        self.client.on_data = self._handle_client_data
        self.client.on_disconnect = self._handle_client_disconnect
        self.data = self.client.data

    @callback
    def _handle_client_data(self, _: dict[str, Any]) -> None:
        """Push fresh WebSocket data into Home Assistant."""
        try:
            self.async_set_updated_data(self.client.data)
        except Exception:
            _LOGGER.exception("Error pushing data update to Home Assistant")

    @callback
    def _handle_client_disconnect(self) -> None:
        """Handle disconnects without pushing stale updates back into HA."""
        try:
            _LOGGER.info("Cloud WebSocket disconnected")
        except Exception:
            _LOGGER.exception("Error handling WebSocket disconnect")

    @callback
    def async_schedule_start(self) -> None:
        """Schedule cloud startup only after Home Assistant is fully running."""
        if self._shutdown_event.is_set():
            return

        if self.hass.is_running or self.hass.state is CoreState.running:
            self._async_schedule_background_start()
            return

        if self._started_listener is not None:
            return

        @callback
        def _handle_hass_started(_: Any) -> None:
            self._started_listener = None
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
        await self._ensure_connection_task()

    async def _ensure_connection_task(self) -> None:
        """Start the background connection manager if needed."""
        if self._connection_task is None or self._connection_task.done():
            self._shutdown_event.clear()
            self._connection_task = self.hass.async_create_task(
                self._connection_manager()
            )

    def _compute_backoff(self, backoff: int) -> float:
        """Return a reconnect delay with small jitter."""
        jitter_ceiling = min(_JITTER_MAX_SECONDS, max(1, int(backoff * 0.1)))
        return min(
            _RECONNECT_BACKOFF_MAX,
            backoff + random.uniform(0, jitter_ceiling),
        )

    async def _wait_for_retry(self, delay: float) -> None:
        """Sleep until the next retry, unless shutdown is requested."""
        try:
            await asyncio.wait_for(self._shutdown_event.wait(), timeout=delay)
        except asyncio.TimeoutError:
            return

    def _log_connection_issue(self, issue_key: str, level: int, message: str, *args: Any) -> None:
        """Log a connection issue, suppressing consecutive duplicates."""
        if self._last_connection_issue == issue_key:
            return
        self._last_connection_issue = issue_key
        _LOGGER.log(level, message, *args)

    async def _connection_manager(self) -> None:
        """Maintain a persistent connection and reconnect with backoff."""
        backoff = _EXPECTED_RECONNECT_BACKOFF_START

        while not self._shutdown_event.is_set():
            if self.client.data.connected:
                await self._wait_for_retry(5)
                continue

            async with self._connect_lock:
                if self._shutdown_event.is_set() or self.client.data.connected:
                    continue

                try:
                    await self.client.connect()
                    self._last_connection_issue = None
                    backoff = _EXPECTED_RECONNECT_BACKOFF_START
                    _LOGGER.info("Chlorinator cloud connected")
                except asyncio.CancelledError:
                    raise
                except (SignallingBusyError, SignallingUnavailableError) as err:
                    delay = self._compute_backoff(backoff)
                    issue_key = f"expected:{type(err).__name__}:{err}"
                    self._log_connection_issue(
                        issue_key,
                        logging.INFO,
                        "Cloud connection unavailable/busy (%s); retrying in %.0fs",
                        err,
                        delay,
                    )
                    await self._wait_for_retry(delay)
                    backoff = min(backoff * 2, _RECONNECT_BACKOFF_MAX)
                except SignallingAuthenticationError as err:
                    delay = self._compute_backoff(_RECONNECT_BACKOFF_MAX)
                    issue_key = f"auth:{err}"
                    self._log_connection_issue(
                        issue_key,
                        logging.WARNING,
                        "Cloud authentication failed (%s); retrying in %.0fs",
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
                        "Unexpected cloud connection failure: %s; retrying in %.0fs",
                        err,
                        delay,
                    )
                    await self._wait_for_retry(delay)
                    backoff = min(max(backoff, _UNEXPECTED_RECONNECT_BACKOFF_START) * 2, _RECONNECT_BACKOFF_MAX)

    async def _async_update_data(self) -> ChlorinatorLiveData:
        """Ensure the persistent connection manager is running."""
        await self._ensure_connection_task()
        return self.client.data

    async def async_shutdown(self) -> None:
        """Disconnect the WebSocket client and stop reconnect attempts."""
        self._shutdown_event.set()

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

        if self._connection_task is not None:
            self._connection_task.cancel()
            try:
                await self._connection_task
            except (asyncio.CancelledError, Exception):
                pass
            self._connection_task = None

        try:
            await self.client.disconnect()
        except Exception:
            _LOGGER.exception("Error shutting down cloud client")
