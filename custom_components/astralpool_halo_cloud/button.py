"""Button platform for the AstralPool Halo Cloud integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HaloCloudCoordinator
from .entity import HaloCloudEntity


@dataclass(frozen=True, kw_only=True)
class HaloButtonEntityDescription(ButtonEntityDescription):
    """Describes a Halo Cloud button."""

    press_fn: Callable[[Any], Awaitable[None]]


BUTTON_DESCRIPTIONS: tuple[HaloButtonEntityDescription, ...] = (
    HaloButtonEntityDescription(
        key="sync_controller_time",
        name="Sync Controller Time",
        icon="mdi:clock-sync",
        press_fn=lambda client: client.sync_controller_clock(),
    ),
    HaloButtonEntityDescription(
        key="heater_setpoint_up",
        name="Heater Setpoint Up",
        icon="mdi:thermometer-plus",
        press_fn=lambda client: client.increase_heater_setpoint(),
    ),
    HaloButtonEntityDescription(
        key="heater_setpoint_down",
        name="Heater Setpoint Down",
        icon="mdi:thermometer-minus",
        press_fn=lambda client: client.decrease_heater_setpoint(),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AstralPool Halo Cloud buttons."""
    coordinator: HaloCloudCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(HaloCloudButton(coordinator, description) for description in BUTTON_DESCRIPTIONS)


class HaloCloudButton(HaloCloudEntity, ButtonEntity):
    """Representation of a Halo Cloud button."""

    entity_description: HaloButtonEntityDescription

    @property
    def available(self) -> bool:
        """Only allow control while the cloud session is actively connected."""
        return super().available and self.coordinator.client.data.connected

    async def async_press(self) -> None:
        """Handle the button press."""
        client = self.coordinator.client
        if not client.data.connected:
            raise HomeAssistantError("Chlorinator cloud is not connected")
        await self.entity_description.press_fn(client)
        self.coordinator.async_set_updated_data(client.data)
