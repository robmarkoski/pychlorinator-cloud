"""Sensor platform for the AstralPool Halo Cloud integration."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    EntityCategory,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent, UnitOfTemperature, UnitOfVolume
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from pychlorinator_cloud.websocket_client import ChlorinatorLiveData

from .const import DOMAIN
from .coordinator import HaloCloudCoordinator
from .entity import HaloCloudEntity


@dataclass(frozen=True, kw_only=True)
class HaloSensorEntityDescription(SensorEntityDescription):
    """Describes a Halo Cloud sensor."""

    value_fn: Callable[[ChlorinatorLiveData], object]


SENSOR_DESCRIPTIONS: tuple[HaloSensorEntityDescription, ...] = (
    # Main state / measurements
    HaloSensorEntityDescription(
        key="mode",
        name="Mode",
        icon="mdi:power",
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda data: data.mode,
    ),
    HaloSensorEntityDescription(
        key="pump_speed",
        name="Pump Speed",
        icon="mdi:speedometer",
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda data: data.pump_speed,
    ),
    HaloSensorEntityDescription(
        key="ph_measurement",
        name="pH",
        icon="mdi:ph",
        device_class=SensorDeviceClass.PH,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.ph_measurement,
    ),
    HaloSensorEntityDescription(
        key="orp_measurement",
        name="ORP Measurement",
        native_unit_of_measurement="mV",
        icon="mdi:beaker-check-outline",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.orp_mv,
    ),
    HaloSensorEntityDescription(
        key="chlorine_status",
        name="Chlorine Status",
        icon="mdi:beaker-outline",
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda data: data.chlorine_control_status,
    ),
    HaloSensorEntityDescription(
        key="ph_status",
        name="pH Status",
        icon="mdi:ph",
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda data: data.ph_control_status,
    ),
    HaloSensorEntityDescription(
        key="info_message",
        name="Info Message",
        icon="mdi:information-outline",
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda data: data.info_message,
    ),
    HaloSensorEntityDescription(
        key="error_message",
        name="Error Message",
        icon="mdi:alert-circle-outline",
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda data: data.error_message,
    ),
    HaloSensorEntityDescription(
        key="timer_info",
        name="Timer Info",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda data: data.timer_info,
    ),
    HaloSensorEntityDescription(
        key="water_temperature",
        name="Water Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.water_temperature_c,
    ),
    HaloSensorEntityDescription(
        key="water_temperature_precise",
        name="Water Temperature Precise",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.water_temperature_precise,
    ),
    HaloSensorEntityDescription(
        key="cell_level",
        name="Cell Level",
        icon="mdi:fuel-cell",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.cell_level,
    ),
    HaloSensorEntityDescription(
        key="cell_current",
        name="Cell Current",
        native_unit_of_measurement=UnitOfElectricCurrent.MILLIAMPERE,
        icon="mdi:fuel-cell",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.cell_current_ma,
    ),
    # Configuration / setpoints
    HaloSensorEntityDescription(
        key="ph_control_type",
        name="pH Control Type",
        icon="mdi:tune",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.ph_control_type,
    ),
    HaloSensorEntityDescription(
        key="orp_control_type",
        name="ORP Control Type",
        icon="mdi:tune-variant",
        device_class=SensorDeviceClass.ENUM,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.orp_control_type,
    ),
    HaloSensorEntityDescription(
        key="ph_setpoint",
        name="pH Setpoint",
        icon="mdi:ph",
        device_class=SensorDeviceClass.PH,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.ph_setpoint,
    ),
    HaloSensorEntityDescription(
        key="orp_setpoint",
        name="ORP Setpoint",
        native_unit_of_measurement="mV",
        icon="mdi:beaker-check-outline",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.orp_setpoint,
    ),
    HaloSensorEntityDescription(
        key="pool_chlorine_setpoint",
        name="Pool Chlorine Setpoint",
        icon="mdi:beaker-plus-outline",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.pool_chlorine_setpoint,
    ),
    HaloSensorEntityDescription(
        key="acid_setpoint",
        name="Acid Setpoint",
        icon="mdi:beaker-minus-outline",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.acid_setpoint,
    ),
    HaloSensorEntityDescription(
        key="spa_chlorine_setpoint",
        name="Spa Chlorine Setpoint",
        icon="mdi:hot-tub",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.spa_chlorine_setpoint,
    ),
    # Device / diagnostics
    HaloSensorEntityDescription(
        key="access_level",
        name="Access Level",
        icon="mdi:shield-account-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.access_level,
    ),
    HaloSensorEntityDescription(
        key="protocol_version",
        name="Protocol Version",
        icon="mdi:identifier",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.protocol_version,
    ),
    HaloSensorEntityDescription(
        key="last_update",
        name="Last Update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.last_update,
    ),
    HaloSensorEntityDescription(
        key="board_temperature",
        name="Board Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.board_temperature_c,
    ),
    HaloSensorEntityDescription(
        key="pool_volume",
        name="Pool Volume",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        icon="mdi:pool",
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.pool_volume_l,
    ),
    HaloSensorEntityDescription(
        key="litres_left_to_filter",
        name="Litres Left to Filter",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        icon="mdi:chart-line",
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.pool_left_filter_l,
    ),
    # Heater
    HaloSensorEntityDescription(
        key="heater_mode",
        name="Heater Mode",
        icon="mdi:heat-pump",
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda data: data.heater_mode,
    ),
    HaloSensorEntityDescription(
        key="heater_pump_mode",
        name="Heater Pump Mode",
        icon="mdi:heat-pump-outline",
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda data: data.heater_pump_mode,
    ),
    HaloSensorEntityDescription(
        key="heater_setpoint",
        name="Heater Setpoint",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer",
        value_fn=lambda data: data.heater_setpoint_c,
    ),
    HaloSensorEntityDescription(
        key="heat_pump_mode",
        name="Heat Pump Mode",
        icon="mdi:heat-pump",
        device_class=SensorDeviceClass.ENUM,
        value_fn=lambda data: data.heat_pump_mode,
    ),
    HaloSensorEntityDescription(
        key="heater_water_temperature",
        name="Heater Water Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda data: data.heater_water_temp_c,
    ),
    HaloSensorEntityDescription(
        key="heater_error",
        name="Heater Error",
        icon="mdi:alert-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.heater_error,
    ),
    # Salt / Error raw code
    HaloSensorEntityDescription(
        key="salt_error_raw",
        name="Salt/Error Code",
        icon="mdi:shaker-outline",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda data: data.salt_error_raw,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AstralPool Halo Cloud sensors."""
    coordinator: HaloCloudCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(HaloCloudSensor(coordinator, description) for description in SENSOR_DESCRIPTIONS)


class HaloCloudSensor(HaloCloudEntity, SensorEntity):
    """Representation of a Halo Cloud sensor."""

    entity_description: HaloSensorEntityDescription

    @property
    def native_value(self):
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        return self.entity_description.value_fn(self.coordinator.data)
