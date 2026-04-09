"""Select platform for the AstralPool Halo Cloud integration."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HaloCloudCoordinator
from .entity import HaloCloudEntity


@dataclass(frozen=True, kw_only=True)
class HaloSelectEntityDescription(SelectEntityDescription):
    """Describes a Halo Cloud select entity."""


MODE_SELECT_DESCRIPTION = HaloSelectEntityDescription(
    key="mode_select",
    name="Mode",
)

POOL_SPA_SELECT_DESCRIPTION = HaloSelectEntityDescription(
    key="pool_spa_select",
    name="Pool/Spa",
)


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
            HaloPoolSpaSelect(coordinator),
        ]
    )


class HaloModeSelect(HaloCloudEntity, SelectEntity):
    """Representation of the Halo operating mode selector."""

    _attr_options = ["Off", "Auto", "Low", "Medium", "High"]

    def __init__(self, coordinator: HaloCloudCoordinator) -> None:
        """Initialise the mode selector."""
        super().__init__(coordinator, MODE_SELECT_DESCRIPTION)

    @property
    def current_option(self) -> str | None:
        """Return the current selected mode."""
        data = self.coordinator.data
        if data is None:
            return None

        if data.mode == "Off":
            return "Off"

        if data.mode != "Off":
            if data.pump_speed == "Low":
                return "Low"
            if data.pump_speed == "Medium":
                return "Medium"
            if data.pump_speed == "High":
                return "High"
            if data.mode == "Auto" and data.pump_speed:
                return "Auto"

        info_message = data.info_message or ""
        if info_message == "Off":
            return "Off"
        if (
            "Sanitising" in info_message
            or "Sampling" in info_message
            or "Filtering" in info_message
            or info_message == "Standby"
        ):
            return "Auto"

        return "Auto"

    async def async_select_option(self, option: str) -> None:
        """Set the selected operating mode."""
        client = self.coordinator.client
        if option == "Off":
            await client.set_mode_off()
        elif option == "Auto":
            await client.set_mode_auto()
        elif option == "Low":
            await client.set_mode_manual()
            await client.set_pump_speed_low()
        elif option == "Medium":
            await client.set_mode_manual()
            await client.set_pump_speed_medium()
        elif option == "High":
            await client.set_mode_manual()
            await client.set_pump_speed_high()
        else:
            raise ValueError(f"Invalid option: {option}")


class HaloPoolSpaSelect(HaloCloudEntity, SelectEntity):
    """Representation of the Halo pool/spa selector."""

    _attr_options = ["Pool", "Spa"]

    def __init__(self, coordinator: HaloCloudCoordinator) -> None:
        """Initialise the pool/spa selector."""
        super().__init__(coordinator, POOL_SPA_SELECT_DESCRIPTION)

    @property
    def current_option(self) -> str | None:
        """Return the current pool/spa selection."""
        data = self.coordinator.data
        if data is None:
            return None
        return "Spa" if data.spa_selection else "Pool"

    async def async_select_option(self, option: str) -> None:
        """Set the selected pool/spa mode."""
        client = self.coordinator.client
        if option == "Pool":
            await client.select_pool()
        elif option == "Spa":
            await client.select_spa()
        else:
            raise ValueError(f"Invalid option: {option}")
