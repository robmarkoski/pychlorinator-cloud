"""Constants for the AstralPool Halo Cloud integration."""

from __future__ import annotations

DOMAIN = "astralpool_halo_cloud"

CONF_SERIAL_NUMBER = "serial_number"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_DEVICE_NAME = "device_name"
CONF_AREA_ID = "area_id"

PLATFORMS = ["sensor", "binary_sensor", "select"]

# Poll every 5 minutes to share the single connection slot with BLE
POLL_INTERVAL = 300  # seconds (5 minutes)

# How long to stay connected collecting data each poll cycle
DATA_COLLECTION_SECONDS = 25  # needs ~20s to get state + measurements + stats


def default_device_name(serial_number: str | None) -> str:
    """Return the default Home Assistant device name for a Halo chlorinator."""
    if serial_number:
        return f"Halo {serial_number}"
    return "Halo Chlorinator"
