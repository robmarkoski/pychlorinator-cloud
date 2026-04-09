"""The AstralPool Halo Cloud integration."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_AREA_ID, DOMAIN, PLATFORMS
from .coordinator import HaloCloudCoordinator


async def _async_apply_area_to_entry_devices(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Apply the configured area to devices created by this config entry."""
    area_id = entry.data.get(CONF_AREA_ID)
    if not area_id:
        return

    device_registry = dr.async_get(hass)
    for device_entry in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        if device_entry.area_id == area_id:
            continue
        device_registry.async_update_device(device_entry.id, area_id=area_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up AstralPool Halo Cloud from a config entry."""
    coordinator = HaloCloudCoordinator(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        await _async_apply_area_to_entry_devices(hass, entry)
        coordinator.async_schedule_start()
    except Exception:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await coordinator.async_shutdown()
        raise

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an AstralPool Halo Cloud config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    coordinator: HaloCloudCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
    await coordinator.async_shutdown()
    return True
