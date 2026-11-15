"""Number platform for the AstralPool Halo Cloud integration."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
import math
from typing import Any

from homeassistant.components.number import NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .pychlorinator_cloud.setpoints import (
    ORP_SETPOINT_MAX_MV,
    ORP_SETPOINT_MIN_MV,
    PH_SETPOINT_MAX,
    PH_SETPOINT_MIN,
    PH_SETPOINT_STEP,
    POOL_CHLORINE_SETPOINT_MAX,
    POOL_CHLORINE_SETPOINT_MIN,
    POOL_CHLORINE_SETPOINT_STEP,
    SetpointValidationError,
    pool_chlorine_setpoint_bounds,
    validate_pool_chlorine_setpoint,
)
from .pychlorinator_cloud.websocket_client import ChlorinatorLiveData

from .const import CONF_CONNECTION_PAUSE_MINUTES, CONF_TIME_DRIFT_THRESHOLD_MINUTES, DOMAIN
from .coordinator import HaloCloudCoordinator
from .entity import HaloCloudEntity


@dataclass(frozen=True, kw_only=True)
class HaloNumberEntityDescription(NumberEntityDescription):
    """Describes a Halo Cloud number entity."""

    value_fn: Callable[[ChlorinatorLiveData], float | int | None]
    set_value_fn: Callable[[Any, float], Awaitable[None]]
    update_value_fn: Callable[[ChlorinatorLiveData, float], None]
    is_supported_fn: Callable[[ChlorinatorLiveData], bool]


NUMBER_DESCRIPTIONS: tuple[HaloNumberEntityDescription, ...] = (
    HaloNumberEntityDescription(
        key="ph_setpoint_control",
        name="pH Setpoint",
        icon="mdi:ph",
        native_unit_of_measurement="pH",
        native_min_value=PH_SETPOINT_MIN,
        native_max_value=PH_SETPOINT_MAX,
        native_step=PH_SETPOINT_STEP,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda data: data.ph_setpoint,
        set_value_fn=lambda client, value: client.set_ph_setpoint(float(value)),
        update_value_fn=lambda data, value: setattr(data, "ph_setpoint", round(float(value), 1)),
        is_supported_fn=lambda data: data.ph_setpoint is not None,
    ),
    HaloNumberEntityDescription(
        key="orp_setpoint_control",
        name="ORP Setpoint",
        icon="mdi:beaker-check-outline",
        native_unit_of_measurement="mV",
        native_min_value=ORP_SETPOINT_MIN_MV,
        native_max_value=ORP_SETPOINT_MAX_MV,
        native_step=10,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda data: data.orp_setpoint,
        set_value_fn=lambda client, value: client.set_orp_setpoint(int(value)),
        update_value_fn=lambda data, value: setattr(data, "orp_setpoint", int(value)),
        is_supported_fn=lambda data: data.orp_setpoint is not None,
    ),
    HaloNumberEntityDescription(
        key="pool_chlorine_setpoint_control",
        name="Pool Chlorine Setpoint",
        icon="mdi:beaker-plus-outline",
        native_min_value=POOL_CHLORINE_SETPOINT_MIN,
        native_max_value=POOL_CHLORINE_SETPOINT_MAX,
        native_step=POOL_CHLORINE_SETPOINT_STEP,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda data: data.pool_chlorine_setpoint,
        set_value_fn=lambda client, value: client.set_pool_chlorine_setpoint(int(value)),
        update_value_fn=lambda data, value: setattr(data, "pool_chlorine_setpoint", int(value)),
        is_supported_fn=lambda data: data.pool_chlorine_setpoint is not None,
    ),
    HaloNumberEntityDescription(
        key="heater_setpoint_control",
        name="Heater Setpoint Control",
        icon="mdi:thermometer",
        native_unit_of_measurement="°C",
        native_min_value=10,
        native_max_value=40,
        native_step=1,
        mode=NumberMode.SLIDER,
        entity_category=EntityCategory.CONFIG,
        value_fn=lambda data: data.heater_setpoint_c,
        set_value_fn=lambda client, value: client.set_heater_setpoint(int(value)),
        update_value_fn=lambda data, value: setattr(data, "heater_setpoint_c", int(value)),
        is_supported_fn=lambda data: data.heater_setpoint_c is not None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AstralPool Halo Cloud number entities."""
    coordinator: HaloCloudCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            *(HaloCloudSetpointNumber(coordinator, description) for description in NUMBER_DESCRIPTIONS),
            HaloCloudTimeDriftThresholdNumber(coordinator),
            HaloCloudConnectionPauseMinutesNumber(coordinator),
            HaloCloudAcidBottleSizeNumber(coordinator),
        ]
    )


class HaloCloudAcidBottleSizeNumber(HaloCloudEntity, NumberEntity):
    """Configurable acid container size (litres) for reservoir tracking."""

    _attr_native_min_value = 1
    _attr_native_max_value = 60
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "L"
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bottle-tonic"

    def __init__(self, coordinator: HaloCloudCoordinator) -> None:
        super().__init__(
            coordinator,
            NumberEntityDescription(key="acid_bottle_size", name="Acid Bottle Size"),
        )

    @property
    def available(self) -> bool:
        return True

    @property
    def native_value(self) -> float:
        return self.coordinator.acid.bottle_size_l

    async def async_set_native_value(self, value: float) -> None:
        await self.coordinator.acid.async_set_bottle_size(value)
        self.coordinator.async_update_listeners()


class HaloCloudSetpointNumber(HaloCloudEntity, NumberEntity):
    """Representation of a controllable Halo setpoint number."""

    entity_description: HaloNumberEntityDescription

    def _capability_bound(self, attr_name: str, fallback: float) -> float:
        data = self.coordinator.data
        value = getattr(data, attr_name, None) if data is not None else None
        return float(value) if value is not None and value > 0 else fallback

    def _pool_chlorine_bounds(self) -> tuple[int, int]:
        data = self.coordinator.data
        return pool_chlorine_setpoint_bounds(
            getattr(data, "min_manual_chlorine_setpoint", None),
            getattr(data, "max_manual_chlorine_setpoint", None),
        )

    @property
    def native_min_value(self) -> float:
        """Return controller-reported bounds when capabilities are populated."""
        if self.entity_description.key == "ph_setpoint_control":
            return self._capability_bound("min_ph_setpoint", PH_SETPOINT_MIN)
        if self.entity_description.key == "orp_setpoint_control":
            return self._capability_bound("min_orp_setpoint", ORP_SETPOINT_MIN_MV)
        if self.entity_description.key == "pool_chlorine_setpoint_control":
            return float(self._pool_chlorine_bounds()[0])
        return float(self.entity_description.native_min_value or 0)

    @property
    def native_max_value(self) -> float:
        """Return controller-reported bounds when they are usable."""
        if self.entity_description.key == "ph_setpoint_control":
            value = self._capability_bound("max_ph_setpoint", PH_SETPOINT_MAX)
            return value if value > self.native_min_value else PH_SETPOINT_MAX
        if self.entity_description.key == "orp_setpoint_control":
            value = self._capability_bound("max_orp_setpoint", ORP_SETPOINT_MAX_MV)
            return value if value > self.native_min_value else ORP_SETPOINT_MAX_MV
        if self.entity_description.key == "pool_chlorine_setpoint_control":
            return float(self._pool_chlorine_bounds()[1])
        return float(self.entity_description.native_max_value or 0)

    @property
    def available(self) -> bool:
        """Return whether this setpoint is safe to expose for writes."""
        data = self.coordinator.data
        return (
            super().available
            and self.coordinator.client.data.connected
            and data is not None
            and self.entity_description.is_supported_fn(data)
        )

    @property
    def native_value(self) -> float | None:
        """Return the current setpoint value."""
        data = self.coordinator.data
        if data is None:
            return None
        value = self.entity_description.value_fn(data)
        if value is None:
            return None
        return float(value)

    async def async_set_native_value(self, value: float) -> None:
        """Write a new setpoint value."""
        client = self.coordinator.client
        data = self.coordinator.data
        if data is None or not client.data.connected:
            raise HomeAssistantError("Chlorinator cloud is not connected")
        if not self.entity_description.is_supported_fn(data):
            raise HomeAssistantError(
                "This setpoint is not available for the current device state"
            )

        try:
            if self.entity_description.key == "pool_chlorine_setpoint_control":
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                    or value != int(value)
                ):
                    raise SetpointValidationError(
                        "Pool chlorine setpoint must be a finite integer"
                    )
                low, high = self._pool_chlorine_bounds()
                validate_pool_chlorine_setpoint(
                    int(value), minimum=low, maximum=high
                )
            await self.entity_description.set_value_fn(client, value)
        except (RuntimeError, SetpointValidationError, ValueError) as err:
            raise HomeAssistantError(str(err)) from err

        self.entity_description.update_value_fn(data, value)
        self.async_write_ha_state()


class HaloCloudTimeDriftThresholdNumber(HaloCloudEntity, NumberEntity):
    """Editable diagnostic threshold for the time-drift binary sensor."""

    _attr_native_min_value = 1
    _attr_native_max_value = 120
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:clock-edit-outline"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: HaloCloudCoordinator) -> None:
        super().__init__(
            coordinator,
            NumberEntityDescription(
                key="time_drift_threshold",
                name="Time Drift Threshold",
            ),
        )

    @property
    def available(self) -> bool:
        """This is a local diagnostic setting, so it should remain editable offline."""
        return self.coordinator.data is not None

    @property
    def native_value(self) -> float:
        """Return the current configured threshold in minutes."""
        return float(self.coordinator._entry.options.get(CONF_TIME_DRIFT_THRESHOLD_MINUTES, 3))

    async def async_set_native_value(self, value: float) -> None:
        """Persist a new threshold in the config entry options."""
        new_value = int(value)
        self.hass.config_entries.async_update_entry(
            self.coordinator._entry,
            options={
                **self.coordinator._entry.options,
                CONF_TIME_DRIFT_THRESHOLD_MINUTES: new_value,
            },
        )
        self.coordinator.async_update_listeners()


class HaloCloudConnectionPauseMinutesNumber(HaloCloudEntity, NumberEntity):
    """Editable duration for temporarily releasing the cloud connection.

    A value of 0 pauses indefinitely until explicit resume.
    """

    _attr_native_min_value = 0
    _attr_native_max_value = 240
    _attr_native_step = 1
    _attr_native_unit_of_measurement = "min"
    _attr_mode = NumberMode.BOX
    _attr_icon = "mdi:timer-pause-outline"

    def __init__(self, coordinator: HaloCloudCoordinator) -> None:
        super().__init__(
            coordinator,
            NumberEntityDescription(
                key="connection_pause_minutes",
                name="Cloud Pause Duration",
                entity_category=EntityCategory.CONFIG,
            ),
        )

    @property
    def available(self) -> bool:
        return self.coordinator.data is not None

    @property
    def native_value(self) -> float:
        return float(self.coordinator.default_pause_minutes)

    async def async_set_native_value(self, value: float) -> None:
        new_value = int(value)
        self.hass.config_entries.async_update_entry(
            self.coordinator._entry,
            options={
                **self.coordinator._entry.options,
                CONF_CONNECTION_PAUSE_MINUTES: new_value,
            },
        )
        self.coordinator.async_update_listeners()
