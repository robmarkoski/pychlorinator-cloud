"""Select platform for the AstralPool Halo Cloud integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HaloCloudCoordinator
from .entity import HaloCloudEntity


@dataclass(frozen=True, kw_only=True)
class HaloSelectEntityDescription(SelectEntityDescription):
    """Describes a Halo Cloud select entity."""

    value_fn: Callable[[Any], str | None] | None = None
    command_fn: Callable[[Any, str], Awaitable[None]] | None = None


MODE_SELECT_DESCRIPTION = HaloSelectEntityDescription(
    key="mode_select",
    name="System Mode",
)

PUMP_SPEED_SELECT_DESCRIPTION = HaloSelectEntityDescription(
    key="pump_speed_select",
    name="Pump Speed Control",
)

LIGHT_SELECT_DESCRIPTION = HaloSelectEntityDescription(
    key="light_mode_select",
    name="Light Mode",
    options=["Off", "On", "Auto"],
    entity_registry_enabled_default=False,
    value_fn=lambda data: data.light_mode,
    command_fn=lambda client, option: client.set_light_mode(option),
)

BLADE_SELECT_DESCRIPTION = HaloSelectEntityDescription(
    key="blade_mode_select",
    name="Blade Mode",
    options=["Off", "Auto", "On"],
    entity_registry_enabled_default=False,
    value_fn=lambda data: data.blade_mode,
    command_fn=lambda client, option: client.set_blade_mode(option),
)

JETS_SELECT_DESCRIPTION = HaloSelectEntityDescription(
    key="jets_mode_select",
    name="Jets Mode",
    options=["Off", "Auto", "On"],
    entity_registry_enabled_default=False,
    value_fn=lambda data: data.jets_mode,
    command_fn=lambda client, option: client.set_jets_mode(option),
)

HEATER_SELECT_DESCRIPTION = HaloSelectEntityDescription(
    key="heater_mode_select",
    name="Heater Mode Control",
    options=["Off", "On"],
    value_fn=lambda data: data.heater_mode,
    command_fn=lambda client, option: client.set_heater_off() if option == "Off" else client.set_heater_on(),
)


ACID_DOSING_OPTIONS = [
    "Resume now",
    "Off 1 minute",
    "Off 2 minutes",
    "Off 3 minutes",
    "Off 4 minutes",
    "Off 5 minutes",
    "Off 15 minutes",
    "Off 30 minutes",
    "Off 45 minutes",
    "Off 1 hour",
    "Off 2 hours",
    "Off 3 hours",
    "Off 6 hours",
    "Off 12 hours",
    "Off 24 hours",
    "Off indefinitely",
]

ACID_DOSING_MINUTES = {
    "Off 1 minute": 1,
    "Off 2 minutes": 2,
    "Off 3 minutes": 3,
    "Off 4 minutes": 4,
    "Off 5 minutes": 5,
    "Off 15 minutes": 15,
    "Off 30 minutes": 30,
    "Off 45 minutes": 45,
    "Off 1 hour": 60,
    "Off 2 hours": 120,
    "Off 3 hours": 180,
    "Off 6 hours": 360,
    "Off 12 hours": 720,
    "Off 24 hours": 1440,
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AstralPool Halo Cloud select entities."""
    coordinator: HaloCloudCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            HaloModeSelect(coordinator),
            HaloPumpSpeedSelect(coordinator),
            HaloActionSelect(coordinator, LIGHT_SELECT_DESCRIPTION),
            HaloActionSelect(coordinator, BLADE_SELECT_DESCRIPTION),
            HaloActionSelect(coordinator, JETS_SELECT_DESCRIPTION),
            HaloActionSelect(coordinator, HEATER_SELECT_DESCRIPTION),
            HaloAcidDosingSelect(coordinator),
        ]
    )


class HaloModeSelect(HaloCloudEntity, SelectEntity):
    """Representation of the Halo system mode selector."""

    _attr_options = ["Off", "Auto", "On"]

    @property
    def available(self) -> bool:
        """Only allow control while the cloud session is actively connected."""
        return super().available and self.coordinator.client.data.connected

    def __init__(self, coordinator: HaloCloudCoordinator) -> None:
        """Initialise the mode selector."""
        super().__init__(coordinator, MODE_SELECT_DESCRIPTION)

    @property
    def current_option(self) -> str | None:
        """Return the current selected mode."""
        data = self.coordinator.data
        if data is None or data.mode is None:
            return None

        if data.mode in self._attr_options:
            return data.mode

        # Fallback for any unexpected mode value
        return None

    async def async_select_option(self, option: str) -> None:
        """Set the selected system mode."""
        client = self.coordinator.client
        if not client.data.connected:
            raise HomeAssistantError("Chlorinator cloud is not connected")
        if option == "Off":
            await client.set_mode_off()
        elif option == "Auto":
            await client.set_mode_auto()
        elif option == "On":
            await client.set_mode_manual()
        else:
            raise ValueError(f"Invalid option: {option}")


class HaloPumpSpeedSelect(HaloCloudEntity, SelectEntity):
    """Representation of the Halo manual pump speed selector."""

    _attr_options = ["Low", "Medium", "High"]

    @property
    def available(self) -> bool:
        """Only allow control while the cloud session is actively connected."""
        return super().available and self.coordinator.client.data.connected

    def __init__(self, coordinator: HaloCloudCoordinator) -> None:
        """Initialise the pump-speed selector."""
        super().__init__(coordinator, PUMP_SPEED_SELECT_DESCRIPTION)

    @property
    def current_option(self) -> str | None:
        """Return the current pump-speed selection when known."""
        data = self.coordinator.data
        if data is None:
            return None
        if data.pump_speed in self._attr_options:
            return data.pump_speed
        return None

    async def async_select_option(self, option: str) -> None:
        """Set the selected manual pump speed."""
        client = self.coordinator.client
        if not client.data.connected:
            raise HomeAssistantError("Chlorinator cloud is not connected")
        await client.set_mode_manual()
        if option == "Low":
            await client.set_pump_speed_low()
        elif option == "Medium":
            await client.set_pump_speed_medium()
        elif option == "High":
            await client.set_pump_speed_high()
        else:
            raise ValueError(f"Invalid option: {option}")


class HaloActionSelect(HaloCloudEntity, SelectEntity):
    """Generic app-style action select for controls with discrete states."""

    entity_description: HaloSelectEntityDescription

    @property
    def available(self) -> bool:
        """Only allow control while the cloud session is actively connected."""
        return super().available and self.coordinator.client.data.connected

    def __init__(self, coordinator: HaloCloudCoordinator, description: HaloSelectEntityDescription) -> None:
        super().__init__(coordinator, description)
        self._attr_options = list(description.options or [])

    @property
    def current_option(self) -> str | None:
        data = self.coordinator.data
        if data is None or self.entity_description.value_fn is None:
            return None
        value = self.entity_description.value_fn(data)
        if value in self._attr_options:
            return value
        return None

    async def async_select_option(self, option: str) -> None:
        client = self.coordinator.client
        if not client.data.connected:
            raise HomeAssistantError("Chlorinator cloud is not connected")
        if option not in self._attr_options:
            raise ValueError(f"Invalid option: {option}")
        if self.entity_description.command_fn is None:
            raise HomeAssistantError("This control is not configured")
        await self.entity_description.command_fn(client, option)
        self.coordinator.async_set_updated_data(client.data)


class HaloAcidDosingSelect(HaloCloudEntity, SelectEntity):
    """Action select for acid dosing hold presets."""

    _attr_options = ACID_DOSING_OPTIONS

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.client.data.connected

    def __init__(self, coordinator: HaloCloudCoordinator) -> None:
        super().__init__(
            coordinator,
            HaloSelectEntityDescription(
                key="acid_dosing_select",
                name="Acid Dosing Hold",
                entity_registry_enabled_default=False,
            ),
        )

    @property
    def current_option(self) -> str | None:
        data = self.coordinator.data
        if data is None:
            return None
        if data.acid_dosing_state == "ResumeNow":
            return "Resume now"
        if data.acid_dosing_state == "OffIndefinitely":
            return "Off indefinitely"
        if data.acid_dosing_state == "OffForPeriod" and data.acid_dosing_hold_minutes is not None:
            for label, minutes in ACID_DOSING_MINUTES.items():
                if minutes == data.acid_dosing_hold_minutes:
                    return label
        return None

    async def async_select_option(self, option: str) -> None:
        client = self.coordinator.client
        if not client.data.connected:
            raise HomeAssistantError("Chlorinator cloud is not connected")
        if option == "Resume now":
            await client.enable_acid_dosing()
        elif option == "Off indefinitely":
            await client.disable_acid_dosing(0)
        else:
            minutes = ACID_DOSING_MINUTES.get(option)
            if minutes is None:
                raise ValueError(f"Invalid option: {option}")
            await client.disable_acid_dosing(minutes)
        self.coordinator.async_set_updated_data(client.data)
