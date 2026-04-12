"""Constants for the AstralPool Halo Cloud integration."""

from __future__ import annotations

DOMAIN = "astralpool_halo_cloud"

CONF_SERIAL_NUMBER = "serial_number"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_DEVICE_NAME = "device_name"
CONF_AREA_ID = "area_id"
CONF_TIME_DRIFT_THRESHOLD_MINUTES = "time_drift_threshold_minutes"

PLATFORMS = ["sensor", "binary_sensor", "select", "number", "button"]


def default_device_name(serial_number: str | None) -> str:
    """Return the default Home Assistant device name for a Halo chlorinator."""
    if serial_number:
        return f"Halo {serial_number}"
    return "Halo Chlorinator"
