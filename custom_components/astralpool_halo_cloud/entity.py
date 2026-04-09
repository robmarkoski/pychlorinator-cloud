"""Base entities for the AstralPool Halo Cloud integration."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import slugify

from .const import CONF_DEVICE_NAME, CONF_SERIAL_NUMBER, DOMAIN, default_device_name
from .coordinator import HaloCloudCoordinator


class HaloCloudEntity(CoordinatorEntity[HaloCloudCoordinator]):
    """Base entity for Halo Cloud entities."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: HaloCloudCoordinator, description) -> None:
        """Initialise the entity."""
        super().__init__(coordinator)
        self.entity_description = description

        serial_number = coordinator._entry.data[CONF_SERIAL_NUMBER]
        device_name = coordinator._entry.data.get(
            CONF_DEVICE_NAME,
            default_device_name(serial_number),
        )
        device_slug = slugify(device_name) or slugify(default_device_name(serial_number))

        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, serial_number)},
            name=device_name,
            manufacturer="AstralPool",
            model="Halo Chlorinator",
        )
        self._attr_name = description.name
        self._attr_object_id = f"{device_slug}_{description.key}"
        self._attr_unique_id = f"{serial_number}_{description.key}"

    @property
    def available(self) -> bool:
        """Return whether the entity has usable data.

        Entities remain unavailable until the first successful live payload is
        received, then retain last-known values across disconnects/contention.
        """
        return (
            self.coordinator.data is not None
            and self.coordinator.data.last_update is not None
        )
