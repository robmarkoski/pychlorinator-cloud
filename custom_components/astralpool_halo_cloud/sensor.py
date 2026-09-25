"""Sensor platform for the AstralPool Halo Cloud integration."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    EntityCategory,
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfTemperature,
    UnitOfTime,
    UnitOfVolume,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from .pychlorinator_cloud.error_codes import ERROR_MESSAGE_OPTIONS, error_info_attributes
from .pychlorinator_cloud.websocket_client import ChlorinatorLiveData

from .const import DOMAIN
from .coordinator import HaloCloudCoordinator
from .entity import HaloCloudEntity


@dataclass(frozen=True, kw_only=True)
class HaloSensorEntityDescription(SensorEntityDescription):
    """Describes a Halo Cloud sensor."""

    value_fn: Callable[[ChlorinatorLiveData], object]
    attributes_fn: Callable[[ChlorinatorLiveData], dict[str, object]] | None = None
    restore_on_startup: bool = False


# Friendly display values for the pH / chlorine "verdict" enums. The raw vendor
# codes (PHWasGreen, ORPIsRed, ChlorineIsLow, ...) are kept on a `raw_code`
# attribute. Green=in range, Yellow=marginal, Red=out of range; Is=current
# reading, Was=last completed reading (collapsed here for a friendly value).
_STATUS_FRIENDLY: dict[str, str] = {
    "None": "Unknown",
    # pH
    "PHIsGreen": "Balanced", "PHWasGreen": "Balanced",
    "PHIsYellow": "Marginal", "PHWasYellow": "Marginal",
    "PHIsRed": "Out of range", "PHWasRed": "Out of range",
    "PHIsLow": "Low", "PHWasLow": "Low",
    "PHIsOK": "OK", "PHWasOK": "OK",
    "PHIsHigh": "High", "PHWasHigh": "High",
    # chlorine / ORP
    "ORPIsGreen": "Balanced", "ORPWasGreen": "Balanced",
    "ORPIsYellow": "Marginal", "ORPWasYellow": "Marginal",
    "ORPIsRed": "Out of range", "ORPWasRed": "Out of range",
    "ChlorineIsLow": "Low", "ChlorineWasLow": "Low",
    "ChlorineIsOK": "OK", "ChlorineWasOK": "OK",
    "ChlorineIsHigh": "High", "ChlorineWasHigh": "High",
}
_STATUS_FRIENDLY_OPTIONS = ["Unknown", "Balanced", "OK", "Marginal", "Low", "High", "Out of range"]


def _friendly_status(raw: str | None) -> str | None:
    if raw is None:
        return None
    # Any unmapped code (incl. the parser's "Unknown(n)" fallback for firmware
    # verdict codes we don't know) must collapse to a value that is in the ENUM
    # options list, or HA raises ValueError on every state update. The exact
    # raw code is preserved on the `raw_code` attribute.
    return _STATUS_FRIENDLY.get(raw, "Unknown")


def _restore_float(state: str) -> float:
    return float(state)


def _restore_int(state: str) -> int:
    return int(float(state))


def _restore_str(state: str) -> str:
    return state


def _restore_timer_summary(state: str) -> None:
    """Placeholder restore converter for attrs-backed timer-summary restore."""
    return None


_RESTORE_ASSIGNMENTS: dict[str, tuple[tuple[str, Callable[[str], object]], ...]] = {
    "ph_measurement": (("ph_measurement", _restore_float),),
    "orp_measurement": (("orp_mv", _restore_int),),
    "water_temperature": (("water_temperature_c", _restore_float),),
    "water_temperature_precise": (("water_temperature_precise", _restore_float),),
    "board_temperature": (("board_temperature_c", _restore_float),),
    "heater_water_temperature": (("heater_water_temp_c", _restore_float),),
    "ph_setpoint": (("ph_setpoint", _restore_float),),
    "orp_setpoint": (("orp_setpoint", _restore_int),),
    "acid_setpoint": (("acid_setpoint", _restore_int),),
    "pool_chlorine_setpoint": (("pool_chlorine_setpoint", _restore_int),),
    "spa_chlorine_setpoint": (("spa_chlorine_setpoint", _restore_int),),
    "ph_control_type": (("ph_control_type", _restore_str),),
    "chlorine_control_type": (("chlorine_control_type", _restore_str),),
    "pool_volume": (("pool_volume_l", _restore_int),),
    "firmware_version": (("firmware_version", _restore_str),),
    "protocol_version": (("protocol_version", _restore_str),),
    "cell_running_hours": (("cell_running_hours", _restore_int),),
    "low_salt_cell_running_hours": (("low_salt_cell_running_hours", _restore_int),),
    "filter_pump_minutes_today": (("filter_pump_minutes_today", _restore_int),),
    "power_board_runtime_hours": (("power_board_runtime_hours", _restore_int),),
    "cell_reversal_count": (("cell_reversal_count", _restore_int),),
    "timer_profile_index": (("timer_profile_index", _restore_int),),
    "timer_next_profile_index": (("timer_next_profile_index", _restore_int),),
    "active_timer_slot": (("timer_profile_index", _restore_int),),
    "timer_season": (("timer_season", _restore_str),),
    "equipment_timer_slots": (("equipment_timer_slots", _restore_int),),
    "equipment_timer_summary": (("__restore_timer_summary__", _restore_timer_summary),),
    "lighting_timer_slots": (("lighting_timer_slots", _restore_int),),
    "acid_dosing_hold_remaining": (
        ("acid_dosing_hold_minutes", _restore_int),
        ("acid_dosing_hold_remaining_seconds", lambda state: _restore_int(state) * 60),
    ),
}


def _active_timer_count(data: ChlorinatorLiveData) -> int | None:
    """Return the count of active equipment timer slots when known."""
    if not data.timer_configs:
        return None
    return sum(1 for timer in data.timer_configs.values() if timer.get("active"))


def _timer_summary_value(data: ChlorinatorLiveData) -> str | None:
    """Return a compact equipment timer summary string."""
    active = _active_timer_count(data)
    if active is None:
        return None
    total = data.equipment_timer_slots
    if total is None:
        return f"{active} active"
    return f"{active}/{total} active"


# Maps the vendor-equipment chip key (used in the bundled Lovelace card and
# the write_equipment_timer service) to (kind, slot_number). Used by the
# timer-summary attributes to derive a presence + label per chip from the
# live valve/gpo setup records the controller emits on 0x0514/0x0516.
# PoolSpa intentionally excluded from the chip catalog — it's a Pool/Spa
# mode select (handled by the dedicated mode select entity), not a per-timer
# toggleable equipment. The protocol still encodes it in the equipment
# bitmap, so existing slots with PoolSpa round-trip via equipment_enabled;
# users just can't add it as a new chip from the timer card.
_TIMER_CHIP_KEYS: tuple[tuple[str, str, int | None], ...] = (
    ("FilterPump", "builtin", None),
    ("Heater", "builtin", None),
    ("Outlet1", "gpo", 1),
    ("Outlet2", "gpo", 2),
    ("Outlet3", "gpo", 3),
    ("Outlet4", "gpo", 4),
    ("Valve1", "valve", 1),
    ("Valve2", "valve", 2),
    ("Valve3", "valve", 3),
    ("Valve4", "valve", 4),
    ("Relay1", "relay", 1),
    ("Relay2", "relay", 2),
)

# Display label for the built-in chips. PoolSpa flips to "Spa" when the
# controller reports spa_selection (see get_equipment_name in the lib).
_BUILTIN_DEFAULT_LABELS: dict[str, str] = {
    "FilterPump": "Filter",
    "Heater": "Heater",
}

# Slot numbers (1-based) for the equipment_timer_slots vendor-app numbering.
# The vendor app calls these "Timer 1".. "Timer 8" — ordered by slot_index.


def _equipment_catalog(data: ChlorinatorLiveData) -> list[dict[str, object]]:
    """Return per-chip {key, label, present, kind} for the timer card.

    Built-ins (FilterPump, Heater) are always present once any timer-
    related data has been observed. GPOs and Valves are present iff
    the controller's setup record marks them enabled. Relays are present
    iff the timer-master capability flag indicates relay support.

    PoolSpa is intentionally NOT in the chip catalog — it's a mode flag,
    not a per-timer toggleable. The protocol still encodes it in the
    equipment bitmap so existing slots with PoolSpa round-trip via
    equipment_enabled.

    Labels prefer custom-named entries over the built-in vendor names
    when the controller reports `is_custom_name=True`. Falls back to the
    chip key as a last resort.
    """
    catalog: list[dict[str, object]] = []
    for key, kind, slot in _TIMER_CHIP_KEYS:
        present = True
        label = _BUILTIN_DEFAULT_LABELS.get(key, key)
        if kind == "gpo" and slot is not None:
            # Strict: GPO is present only when the controller's setup record
            # explicitly marks it enabled. A custom name alone is NOT enough
            # — vendor setup wizards can leave a stale name on a disabled
            # GPO. (A controller with 0 GPOs connected still reports names
            # defaulting to "No Name", so a name-based fallback used to keep
            # them all visible.)
            present = bool(data.gpo_enabled.get(slot, False))
            label = data.gpo_names.get(slot) or f"Outlet {slot}"
        elif kind == "valve" and slot is not None:
            # Strict: valve is present only when the controller's setup
            # record marks it enabled. Custom-name presence alone is no
            # longer enough — a leftover custom-name slot for a removed
            # valve would still show. The valve-enabled flag is the source
            # of truth from the controller. 2026-05-26 user-feedback fix.
            present = bool(data.valve_enabled.get(slot, False))
            custom_name = data.valve_custom_names.get(slot - 1)
            if data.valve_is_custom_name.get(slot) and custom_name:
                label = custom_name
            else:
                label = data.valve_names.get(slot) or f"Valve {slot}"
        elif kind == "relay" and slot is not None:
            # Relays don't have GPO/valve-style setup records. The closest
            # signal we have is the per-slot equipment_enable bitmap from
            # 0x0193 readbacks: TIMER_EQUIPMENT_FLAGS maps bit 0x0800 to
            # Relay1 and bit 0x1000 to Relay2 (see pychlorinator_cloud/
            # timers.py). If any observed slot has the relay bit set, treat
            # that relay channel as available.
            #
            # NB: `timer_capability_flags` is a tuple[int, ...] (one byte
            # per capability category from cmd 0x0190), NOT a flat bitfield
            # — hence we OR-reduce defensively to tolerate list / tuple /
            # int / None payloads. We then bias toward 'present' when we
            # have NO observed slot equipment data yet, so the chip stays
            # visible during the bootstrap window instead of disappearing.
            relay_bit = 0x0800 if slot == 1 else 0x1000
            present = False
            observed_any_equipment_bits = False
            for slot_config in data.timer_configs.values():
                eq_flags = slot_config.get("equipment_flags") if isinstance(slot_config, dict) else None
                if eq_flags is None:
                    continue
                observed_any_equipment_bits = True
                if eq_flags & relay_bit:
                    present = True
                    break
            if not observed_any_equipment_bits:
                # Bootstrap window or no slots populated — keep relay chips
                # visible so the user can still configure them. Better to
                # show an unused chip than to hide a present-but-unused
                # relay channel.
                present = True
            label = f"Relay {slot}"
        catalog.append(
            {
                "key": key,
                "label": label,
                "present": present,
                "kind": kind,
            }
        )
    return catalog


# Fallback labels for equipment keys that aren't in the chip catalog but
# can still appear in a slot's equipment_enabled bitmap (e.g. PoolSpa was
# removed from the chip list in 2026-05-26 but legacy slots may still
# carry it). The descriptor surfaces a friendly label instead of the raw
# key. `spa_selection` controls whether PoolSpa renders as "Spa" or
# "Pool/Spa" — matches the chip-catalog behaviour before the removal.
def _descriptor_fallback_label(key: str, spa_selection: bool = False) -> str:
    if key == "PoolSpa":
        return "Spa" if spa_selection else "Pool/Spa"
    return key


def _slot_descriptor(
    slot: dict[str, Any],
    catalog: list[dict[str, object]],
    *,
    spa_selection: bool = False,
) -> str:
    """Return the vendor-app-style equipment summary for a timer slot."""
    active = slot.get("active", slot.get("enabled"))
    if not active:
        return "Disabled"

    equipment_keys = slot.get("equipment_enabled") or []
    if not equipment_keys:
        return "Unconfigured"

    label_by_key = {
        item["key"]: item.get("label") or item["key"]
        for item in catalog
        if isinstance(item.get("key"), str)
    }
    labels = [
        str(
            label_by_key.get(
                key,
                _descriptor_fallback_label(key, spa_selection),
            )
        )
        for key in equipment_keys
    ]
    return ", ".join(labels) if labels else "Unconfigured"


def _slot_descriptors(
    slots: dict[int, dict[str, Any]],
    catalog: list[dict[str, object]],
    *,
    spa_selection: bool = False,
) -> dict[str, str]:
    """Return descriptors keyed by string slot_index in timer-slot order."""
    return {
        str(slot_index): _slot_descriptor(slot, catalog, spa_selection=spa_selection)
        for slot_index, slot in sorted(slots.items())
    }


def _timer_summary_attributes(data: ChlorinatorLiveData) -> dict[str, object]:
    """Return schedule details for the timer summary sensor.

    Surfaces enough metadata for the bundled `halo-timer-card` to render
    vendor-app-equivalent UX:
    - `equipment_catalog`: per-chip {key, label, present, kind} so the card
      can hide unconnected valves/outlets and show custom names
    - `cmd_0x0193_last_seen`: ISO timestamp of last equipment-timer-config
      readback so the card can display "updated Xm ago"
    - `refresh_command_cmd_id`: lets the card surface the refresh button
    - `slot_labels`: per-slot "Timer N" labels matching the vendor app
    """
    if (
        not data.timer_configs
        and data.timer_season is None
        and data.equipment_timer_slots is None
    ):
        return {}

    winter_slots = [
        data.timer_configs_winter[index] for index in sorted(data.timer_configs_winter)
    ]
    summer_slots = [
        data.timer_configs_summer[index] for index in sorted(data.timer_configs_summer)
    ]
    timer_config_last_seen = data.cmd_last_seen.get(0x0193)
    slot_count = data.equipment_timer_slots or 8
    restored = bool(getattr(data, "timer_summary_restored", False))
    restored_equipment_catalog = getattr(
        data, "timer_summary_restored_equipment_catalog", None
    )
    restored_slot_labels = getattr(data, "timer_summary_restored_slot_labels", None)
    slot_labels = {
        str(slot_index): f"Timer {slot_index + 1}"
        for slot_index in range(slot_count)
    }
    if restored and isinstance(restored_slot_labels, dict):
        slot_labels = restored_slot_labels
    equipment_catalog = (
        restored_equipment_catalog
        if restored and isinstance(restored_equipment_catalog, list)
        else _equipment_catalog(data)
    )
    return {
        "season": data.timer_season,
        "current_season": data.timer_season,
        "season_source": data.timer_season_source,
        "profile_index": data.timer_profile_index,
        "equipment_timer_slots": data.equipment_timer_slots,
        "lighting_timer_slots": data.lighting_timer_slots,
        "capability_flags": data.timer_capability_flags,
        "slot_count_seen": len(data.timer_configs),
        "winter_slot_count_seen": len(data.timer_configs_winter),
        "summer_slot_count_seen": len(data.timer_configs_summer),
        "winter_slots": winter_slots,
        "summer_slots": summer_slots,
        "winter_light_slots": [
            data.timer_configs_light_winter[i]
            for i in sorted(data.timer_configs_light_winter)
        ],
        "summer_light_slots": [
            data.timer_configs_light_summer[i]
            for i in sorted(data.timer_configs_light_summer)
        ],
        "lighting_zone_names": dict(data.light_zone_names or {}),
        "slot_labels": slot_labels,
        "slot_descriptors": _slot_descriptors(
            data.timer_configs,
            equipment_catalog,
            spa_selection=bool(data.spa_selection),
        ),
        "winter_slot_descriptors": _slot_descriptors(
            data.timer_configs_winter,
            equipment_catalog,
            spa_selection=bool(data.spa_selection),
        ),
        "summer_slot_descriptors": _slot_descriptors(
            data.timer_configs_summer,
            equipment_catalog,
            spa_selection=bool(data.spa_selection),
        ),
        "equipment_catalog": equipment_catalog,
        "timer_config_last_seen": (
            timer_config_last_seen.isoformat() if timer_config_last_seen else None
        ),
        "restored": restored,
        "restored_from_at": getattr(data, "timer_summary_restored_from", None),
    }


def _heat_demand_summary_value(data: ChlorinatorLiveData) -> str | None:
    """Return a compact heat-demand schedule summary string.

    Returns:
      - "Off" when heat demand itself is disabled
      - "Always On" when enabled but window is off (= 24h)
      - "HH:MM-HH:MM" when window is enabled
      - None until the first 0x0451 snapshot lands
    """
    if data.heat_demand_enabled is None:
        return None
    if not data.heat_demand_enabled:
        return "Off"
    if not data.heat_demand_window_enabled:
        return "Always On"
    if (
        data.heat_demand_window_start_hour is None
        or data.heat_demand_window_start_minute is None
        or data.heat_demand_window_stop_hour is None
        or data.heat_demand_window_stop_minute is None
    ):
        return "On"
    return (
        f"{data.heat_demand_window_start_hour:02d}:"
        f"{data.heat_demand_window_start_minute:02d}-"
        f"{data.heat_demand_window_stop_hour:02d}:"
        f"{data.heat_demand_window_stop_minute:02d}"
    )


def _heat_demand_summary_attributes(data: ChlorinatorLiveData) -> dict[str, object]:
    """Surface the raw heat-demand fields as attributes."""
    if data.heat_demand_enabled is None:
        return {}
    return {
        "enabled": data.heat_demand_enabled,
        "window_enabled": data.heat_demand_window_enabled,
        "window_start_hour": data.heat_demand_window_start_hour,
        "window_start_minute": data.heat_demand_window_start_minute,
        "window_stop_hour": data.heat_demand_window_stop_hour,
        "window_stop_minute": data.heat_demand_window_stop_minute,
        "activated": data.heat_demand_activated,
    }


def _pump_speed_attributes(data: ChlorinatorLiveData) -> dict[str, object]:
    """Surface the underlying inputs to current_operating_speed as attributes.

    The pump runs at one speed at a time. The single Pump Speed sensor
    reports that one number. These attributes are for users who want to
    see what the manual setpoint and active timer would dictate if the
    derivation changed.
    """
    attrs: dict[str, object] = {}
    if data.pump_speed is not None:
        attrs["manual_setpoint"] = data.pump_speed
    if data.timer_pump_speed is not None:
        attrs["timer_setpoint"] = data.timer_pump_speed
    if data.mode is not None:
        attrs["system_mode"] = data.mode
    attrs["timer_profile_index"] = data.timer_profile_index
    attrs["ai_mode_active"] = data.ai_mode_active
    attrs["pump_is_operating"] = data.pump_is_operating
    if data.priming_active:
        attrs["priming_active"] = True
    return attrs


def _light_state_attributes(data: ChlorinatorLiveData) -> dict[str, object]:
    """Return raw light-state fields for live enum capture."""
    if data.light_zone1_mode_raw is None:
        return {}
    attrs: dict[str, object] = {
        "zone1_mode_raw": data.light_zone1_mode_raw,
        "zone2_mode_raw": data.light_zone2_mode_raw,
        "zone3_mode_raw": data.light_zone3_mode_raw,
        "zone4_mode_raw": data.light_zone4_mode_raw,
        "zone1_on": data.light_zone1_on,
        "zone2_on": data.light_zone2_on,
        "zone3_on": data.light_zone3_on,
        "zone4_on": data.light_zone4_on,
    }
    if data.lighting_model_label:
        attrs["lighting_model"] = data.lighting_model_label
    # User-assigned light-zone names (cmd 0x012F), if the controller has any.
    for zone_index, name in sorted((data.light_zone_names or {}).items()):
        attrs[f"zone{zone_index + 1}_name"] = name
    return attrs


def _equipment_name_attributes(
    kind: str,
    slot: int,
) -> Callable[[ChlorinatorLiveData], dict[str, object]]:
    """Return custom-name diagnostic attributes for a GPO or valve slot."""
    custom_field = "gpo_is_custom_name" if kind == "gpo" else "valve_is_custom_name"

    def _attributes(data: ChlorinatorLiveData) -> dict[str, object]:
        custom_names = getattr(data, custom_field)
        return {"is_custom_name": custom_names.get(slot, False)}

    return _attributes


def _valve_display_name(data: ChlorinatorLiveData, slot: int) -> str | None:
    """Return a valve setup name, resolving custom names when present."""
    if data.valve_is_custom_name.get(slot):
        custom_name = data.valve_custom_names.get(slot - 1)
        if custom_name:
            return custom_name
    return data.valve_names.get(slot)


def _equipment_name_value(
    kind: str, slot: int
) -> Callable[[ChlorinatorLiveData], str | None]:
    """Return equipment-name sensor state for a GPO or valve slot."""
    if kind == "valve":
        return lambda data, slot=slot: _valve_display_name(data, slot)
    return lambda data, slot=slot: data.gpo_names.get(slot)


def _equipment_name_description(kind: str, slot: int) -> HaloSensorEntityDescription:
    """Build a default-disabled diagnostic equipment-name sensor."""
    name_prefix = "GPO" if kind == "gpo" else "Valve"
    return HaloSensorEntityDescription(
        key=f"{kind}{slot}_name",
        name=f"{name_prefix}{slot} Name",
        icon="mdi:tag-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_equipment_name_value(kind, slot),
        attributes_fn=_equipment_name_attributes(kind, slot),
    )


def _light_zone_index_from_key(key: str) -> int | None:
    """Return the light zone index encoded in an entity key, if any."""
    sensor_keys = {
        "zone1_manual_mode": 1,
        "zone2_manual_mode": 2,
        "zone3_manual_mode": 3,
        "zone4_manual_mode": 4,
        "zone1_active_source": 1,
        "zone2_active_source": 2,
        "zone3_active_source": 3,
        "zone4_active_source": 4,
    }
    return sensor_keys.get(key)


def _light_zone_available(data: ChlorinatorLiveData, zone_index: int) -> bool:
    """Return whether a light zone exists on this controller."""
    if data.lighting_enabled is False:
        return False
    if data.lighting_num_zones_in_use is not None:
        return zone_index <= data.lighting_num_zones_in_use
    return True


SENSOR_DESCRIPTIONS: tuple[HaloSensorEntityDescription, ...] = (
    # Main state / measurements
    HaloSensorEntityDescription(
        key="mode",
        name="System Mode",
        icon="mdi:power",
        device_class=SensorDeviceClass.ENUM,
        options=["Off", "Auto", "On"],
        value_fn=lambda data: data.mode,
    ),
    # Single user-facing Pump Speed sensor. Reflects what the pump is
    # actually running at right now, derived from system mode + manual
    # speed + active timer + AI mode. The two legacy diagnostic sources
    # (manual_setpoint, timer_setpoint) are exposed as attributes for
    # debugging without cluttering the dashboard with three sensors
    # that can never all be "right" simultaneously (the pump runs at
    # exactly one speed at a time).
    HaloSensorEntityDescription(
        key="current_operating_speed",
        name="Pump Speed",
        icon="mdi:speedometer",
        device_class=SensorDeviceClass.ENUM,
        options=["Low", "Medium", "High", "AI", "Priming"],
        value_fn=lambda data: data.current_operating_speed,
        attributes_fn=_pump_speed_attributes,
    ),
    # Diagnostic surfaces for the underlying values (default-disabled).
    HaloSensorEntityDescription(
        key="pump_speed",
        name="Manual Pump Speed (diagnostic)",
        icon="mdi:speedometer",
        device_class=SensorDeviceClass.ENUM,
        options=["Low", "Medium", "High"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.pump_speed,
    ),
    HaloSensorEntityDescription(
        key="timer_pump_speed",
        name="Timer Pump Speed (diagnostic)",
        icon="mdi:speedometer",
        device_class=SensorDeviceClass.ENUM,
        options=["Low", "Medium", "High"],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.timer_pump_speed,
    ),
    HaloSensorEntityDescription(
        key="priming_countdown",
        name="Priming Countdown",
        native_unit_of_measurement="s",
        icon="mdi:timer-sand",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.priming_countdown,
    ),
    HaloSensorEntityDescription(
        key="ph_measurement",
        name="pH",
        icon="mdi:ph",
        device_class=SensorDeviceClass.PH,
        state_class=SensorStateClass.MEASUREMENT,
        restore_on_startup=True,
        value_fn=lambda data: data.ph_measurement,
    ),
    HaloSensorEntityDescription(
        key="orp_measurement",
        name="ORP Measurement",
        native_unit_of_measurement="mV",
        icon="mdi:beaker-check-outline",
        state_class=SensorStateClass.MEASUREMENT,
        restore_on_startup=True,
        value_fn=lambda data: data.orp_mv,
    ),
    HaloSensorEntityDescription(
        key="highest_ph_measured",
        name="Highest pH Measured",
        icon="mdi:ph",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.highest_ph_measured,
    ),
    HaloSensorEntityDescription(
        key="lowest_ph_measured",
        name="Lowest pH Measured",
        icon="mdi:ph",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.lowest_ph_measured,
    ),
    HaloSensorEntityDescription(
        key="highest_orp_measured",
        name="Highest ORP Measured",
        native_unit_of_measurement="mV",
        icon="mdi:beaker-check-outline",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.highest_orp_measured,
    ),
    HaloSensorEntityDescription(
        key="lowest_orp_measured",
        name="Lowest ORP Measured",
        native_unit_of_measurement="mV",
        icon="mdi:beaker-check-outline",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.lowest_orp_measured,
    ),
    HaloSensorEntityDescription(
        key="chlorine_status",
        name="Chlorine Status",
        icon="mdi:beaker-outline",
        device_class=SensorDeviceClass.ENUM,
        options=_STATUS_FRIENDLY_OPTIONS,
        value_fn=lambda data: _friendly_status(data.chlorine_control_status),
        attributes_fn=lambda data: {"raw_code": data.chlorine_control_status},
    ),
    HaloSensorEntityDescription(
        key="ph_status",
        name="pH Status",
        icon="mdi:ph",
        device_class=SensorDeviceClass.ENUM,
        options=_STATUS_FRIENDLY_OPTIONS,
        value_fn=lambda data: _friendly_status(data.ph_control_status),
        attributes_fn=lambda data: {"raw_code": data.ph_control_status},
    ),
    HaloSensorEntityDescription(
        key="info_message",
        name="Info Message",
        icon="mdi:information-outline",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "Off",
            "Sanitising",
            "AIModeSanitising",
            "AIModeSampling",
            "Sampling",
            "Standby",
            "PrePurge",
            "PostPurg",
            "SanitisingUntilFirstTimer",
            "Filtering",
            "FilteringAndCleaning",
            "CalibratingSensor",
            "Backwashing",
            "PrimingAcidPump",
            "ManualAcidDose",
            "LowSpeedNoChlorinating",
            "SanitisingForPeriod",
            "SanitisingAndCleaningForPeriod",
            "LowTemperatureReducedOutput",
            "HeaterCooldownInProgress",
        ],
        value_fn=lambda data: data.info_message,
    ),
    HaloSensorEntityDescription(
        key="error_message",
        name="Error Message",
        icon="mdi:alert-circle-outline",
        device_class=SensorDeviceClass.ENUM,
        options=list(ERROR_MESSAGE_OPTIONS),
        value_fn=lambda data: data.error_message,
        attributes_fn=error_info_attributes,
    ),
    HaloSensorEntityDescription(
        key="timer_info",
        name="Timer Info",
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.ENUM,
        options=[
            "Idle",
            "SanitisingPoolOff",
            "SanitisingPoolUntil",
            "SanitisingSpaOff",
            "SanitisingSpaUntil",
            "SanitisingOff",
            "SanitisingUntil",
            "PrimingFor",
            "HeaterCooldownTimeRemaining",
        ],
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.timer_info,
    ),
    HaloSensorEntityDescription(
        key="water_temperature",
        name="Water Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        restore_on_startup=True,
        value_fn=lambda data: data.water_temperature_c,
    ),
    HaloSensorEntityDescription(
        key="water_temperature_precise",
        name="Water Temperature Precise",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
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
    HaloSensorEntityDescription(
        key="cell_reversal_count",
        name="Cell Reversal Count",
        icon="mdi:counter",
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.cell_reversal_count,
    ),
    HaloSensorEntityDescription(
        key="power_board_runtime_hours",
        name="Power Board Runtime",
        native_unit_of_measurement=UnitOfTime.HOURS,
        icon="mdi:timer-outline",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.power_board_runtime_hours,
    ),
    HaloSensorEntityDescription(
        key="cell_running_hours",
        name="Cell Running Hours",
        native_unit_of_measurement=UnitOfTime.HOURS,
        icon="mdi:timer-cog-outline",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.cell_running_hours,
    ),
    HaloSensorEntityDescription(
        key="low_salt_cell_running_hours",
        name="Cell Running Hours (Low Salt)",
        native_unit_of_measurement=UnitOfTime.HOURS,
        icon="mdi:timer-alert-outline",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.low_salt_cell_running_hours,
    ),
    HaloSensorEntityDescription(
        key="filter_pump_minutes_today",
        name="Filter Pump Runtime Today",
        native_unit_of_measurement=UnitOfTime.MINUTES,
        icon="mdi:timer-play-outline",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.filter_pump_minutes_today,
    ),
    # Configuration / setpoints
    HaloSensorEntityDescription(
        key="ph_control_type",
        name="pH Control Type",
        icon="mdi:ph",
        device_class=SensorDeviceClass.ENUM,
        options=["None", "Manual", "Automatic"],
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: (
            data.ph_control_type
            if data.ph_control_type in ("None", "Manual", "Automatic")
            else None
        ),
    ),
    HaloSensorEntityDescription(
        key="chlorine_control_type",
        name="Chlorine Control Type",
        icon="mdi:beaker-outline",
        device_class=SensorDeviceClass.ENUM,
        options=["None", "Manual", "Automatic"],
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: (
            data.chlorine_control_type
            if data.chlorine_control_type in ("None", "Manual", "Automatic")
            else None
        ),
    ),
    HaloSensorEntityDescription(
        key="ph_setpoint",
        name="pH Setpoint Readback",
        icon="mdi:ph",
        device_class=SensorDeviceClass.PH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.ph_setpoint,
    ),
    HaloSensorEntityDescription(
        key="orp_setpoint",
        name="ORP Setpoint Readback",
        native_unit_of_measurement="mV",
        icon="mdi:beaker-check-outline",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.orp_setpoint,
    ),
    HaloSensorEntityDescription(
        key="pool_chlorine_setpoint",
        name="Pool Chlorine Setpoint",
        icon="mdi:beaker-plus-outline",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.pool_chlorine_setpoint,
    ),
    HaloSensorEntityDescription(
        key="acid_setpoint",
        name="Acid Setpoint",
        icon="mdi:beaker-minus-outline",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.acid_setpoint,
    ),
    HaloSensorEntityDescription(
        key="acid_dosing_hold_remaining",
        name="Acid Dosing Hold Remaining",
        icon="mdi:timer-sand",
        native_unit_of_measurement="min",
        state_class=SensorStateClass.MEASUREMENT,
        restore_on_startup=True,
        value_fn=lambda data: (
            data.acid_dosing_hold_minutes
            if data.acid_dosing_hold_remaining_seconds
            and data.acid_dosing_hold_remaining_seconds > 0
            else 0
        ),
    ),
    HaloSensorEntityDescription(
        key="filter_sanitise_remaining",
        name="Filter/Sanitise Remaining",
        icon="mdi:timer-sand",
        native_unit_of_measurement="s",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.filter_sanitise_remaining_seconds,
    ),
    HaloSensorEntityDescription(
        key="spa_chlorine_setpoint",
        name="Spa Chlorine Setpoint",
        icon="mdi:hot-tub",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
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
        restore_on_startup=True,
        value_fn=lambda data: data.protocol_version,
    ),
    HaloSensorEntityDescription(
        key="firmware_version",
        name="Firmware Version",
        icon="mdi:chip",
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.firmware_version or None,
    ),
    HaloSensorEntityDescription(
        key="last_update",
        name="Last Update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.last_update,
    ),
    HaloSensorEntityDescription(
        key="controller_datetime",
        name="Controller Time",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.controller_datetime,
    ),
    HaloSensorEntityDescription(
        key="board_temperature",
        name="Board Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.board_temperature_c,
    ),
    HaloSensorEntityDescription(
        key="wifi_rssi",
        name="WiFi Signal Strength",
        native_unit_of_measurement="dBm",
        device_class=SensorDeviceClass.SIGNAL_STRENGTH,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=True,
        restore_on_startup=False,
        value_fn=lambda data: data.wifi_rssi_dbm,
    ),
    HaloSensorEntityDescription(
        key="pool_volume",
        name="Pool Volume",
        native_unit_of_measurement=UnitOfVolume.LITERS,
        icon="mdi:pool",
        device_class=SensorDeviceClass.VOLUME_STORAGE,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
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
        options=["Off", "On"],
        value_fn=lambda data: data.heater_mode,
    ),
    HaloSensorEntityDescription(
        key="heater_pump_mode",
        name="Heater Pump Mode",
        icon="mdi:heat-pump-outline",
        device_class=SensorDeviceClass.ENUM,
        options=["Off", "Auto", "On"],
        entity_category=EntityCategory.DIAGNOSTIC,
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
        options=["Cooling", "Heating", "Auto"],
        value_fn=lambda data: data.heat_pump_mode,
    ),
    HaloSensorEntityDescription(
        key="heater_water_temperature",
        name="Heater Water Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        restore_on_startup=True,
        value_fn=lambda data: data.heater_water_temp_c,
    ),
    HaloSensorEntityDescription(
        key="heater_error",
        name="Heater Error",
        icon="mdi:alert-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.heater_error,
    ),
    HaloSensorEntityDescription(
        key="heater_message",
        name="Heater Message",
        icon="mdi:radiator",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.heater_message,
        attributes_fn=lambda data: (
            {"detail": data.heater_message_detail}
            if data.heater_message_detail
            else {}
        ),
    ),
    HaloSensorEntityDescription(
        key="solar_roof_temperature",
        name="Solar Roof Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.solar_roof_temp_c,
    ),
    HaloSensorEntityDescription(
        key="solar_water_temperature",
        name="Solar Water Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.solar_water_temp_c,
    ),
    HaloSensorEntityDescription(
        key="solar_mode",
        name="Solar Mode",
        icon="mdi:solar-power",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.solar_mode,
    ),
    HaloSensorEntityDescription(
        key="solar_message",
        name="Solar Message",
        icon="mdi:solar-power",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.solar_message,
    ),
    HaloSensorEntityDescription(
        key="zone1_manual_mode",
        name="Zone 1 Manual Mode",
        icon="mdi:lightbulb-on-outline",
        device_class=SensorDeviceClass.ENUM,
        options=["Off", "Auto", "On"],
        value_fn=lambda data: data.light_zone1_mode,
    ),
    HaloSensorEntityDescription(
        key="zone2_manual_mode",
        name="Zone 2 Manual Mode",
        icon="mdi:lightbulb-on-outline",
        device_class=SensorDeviceClass.ENUM,
        options=["Off", "Auto", "On"],
        value_fn=lambda data: data.light_zone2_mode,
    ),
    HaloSensorEntityDescription(
        key="zone3_manual_mode",
        name="Zone 3 Manual Mode",
        icon="mdi:lightbulb-on-outline",
        device_class=SensorDeviceClass.ENUM,
        options=["Off", "Auto", "On"],
        value_fn=lambda data: data.light_zone3_mode,
    ),
    HaloSensorEntityDescription(
        key="zone4_manual_mode",
        name="Zone 4 Manual Mode",
        icon="mdi:lightbulb-on-outline",
        device_class=SensorDeviceClass.ENUM,
        options=["Off", "Auto", "On"],
        value_fn=lambda data: data.light_zone4_mode,
    ),
    HaloSensorEntityDescription(
        key="zone1_active_source",
        name="Zone 1 Active Source",
        icon="mdi:source-branch",
        device_class=SensorDeviceClass.ENUM,
        options=["manual_on", "manual_off", "timer", "off"],
        value_fn=lambda data: data.light_zone1_active_source,
    ),
    HaloSensorEntityDescription(
        key="zone2_active_source",
        name="Zone 2 Active Source",
        icon="mdi:source-branch",
        device_class=SensorDeviceClass.ENUM,
        options=["manual_on", "manual_off", "timer", "off"],
        value_fn=lambda data: data.light_zone2_active_source,
    ),
    HaloSensorEntityDescription(
        key="zone3_active_source",
        name="Zone 3 Active Source",
        icon="mdi:source-branch",
        device_class=SensorDeviceClass.ENUM,
        options=["manual_on", "manual_off", "timer", "off"],
        value_fn=lambda data: data.light_zone3_active_source,
    ),
    HaloSensorEntityDescription(
        key="zone4_active_source",
        name="Zone 4 Active Source",
        icon="mdi:source-branch",
        device_class=SensorDeviceClass.ENUM,
        options=["manual_on", "manual_off", "timer", "off"],
        value_fn=lambda data: data.light_zone4_active_source,
    ),
    # Salt / Error raw code
    HaloSensorEntityDescription(
        key="salt_error_raw",
        name="Salt/Error Code",
        icon="mdi:shaker-outline",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda data: data.salt_error_raw,
    ),
    # Timer diagnostics (read-only)
    HaloSensorEntityDescription(
        key="timer_season",
        name="Timer Season",
        icon="mdi:weather-sunny-alert",
        device_class=SensorDeviceClass.ENUM,
        options=["Winter", "Summer"],
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.timer_season,
    ),
    HaloSensorEntityDescription(
        key="timer_profile_index",
        name="Timer Profile Index",
        icon="mdi:counter",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.timer_profile_index,
    ),
    HaloSensorEntityDescription(
        key="active_timer_slot",
        name="Active Timer Slot",
        icon="mdi:counter",
        state_class=SensorStateClass.MEASUREMENT,
        restore_on_startup=True,
        value_fn=lambda data: data.timer_profile_index,
    ),
    HaloSensorEntityDescription(
        key="timer_next_profile_index",
        name="Next Timer Slot",
        icon="mdi:counter",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.timer_next_profile_index,
    ),
    HaloSensorEntityDescription(
        key="equipment_timer_slots",
        name="Equipment Timer Slots",
        icon="mdi:table-column-plus-after",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.equipment_timer_slots,
    ),
    HaloSensorEntityDescription(
        key="lighting_timer_slots",
        name="Lighting Timer Slots",
        icon="mdi:table-column-plus-after",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        restore_on_startup=True,
        value_fn=lambda data: data.lighting_timer_slots,
    ),
    HaloSensorEntityDescription(
        key="equipment_timer_active_slots",
        name="Equipment Timer Active Slots",
        icon="mdi:timer-play-outline",
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_active_timer_count,
    ),
    HaloSensorEntityDescription(
        key="equipment_timer_summary",
        name="Equipment Timer Summary",
        icon="mdi:timer-cog-outline",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_timer_summary_value,
        attributes_fn=_timer_summary_attributes,
        restore_on_startup=True,
    ),
    HaloSensorEntityDescription(
        key="heat_demand_schedule",
        name="Heat Demand Schedule",
        icon="mdi:radiator",
        value_fn=_heat_demand_summary_value,
        attributes_fn=_heat_demand_summary_attributes,
    ),
    *(_equipment_name_description("gpo", slot) for slot in range(1, 5)),
    *(_equipment_name_description("valve", slot) for slot in range(1, 5)),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up AstralPool Halo Cloud sensors."""
    coordinator: HaloCloudCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        (
            HaloCloudRestoringSensor(coordinator, description)
            if description.restore_on_startup
            else HaloCloudSensor(coordinator, description)
        )
        for description in SENSOR_DESCRIPTIONS
    )
    async_add_entities(
        HaloCloudAcidSensor(coordinator, description, value_fn)
        for description, value_fn in ACID_SENSOR_SPECS
    )


# Acid reservoir sensors — HA-side derived values from the AcidReservoirTracker
# (see acid.py). value_fn receives the coordinator so it can read both the
# tracker and live client data (for "used today").
ACID_SENSOR_SPECS: tuple[
    tuple[SensorEntityDescription, Callable[[HaloCloudCoordinator], object]], ...
] = (
    (
        SensorEntityDescription(
            key="acid_remaining",
            name="Acid Remaining",
            native_unit_of_measurement="L",
            icon="mdi:bottle-tonic-outline",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        lambda c: c.acid.remaining_l,
    ),
    (
        SensorEntityDescription(
            key="acid_remaining_percent",
            name="Acid Remaining Percent",
            native_unit_of_measurement="%",
            icon="mdi:gauge",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        lambda c: c.acid.percent,
    ),
    (
        SensorEntityDescription(
            key="acid_used_today",
            name="Acid Used Today",
            native_unit_of_measurement="mL",
            icon="mdi:beaker-outline",
            state_class=SensorStateClass.TOTAL,
        ),
        lambda c: c.acid.used_today_ml(
            c.data.acid_dosing_seconds_today,
            c.data.acid_pump_size_ml_per_min,
            c.data.firmware_version,
        ),
    ),
    (
        SensorEntityDescription(
            key="acid_avg_daily",
            name="Acid Average Daily Use",
            native_unit_of_measurement="mL",
            icon="mdi:chart-line",
            state_class=SensorStateClass.MEASUREMENT,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        lambda c: (
            round(c.acid.avg_daily_ml(), 0) if c.acid.avg_daily_ml() is not None else None
        ),
    ),
    (
        SensorEntityDescription(
            key="acid_days_remaining",
            name="Acid Days Remaining",
            native_unit_of_measurement=UnitOfTime.DAYS,
            icon="mdi:calendar-clock",
            state_class=SensorStateClass.MEASUREMENT,
        ),
        lambda c: c.acid.days_remaining(),
    ),
    (
        SensorEntityDescription(
            key="acid_last_refill",
            name="Acid Last Refill",
            icon="mdi:calendar-check",
            device_class=SensorDeviceClass.TIMESTAMP,
            entity_category=EntityCategory.DIAGNOSTIC,
        ),
        lambda c: c.acid.last_refill,
    ),
)


class HaloCloudAcidSensor(HaloCloudEntity, SensorEntity):
    """HA-side derived acid reservoir sensor (independent of live payloads)."""

    def __init__(
        self,
        coordinator: HaloCloudCoordinator,
        description: SensorEntityDescription,
        value_fn: Callable[[HaloCloudCoordinator], object],
    ) -> None:
        super().__init__(coordinator, description)
        self._acid_value_fn = value_fn

    @property
    def available(self) -> bool:
        # Derived from persisted HA-side state; available even before first
        # live payload. "Used today" simply reports None until data arrives.
        return True

    @property
    def native_value(self):
        return self._acid_value_fn(self.coordinator)


class HaloCloudSensor(HaloCloudEntity, SensorEntity):
    """Representation of a Halo Cloud sensor."""

    entity_description: HaloSensorEntityDescription
    _restored_on_startup = False

    @property
    def available(self) -> bool:
        if not super().available:
            if (
                not self.entity_description.restore_on_startup
                or not self._restored_on_startup
            ):
                return False
            restored_value = self.entity_description.value_fn(self.coordinator.data)
            return restored_value is not None
        if self.entity_description.key == "pump_speed":
            data = self.coordinator.data
            # Always available once we've observed a configured manual pump speed.
            # Previously gated to mode == "On" only, which left users with
            # "unavailable" while the controller was operating in Auto/AI —
            # the configured manual speed is still meaningful information at
            # all times (it's the speed the controller would use in On, and
            # is the default fallback during Auto-Sanitising).
            return data is not None and data.pump_speed in {"Low", "Medium", "High"}
        if self.entity_description.key == "current_operating_speed":
            data = self.coordinator.data
            return data is not None and data.current_operating_speed in {
                "Low",
                "Medium",
                "High",
                "AI",
                "Priming",
            }
        if self.entity_description.key == "timer_pump_speed":
            data = self.coordinator.data
            return data is not None and data.timer_pump_speed in {
                "Low",
                "Medium",
                "High",
            }
        zone_index = _light_zone_index_from_key(self.entity_description.key)
        if zone_index is not None:
            data = self.coordinator.data
            return data is not None and _light_zone_available(data, zone_index)
        return True

    @property
    def native_value(self):
        """Return the sensor value."""
        if self.coordinator.data is None:
            return None
        value = self.entity_description.value_fn(self.coordinator.data)
        if self.entity_description.key == "pump_speed":
            # Surface the configured manual speed regardless of mode; the
            # controller defaults to this speed during Auto-Sanitising and
            # exposes it as the manual setting at all times.
            if value not in {"Low", "Medium", "High"}:
                return None
        if self.entity_description.key == "current_operating_speed":
            if value not in {"Low", "Medium", "High", "AI", "Priming"}:
                return None
        if self.entity_description.key == "timer_pump_speed":
            if value not in {"Low", "Medium", "High"}:
                return None
        return value

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """Return optional extra state attributes."""
        if (
            self.coordinator.data is None
            or self.entity_description.attributes_fn is None
        ):
            return None
        attributes = self.entity_description.attributes_fn(self.coordinator.data)
        return attributes or None


class HaloCloudRestoringSensor(HaloCloudSensor, RestoreEntity):
    """Representation of a Halo Cloud sensor with startup state hydration."""

    async def async_added_to_hass(self) -> None:
        """Restore last-known values for selected slow-moving sensors."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if not last_state:
            return
        if self.entity_description.key == "equipment_timer_summary":
            self._restore_value(last_state.state or "", last_state.attributes)
            return
        if last_state.state not in (None, "unknown", "unavailable"):
            self._restore_value(last_state.state, last_state.attributes)

    def _restore_value(self, state: str, attrs: dict[str, object]) -> None:
        """Hydrate the live-data field backing this sensor."""
        if self.entity_description.key == "equipment_timer_summary":
            if self._restore_timer_summary_attrs(attrs):
                self._restored_on_startup = True
                # Force an immediate state push so the "restored: True" flag
                # is observable to the recorder + card UI BEFORE the next
                # 0x0193 land clears it. Without this, the True->False
                # window collapses to nothing and the (restored) badge
                # never renders.
                self.async_write_ha_state()
            return

        # Chemistry measurements are restored on startup to avoid "Unknown" gaps
        # during the 60-90s optional-tier read window after restart. Automations
        # that need freshness guarantees should explicitly guard on
        # `state.last_changed > 5min` rather than trusting the value blindly.
        assignments = _RESTORE_ASSIGNMENTS.get(self.entity_description.key, ())
        for field_name, converter in assignments:
            try:
                restored_value = converter(state)
            except (TypeError, ValueError):
                return
            setattr(self.coordinator.data, field_name, restored_value)
        if assignments:
            self._restored_on_startup = True

    def _restore_timer_summary_attrs(self, attrs: dict[str, object]) -> bool:
        """Hydrate equipment timer config from persisted summary attributes."""
        data = self.coordinator.data

        def _coerce_slots(slot_list: object) -> dict[int, dict[str, Any]]:
            if not isinstance(slot_list, list | tuple):
                return {}
            restored_slots: dict[int, dict[str, Any]] = {}
            for slot in slot_list:
                if not isinstance(slot, dict):
                    continue
                raw_slot_index = slot.get("slot_index")
                if raw_slot_index is None or isinstance(raw_slot_index, bool):
                    continue
                try:
                    slot_index = int(raw_slot_index)
                except (TypeError, ValueError):
                    continue
                restored_slots[slot_index] = dict(slot)
            return restored_slots

        winter_slots = _coerce_slots(attrs.get("winter_slots"))
        summer_slots = _coerce_slots(attrs.get("summer_slots"))
        fallback_slots = _coerce_slots(attrs.get("slots"))

        if winter_slots:
            data.timer_configs_winter = winter_slots
        if summer_slots:
            data.timer_configs_summer = summer_slots

        season = attrs.get("season") or attrs.get("current_season")
        if isinstance(season, str):
            data.timer_season = season
            if not winter_slots and not summer_slots and fallback_slots:
                if season == "Summer":
                    data.timer_configs_summer = fallback_slots
                else:
                    data.timer_configs_winter = fallback_slots

        season_source = attrs.get("season_source")
        if isinstance(season_source, str):
            data.timer_season_source = season_source

        profile_index = attrs.get("profile_index")
        if isinstance(profile_index, int) and not isinstance(profile_index, bool):
            data.timer_profile_index = profile_index

        equipment_timer_slots = attrs.get("equipment_timer_slots")
        if isinstance(equipment_timer_slots, int) and not isinstance(
            equipment_timer_slots, bool
        ):
            data.equipment_timer_slots = equipment_timer_slots

        last_seen_iso = attrs.get("timer_config_last_seen")
        parsed_last_seen: dt.datetime | None = None
        if isinstance(last_seen_iso, str):
            try:
                parsed_last_seen = dt.datetime.fromisoformat(
                    last_seen_iso.replace("Z", "+00:00")
                )
            except ValueError:
                parsed_last_seen = None
        if parsed_last_seen is not None:
            data.cmd_last_seen[0x0193] = parsed_last_seen

        equipment_catalog = attrs.get("equipment_catalog")
        if isinstance(equipment_catalog, list):
            data.timer_summary_restored_equipment_catalog = [
                dict(item) for item in equipment_catalog if isinstance(item, dict)
            ]

        slot_labels = attrs.get("slot_labels")
        if isinstance(slot_labels, dict):
            data.timer_summary_restored_slot_labels = {
                str(key): str(value)
                for key, value in slot_labels.items()
                if isinstance(value, str)
            }

        restored_any = bool(winter_slots or summer_slots or fallback_slots)
        if restored_any:
            data.timer_summary_restored = True
            data.timer_summary_restored_from = (
                last_seen_iso if isinstance(last_seen_iso, str) else None
            )
        return restored_any
