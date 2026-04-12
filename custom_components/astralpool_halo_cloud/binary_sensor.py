"""Binary sensor platform for the AstralPool Halo Cloud integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import datetime

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from pychlorinator_cloud.websocket_client import ChlorinatorLiveData
from homeassistant.util import dt as dt_util

from .const import CONF_TIME_DRIFT_THRESHOLD_MINUTES, DOMAIN
from .coordinator import HaloCloudCoordinator
from .entity import HaloCloudEntity


SANITISING_INFO_MESSAGES = {
    "Sanitising",
    "AIModeSanitising",
    "SanitisingUntilFirstTimer",
    "SanitisingForPeriod",
    "SanitisingAndCleaningForPeriod",
}

SAMPLING_INFO_MESSAGES = {"Sampling", "AIModeSampling"}


def match_error(data: ChlorinatorLiveData, expected: str) -> bool:
    """Return whether the current error message matches the expected value."""
    return data.error_message == expected


def match_info(data: ChlorinatorLiveData, expected: str) -> bool:
    """Return whether the current info message matches the expected value."""
    return data.info_message == expected


def match_info_any(data: ChlorinatorLiveData, expected_values: set[str]) -> bool:
    """Return whether the current info message matches any expected value."""
    return data.info_message in expected_values


def controller_clock_drift_gt_threshold(
    data: ChlorinatorLiveData,
    threshold_minutes: float,
) -> bool | None:
    """Return whether the controller clock differs from HA by more than the configured threshold."""
    if data.controller_datetime is None:
        return None
    controller_dt = data.controller_datetime
    now = dt_util.now()
    if controller_dt.tzinfo is None:
        controller_dt = dt_util.as_local(controller_dt.replace(tzinfo=datetime.timezone.utc))
    delta = abs((dt_util.as_local(controller_dt) - now).total_seconds())
    return delta > (threshold_minutes * 60)


@dataclass(frozen=True, kw_only=True)
class HaloBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Halo Cloud binary sensor."""

    value_fn: Callable[[ChlorinatorLiveData], bool | None]


BINARY_SENSOR_DESCRIPTIONS: tuple[HaloBinarySensorEntityDescription, ...] = (
    HaloBinarySensorEntityDescription(
        key="connected",
        name="Cloud Connected",
        icon="mdi:cloud-check",
        device_class=BinarySensorDeviceClass.CONNECTIVITY,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.connected,
    ),
    HaloBinarySensorEntityDescription(
        key="pump_operating",
        name="Pump Operating",
        icon="mdi:pump",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda data: data.pump_is_operating,
    ),
    HaloBinarySensorEntityDescription(
        key="cell_operating",
        name="Cell Operating",
        icon="mdi:fuel-cell",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda data: data.cell_is_operating,
    ),
    HaloBinarySensorEntityDescription(
        key="cell_reversed",
        name="Cell Reversed",
        icon="mdi:swap-horizontal",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.cell_is_reversed,
    ),
    HaloBinarySensorEntityDescription(
        key="cell_reversing",
        name="Cell Reversing",
        icon="mdi:swap-horizontal-bold",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.cell_is_reversing,
    ),
    HaloBinarySensorEntityDescription(
        key="cooling_fan_on",
        name="Cooling Fan",
        icon="mdi:fan",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.cooling_fan_on,
    ),
    HaloBinarySensorEntityDescription(
        key="dosing_pump_on",
        name="Dosing Pump",
        icon="mdi:beaker",
        device_class=BinarySensorDeviceClass.RUNNING,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.dosing_pump_on,
    ),
    HaloBinarySensorEntityDescription(
        key="ai_mode_active",
        name="AI Mode Active",
        icon="mdi:brain",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.ai_mode_active,
    ),
    HaloBinarySensorEntityDescription(
        key="spa_selection",
        name="Spa Selected",
        icon="mdi:hot-tub",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.spa_selection,
    ),
    HaloBinarySensorEntityDescription(
        key="heater_on",
        name="Heater On",
        icon="mdi:fire",
        device_class=BinarySensorDeviceClass.HEAT,
        value_fn=lambda data: data.heater_on,
    ),
    HaloBinarySensorEntityDescription(
        key="time_drift",
        name="Time Drift",
        icon="mdi:clock-alert-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: None,
    ),
    HaloBinarySensorEntityDescription(
        key="no_flow",
        name="No Flow",
        icon="mdi:waves",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: match_error(data, "NoFlow"),
    ),
    HaloBinarySensorEntityDescription(
        key="low_salt",
        name="Low Salt",
        icon="mdi:shaker-alert",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: match_error(data, "LowSalt"),
    ),
    HaloBinarySensorEntityDescription(
        key="high_salt",
        name="High Salt",
        icon="mdi:shaker-alert",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: match_error(data, "HighSalt"),
    ),
    HaloBinarySensorEntityDescription(
        key="sampling_only",
        name="Sampling Only",
        icon="mdi:test-tube",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: match_error(data, "SamplingOnly"),
    ),
    HaloBinarySensorEntityDescription(
        key="dosing_disabled",
        name="Dosing Disabled",
        icon="mdi:beaker-remove",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: match_error(data, "DosingDisabled"),
    ),
    HaloBinarySensorEntityDescription(
        key="daily_acid_dose_limit_reached",
        name="Daily Acid Dose Limit Reached",
        icon="mdi:beaker-alert",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: match_error(data, "DlyAcidDoseLimit"),
    ),
    HaloBinarySensorEntityDescription(
        key="cell_disabled",
        name="Cell Disabled",
        icon="mdi:fuel-cell-off",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: match_error(data, "CellDis"),
    ),
    HaloBinarySensorEntityDescription(
        key="sanitising_active",
        name="Sanitising Active",
        icon="mdi:sparkles",
        value_fn=lambda data: match_info_any(data, SANITISING_INFO_MESSAGES),
    ),
    HaloBinarySensorEntityDescription(
        key="filtering_only",
        name="Filtering Only",
        icon="mdi:filter",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: match_info(data, "Filtering"),
    ),
    HaloBinarySensorEntityDescription(
        key="sampling_active",
        name="Sampling Active",
        icon="mdi:test-tube",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: match_info_any(data, SAMPLING_INFO_MESSAGES),
    ),
    HaloBinarySensorEntityDescription(
        key="standby",
        name="Standby",
        icon="mdi:pause-circle-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: match_info(data, "Standby"),
    ),
    HaloBinarySensorEntityDescription(
        key="low_speed_no_chlorinating",
        name="Low Speed No Chlorinating",
        icon="mdi:speedometer-slow",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: match_info(data, "LowSpeedNoChlorinating"),
    ),
    HaloBinarySensorEntityDescription(
        key="reduced_output_low_temperature",
        name="Reduced Output Low Temperature",
        icon="mdi:thermometer-low",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: match_info(data, "LowTemperatureReducedOutput"),
    ),
    HaloBinarySensorEntityDescription(
        key="heater_cooldown_active",
        name="Heater Cooldown Active",
        icon="mdi:radiator",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: match_info(data, "HeaterCooldownInProgress"),
    ),
    HaloBinarySensorEntityDescription(
        key="manual_acid_dose_active",
        name="Manual Acid Dose Active",
        icon="mdi:beaker-plus",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: match_info(data, "ManualAcidDose"),
    ),
    HaloBinarySensorEntityDescription(
        key="backwashing",
        name="Backwashing",
        icon="mdi:water",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: match_info(data, "Backwashing"),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AstralPool Halo Cloud binary sensors."""
    coordinator: HaloCloudCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        HaloCloudBinarySensor(coordinator, description)
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class HaloCloudBinarySensor(HaloCloudEntity, BinarySensorEntity):
    """Representation of a Halo Cloud binary sensor."""

    entity_description: HaloBinarySensorEntityDescription

    @property
    def is_on(self) -> bool | None:
        """Return the binary sensor state."""
        if self.coordinator.data is None:
            return None
        if self.entity_description.key == "time_drift":
            threshold = float(
                self.coordinator._entry.options.get(
                    CONF_TIME_DRIFT_THRESHOLD_MINUTES,
                    3,
                )
            )
            return controller_clock_drift_gt_threshold(self.coordinator.data, threshold)
        return self.entity_description.value_fn(self.coordinator.data)
