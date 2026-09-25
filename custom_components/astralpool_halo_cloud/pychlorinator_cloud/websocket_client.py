"""WebSocket-only cloud client for Halo protocol v2.0."""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import os
import ssl
import struct
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, ClassVar, Literal, Optional

import websockets

from .const import (
    SIGNALLING_AUTH_PASSWORD,
    SIGNALLING_AUTH_USERNAME,
    SIGNALLING_WS_URL,
    VOMIT_LEGACY_CMD_ID,
    VOMIT_SENTINEL_CMD_ID,
)
from .exceptions import (
    SignallingAuthenticationError,
    SignallingBusyError,
    SignallingError,
    SignallingUnavailableError,
)
from .error_codes import ERROR_CODE_TABLE
from .light_colours import available_light_colours, resolve_light_colour
from .payload_parsers import parse_data_payload
from .setpoints import build_setpoint_command
from .signalling import map_signalling_failure

LOGGER = logging.getLogger(__name__)


def _ws_json_dumps(obj: Any) -> str:
    """Compact JSON for WebSocket frames to match vendor app byte-for-byte."""
    return json.dumps(obj, separators=(",", ":"))


TIMER_SEASONS = {"Winter": 0, "Summer": 1}
TIMER_MODES = {"Normal": 0, "Dusk": 1, "Dawn": 2}
# Timer Parameter (pump speed) uses the vendor SpeedLevels enum: Low=0,
# Medium=1, High=2, AI=3 — matching the read parser (timers.TIMER_SPEED_LEVELS).
# (Previously 1-based here, which silently wrote one speed too high.)
TIMER_PUMP_SPEEDS = {"Low": 0, "Medium": 1, "High": 2, "AI": 3}
EQUIPMENT_NAMES = [
    "PoolSpa",
    "FilterPump",
    "Heater",
    "Outlet1",
    "Outlet2",
    "Outlet3",
    "Outlet4",
    "Valve1",
    "Valve2",
    "Valve3",
    "Valve4",
    "Relay1",
    "Relay2",
]
EQUIPMENT_ENABLE_MASKS = {
    name: 1 << index for index, name in enumerate(EQUIPMENT_NAMES)
}


@dataclass
class ChlorinatorLiveData:
    """Aggregated live data from the chlorinator."""

    connected: bool = False
    # Set once the vendor "vomit" opening handshake completes (0x0005 sentinel
    # seen). Diagnostic only — the connection is usable regardless, but this
    # tracks whether the relay considers us a fully-established logical client.
    logical_connection_established: bool = False
    access_level: int = 0
    protocol_version: str = ""
    firmware_version: str = ""
    last_update: Optional[datetime.datetime] = None
    # WiFi link health (from cloud signalling preflight)
    # Self-reported by chlorinator to Astral signalling endpoint.
    # Updated on every connect() preflight availability check.
    wifi_rssi_dbm: Optional[int] = None

    # State (cmd 0x0068) — mapped from BLE StateCharacteristic3
    mode: Optional[str] = None  # Off, ManualOn, Auto
    pump_speed: Optional[str] = (
        None  # Manual speed setting: Low, Medium, High (from 0x0324 sub 0x03)
    )
    current_operating_speed: Optional[str] = (
        None  # Conservative runtime readback: Low, Medium, High, AI
    )
    timer_pump_speed: Optional[str] = (
        None  # Live timer/profile speed readback from 0x00CA
    )
    timer_pump_speed_code: Optional[int] = None
    priming_active: Optional[bool] = None
    priming_countdown: Optional[int] = None
    priming_phase_code: Optional[int] = None
    # Valve state (from 0x0068 byte[12] — bitmask)
    valve_0_active: bool = False
    valve_1_active: bool = False
    pump_is_operating: bool = False
    cell_is_operating: bool = False
    cell_is_reversed: bool = False
    cell_is_reversing: bool = False
    chemistry_values_current: bool = False
    chemistry_values_valid: bool = False
    sanitising_until_next_timer_tomorrow: bool = False
    cooling_fan_on: bool = False
    dosing_pump_on: bool = False
    ai_mode_active: bool = False
    ph_measurement: Optional[float] = None
    ph_control_status: Optional[str] = None  # PHIsGreen, PHIsYellow, etc.
    chlorine_control_status: Optional[str] = None  # ORPIsGreen, ChlorineIsLow, etc.
    info_message: Optional[str] = None  # MainText enum
    error_message: Optional[str] = None  # SubText4ErrorInfo
    error_severity: Optional[str] = None
    error_category: Optional[str] = None
    error_reason: Optional[str] = None
    error_action: Optional[str] = None
    timer_info: Optional[str] = None  # SubText3
    spa_selection: bool = False
    spa_enabled: Optional[bool] = None
    orp_mv: Optional[int] = None  # ORP in millivolts
    water_temperature_c: Optional[float] = None  # precise from 0x0009
    cell_current_ma: Optional[int] = None
    cell_level: Optional[int] = None  # RealCelllevel (0-10)

    # Temperature (cmd 0x0009)
    water_temperature_precise: Optional[float] = None

    # Statistics (cmd 0x0258 / 0x0259 / 0x025a)
    highest_ph_measured: Optional[float] = None
    lowest_ph_measured: Optional[float] = None
    highest_orp_measured: Optional[int] = None
    lowest_orp_measured: Optional[int] = None
    # CellStatistics (cmd 0x0259 — corrected field map 2026-07-06)
    cell_reversal_count: Optional[int] = None
    cell_running_hours: Optional[int] = None
    low_salt_cell_running_hours: Optional[int] = None
    previous_days_cell_load_percent: Optional[int] = None
    # Acid dosing pump run-time TODAY (seconds); resets at controller day
    # rollover. Convert to mL via acid_pump_size (mL/min). Basis for the
    # HA-side acid reservoir tracking.
    acid_dosing_seconds_today: Optional[int] = None
    filter_pump_minutes_today: Optional[int] = None
    stats_flag_byte: Optional[int] = None
    power_board_runtime_hours: Optional[int] = None

    # Capabilities (cmd 0x0069)
    ph_control_type: Optional[str] = None
    chlorine_control_type: Optional[str] = None
    min_ph_setpoint: Optional[float] = None
    max_ph_setpoint: Optional[float] = None
    min_orp_setpoint: Optional[int] = None
    max_orp_setpoint: Optional[int] = None
    min_manual_chlorine_setpoint: Optional[int] = None
    max_manual_chlorine_setpoint: Optional[int] = None
    min_manual_acid_setpoint: Optional[int] = None
    max_manual_acid_setpoint: Optional[int] = None
    # Acid dosing capability + pump dose rate (mL/min) from capabilities 0x0069.
    dosing_capable: Optional[bool] = None
    acid_pump_size_ml_per_min: Optional[int] = None

    # Setpoints (cmd 0x0066 — SetPointCharacteristic)
    ph_setpoint: Optional[float] = None
    orp_setpoint: Optional[int] = None
    pool_chlorine_setpoint: Optional[int] = None
    acid_setpoint: Optional[int] = None
    spa_chlorine_setpoint: Optional[int] = None

    # Temperature (cmd 0x0009 — TempCharacteristic)
    board_temperature_c: Optional[float] = None

    # Water volume (cmd 0x0065 — WaterVolumeCharacteristic)
    pool_volume_l: Optional[int] = None
    pool_left_filter_l: Optional[int] = None

    # Salt / error raw value (from 0x0068 SubText4ErrorInfo)
    salt_error_raw: Optional[int] = (
        None  # Raw error code (702=LowSalt, 701=HighSalt, etc.)
    )

    # Heater (cmd 0x044e — HeaterStateCharacteristic)
    heater_mode: Optional[str] = None  # Off, On
    heater_pump_mode: Optional[str] = None  # Off, Auto, On
    heater_setpoint_c: Optional[int] = None
    heat_pump_mode: Optional[str] = None  # Cooling, Heating, Auto
    heater_water_temp_c: Optional[float] = None
    heater_on: bool = False
    heater_error: Optional[int] = None
    # Heater status message (HeaterMessageEnum) + diagnostic flags. Previously
    # parsed-but-dropped; now surfaced. heater_message is the vendor's primary
    # "why is the heater doing X" string (Cooldown, ValveInterlock, etc.).
    heater_message: Optional[str] = None
    heater_message_detail: Optional[str] = None
    heater_flame: Optional[bool] = None
    heater_pressure: Optional[bool] = None
    heater_gas_valve: Optional[bool] = None
    heater_lockout: Optional[bool] = None
    heater_service_required: Optional[bool] = None
    heater_cooling_available: Optional[bool] = None

    # Heat-Demand settings (cmd 0x0451 — HeaterDemandSettingsCharacteristic)
    # Single window + enable + activation flag; NOT a slot array.
    heat_demand_enabled: Optional[bool] = None
    heat_demand_window_enabled: Optional[bool] = None
    heat_demand_window_start_hour: Optional[int] = None
    heat_demand_window_start_minute: Optional[int] = None
    heat_demand_window_stop_hour: Optional[int] = None
    heat_demand_window_stop_minute: Optional[int] = None
    heat_demand_activated: Optional[bool] = None

    # Controller clock (cmd 0x0002 / 0x0003)
    controller_datetime: Optional[datetime.datetime] = None
    controller_weekday: Optional[int] = None

    # App-writable controls not yet fully observable from readback
    light_mode: Optional[str] = None  # Off, On, Auto
    light_zone1_mode_raw: Optional[int] = None
    light_zone2_mode_raw: Optional[int] = None
    light_zone3_mode_raw: Optional[int] = None
    light_zone4_mode_raw: Optional[int] = None
    light_zone1_mode: Optional[str] = None
    light_zone2_mode: Optional[str] = None
    light_zone3_mode: Optional[str] = None
    light_zone4_mode: Optional[str] = None
    light_zone1_on: Optional[bool] = None
    light_zone2_on: Optional[bool] = None
    light_zone3_on: Optional[bool] = None
    light_zone4_on: Optional[bool] = None
    light_zone1_active_source: Optional[str] = None
    light_zone2_active_source: Optional[str] = None
    light_zone3_active_source: Optional[str] = None
    light_zone4_active_source: Optional[str] = None
    lighting_enabled: Optional[bool] = None
    onboard_light_enabled: Optional[bool] = None
    lighting_model: Optional[int] = None
    lighting_model_label: Optional[str] = None
    lighting_num_zones_in_use: Optional[int] = None
    zone1_is_multicolour: Optional[bool] = None
    zone2_is_multicolour: Optional[bool] = None
    zone3_is_multicolour: Optional[bool] = None
    zone4_is_multicolour: Optional[bool] = None
    blade_mode: Optional[str] = None  # Off, Auto, On
    jets_mode: Optional[str] = None  # Off, Auto, On
    # Solar (cmd 0x04B2 SolarStateCharacteristic; accessory-dependent)
    solar_roof_temp_c: Optional[float] = None
    solar_water_temp_c: Optional[float] = None
    solar_temp_c: Optional[float] = None
    solar_mode: Optional[str] = None  # Off, Auto, On
    solar_message: Optional[str] = None
    solar_pump_on: Optional[bool] = None
    solar_flush_active: Optional[bool] = None
    acid_dosing_state: Optional[str] = None  # ResumeNow, OffIndefinitely, OffForPeriod
    acid_dosing_hold_minutes: Optional[int] = None
    # Acid dosing hold countdown (from 0x006a readback)
    acid_dosing_hold_remaining_seconds: Optional[int] = None
    filter_sanitise_remaining_seconds: Optional[int] = None

    # Timer diagnostics (read-only for now)
    equipment_timer_slots: Optional[int] = None
    lighting_timer_slots: Optional[int] = None
    timer_capability_flags: list[int] = field(default_factory=list)
    timer_season: Optional[str] = None
    timer_season_source: Optional[str] = None
    timer_no_timer_model: Optional[int] = None
    timer_master_is_present: Optional[int] = None
    timer_dusk_time_hour: Optional[int] = None
    timer_dusk_time_mins: Optional[int] = None
    timer_dawn_time_hour: Optional[int] = None
    timer_dawn_time_mins: Optional[int] = None
    timer_profile_index: Optional[int] = None
    timer_next_profile_index: Optional[int] = None
    timer_configs_winter: dict[int, dict[str, Any]] = field(default_factory=dict)
    timer_configs_summer: dict[int, dict[str, Any]] = field(default_factory=dict)
    # Lighting timers (TimerType=1) are stored separately so they no longer
    # collide with equipment timers (TimerType=0) at the same slot index.
    timer_configs_light_winter: dict[int, dict[str, Any]] = field(default_factory=dict)
    timer_configs_light_summer: dict[int, dict[str, Any]] = field(default_factory=dict)
    timer_summary_restored: bool = False
    timer_summary_restored_from: str | None = None
    timer_summary_restored_equipment_catalog: list[dict[str, Any]] | None = None
    timer_summary_restored_slot_labels: dict[str, str] | None = None

    @property
    def timer_configs(self) -> dict[int, dict[str, Any]]:
        """Return timer configs for the currently active season."""
        if self.timer_season == "Summer":
            return self.timer_configs_summer
        return self.timer_configs_winter

    # Equipment names (cmd 0x0514 GPO setup / 0x0516 valve setup)
    gpo_names: dict[int, str] = field(default_factory=dict)
    gpo_enabled: dict[int, bool] = field(default_factory=dict)
    gpo_use_timers: dict[int, bool] = field(default_factory=dict)
    gpo_is_custom_name: dict[int, bool] = field(default_factory=dict)
    valve_names: dict[int, str] = field(default_factory=dict)
    valve_enabled: dict[int, bool] = field(default_factory=dict)
    valve_use_timers: dict[int, bool] = field(default_factory=dict)
    valve_is_custom_name: dict[int, bool] = field(default_factory=dict)
    valve_custom_names: dict[int, str] = field(default_factory=dict)
    # User-assigned custom names: GPO (cmd 0x0519, slot 1-4), relay (0x051A,
    # index 0-1), lighting zone (0x012F, zone 0-3).
    gpo_custom_names: dict[int, str] = field(default_factory=dict)
    relay_custom_names: dict[int, str] = field(default_factory=dict)
    light_zone_names: dict[int, str] = field(default_factory=dict)

    # Raw payloads for debugging
    raw_payloads: dict[int, bytes] = field(default_factory=dict)
    cmd_last_seen: dict[int, datetime.datetime] = field(default_factory=dict)


# ChlorinatorActions — the action enum for cloud writes
# Confirmed command ID: 0x01F4 (500)
ACTION_CMD_ID = 0x01F4
LIGHT_CMD_ID = 0x01F5
HEATER_CMD_ID = 0x01F6
TIME_CMD_ID = 0x0002
DATE_CMD_ID = 0x0003

STATE_CMD_ID = 0x0068
SETPOINT_CMD_ID = 0x0066
SETTINGS_CMD_ID = 0x0064
MEASUREMENTS_CMD_ID = 0x0259
PROBE_STATISTICS_CMD_ID = 0x0258
STATISTICS_B_CMD_ID = 0x025A
CAPABILITIES_CMD_ID = 0x0069
MAINTENANCE_STATE_CMD_ID = 0x006A
LIGHT_STATE_CMD_ID = 0x012C
LIGHT_CAPABILITIES_CMD_ID = 0x012D
TEMPERATURE_CMD_ID = 0x0009
HEATER_STATE_CMD_ID = 0x044E
HEAT_DEMAND_CMD_ID = 0x0451

# Heat-demand write race-tolerance tuning (see write_heat_demand docstring).
# The vendor controller acks 0x0451 writes immediately with the PRE-WRITE
# snapshot, then updates its internal state asynchronously. Live testing on
# Rob's controller showed the second poll consistently sees the new values
# within ~1–2s of the first.
HEAT_DEMAND_SETTLE_DELAY_SECONDS = 1.5
HEAT_DEMAND_READBACK_PROCESS_SECONDS = 0.5
HEAT_DEMAND_RECONFIRM_DELAY_SECONDS = 2.0
EQUIPMENT_MODE_CMD_ID = 0x00C9  # live GPO/Blade/Jets/valve/relay Off/Auto/On
SOLAR_STATE_CMD_ID = 0x04B2  # live solar roof/water/solar temps, pump, mode
EQUIPMENT_PARAMETER_CMD_ID = 0x00CA
TIMER_CAPABILITIES_CMD_ID = 0x0190
TIMER_SETUP_CMD_ID = 0x0191
TIMER_STATE_CMD_ID = 0x0192
TIMER_CONFIG_CMD_ID = 0x0193
GPO_SETUP_CMD_ID = 0x0514
VALVE_SETUP_CMD_ID = 0x0516
CUSTOM_NAMES_VOMIT_CMD_ID = 0x001B
VALVE_CUSTOM_NAME_CMD_ID = 0x051B
POST_ACTION_REFRESH_CMD_IDS = (STATE_CMD_ID, 0x0324, MAINTENANCE_STATE_CMD_ID)

# Startup-refresh cmd sets are split into two tiers.
#
# MANDATORY: entity-availability gates. These must fire immediately on every
# successful cloud connect, including quiet reconnects. Astral signalling
# sessions often last only 15-25s, so delaying these reads can leave all v2
# entities unavailable for the entire session.
#
# SETTLED_OPTIONAL: low-priority reads that can wait behind the mandatory tier.
# These populate statistics, watermarks, board temperature, and timer metadata.
#
# Earlier diagnostic builds used app-observed command IDs such as 0x006B /
# 0x0005 before parser support existed. They remain absent until mapped.
MANDATORY_REFRESH_CMD_IDS = (
    STATE_CMD_ID,  # 0x0068 — state, pH/ORP, cell current, pump state
    SETPOINT_CMD_ID,  # 0x0066 — pH/ORP/chlorine setpoint readback
    SETTINGS_CMD_ID,  # 0x0064 — cell model, pump config, dosing capable
    CAPABILITIES_CMD_ID,  # 0x0069 — control types, bounds, capabilities
    MAINTENANCE_STATE_CMD_ID,  # 0x006A — acid dosing hold state
    EQUIPMENT_MODE_CMD_ID,  # 0x00C9 — live GPO/Blade/Jets/valve/relay modes
    EQUIPMENT_PARAMETER_CMD_ID,  # 0x00CA — live timer pump speed
    TIMER_STATE_CMD_ID,  # 0x0192 — timer profile index
    HEATER_STATE_CMD_ID,  # 0x044E — heater temperature, mode, error
    HEAT_DEMAND_CMD_ID,  # 0x0451 — heater demand window + enable + activation
    LIGHT_STATE_CMD_ID,  # 0x012C — per-zone light mode and on/off
    LIGHT_CAPABILITIES_CMD_ID,  # 0x012D — lighting capability / zone count gate
)

QUIET_RECONNECT_OPTIONAL_CMD_IDS = (
    PROBE_STATISTICS_CMD_ID,  # 0x0258 — probe watermarks
    STATISTICS_B_CMD_ID,  # 0x025A — power-board runtime
    MEASUREMENTS_CMD_ID,  # 0x0259 — cell/statistics counters
    TEMPERATURE_CMD_ID,  # 0x0009 — board/water temperature
    GPO_SETUP_CMD_ID,  # 0x0514 — GPO outlet setup + built-in name enum
    VALVE_SETUP_CMD_ID,  # 0x0516 — valve setup + built-in name enum
)

SETTLED_OPTIONAL_CMD_IDS = QUIET_RECONNECT_OPTIONAL_CMD_IDS + (
    TIMER_CAPABILITIES_CMD_ID,  # 0x0190 — timer slot counts
    TIMER_SETUP_CMD_ID,  # 0x0191 — timer season/setup summary
    SOLAR_STATE_CMD_ID,  # 0x04B2 — solar state (accessory-dependent; optional tier)
)

SETTLED_OPTIONAL_SKIP_IF_SETTLED_CMD_IDS = (MEASUREMENTS_CMD_ID,)

# Backward-compatible union. External callers and tests may inspect this for
# the full set of cmds the startup refresh CAN send, but the runtime now
# splits dispatch by tier.
STARTUP_REFRESH_CMD_IDS = MANDATORY_REFRESH_CMD_IDS + SETTLED_OPTIONAL_CMD_IDS

LIGHT_MODES = {
    1: "Auto",
    2: "Off",
    3: "On",
}
VENDOR_LIGHT_ACTIONS = {
    "Auto": 2,
    "Off": 3,
    "On": 4,
}
ACTION_MODES = {1: "Off", 2: "Auto", 3: "On"}

BLADE_TARGET_ID = 6
JETS_TARGET_ID = 7
EQUIPMENT_SETUP_CMD_IDS = {GPO_SETUP_CMD_ID, VALVE_SETUP_CMD_ID}
RECEIVE_WATCHDOG_INTERVAL_SECONDS = 2.0
# The relay can legitimately pause inbound frames for 10-15s during noisy Wi-Fi
# / controller handover windows while still accepting our outbound keepalive +
# controller-time poll. Server-sent disconnect/dataexchangeerror remains fatal;
# this watchdog is only for genuinely silent sockets.
RECEIVE_WATCHDOG_TIMEOUT_SECONDS = 30.0
# Steady-state keepalive + 0x0002 poll interval. Matches the vendor's ~4.15s
# (was 2s). Halves our steady-state read volume; do NOT exceed 4s (0x0002 is
# the application-layer heartbeat the relay uses to detect dead clients).
KEEPALIVE_POLL_SECONDS = 4.0

# Vendor "vomit" opening handshake (PerformVomitLegacyAsync). The app opens
# EVERY cloud connection by reading 0x006B then 0x0005 as the first
# post-connect traffic. The controller replies with a 0x0005 sentinel
# (prefix 0x01, first data byte 0x02); on the vendor side this sets
# haveSentLogicalConnectionNotification / DeviceState=Connected — a
# server-recognised "logical connection" marker. 2026-07-06 capture audit:
# the live app sends this on every connect and reconnects aggressively (2-8s
# gaps) yet holds 900s+ sessions, while we (who never sent it) get reaped
# early — the half-open-connection hypothesis.
# Sentinel: a received 0x0005 frame with response prefix 0x01 whose first
# payload byte is 0x02 (msgCommand==5, msgType==1, msgData[0]==2).
_VOMIT_SENTINEL_PREFIX = 0x01
_VOMIT_SENTINEL_FIRST_BYTE = 0x02


def _validate_choice(name: str, value: str, choices: dict[str, int]) -> int:
    """Return the encoded byte for a named protocol choice."""
    try:
        return choices[value]
    except KeyError as exc:
        raise ValueError(f"Invalid {name}: {value}") from exc


def _validate_byte_range(name: str, value: int, minimum: int, maximum: int) -> None:
    """Validate an integer byte field range."""
    if not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"Invalid {name}: {value} (expected {minimum}..{maximum})")


def build_equipment_timer_payload(
    *,
    season: Literal["Winter", "Summer"],
    slot_index: int,
    enabled: bool,
    start_hour: int,
    start_min: int,
    start_mode: Literal["Normal", "Dusk", "Dawn"] = "Normal",
    stop_hour: int,
    stop_min: int,
    stop_mode: Literal["Normal", "Dusk", "Dawn"] = "Normal",
    equipment: list[str],
    pump_speed: Literal["Low", "Medium", "High", "AI"] = "Medium",
) -> bytes:
    """Build the 13-byte TimeConfigCharacteristic3 equipment-timer payload."""
    season_code = _validate_choice("season", season, TIMER_SEASONS)
    start_mode_code = _validate_choice("start_mode", start_mode, TIMER_MODES)
    stop_mode_code = _validate_choice("stop_mode", stop_mode, TIMER_MODES)
    pump_speed_code = _validate_choice("pump_speed", pump_speed, TIMER_PUMP_SPEEDS)
    _validate_byte_range("slot_index", slot_index, 0, 7)
    _validate_byte_range("start_hour", start_hour, 0, 23)
    _validate_byte_range("stop_hour", stop_hour, 0, 23)
    _validate_byte_range("start_min", start_min, 0, 59)
    _validate_byte_range("stop_min", stop_min, 0, 59)

    enables = 0
    for name in equipment:
        try:
            enables |= EQUIPMENT_ENABLE_MASKS[name]
        except KeyError as exc:
            raise ValueError(f"Unknown equipment name: {name}") from exc
    if pump_speed == "AI" and "FilterPump" not in equipment:
        raise ValueError("pump_speed AI requires FilterPump equipment")

    return struct.pack(
        "<BBBBHBBBBBBB",
        0,  # TimerType: equipment
        slot_index,
        season_code,
        1 if enabled else 0,
        enables,
        start_mode_code,
        start_hour,
        start_min,
        stop_mode_code,
        stop_hour,
        stop_min,
        pump_speed_code,
    )


def build_lighting_timer_payload(
    *,
    season: Literal["Winter", "Summer"],
    slot_index: int,
    enabled: bool,
    start_hour: int,
    start_min: int,
    start_mode: Literal["Normal", "Dusk", "Dawn"] = "Normal",
    stop_hour: int,
    stop_min: int,
    stop_mode: Literal["Normal", "Dusk", "Dawn"] = "Normal",
    zones: list[int],
) -> bytes:
    """Build the 13-byte TimeConfigCharacteristic3 lighting-timer payload.

    Same struct as equipment timers but TimerType=1, only 2 slots, and the
    ``Enables`` ushort carries a light-zone bitmap in its low 4 bits (zone
    index 0-3 -> bit 0-3). The Parameter (pump-speed) byte is unused for
    lighting timers and sent as 0.
    """
    season_code = _validate_choice("season", season, TIMER_SEASONS)
    start_mode_code = _validate_choice("start_mode", start_mode, TIMER_MODES)
    stop_mode_code = _validate_choice("stop_mode", stop_mode, TIMER_MODES)
    _validate_byte_range("slot_index", slot_index, 0, 1)  # 2 lighting slots
    _validate_byte_range("start_hour", start_hour, 0, 23)
    _validate_byte_range("stop_hour", stop_hour, 0, 23)
    _validate_byte_range("start_min", start_min, 0, 59)
    _validate_byte_range("stop_min", stop_min, 0, 59)

    enables = 0
    for zone in zones:
        _validate_byte_range("zone", zone, 0, 3)
        enables |= 1 << zone

    return struct.pack(
        "<BBBBHBBBBBBB",
        1,  # TimerType: lighting
        slot_index,
        season_code,
        1 if enabled else 0,
        enables,
        start_mode_code,
        start_hour,
        start_min,
        stop_mode_code,
        stop_hour,
        stop_min,
        0,  # Parameter unused for lighting timers
    )


def build_heat_demand_payload(
    *,
    enabled: bool,
    window_enabled: bool,
    start_hour: int,
    start_minute: int,
    stop_hour: int,
    stop_minute: int,
    activated: bool,
) -> bytes:
    """Build the 7-byte HeaterDemandSettingsCharacteristic write payload.

    Decompiled from BusinessObjects.dll (Pack=1):
      [0] HeatDemandEnabled
      [1] EnableHeatDemandWindow
      [2] HeatDemandWindowStopHour
      [3] HeatDemandWindowStopMinute
      [4] HeatDemandWindowStartHour
      [5] HeatDemandWindowStartMinute
      [6] HeatDemandActivated
    """
    _validate_byte_range("start_hour", start_hour, 0, 23)
    _validate_byte_range("start_minute", start_minute, 0, 59)
    _validate_byte_range("stop_hour", stop_hour, 0, 23)
    _validate_byte_range("stop_minute", stop_minute, 0, 59)

    return struct.pack(
        "<BBBBBBB",
        1 if enabled else 0,
        1 if window_enabled else 0,
        stop_hour,
        stop_minute,
        start_hour,
        start_minute,
        1 if activated else 0,
    )


async def _sleep_briefly(delay_seconds: float) -> None:
    """Sleep helper isolated for bounded post-write refreshes."""
    await asyncio.sleep(delay_seconds)


def _assemble_custom_name(buf: dict[Any, Any] | None) -> str | None:
    """Reassemble a 3-fragment custom name buffer (valve/light-zone/etc.).

    Returns None until all three message fragments (0,1,2) have arrived, then
    joins them, truncates to the declared length, decodes UTF-8 and strips NULs.
    """
    if not buf or not all(message_number in buf for message_number in (0, 1, 2)):
        return None
    target_length = int(buf.get("length", 0))
    if target_length == 0:
        return ""
    assembled = buf[0] + buf[1] + buf[2]
    return assembled[:target_length].decode("utf-8", errors="replace").rstrip("\x00")


class HaloWebSocketClient:
    """Simple WebSocket client for Halo cloud protocol v2.0."""

    def __init__(
        self,
        serial_number: str,
        username: str,
        password: str,
        url: str = SIGNALLING_WS_URL,
    ):
        self.serial_number = serial_number
        self.username = username
        self.password = password
        self.url = url
        self.data = ChlorinatorLiveData()
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._receive_task: Optional[asyncio.Task] = None
        self._keepalive_task: Optional[asyncio.Task] = None
        self._watchdog_task: asyncio.Task | None = None
        self._request_all_data_task: Optional[asyncio.Task] = None
        self._running = False
        self._connected_at: datetime.datetime | None = None
        self._last_message_received_at: float = 0.0
        self.last_session_duration_seconds: float | None = None
        self._last_logged_state_hex: str | None = None
        self._ssl_context: ssl.SSLContext | None = None
        self._ssl_context_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._session_id = uuid.uuid4().hex[:8]
        self._trace_until: float | None = None
        self._disable_startup_refresh = os.environ.get(
            "HALO_DISABLE_STARTUP_REFRESH", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._trace_connect_window = os.environ.get(
            "HALO_TRACE_CONNECT_WINDOW", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        self._quiet_reconnect = False
        self._first_payload_seen = False
        self._seen_state_this_session = False
        self._seen_manual_speed_this_session = False
        self._last_optional_refresh: datetime.datetime | None = None
        self._last_timer_config_refresh: datetime.datetime | None = None
        self._valve_custom_name_buffer: dict[int, dict[Any, Any]] = {}
        self._light_zone_name_buffer: dict[int, dict[Any, Any]] = {}
        self._gpo_custom_name_buffer: dict[int, dict[Any, Any]] = {}
        self._relay_custom_name_buffer: dict[int, dict[Any, Any]] = {}
        self.on_data: Optional[Callable[[dict[str, Any]], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None

    def _trace_connect_event(
        self, direction: str, label: str, detail: str = ""
    ) -> None:
        """Log a short connect-window trace for early-session analysis."""
        if not self._trace_connect_window or self._trace_until is None:
            return
        loop = asyncio.get_running_loop()
        now = loop.time()
        if now > self._trace_until:
            return
        remaining = self._trace_until - now
        elapsed = 10.0 - remaining
        if detail:
            LOGGER.info(
                "[trace %s +%.3fs] %s %s %s",
                self._session_id,
                elapsed,
                direction,
                label,
                detail,
            )
        else:
            LOGGER.info(
                "[trace %s +%.3fs] %s %s",
                self._session_id,
                elapsed,
                direction,
                label,
            )

    # Vendor handshake fingerprint (from captured vendor-app traffic):
    #   user-agent: Dart/3.9 (dart:io)
    #   cache-control: no-cache
    #   accept-encoding: gzip
    #   sec-websocket-extensions: permessage-deflate; client_max_window_bits
    #   (no Origin header)
    # The default `websockets` library handshake is recognisably bot-like
    # ("Python/x.y websockets/Z.Z" UA, no cache-control / accept-encoding,
    # different extension parameter set). 2026-05-26: hypothesis is the
    # relay UA/header-fingerprints clients and treats default-`websockets`
    # as bot-class (shorter session ceiling). This shim mimics the vendor.
    _VENDOR_USER_AGENT: ClassVar[str] = "Dart/3.9 (dart:io)"

    def _auth_headers(self) -> dict[str, str]:
        creds = base64.b64encode(
            f"{SIGNALLING_AUTH_USERNAME}:{SIGNALLING_AUTH_PASSWORD}".encode()
        ).decode()
        # Order matters less than presence, but match vendor field order where
        # we can (Connection / Upgrade are written by `websockets` itself, so
        # we only add what the library does NOT already send by default).
        return {
            "Authorization": f"Basic {creds}",
            "Cache-Control": "no-cache",
            "Accept-Encoding": "gzip",
        }

    async def _get_ssl_context(self) -> ssl.SSLContext:
        """Build the SSL context lazily off the event loop."""
        if self._ssl_context is None:
            async with self._ssl_context_lock:
                if self._ssl_context is None:
                    self._ssl_context = await asyncio.to_thread(
                        ssl.create_default_context
                    )
        return self._ssl_context

    async def _close_websocket(self) -> None:
        """Close and clear the current websocket instance."""
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                LOGGER.debug("Ignoring websocket close failure", exc_info=True)
            finally:
                self._ws = None

    def _update_rssi_from_payload(self, payload: dict[str, Any]) -> None:
        """Update chlorinator-side WiFi RSSI from signalling availability."""
        connectivity = payload.get("connectivityinfo") or {}
        rssi_raw = connectivity.get("wifirssi")
        if isinstance(rssi_raw, (int, float)):
            rssi_int = int(rssi_raw)
            if -120 <= rssi_int <= 0:
                self.data.wifi_rssi_dbm = rssi_int

    async def connect(self) -> None:
        """Connect to the chlorinator via cloud WebSocket."""
        LOGGER.info("Connecting to %s for SN %s", self.url, self.serial_number)

        try:
            availability = await self.preflight_availability()
            payload = availability.get("payload") or {}
            LOGGER.info(
                "Signalling query says available for SN %s (avail=%s, connectivity=%s)",
                self.serial_number,
                payload.get("avail"),
                payload.get("connectivityinfo"),
            )
            self._update_rssi_from_payload(payload)
        except (
            SignallingUnavailableError,
            SignallingBusyError,
            SignallingAuthenticationError,
        ):
            raise
        except Exception as exc:
            LOGGER.debug(
                "Availability preflight failed before connect for SN %s: %s",
                self.serial_number,
                exc,
                exc_info=True,
            )

        ssl_context = await self._get_ssl_context()

        try:
            websocket = await websockets.connect(
                self.url,
                additional_headers=self._auth_headers(),
                user_agent_header=self._VENDOR_USER_AGENT,
                # Vendor uses Dart's dart:io WebSocket which does NOT send
                # WS-level ping frames (only app-level JSON keepalives). The
                # `websockets` library defaults to a 20s ping_interval which
                # is an extra distinguishing fingerprint. Disable to match.
                ping_interval=None,
                open_timeout=10,
                ssl=ssl_context,
            )
        except Exception as exc:
            raise SignallingError(f"WebSocket handshake failed: {exc}") from exc

        self._ws = websocket

        try:
            connect_msg = {
                "type": "connect",
                "name": self.serial_number,
                "payload": {
                    "userName": self.username,
                    "password": self.password,
                },
            }
            await self._ws.send(_ws_json_dumps(connect_msg))
            LOGGER.debug("Sent connect message")

            try:
                resp_raw = await asyncio.wait_for(self._ws.recv(), timeout=15)
            except asyncio.TimeoutError as exc:
                try:
                    availability = await self.preflight_availability()
                except (
                    SignallingUnavailableError,
                    SignallingBusyError,
                    SignallingAuthenticationError,
                ) as preflight_err:
                    raise preflight_err from exc
                except Exception as preflight_exc:
                    LOGGER.debug(
                        "Availability recheck after connect timeout failed for SN %s: %s",
                        self.serial_number,
                        preflight_exc,
                        exc_info=True,
                    )
                else:
                    payload = availability.get("payload") or {}
                    self._update_rssi_from_payload(payload)
                    raise SignallingError(
                        "Timed out waiting for connect response despite signalling availability "
                        f"(avail={payload.get('avail')}, connectivity={payload.get('connectivityinfo')})"
                    ) from exc
                raise SignallingError("Timed out waiting for connect response") from exc

            try:
                resp = json.loads(resp_raw)
            except json.JSONDecodeError as exc:
                raise SignallingError(
                    f"Invalid connect response JSON: {resp_raw!r}"
                ) from exc

            LOGGER.debug("Connect response: %s", resp)

            if resp.get("type") != "connectresp":
                raise SignallingError(f"Unexpected connect response: {resp}")

            if int(resp.get("success", 0)) != 1:
                payload = resp.get("payload") or {}
                reason_code = int(
                    payload.get("errorcode")
                    or payload.get("errorCode")
                    or payload.get("failReason")
                    or resp.get("errorcode")
                    or resp.get("errorCode")
                    or 0
                )
                raise map_signalling_failure(reason_code)

            payload = resp.get("payload") or {}
            self.data.connected = True
            self.data.access_level = int(
                payload.get("accesslevel", payload.get("accessLevel", 0))
            )
            build_info = payload.get("buildinfo", {}) or {}
            self.data.protocol_version = str(build_info.get("protocol", "unknown"))
            self.data.firmware_version = str(build_info.get("pbver", "unknown"))

            LOGGER.info(
                "Connected! Protocol v%s, firmware v%s, access level %d",
                self.data.protocol_version,
                self.data.firmware_version,
                self.data.access_level,
            )

            self._running = True
            self._connected_at = datetime.datetime.now(tz=datetime.timezone.utc)
            self._first_payload_seen = False
            self._seen_state_this_session = False
            self._seen_manual_speed_this_session = False
            self.data.logical_connection_established = False
            self._trace_until = asyncio.get_running_loop().time() + 10.0
            self._trace_connect_event("tx", "connect")
            self._trace_connect_event(
                "rx",
                "connectresp",
                (
                    f"protocol={self.data.protocol_version} "
                    f"firmware={self.data.firmware_version} "
                    f"access={self.data.access_level}"
                ),
            )
            self._last_message_received_at = time.monotonic()
            self._receive_task = asyncio.create_task(self._receive_loop())
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            self._watchdog_task = asyncio.create_task(self._receive_watchdog())
            self._request_all_data_task = asyncio.create_task(self._request_all_data())
        except Exception:
            self.data.connected = False
            self._running = False
            await self._cancel_background_tasks()
            await self._close_websocket()
            raise

    def _has_settled_bootstrap_snapshot(self) -> bool:
        """Return whether this session has enough fresh state to skip bootstrap reads."""
        if not self._seen_state_this_session:
            return False
        if self.data.mode not in {"Off", "Auto", "On"}:
            return False
        if self.data.mode == "On":
            return self._seen_manual_speed_this_session and self.data.pump_speed in {
                "Low",
                "Medium",
                "High",
            }
        return True

    def _quiet_reconnect_missing_core_cmds(self) -> list[int]:
        """Return optional quiet-reconnect reads for fields still missing."""
        cmd_ids: list[int] = []
        if (
            self.data.highest_ph_measured is None
            or self.data.highest_orp_measured is None
        ):
            cmd_ids.append(PROBE_STATISTICS_CMD_ID)
        if self.data.power_board_runtime_hours is None:
            cmd_ids.append(STATISTICS_B_CMD_ID)
        if self.data.cell_running_hours is None:
            cmd_ids.append(MEASUREMENTS_CMD_ID)
        if (
            self.data.board_temperature_c is None
            or self.data.water_temperature_c is None
        ):
            cmd_ids.append(TEMPERATURE_CMD_ID)
        if len(self.data.gpo_names) < 4:
            cmd_ids.append(GPO_SETUP_CMD_ID)
        if len(self.data.valve_names) < 4:
            cmd_ids.append(VALVE_SETUP_CMD_ID)
        return cmd_ids

    async def _perform_vomit_handshake(self) -> bool:
        """Emulate the vendor PerformVomitLegacyAsync opening handshake.

        The vendor app opens EVERY cloud connection by reading 0x006B then
        0x0005 as the first post-connect traffic. The controller replies with a
        0x0005 sentinel (response prefix 0x01, first payload byte 0x02) which
        marks the session as a fully-established "logical connection"
        server-side. We never sent these, which may leave the relay treating us
        as a half-open client and reaping us early (2026-07-06 capture audit).

        Like the vendor, we do NOT block subsequent reads on the sentinel — the
        vendor fires its stats reads immediately after 0x0005 and only uses the
        sentinel for its own UI state. The sentinel is recognised
        asynchronously by the receive loop (see _update_data), which sets
        ``data.logical_connection_established`` for diagnostics. Returning the
        two reads promptly preserves the quiet-reconnect fast path (Astral
        sessions can last only 15-25s, so mandatory reads must not wait).

        A prior (2026-05-21) attempt sent these synchronously inside connect()
        and caused connectresp timeouts; this runs in the background
        startup-refresh task, after connect() has already returned.
        """
        # Let the receive loop spin up so it can catch the sentinel reply.
        await asyncio.sleep(0.1)
        if not self._running:
            return False
        try:
            await self.request_data(VOMIT_LEGACY_CMD_ID, source="vomit_legacy")
            await asyncio.sleep(0.05)  # vendor spaces the two reads ~50ms
            await self.request_data(VOMIT_SENTINEL_CMD_ID, source="vomit_sentinel")
        except asyncio.CancelledError:
            raise
        except Exception as err:
            LOGGER.debug("Vomit handshake send failed: %s", err)
            return False
        return True

    async def _request_all_data(self) -> None:
        """Send a light, app-like post-connect refresh.

        Earlier builds sent a broad catch-all burst after every reconnect. In
        real-world Astral sessions that was noisy and likely contributed to the
        fragile reconnect/disconnect cycle. The Halo app appears to do a much
        smaller follow-up read set after connect, so mirror that pattern here.
        """
        if self._disable_startup_refresh:
            LOGGER.info("Startup refresh disabled by HALO_DISABLE_STARTUP_REFRESH")
            self._trace_connect_event("tx", "startup_refresh", "disabled")
            return

        # Vendor parity: open EVERY connect with the legacy vomit handshake
        # (0x006B + 0x0005) as the first post-connect reads, before mandatory
        # or optional refreshes. See _perform_vomit_handshake.
        if self._running:
            self._trace_connect_event("tx", "vomit_handshake", "start")
            await self._perform_vomit_handshake()
        if not self._running:
            return

        if self._quiet_reconnect:
            LOGGER.info(
                "Quiet reconnect: firing mandatory reads immediately (no delay)"
            )
            self._trace_connect_event(
                "tx", "startup_refresh", "quiet_reconnect_mandatory"
            )
            self._quiet_reconnect = False

            mandatory = list(MANDATORY_REFRESH_CMD_IDS)
            for cmd_id in mandatory:
                if not self._running:
                    break
                try:
                    await self.request_data(cmd_id, source="mandatory_refresh")
                    await asyncio.sleep(0.3)
                except asyncio.CancelledError:
                    raise
                except Exception as err:
                    LOGGER.debug("Mandatory refresh 0x%04x failed: %s", cmd_id, err)

            optional = [
                cmd_id
                for cmd_id in self._quiet_reconnect_missing_core_cmds()
                if cmd_id not in set(mandatory)
            ]
            if optional and self._running:
                LOGGER.info(
                    "Quiet reconnect: session alive, firing %d optional reads",
                    len(optional),
                )
                for cmd_id in optional:
                    if not self._running:
                        break
                    try:
                        if cmd_id in EQUIPMENT_SETUP_CMD_IDS:
                            continue
                        await self.request_data(cmd_id, source="optional_refresh")
                        await asyncio.sleep(0.5)
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:
                        LOGGER.debug("Optional refresh 0x%04x failed: %s", cmd_id, err)
                if (
                    any(cmd_id in EQUIPMENT_SETUP_CMD_IDS for cmd_id in optional)
                    and self._running
                ):
                    try:
                        await self.request_equipment_setup()
                        await self.request_custom_names_vomit()
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:
                        LOGGER.debug("Optional equipment setup refresh failed: %s", err)
            return

        # 2026-05-21: Vendor app fires reads IMMEDIATELY after connectresp.
        # Our old 10s sleep caused the controller (or relay) to treat us as a
        # dead client and kill the session. The H4-disprove test confirmed:
        # with NO reads, session died at 13.2s. With reads, 18-31s. Vendor
        # app fires 5 reads within 200ms and gets a full state stream back.
        #
        # Match vendor: fire mandatory reads immediately, no pre-wait.
        await asyncio.sleep(0.1)  # yield to let receive loop start
        if not self._running:
            return

        # NOTE(2026-07-06): the vomit handshake (0x006B + 0x0005) IS now
        # implemented and runs first for every connect — see
        # _perform_vomit_handshake, invoked at the top of this method. The
        # 2026-05-21 attempt was reverted for a connectresp-timeout because it
        # ran synchronously inside connect(); the current version runs in this
        # background task after connect() returns, which avoids that failure.

        # Build the staged refresh in two tiers:
        # - MANDATORY cmds always fire (controller doesn't push these on its
        #   own); they're listed first so heater / temperature / timer data
        #   refreshes even when settled-bootstrap optimisation skips the rest.
        # - SETTLED_OPTIONAL cmds fire only if we don't already have a settled
        #   bootstrap snapshot.
        settled = self._first_payload_seen and self._has_settled_bootstrap_snapshot()
        if settled:
            LOGGER.info(
                "Settled bootstrap snapshot present; sending only mandatory non-pushed cmds in startup refresh"
            )
            self._trace_connect_event(
                "tx", "startup_refresh", "mandatory_only_settled_snapshot"
            )
            app_like_cmds = MANDATORY_REFRESH_CMD_IDS
        else:
            app_like_cmds = STARTUP_REFRESH_CMD_IDS

        LOGGER.debug("Requesting extra-quiet staged post-connect refresh...")
        for cmd_id in app_like_cmds:
            if not self._running:
                break
            # Only the SETTLED_OPTIONAL tier is allowed to short-circuit when
            # the snapshot becomes settled mid-loop. MANDATORY cmds always
            # complete — they're for data the controller does not push on its
            # own keepalive cycle.
            if (
                cmd_id in SETTLED_OPTIONAL_SKIP_IF_SETTLED_CMD_IDS
                and self._has_settled_bootstrap_snapshot()
            ):
                LOGGER.info(
                    "Skipping optional 0x%04x because core state is now settled",
                    cmd_id,
                )
                self._trace_connect_event(
                    "tx", "startup_refresh", "optional_skipped_settled_snapshot"
                )
                continue
            try:
                if cmd_id in EQUIPMENT_SETUP_CMD_IDS:
                    continue
                await self.request_data(cmd_id, source="startup_refresh")
                await asyncio.sleep(0.1)  # vendor fires at ~50ms spacing
            except asyncio.CancelledError:
                raise
            except Exception as err:
                LOGGER.debug("Staged post-connect read 0x%04x failed: %s", cmd_id, err)

        if self._running:
            try:
                await self.request_full_timer_config(source="startup_sweep")
            except asyncio.CancelledError:
                raise
            except Exception as err:
                LOGGER.debug("Startup timer slot sweep failed: %s", err)

        if not settled and self._running:
            try:
                await self.request_equipment_setup()
                await self.request_custom_names_vomit()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                LOGGER.debug("Staged equipment setup refresh failed: %s", err)

        LOGGER.debug("Extra-quiet staged post-connect refresh complete")

        await self.request_data(
            MEASUREMENTS_CMD_ID,
            source="acid_debug_measurements",
        )

    async def query_availability(self) -> dict[str, Any]:
        """Check chlorinator availability without connecting."""
        ssl_context = await self._get_ssl_context()
        async with websockets.connect(
            self.url,
            additional_headers=self._auth_headers(),
            user_agent_header=self._VENDOR_USER_AGENT,
            ping_interval=None,
            open_timeout=10,
            ssl=ssl_context,
        ) as ws:
            await ws.send(_ws_json_dumps({"type": "query", "name": self.serial_number}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            return resp

    async def preflight_availability(self) -> dict[str, Any]:
        """Query signalling availability and raise mapped failures when explicit."""
        resp = await self.query_availability()
        if resp.get("type") not in ("query", "queryresp"):
            raise SignallingError(f"Unexpected query response: {resp}")
        if int(resp.get("success", 0)) != 1:
            payload = resp.get("payload") or {}
            reason_code = int(
                payload.get("errorcode")
                or payload.get("errorCode")
                or payload.get("failReason")
                or resp.get("errorcode")
                or resp.get("errorCode")
                or 0
            )
            raise map_signalling_failure(reason_code)
        return resp

    async def send_command(
        self, command_bytes: bytes, *, source: str = "command"
    ) -> None:
        """Send raw command bytes to the chlorinator."""
        if not self._ws or not self.data.connected:
            raise RuntimeError("Not connected")

        self._trace_connect_event("tx", source, command_bytes.hex())
        msg = {
            "type": "dataexchange",
            "payload": {
                "data": base64.b64encode(command_bytes).decode("ascii"),
            },
        }
        async with self._send_lock:
            await self._ws.send(_ws_json_dumps(msg))

    async def request_data(self, cmd_id: int, *, source: str = "request_data") -> None:
        """Request a fresh snapshot for a single characteristic."""
        read_cmd = bytes([0x02]) + struct.pack("<H", cmd_id) + bytes(17)
        await self.send_command(read_cmd, source=f"{source}(0x{cmd_id:04x})")

    async def request_timer_config(
        self,
        *,
        timer_type: int,
        slot_index: int | None = None,
        timer_index: int | None = None,
        season: Literal["Winter", "Summer"] | None = None,
        is_summer: bool | None = None,
        source: str = "timer_config_read",
    ) -> None:
        """Request one 0x0193 timer slot using the vendor selector framing."""
        if slot_index is None:
            if timer_index is None:
                raise ValueError("slot_index or timer_index is required")
            slot_index = timer_index
        if season is None:
            season = "Summer" if is_summer else "Winter"
        _validate_byte_range("timer_type", timer_type, 0, 1)
        _validate_byte_range("slot_index", slot_index, 0, 7)
        summer_byte = _validate_choice("season", season, TIMER_SEASONS)
        read_cmd = (
            bytes([0x02])
            + struct.pack("<H", TIMER_CONFIG_CMD_ID)
            + bytes([timer_type, slot_index, summer_byte])
            + bytes(14)
        )
        await self.send_command(
            read_cmd,
            source=(
                f"{source}(type={timer_type},slot={slot_index},season={season})"
            ),
        )

    async def request_full_timer_config(
        self,
        season: Literal["Winter", "Summer"] | None = None,
        *,
        source: str = "timer_config_sweep",
        enforce_rate_limit: bool = False,
    ) -> None:
        """Request all 8 equipment timer slots for one season."""
        selected_season = season or self.data.timer_season or "Winter"
        _validate_choice("season", selected_season, TIMER_SEASONS)
        if enforce_rate_limit:
            now = datetime.datetime.now(tz=datetime.timezone.utc)
            last = self._last_timer_config_refresh
            if last is not None and (now - last).total_seconds() < 30:
                remaining = 30 - (now - last).total_seconds()
                raise RuntimeError(
                    f"Timer refresh rate-limited; try again in {remaining:.0f}s"
                )
            self._last_timer_config_refresh = now

        for index in range(8):
            await self.request_timer_config(
                timer_type=0,
                timer_index=index,
                is_summer=selected_season == "Summer",
                source=source,
            )
            await _sleep_briefly(0.2)

    async def request_custom_names_vomit(self) -> None:
        """Trigger controller to emit custom-name chunks for equipment."""
        await self.request_data(CUSTOM_NAMES_VOMIT_CMD_ID, source="custom_names_vomit")

    async def _send_selector_read(
        self,
        cmd_id: int,
        byte3: int,
        byte4: int,
        *,
        source: str = "selector_read",
    ) -> None:
        """Request a per-device setup record with selector bytes at offsets 3-4."""
        read_cmd = (
            bytes([0x02])
            + struct.pack("<H", cmd_id)
            + bytes([byte3 & 0xFF, byte4 & 0xFF])
            + bytes(15)
        )
        await self.send_command(
            read_cmd,
            source=f"{source}(0x{cmd_id:04x},{byte3},{byte4})",
        )

    async def request_equipment_setup(self) -> None:
        """Request all GPO + valve setup records (names, enabled flags, timer use)."""
        for device_type, index in [(7, 0), (7, 1), (8, 0), (8, 1)]:
            await self._send_selector_read(GPO_SETUP_CMD_ID, device_type, index)
            await _sleep_briefly(0.3)

        for index in range(4):
            await self._send_selector_read(VALVE_SETUP_CMD_ID, index, 0)
            await _sleep_briefly(0.3)

    def _assemble_valve_custom_name(self, valve_index: int) -> str | None:
        """Reassemble a valve custom name once all three chunks have arrived."""
        return _assemble_custom_name(self._valve_custom_name_buffer.get(valve_index))

    def _assemble_light_zone_name(self, zone_index: int) -> str | None:
        """Reassemble a light-zone custom name once all three chunks arrive."""
        return _assemble_custom_name(self._light_zone_name_buffer.get(zone_index))

    async def refresh_optional_values(self) -> None:
        """Re-request the optional-tier reads (statistics, temperature, watermarks).

        Does NOT re-request mandatory-tier reads — those arrive via spontaneous
        push and re-requesting them only contends with the cloud session.
        """
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        last = self._last_optional_refresh
        if last is not None and (now - last).total_seconds() < 30:
            remaining = 30 - (now - last).total_seconds()
            raise RuntimeError(f"Refresh rate-limited; try again in {remaining:.0f}s")
        self._last_optional_refresh = now

        optional_cmds = QUIET_RECONNECT_OPTIONAL_CMD_IDS + (
            TIMER_CAPABILITIES_CMD_ID,
            TIMER_SETUP_CMD_ID,
        )
        for cmd_id in dict.fromkeys(optional_cmds):
            if cmd_id in EQUIPMENT_SETUP_CMD_IDS:
                continue
            await self.request_data(cmd_id, source="refresh_optional")
            await _sleep_briefly(0.3)
        await self.request_equipment_setup()
        await self.request_custom_names_vomit()

    async def refresh_timer_config(self) -> None:
        """Re-request the current season's equipment timer slots with a rate limit."""
        await self.request_full_timer_config(enforce_rate_limit=True, source="refresh_timer_config")

    def get_equipment_name(self, slot_index: int) -> str:
        """Map an equipment slot index (0..12) to its display name."""
        if slot_index == 0:
            return "Spa" if self.data.spa_selection else "Pool"
        if slot_index == 1:
            return "Filter Pump"
        if slot_index == 2:
            return "Heater"
        if 3 <= slot_index <= 6:
            gpo_slot = slot_index - 2
            custom_name = self.data.gpo_custom_names.get(gpo_slot)
            if self.data.gpo_is_custom_name.get(gpo_slot) and custom_name:
                return custom_name
            return self.data.gpo_names.get(gpo_slot, f"GPO{gpo_slot}")
        if 7 <= slot_index <= 10:
            valve_slot = slot_index - 6
            custom_name = self.data.valve_custom_names.get(valve_slot - 1)
            if self.data.valve_is_custom_name.get(valve_slot) and custom_name:
                return custom_name
            return self.data.valve_names.get(valve_slot, f"Valve{valve_slot}")
        if 11 <= slot_index <= 12:
            relay_index = slot_index - 11
            custom_name = self.data.relay_custom_names.get(relay_index)
            if custom_name:
                return custom_name
            return f"Relay{slot_index - 10}"
        return f"Equipment{slot_index}"

    def _cmd_seen_recently(self, cmd_id: int, now: datetime.datetime) -> float | None:
        last_seen = self.data.cmd_last_seen.get(cmd_id)
        if last_seen is None:
            return None
        return (now - last_seen).total_seconds()

    async def _refresh_after_action(self, *cmd_ids: int) -> None:
        """Request a bounded state refresh after a control write.

        Refreshes are opt-in. The controller usually pushes fresh state on the
        next keepalive cycle, so skip reads when that push has already arrived.
        """
        await _sleep_briefly(2.0)
        refresh_ids = list(cmd_ids or ())
        seen: set[int] = set()
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        for cmd_id in refresh_ids:
            if cmd_id in seen:
                continue
            seen.add(cmd_id)
            age_seconds = self._cmd_seen_recently(cmd_id, now)
            if age_seconds is not None and age_seconds < 2.0:
                LOGGER.debug(
                    "Skipping post-action re-read of 0x%04x: spontaneous push arrived %.1fs ago",
                    cmd_id,
                    age_seconds,
                )
                continue
            try:
                await self.request_data(cmd_id)
                await _sleep_briefly(0.3)
            except Exception:
                LOGGER.debug(
                    "Post-action refresh for cmd 0x%04x failed", cmd_id, exc_info=True
                )

    async def _refresh_characteristics(
        self,
        *cmd_ids: int,
        initial_delay_seconds: float = 2.0,
    ) -> None:
        """Request a bounded refresh for specific characteristics."""
        await _sleep_briefly(initial_delay_seconds)
        seen: set[int] = set()
        now = datetime.datetime.now(tz=datetime.timezone.utc)
        for cmd_id in cmd_ids:
            if cmd_id in seen:
                continue
            seen.add(cmd_id)
            age_seconds = self._cmd_seen_recently(cmd_id, now)
            if age_seconds is not None and age_seconds < 2.0:
                LOGGER.debug(
                    "Skipping post-action re-read of 0x%04x: spontaneous push arrived %.1fs ago",
                    cmd_id,
                    age_seconds,
                )
                continue
            try:
                await self.request_data(cmd_id)
                await _sleep_briefly(0.3)
            except Exception:
                LOGGER.debug(
                    "Post-action refresh for cmd 0x%04x failed",
                    cmd_id,
                    exc_info=True,
                )

    async def _send_padded_write(
        self,
        cmd_id: int,
        payload: bytes,
        *,
        refresh_cmd_ids: tuple[int, ...] = (),
        refresh_delay_seconds: float = 2.0,
    ) -> None:
        """Send a write-style command with the Halo app's padded 17-byte payload body."""
        if len(payload) > 17:
            raise ValueError(
                f"Payload too long for cmd 0x{cmd_id:04x}: {len(payload)} > 17"
            )
        command = bytes([0x03]) + struct.pack("<H", cmd_id) + payload.ljust(17, b"\x00")
        await self.send_command(command, source=f"write(0x{cmd_id:04x})")
        if refresh_cmd_ids:
            await self._refresh_characteristics(
                *refresh_cmd_ids,
                initial_delay_seconds=refresh_delay_seconds,
            )

    async def send_action(
        self,
        action: int,
        data: bytes = b"",
        *,
        refresh_cmd_ids: tuple[int, ...] = (),
    ) -> None:
        """Send a chlorinator action command with explicit AppAction data bytes."""
        if len(data) > 15:
            raise ValueError(
                f"AppAction data too long for cloud frame: {len(data)} > 15"
            )
        if action in (4, 5, 6):
            LOGGER.info("Sending manual pump-speed action %s", action)
        payload = bytes([action]) + data.ljust(16, b"\x00")
        command = bytes([0x03]) + struct.pack("<H", ACTION_CMD_ID) + payload
        await self.send_command(command, source=f"action({action},{data.hex()})")
        if refresh_cmd_ids:
            await self._refresh_after_action(*refresh_cmd_ids)

    async def send_action_int(self, action: int, value: int = 0) -> None:
        """Legacy wrapper; prefer send_action(data=...) for new code."""
        await self.send_action(action, struct.pack("<i", value))

    async def set_light_mode(self, mode: str, zone: int = 0) -> None:
        """Set light mode using the app-confirmed 0x01F5 path."""
        action = VENDOR_LIGHT_ACTIONS.get(mode)
        if action is None:
            raise ValueError(f"Invalid light mode: {mode}")
        if not 0 <= zone <= 255:
            raise ValueError(f"Invalid light zone: {zone}")
        await self._send_padded_write(
            LIGHT_CMD_ID,
            bytes([action, zone]),
        )
        self.data.light_mode = mode

    async def set_light_colour(self, colour: str, zone: int = 0) -> None:
        """Set a lighting zone's colour/effect (SetZoneColour, action 5).

        ``colour`` is a model-specific colour/effect name; the wire byte is the
        vendor per-model ``LightColour.Value`` resolved from the controller's
        reported lighting model. Frame: 0x01F5 [5, zone, value].
        """
        if self.data.lighting_model is None:
            raise RuntimeError(
                "Lighting model unknown; wait for capabilities (0x012D) before "
                "setting a colour."
            )
        if not 0 <= zone <= 3:
            raise ValueError(f"Invalid light zone: {zone}")
        value = resolve_light_colour(self.data.lighting_model, colour)
        if value is None:
            valid = ", ".join(available_light_colours(self.data.lighting_model))
            raise ValueError(
                f"Colour {colour!r} is not valid for this light model. "
                f"Valid options: {valid}"
            )
        await self._send_padded_write(LIGHT_CMD_ID, bytes([5, zone, value]))
        LOGGER.info("Set light zone %d colour to %s (value %d)", zone, colour, value)

    async def synchronise_light_colour(self, zone: int = 0) -> None:
        """Synchronise all zones to a zone's colour (SynchroniseZoneColour, 6)."""
        if not 0 <= zone <= 3:
            raise ValueError(f"Invalid light zone: {zone}")
        await self._send_padded_write(LIGHT_CMD_ID, bytes([6, zone]))

    async def set_equipment_mode(self, target_id: int, mode: str) -> None:
        """Set a generic equipment target using the 0x01F4 action path."""
        action = {value: key for key, value in ACTION_MODES.items()}.get(mode)
        if action is None:
            raise ValueError(f"Invalid equipment mode: {mode}")
        await self.send_action(action, bytes([target_id]))
        if target_id == BLADE_TARGET_ID:
            self.data.blade_mode = mode
        elif target_id == JETS_TARGET_ID:
            self.data.jets_mode = mode

    async def set_blade_mode(self, mode: str) -> None:
        await self.set_equipment_mode(BLADE_TARGET_ID, mode)

    async def set_jets_mode(self, mode: str) -> None:
        await self.set_equipment_mode(JETS_TARGET_ID, mode)

    async def set_heater_off(self) -> None:
        await self._send_padded_write(
            HEATER_CMD_ID,
            b"\x04",
            refresh_cmd_ids=(HEATER_STATE_CMD_ID,),
            refresh_delay_seconds=2.5,
        )
        self.data.heater_mode = "Off"
        self.data.heater_on = False

    async def set_heater_on(self) -> None:
        await self._send_padded_write(
            HEATER_CMD_ID,
            b"\x05",
            refresh_cmd_ids=(HEATER_STATE_CMD_ID,),
            refresh_delay_seconds=2.5,
        )
        self.data.heater_mode = "On"
        self.data.heater_on = True

    async def increase_heater_setpoint(self) -> None:
        await self._send_padded_write(
            HEATER_CMD_ID,
            b"\x06",
            refresh_cmd_ids=(HEATER_STATE_CMD_ID,),
            refresh_delay_seconds=2.5,
        )
        if self.data.heater_setpoint_c is not None:
            self.data.heater_setpoint_c = min(self.data.heater_setpoint_c + 1, 40)

    async def decrease_heater_setpoint(self) -> None:
        await self._send_padded_write(
            HEATER_CMD_ID,
            b"\x07",
            refresh_cmd_ids=(HEATER_STATE_CMD_ID,),
            refresh_delay_seconds=2.5,
        )
        if self.data.heater_setpoint_c is not None:
            self.data.heater_setpoint_c = max(self.data.heater_setpoint_c - 1, 10)

    async def set_heater_setpoint(self, target_c: int) -> None:
        """Set the heater setpoint by stepping to the requested temperature."""
        if self.data.heater_setpoint_c is None:
            raise RuntimeError("Heater setpoint is not known yet")

        bounded_target = max(10, min(40, int(target_c)))
        current = int(self.data.heater_setpoint_c)
        if bounded_target == current:
            return

        step_fn = (
            self.increase_heater_setpoint
            if bounded_target > current
            else self.decrease_heater_setpoint
        )
        for _ in range(abs(bounded_target - current)):
            await step_fn()
            await _sleep_briefly(0.15)

    async def sync_controller_clock(
        self, when: datetime.datetime | None = None
    ) -> None:
        """Sync the controller date and time using the app-confirmed writes."""
        local_now = (
            when.astimezone()
            if when is not None
            else datetime.datetime.now().astimezone()
        )
        time_payload = bytes(
            [
                local_now.second,
                local_now.minute,
                local_now.hour,
                local_now.isoweekday(),
            ]
        )
        date_payload = bytes(
            [
                local_now.day,
                local_now.month,
                local_now.year % 100,
            ]
        )
        await self._send_padded_write(TIME_CMD_ID, time_payload)
        await _sleep_briefly(0.2)
        await self._send_padded_write(DATE_CMD_ID, date_payload)

    def _equipment_timer_readback_matches(
        self,
        season: Literal["Winter", "Summer"],
        slot_index: int,
        expected: dict[str, int | bool],
    ) -> bool | None:
        """Return whether a parsed timer slot matches, or None if unavailable."""
        actual = (
            self.data.timer_configs_summer
            if season == "Summer"
            else self.data.timer_configs_winter
        ).get(slot_index)
        if actual is None:
            return None

        comparisons = {
            "timer_type": expected["timer_type"],
            "slot_index": expected["slot_index"],
            "timer_mode": expected["timer_mode"],
            "active": expected["active"],
            "equipment_flags": expected["equipment_flags"],
            "start_mode": expected["start_mode"],
            "start_hour": expected["start_hour"],
            "start_minute": expected["start_minute"],
            "stop_mode": expected["stop_mode"],
            "stop_hour": expected["stop_hour"],
            "stop_minute": expected["stop_minute"],
            "speed_code": expected["speed_code"],
        }
        for key, expected_value in comparisons.items():
            if actual.get(key) != expected_value:
                LOGGER.warning(
                    "Equipment timer read-back mismatch for slot %s: %s expected %r got %r",
                    slot_index,
                    key,
                    expected_value,
                    actual.get(key),
                )
                return False
        return True

    async def write_equipment_timer(
        self,
        *,
        season: Literal["Winter", "Summer"],
        slot_index: int,
        enabled: bool,
        start_hour: int,
        start_min: int,
        start_mode: Literal["Normal", "Dusk", "Dawn"] = "Normal",
        stop_hour: int,
        stop_min: int,
        stop_mode: Literal["Normal", "Dusk", "Dawn"] = "Normal",
        equipment: list[str],
        pump_speed: Literal["Low", "Medium", "High", "AI"] = "Medium",
    ) -> None:
        """Write a full equipment timer slot configuration.

        Writes cmd 0x0193 with TimerType=0. The controller persists immediately;
        no save command needed. Reads back the slot afterwards to verify.
        """
        payload = build_equipment_timer_payload(
            season=season,
            slot_index=slot_index,
            enabled=enabled,
            start_hour=start_hour,
            start_min=start_min,
            start_mode=start_mode,
            stop_hour=stop_hour,
            stop_min=stop_min,
            stop_mode=stop_mode,
            equipment=equipment,
            pump_speed=pump_speed,
        )
        if (stop_hour * 60 + stop_min) < (start_hour * 60 + start_min):
            LOGGER.warning(
                "Writing overnight equipment timer slot %s (%02d:%02d -> %02d:%02d)",
                slot_index,
                start_hour,
                start_min,
                stop_hour,
                stop_min,
            )

        await self._send_padded_write(TIMER_CONFIG_CMD_ID, payload)
        await self.request_timer_config(
            timer_type=0,
            slot_index=slot_index,
            season=season,
            source="equipment_timer_verify",
        )
        await _sleep_briefly(0.3)

        expected = {
            "timer_type": 0,
            "slot_index": slot_index,
            "timer_mode": TIMER_SEASONS[season],
            "active": bool(enabled),
            "equipment_flags": struct.unpack_from("<H", payload, 4)[0],
            "start_mode": TIMER_MODES[start_mode],
            "start_hour": start_hour,
            "start_minute": start_min,
            "stop_mode": TIMER_MODES[stop_mode],
            "stop_hour": stop_hour,
            "stop_minute": stop_min,
            "speed_code": TIMER_PUMP_SPEEDS[pump_speed],
        }
        readback_matches = self._equipment_timer_readback_matches(
            season,
            slot_index,
            expected,
        )
        if readback_matches is not False:
            timer_configs = (
                self.data.timer_configs_summer
                if season == "Summer"
                else self.data.timer_configs_winter
            )
            equipment_flags = int(expected["equipment_flags"])
            timer_configs[slot_index] = {
                "timer_type": 0,
                "season": season,
                "slot_index": slot_index,
                "timer_mode": expected["timer_mode"],
                "active": expected["active"],
                "equipment_flags": equipment_flags,
                "equipment_enabled": [
                    name
                    for name, mask in EQUIPMENT_ENABLE_MASKS.items()
                    if equipment_flags & mask
                ],
                "start_mode": expected["start_mode"],
                "start_time": f"{start_hour:02d}:{start_min:02d}",
                "start_hour": start_hour,
                "start_minute": start_min,
                "stop_mode": expected["stop_mode"],
                "stop_time": f"{stop_hour:02d}:{stop_min:02d}",
                "stop_hour": stop_hour,
                "stop_minute": stop_min,
                "duration_minutes": None,
                "overnight": (stop_hour * 60 + stop_min)
                < (start_hour * 60 + start_min),
                "speed": pump_speed,
                "speed_code": expected["speed_code"],
            }

    async def write_lighting_timer(
        self,
        *,
        season: Literal["Winter", "Summer"],
        slot_index: int,
        enabled: bool,
        start_hour: int,
        start_min: int,
        start_mode: Literal["Normal", "Dusk", "Dawn"] = "Normal",
        stop_hour: int,
        stop_min: int,
        stop_mode: Literal["Normal", "Dusk", "Dawn"] = "Normal",
        zones: list[int],
    ) -> None:
        """Write a lighting timer slot (cmd 0x0193 with TimerType=1).

        Mirrors the equipment-timer write but targets the 2 lighting slots and
        encodes a light-zone bitmap (zone 0-3) instead of equipment flags. The
        controller persists immediately; the slot is re-read afterwards.
        """
        payload = build_lighting_timer_payload(
            season=season,
            slot_index=slot_index,
            enabled=enabled,
            start_hour=start_hour,
            start_min=start_min,
            start_mode=start_mode,
            stop_hour=stop_hour,
            stop_min=stop_min,
            stop_mode=stop_mode,
            zones=zones,
        )
        if (stop_hour * 60 + stop_min) < (start_hour * 60 + start_min):
            LOGGER.warning(
                "Writing overnight lighting timer slot %s (%02d:%02d -> %02d:%02d)",
                slot_index,
                start_hour,
                start_min,
                stop_hour,
                stop_min,
            )
        await self._send_padded_write(TIMER_CONFIG_CMD_ID, payload)
        await self.request_timer_config(
            timer_type=1,
            slot_index=slot_index,
            season=season,
            source="lighting_timer_verify",
        )

    def _require_timer_setup_field(self, name: str, value: Optional[int]) -> int:
        if value is None:
            raise RuntimeError(
                f"Cannot write timer season because {name} is not known yet"
            )
        return value

    async def write_timer_season(self, season: Literal["Winter", "Summer"]) -> None:
        """Switch the active timer season (cmd 0x0191 TimerSetup write).

        Equipment timers + light timers use independent slot sets per season;
        this controls which set is active.
        """
        season_code = _validate_choice("season", season, TIMER_SEASONS)
        if self.data.timer_dusk_time_hour is None or self.data.timer_dawn_time_hour is None:
            await self.request_data(TIMER_SETUP_CMD_ID, source="timer_season_read_before_write")
            await _sleep_briefly(0.3)

        payload = struct.pack(
            "<BBBBBBB",
            self._require_timer_setup_field(
                "timer_no_timer_model", self.data.timer_no_timer_model
            ),
            self._require_timer_setup_field(
                "timer_master_is_present", self.data.timer_master_is_present
            ),
            season_code,
            self._require_timer_setup_field(
                "timer_dusk_time_hour", self.data.timer_dusk_time_hour
            ),
            self._require_timer_setup_field(
                "timer_dusk_time_mins", self.data.timer_dusk_time_mins
            ),
            self._require_timer_setup_field(
                "timer_dawn_time_hour", self.data.timer_dawn_time_hour
            ),
            self._require_timer_setup_field(
                "timer_dawn_time_mins", self.data.timer_dawn_time_mins
            ),
        )
        await self._send_padded_write(
            TIMER_SETUP_CMD_ID,
            payload,
            refresh_cmd_ids=(TIMER_SETUP_CMD_ID,),
            refresh_delay_seconds=0.3,
        )
        self.data.timer_season = season
        self.data.timer_season_source = "write"

    async def set_mode_off(self) -> None:
        await self.send_action(1, b"")
        self.data.mode = "Off"
        self.data.info_message = "Off"

    async def set_mode_auto(self) -> None:
        await self.send_action(2, b"")
        self.data.mode = "Auto"

    async def set_mode_manual(self) -> None:
        await self.send_action(3, b"")
        self.data.mode = "On"

    async def set_pump_speed_low(self) -> None:
        await self.send_action(4, b"")
        self.data.mode = "On"
        self.data.pump_speed = "Low"

    async def set_pump_speed_medium(self) -> None:
        await self.send_action(5, b"")
        self.data.mode = "On"
        self.data.pump_speed = "Medium"

    async def set_pump_speed_high(self) -> None:
        await self.send_action(6, b"")
        self.data.mode = "On"
        self.data.pump_speed = "High"

    async def select_pool(self) -> None:
        await self.send_action(7, b"")

    async def select_spa(self) -> None:
        await self.send_action(8, b"")

    async def dismiss_info_message(self, message_code: int = 0) -> None:
        data = struct.pack("<H", message_code) if message_code else b""
        await self.send_action(9, data)

    async def disable_acid_dosing(self, minutes: int = 0) -> None:
        if minutes > 0:
            await self.send_action(
                11,
                struct.pack("<i", minutes),
                refresh_cmd_ids=(MAINTENANCE_STATE_CMD_ID,),
            )
            self.data.acid_dosing_state = "OffForPeriod"
            self.data.acid_dosing_hold_minutes = minutes
        else:
            await self.send_action(
                10,
                b"\x01",
                refresh_cmd_ids=(MAINTENANCE_STATE_CMD_ID,),
            )
            self.data.acid_dosing_state = "OffIndefinitely"
            self.data.acid_dosing_hold_minutes = None

    async def enable_acid_dosing(self) -> None:
        current_state = getattr(self.data, "acid_dosing_state", None)
        if current_state == "OffIndefinitely":
            await self.send_action(
                10,
                b"\x00",
                refresh_cmd_ids=(MAINTENANCE_STATE_CMD_ID,),
            )
        else:
            await self.send_action(
                11,
                struct.pack("<i", 0),
                refresh_cmd_ids=(MAINTENANCE_STATE_CMD_ID,),
            )
        self.data.acid_dosing_state = "ResumeNow"
        self.data.acid_dosing_hold_minutes = 0

    async def start_sanitise_until_timer_tomorrow(self) -> None:
        """Sanitise until the first scheduled timer slot tomorrow."""
        await self.send_action(
            22,
            b"",
            refresh_cmd_ids=(MAINTENANCE_STATE_CMD_ID,),
        )

    async def start_filter_for_period(self, minutes: int) -> None:
        """Filter (no chlorination) for the given period in minutes."""
        if minutes <= 0 or minutes > 10080:
            raise ValueError(f"Invalid filter period: {minutes}")
        await self.send_action(
            23,
            struct.pack("<i", minutes),
            refresh_cmd_ids=(MAINTENANCE_STATE_CMD_ID,),
        )

    async def start_sanitise_for_period(self, minutes: int) -> None:
        """Sanitise (with chlorination) for the given period in minutes."""
        if minutes <= 0 or minutes > 10080:
            raise ValueError(f"Invalid sanitise period: {minutes}")
        await self.send_action(
            31,
            struct.pack("<i", minutes),
            refresh_cmd_ids=(MAINTENANCE_STATE_CMD_ID,),
        )

    # TODO(v1.7): Add action 24 (SanitiseAndCleanForPeriod) only after a
    # capture confirms the controller has a configured cleaner-pump GPO.

    async def abort_maintenance_task(self) -> None:
        """Cancel any active sanitise/filter maintenance task."""
        await self.send_action(
            21,
            b"",
            refresh_cmd_ids=(MAINTENANCE_STATE_CMD_ID,),
        )

    def _require_known_setpoint_value(
        self, name: str, value: Optional[int | float]
    ) -> int | float:
        if value is None:
            raise RuntimeError(
                f"Cannot build setpoint write because {name} is not known yet. "
                "Wait for the initial setpoint snapshot or provide all required values explicitly."
            )
        return value

    async def write_setpoints(
        self,
        *,
        ph_setpoint: Optional[float] = None,
        orp_setpoint: Optional[int] = None,
        pool_chlorine_setpoint: Optional[int] = None,
        acid_setpoint: Optional[int] = None,
        spa_chlorine_setpoint: Optional[int] = None,
    ) -> None:
        """Write cmd 0x0066 setpoints with bounds validation.

        Notes:
        - The app/research shows pH/ORP changes use a dedicated setpoint write path.
        - Cloud write behaviour for this path is still being confirmed live, so keep
          usage cautious.
        - Because the packet carries all setpoint fields together, omitted values are
          filled from the latest known live snapshot.
        """
        command = build_setpoint_command(
            ph_setpoint=(
                ph_setpoint
                if ph_setpoint is not None
                else self._require_known_setpoint_value(
                    "ph_setpoint", self.data.ph_setpoint
                )
            ),
            orp_setpoint=(
                orp_setpoint
                if orp_setpoint is not None
                else self._require_known_setpoint_value(
                    "orp_setpoint", self.data.orp_setpoint
                )
            ),
            pool_chlorine_setpoint=(
                pool_chlorine_setpoint
                if pool_chlorine_setpoint is not None
                else self._require_known_setpoint_value(
                    "pool_chlorine_setpoint", self.data.pool_chlorine_setpoint
                )
            ),
            acid_setpoint=(
                acid_setpoint
                if acid_setpoint is not None
                else self._require_known_setpoint_value(
                    "acid_setpoint", self.data.acid_setpoint
                )
            ),
            spa_chlorine_setpoint=(
                spa_chlorine_setpoint
                if spa_chlorine_setpoint is not None
                else self._require_known_setpoint_value(
                    "spa_chlorine_setpoint", self.data.spa_chlorine_setpoint
                )
            ),
        )
        await self.send_command(command, source="setpoints")

    async def set_ph_setpoint(self, value: float) -> None:
        await self.write_setpoints(ph_setpoint=value)

    async def set_orp_setpoint(self, value: int) -> None:
        await self.write_setpoints(orp_setpoint=value)

    def _require_known_heat_demand_value(self, name: str, value):
        if value is None:
            raise RuntimeError(
                f"Cannot build heat-demand write because {name} is not known yet. "
                "Wait for the initial 0x0451 snapshot or provide all required "
                "values explicitly."
            )
        return value

    async def request_heat_demand_settings(
        self, *, source: str = "heat_demand_read"
    ) -> None:
        """Request a fresh snapshot of cmd 0x0451 (HeaterDemandSettings)."""
        await self.request_data(HEAT_DEMAND_CMD_ID, source=source)

    async def write_heat_demand(
        self,
        *,
        enabled: Optional[bool] = None,
        window_enabled: Optional[bool] = None,
        start_hour: Optional[int] = None,
        start_minute: Optional[int] = None,
        stop_hour: Optional[int] = None,
        stop_minute: Optional[int] = None,
        activated: Optional[bool] = None,
    ) -> None:
        """Write cmd 0x0451 HeaterDemandSettings (heater schedule).

        Decompiled vendor path: `WriteHeaterDemandSettingsCharacteristic` on
        BLE message 1105 (= cmd 0x0451), Pack=1 7-byte struct. Cloud framing
        mirrors equipment-timer writes: 0x03 prefix + cmd LE + payload padded
        to 17 bytes.

        Omitted fields are filled from the latest known live snapshot so a
        partial update does not zero unrelated fields. The controller persists
        immediately; we issue an explicit read-back and warn on mismatch.
        """
        resolved_enabled = (
            enabled
            if enabled is not None
            else bool(
                self._require_known_heat_demand_value(
                    "heat_demand_enabled", self.data.heat_demand_enabled
                )
            )
        )
        resolved_window_enabled = (
            window_enabled
            if window_enabled is not None
            else bool(
                self._require_known_heat_demand_value(
                    "heat_demand_window_enabled",
                    self.data.heat_demand_window_enabled,
                )
            )
        )
        resolved_start_hour = (
            start_hour
            if start_hour is not None
            else int(
                self._require_known_heat_demand_value(
                    "heat_demand_window_start_hour",
                    self.data.heat_demand_window_start_hour,
                )
            )
        )
        resolved_start_minute = (
            start_minute
            if start_minute is not None
            else int(
                self._require_known_heat_demand_value(
                    "heat_demand_window_start_minute",
                    self.data.heat_demand_window_start_minute,
                )
            )
        )
        resolved_stop_hour = (
            stop_hour
            if stop_hour is not None
            else int(
                self._require_known_heat_demand_value(
                    "heat_demand_window_stop_hour",
                    self.data.heat_demand_window_stop_hour,
                )
            )
        )
        resolved_stop_minute = (
            stop_minute
            if stop_minute is not None
            else int(
                self._require_known_heat_demand_value(
                    "heat_demand_window_stop_minute",
                    self.data.heat_demand_window_stop_minute,
                )
            )
        )
        resolved_activated = (
            activated
            if activated is not None
            else bool(
                self._require_known_heat_demand_value(
                    "heat_demand_activated", self.data.heat_demand_activated
                )
            )
        )

        payload = build_heat_demand_payload(
            enabled=resolved_enabled,
            window_enabled=resolved_window_enabled,
            start_hour=resolved_start_hour,
            start_minute=resolved_start_minute,
            stop_hour=resolved_stop_hour,
            stop_minute=resolved_stop_minute,
            activated=resolved_activated,
        )

        if (resolved_stop_hour * 60 + resolved_stop_minute) < (
            resolved_start_hour * 60 + resolved_start_minute
        ):
            LOGGER.warning(
                "Writing overnight heat-demand window (%02d:%02d -> %02d:%02d)",
                resolved_start_hour,
                resolved_start_minute,
                resolved_stop_hour,
                resolved_stop_minute,
            )

        await self._send_padded_write(HEAT_DEMAND_CMD_ID, payload)

        expected = {
            "heat_demand_enabled": resolved_enabled,
            "heat_demand_window_enabled": resolved_window_enabled,
            "heat_demand_window_start_hour": resolved_start_hour,
            "heat_demand_window_start_minute": resolved_start_minute,
            "heat_demand_window_stop_hour": resolved_stop_hour,
            "heat_demand_window_stop_minute": resolved_stop_minute,
            "heat_demand_activated": resolved_activated,
        }

        def _current_actual() -> dict[str, object]:
            return {
                "heat_demand_enabled": self.data.heat_demand_enabled,
                "heat_demand_window_enabled": self.data.heat_demand_window_enabled,
                "heat_demand_window_start_hour": self.data.heat_demand_window_start_hour,
                "heat_demand_window_start_minute": self.data.heat_demand_window_start_minute,
                "heat_demand_window_stop_hour": self.data.heat_demand_window_stop_hour,
                "heat_demand_window_stop_minute": self.data.heat_demand_window_stop_minute,
                "heat_demand_activated": self.data.heat_demand_activated,
            }

        # 2026-05-21: live testing on Rob's controller showed the immediate
        # readback after a 0x0451 write returns the PRE-WRITE state. The
        # controller stores the new values but the first readback samples a
        # pre-write snapshot (vendor protocol race — ack-then-update). End
        # state is correct so the write IS landing; we just need to wait past
        # the race window before comparing. Strategy:
        #   1. Settle for HEAT_DEMAND_SETTLE_DELAY_SECONDS, request_data, brief wait
        #   2. If mismatch, re-poll once after HEAT_DEMAND_RECONFIRM_DELAY_SECONDS
        #      — mismatch usually clears by the second poll
        #   3. Only warn if the second poll also disagrees
        await _sleep_briefly(HEAT_DEMAND_SETTLE_DELAY_SECONDS)
        await self.request_heat_demand_settings(source="heat_demand_verify")
        await _sleep_briefly(HEAT_DEMAND_READBACK_PROCESS_SECONDS)

        actual = _current_actual()
        mismatches = {
            k: (actual[k], expected[k]) for k in expected if actual[k] != expected[k]
        }
        if mismatches:
            LOGGER.debug(
                "Heat-demand first-readback differs (ack-then-update race); "
                "re-polling: %s",
                mismatches,
            )
            await _sleep_briefly(HEAT_DEMAND_RECONFIRM_DELAY_SECONDS)
            await self.request_heat_demand_settings(
                source="heat_demand_reconfirm"
            )
            await _sleep_briefly(HEAT_DEMAND_READBACK_PROCESS_SECONDS)
            actual = _current_actual()
            mismatches = {
                k: (actual[k], expected[k])
                for k in expected
                if actual[k] != expected[k]
            }
            if mismatches:
                LOGGER.warning(
                    "Heat-demand readback mismatch after write (persisted past "
                    "reconfirm poll): %s",
                    mismatches,
                )

    async def _cancel_background_tasks(
        self, current_task: asyncio.Task | None = None
    ) -> None:
        """Cancel client background tasks, skipping the current task when needed."""
        task_names = (
            "_request_all_data_task",
            "_receive_task",
            "_keepalive_task",
            "_watchdog_task",
        )
        for task_name in task_names:
            task = getattr(self, task_name)
            if task is None or task is current_task:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                LOGGER.debug("Ignoring task shutdown failure", exc_info=True)
            finally:
                setattr(self, task_name, None)
        if current_task is None:
            self._request_all_data_task = None
            self._receive_task = None
            self._keepalive_task = None
            self._watchdog_task = None

    async def disconnect(self) -> None:
        """Disconnect cleanly."""
        self._running = False
        await self._cancel_background_tasks()

        if self._ws:
            try:
                await self._ws.send(_ws_json_dumps({"type": "disconnect"}))
            except Exception:
                pass

        await self._close_websocket()
        self.data.connected = False
        self._connected_at = None

    async def _handle_fatal_session(self, reason: str) -> None:
        """Cleanly terminate a session that cannot continue."""
        LOGGER.warning("Fatal session event (%s), closing WebSocket session", reason)

        if self._connected_at is not None:
            self.last_session_duration_seconds = (
                datetime.datetime.now(tz=datetime.timezone.utc) - self._connected_at
            ).total_seconds()

        self._running = False
        self.data.connected = False
        self._connected_at = None

        current_task = asyncio.current_task()
        if self._watchdog_task is current_task:
            self._watchdog_task = None
        for task_name in (
            "_request_all_data_task",
            "_keepalive_task",
            "_watchdog_task",
        ):
            task = getattr(self, task_name)
            if task is None or task is current_task:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                LOGGER.debug("Ignoring task shutdown failure", exc_info=True)
            finally:
                setattr(self, task_name, None)

        ws = self._ws
        if ws is not None:
            try:
                await asyncio.wait_for(
                    ws.send(_ws_json_dumps({"type": "disconnect"})),
                    timeout=1.0,
                )
            except Exception:
                LOGGER.debug("Ignoring fatal-session disconnect send failure", exc_info=True)

        await self._close_websocket()

    async def _keepalive_loop(self) -> None:
        """Send a JSON keepalive + a 0x0002 controller-time poll every ~4s.

        Vendor app sends a keepalive AND a 0x0002 (controller time) read every
        ~4.15s; the 0x0002 poll IS the application-level heartbeat — the relay
        kills clients that look dead at the application layer (hard invariant).

        2026-05-26: an A/B soak (both run orders) showed poll cadence (2s vs
        the vendor's 4s) and read-variety have NO effect on session length —
        the server-initiated disconnect is driven by reconnect churn pinning
        us in the relay's penalty tier. That is handled by the coordinator's
        escalating post-disconnect cooldown, not here. (An earlier idle-read
        rotator was removed: it never fired, and read variety was disproven.)

        2026-05-26 (Rob): we briefly removed the paired 0x0002 poll to test the
        bare-keepalive (decompile-vendor) theory. RESULT: a clean post-power-
        cycle session died at 11.5s with ~4.1s of app-layer silence before the
        kick — bare keepalive is NOT sufficient; the server wants application-
        layer traffic. So the 0x0002 poll is RESTORED and CONFIRMED needed
        (empirically, even though the decompiled keepalive timer alone doesn't
        send it — the real vendor session has 0x0002 from another timer).
        Cadence is the vendor's ~4s (was 2s).
        """
        try:
            while self._running and self._ws:
                await asyncio.sleep(KEEPALIVE_POLL_SECONDS)
                if self._ws and self._running:
                    try:
                        self._trace_connect_event("tx", "keepalive")
                        async with self._send_lock:
                            await self._ws.send(_ws_json_dumps({"type": "keepalive"}))
                        await self.request_data(TIME_CMD_ID, source="keepalive_poll")
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        LOGGER.warning(
                            "Keepalive send failed (%s: %s); treating session as dead",
                            type(exc).__name__,
                            exc,
                        )
                        # Schedule fatal handler without awaiting (we are the
                        # keepalive task; awaiting it would cancel ourselves).
                        asyncio.create_task(
                            self._handle_fatal_session("keepalive_send_failed")
                        )
                        return
        except asyncio.CancelledError:
            pass

    async def _receive_watchdog(self) -> None:
        """Detect dead connections when the server stops sending messages."""
        try:
            while self._running and self.data.connected:
                await asyncio.sleep(RECEIVE_WATCHDOG_INTERVAL_SECONDS)
                if not self._running or not self.data.connected:
                    break
                elapsed = time.monotonic() - self._last_message_received_at
                if elapsed > RECEIVE_WATCHDOG_TIMEOUT_SECONDS:
                    LOGGER.warning(
                        "Receive watchdog: no message in %.1fs, treating connection as dead",
                        elapsed,
                    )
                    await self._handle_fatal_session("receive_watchdog_timeout")
                    break
        except asyncio.CancelledError:
            pass

    async def _handle_incoming_message(self, msg: dict[str, Any]) -> bool:
        """Handle one inbound server message. Return False to end the receive loop."""
        self._last_message_received_at = time.monotonic()
        msg_type = msg.get("type")

        if msg_type == "dataexchange":
            payload = msg.get("payload", {})
            data_b64 = payload.get("data", "")
            if data_b64:
                data_bytes = base64.b64decode(data_b64)
                self._first_payload_seen = True
                self._trace_connect_event("rx", "dataexchange", data_bytes.hex())

                if len(data_bytes) >= 16:
                    cmd_id = struct.unpack_from("<H", data_bytes, 1)[0]
            
                    if cmd_id == MEASUREMENTS_CMD_ID:
                        dosing_raw_bytes = data_bytes[14:16]
                        dosing_raw_value = struct.unpack_from("<H", data_bytes, 14)[0]

            
                parsed = parse_data_payload(data_bytes)
                self._update_data(parsed, data_bytes)
                if self.on_data:
                    try:
                        self.on_data(parsed)
                    except Exception:
                        LOGGER.exception("on_data callback failed")

        elif msg_type == "dataexchangeerror":
            LOGGER.warning("Received dataexchangeerror from server: %s", msg)
            await self._handle_fatal_session("dataexchangeerror")
            return False

        elif msg_type == "keepalive":
            self._trace_connect_event("rx", "keepalive")
            LOGGER.debug("Keepalive received")

        elif msg_type == "disconnect":
            self._trace_connect_event("rx", "disconnect")
            LOGGER.info("Server sent disconnect message: %s", msg)
            self._quiet_reconnect = True
            await self._handle_fatal_session("server_disconnect")
            return False

        else:
            LOGGER.debug("Unknown message type: %s", msg_type)

        return True

    async def _receive_loop(self) -> None:
        """Listen for messages from the WebSocket."""
        try:
            while self._running and self._ws:
                try:
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    LOGGER.debug("No message in 30s, still connected")
                    continue

                msg = json.loads(raw)
                if not await self._handle_incoming_message(msg):
                    break

        except websockets.ConnectionClosed:
            LOGGER.info("WebSocket connection closed")
        except asyncio.CancelledError:
            pass
        except Exception as err:
            LOGGER.error("Receive loop error: %s", err)
        finally:
            current_task = asyncio.current_task()
            if self._connected_at is not None:
                self.last_session_duration_seconds = (
                    datetime.datetime.now(tz=datetime.timezone.utc) - self._connected_at
                ).total_seconds()
            self.data.connected = False
            self.data.logical_connection_established = False
            self._running = False
            self._connected_at = None
            await self._cancel_background_tasks(current_task=current_task)
            if self._receive_task is current_task:
                self._receive_task = None
            await self._close_websocket()
            if self.on_disconnect:
                try:
                    self.on_disconnect()
                except Exception:
                    LOGGER.exception("on_disconnect callback failed")

    def _recompute_current_operating_speed(self) -> None:
        """Update conservative live operating speed from the strongest readback available."""
        current_speed: str | None = None

        if self.data.priming_active:
            current_speed = "Priming"
        elif (
            self.data.mode == "Auto"
            and self.data.ai_mode_active
            and self.data.pump_is_operating
        ):
            current_speed = "AI"
        elif self.data.info_message == "LowSpeedNoChlorinating":
            current_speed = "Low"
        elif (
            self.data.timer_profile_index is not None
            and self.data.timer_profile_index > 0
            and self.data.timer_pump_speed in {"Low", "Medium", "High"}
        ):
            current_speed = self.data.timer_pump_speed
        elif (
            self.data.mode == "On"
            and self.data.pump_is_operating
            and self.data.pump_speed in {"Low", "Medium", "High"}
        ):
            current_speed = self.data.pump_speed

        self.data.current_operating_speed = current_speed

    def _update_data(self, parsed: dict[str, Any], raw: bytes) -> None:
        """Update the live data model from a parsed payload."""
        cmd_id = parsed.get("cmd_id", 0)
        self.data.raw_payloads[cmd_id] = raw

        # Vomit-handshake sentinel: a 0x0005 response (prefix 0x01) whose first
        # payload byte is 0x02 signals the vendor "logical connection" is
        # established server-side. Record it (diagnostic; nothing blocks on it).
        if (
            cmd_id == VOMIT_SENTINEL_CMD_ID
            and parsed.get("prefix") == _VOMIT_SENTINEL_PREFIX
            and len(raw) > 3
            and raw[3] == _VOMIT_SENTINEL_FIRST_BYTE
        ):
            if not self.data.logical_connection_established:
                LOGGER.info("Vomit handshake: logical connection established")
            self.data.logical_connection_established = True

        if parsed.get("error") is not None:
            # Truncated / malformed frame: the raw payload is recorded above, but
            # do NOT feed a short frame into the model. Several branches index
            # required keys (e.g. gpo_setup -> parsed["gpo_slot"]) and would
            # KeyError, killing the receive loop — and thus the whole session —
            # on a single bad frame from the relay.
            return

        now = datetime.datetime.now(tz=datetime.timezone.utc)
        self.data.last_update = now
        if cmd_id:
            self.data.cmd_last_seen[cmd_id] = now

        if parsed.get("type") == "state":
            self._seen_state_this_session = True
            state_hex = raw.hex()
            if state_hex != self._last_logged_state_hex:
                self._last_logged_state_hex = state_hex
                LOGGER.debug(
                    "State 0x0068 changed: raw=%s flags=0x%04x main=%s(%s) timer=%s error=%s",
                    state_hex,
                    parsed.get("flags_raw", 0),
                    parsed.get("info_message"),
                    parsed.get("info_message_code"),
                    parsed.get("timer_info"),
                    parsed.get("error_info"),
                )
            self.data.info_message = parsed.get("info_message")
            self.data.chemistry_values_current = parsed.get(
                "chemistry_values_current",
                False,
            )
            self.data.chemistry_values_valid = parsed.get(
                "chemistry_values_valid",
                False,
            )
            self.data.sanitising_until_next_timer_tomorrow = parsed.get(
                "sanitising_until_next_timer_tomorrow",
                False,
            )
            self.data.cell_is_operating = parsed.get("cell_is_operating", False)
            self.data.cell_is_reversed = parsed.get("cell_is_reversed", False)
            self.data.cell_is_reversing = parsed.get("cell_is_reversing", False)
            self.data.cooling_fan_on = parsed.get("cooling_fan_on", False)
            self.data.dosing_pump_on = parsed.get("dosing_pump_on", False)
            self.data.ai_mode_active = parsed.get("ai_mode_active", False)
            self.data.spa_selection = parsed.get("spa_mode", False)
            self.data.cell_level = parsed.get("cell_level")
            self.data.cell_current_ma = parsed.get("cell_current_ma")
            self.data.chlorine_control_status = parsed.get("chlorine_control_status")
            self.data.ph_control_status = parsed.get("ph_control_status")
            self.data.orp_mv = parsed.get("orp_mv")
            self.data.ph_measurement = parsed.get("ph_measurement")
            self.data.timer_info = parsed.get("timer_info")
            self.data.priming_active = parsed.get("priming_active")
            self.data.priming_countdown = parsed.get("priming_countdown")
            self.data.priming_phase_code = parsed.get("priming_phase_code")
            self.data.valve_0_active = parsed.get("valve_0_active", False)
            self.data.valve_1_active = parsed.get("valve_1_active", False)
            error_code = parsed.get("error_info", 0)
            self.data.salt_error_raw = error_code
            if error_code == 0:
                self.data.error_message = "NoError"
                self.data.error_severity = None
                self.data.error_category = None
                self.data.error_reason = None
                self.data.error_action = None
            else:
                info = ERROR_CODE_TABLE.get(error_code)
                if info is None:
                    self.data.error_message = f"Unknown ({error_code})"
                    self.data.error_severity = "Unknown"
                    self.data.error_category = "Unknown"
                    self.data.error_reason = None
                    self.data.error_action = None
                else:
                    self.data.error_message = info.label
                    self.data.error_severity = info.severity
                    self.data.error_category = info.category
                    self.data.error_reason = info.reason
                    self.data.error_action = info.action
            info_code = parsed.get("info_message_code", -1)
            if info_code == 0:
                self.data.mode = "Off"
            elif info_code == 5:
                # Standby — system is in Auto but idle between timer runs
                self.data.mode = "Auto"
            elif info_code in (1, 15):
                # Plain Sanitising and LowSpeedNoChlorinating are the closest
                # observable manual-running states we currently have.
                self.data.mode = "On"
            elif info_code in (2, 3, 4, 8, 9, 10, 16, 17, 18, 19):
                self.data.mode = "Auto"
            self.data.pump_is_operating = info_code not in (0, 5, None)

            # When the controller explicitly reports LowSpeedNoChlorinating,
            # trust that as a real live low-speed state and let the manual
            # speed surface follow it unless later confirmed otherwise.
            if info_code == 15:
                self.data.pump_speed = "Low"

            self._recompute_current_operating_speed()

            # Keep manual pump-speed state sourced from config/write actions.
            # In Auto/AI operation the controller can vary speed dynamically,
            # so the manual speed setting should not be overwritten from the
            # live state text.
        elif parsed.get("type") == "config":
            if parsed.get("pump_speed") is not None:
                sub_command = parsed.get("sub_command")
                LOGGER.debug(
                    "Config pump-speed update: sub=%s code=%s mapped=%s raw=%s",
                    sub_command,
                    parsed.get("pump_speed_code"),
                    parsed.get("pump_speed"),
                    parsed.get("data_hex"),
                )
                # pump_speed Medium↔High flip fix (confirmed against a live
                # logbook capture). The 0x0324 carousel
                # cycles every 5-10s. Both sub=0x00 and sub=0x03 were being
                # accepted as authoritative for `pump_speed`, so the selector
                # flipped every rotation:
                #   sub=0x00 → stale `manual_speed_action_code` echo → set Medium
                #   sub=0x03 → authoritative `configured_speed_code` → set High
                #   …repeat…
                #
                # Fix: sub=0x03 is the ONLY authoritative source for the
                # selector. sub=0x00 carries the controller's echo of the
                # last manual-speed write (or a connect-time default seed of
                # 5/Medium) — useful as a diagnostic in
                # `manual_speed_action_code`, but NOT live state. The vendor
                # app derives the live selector from sub=0x03 only.
                if sub_command == 0x03:
                    self.data.pump_speed = parsed["pump_speed"]
                    self._seen_manual_speed_this_session = True
                # sub=0x00 intentionally does NOT update self.data.pump_speed.
                # Its action-code is still available via the
                # `manual_speed_action` / `manual_speed_action_code` parsed
                # fields for capture tooling + diagnostics.
                self._recompute_current_operating_speed()
        elif parsed.get("type") == "dosing_state":
            acid_hold_remaining_minutes = parsed.get("acid_hold_remaining_minutes", 0)
            self.data.acid_dosing_hold_minutes = acid_hold_remaining_minutes
            self.data.acid_dosing_hold_remaining_seconds = (
                acid_hold_remaining_minutes * 60
            )
            self.data.filter_sanitise_remaining_seconds = parsed.get(
                "filter_remaining_seconds",
                0,
            )
            # Update acid_dosing_state from readback (was previously write-only).
            self.data.acid_dosing_state = parsed.get(
                "acid_dosing_state",
                self.data.acid_dosing_state,
            )
        elif parsed.get("type") == "light_state":
            self.data.light_zone1_mode_raw = parsed.get("zone1_mode_raw")
            self.data.light_zone2_mode_raw = parsed.get("zone2_mode_raw")
            self.data.light_zone3_mode_raw = parsed.get("zone3_mode_raw")
            self.data.light_zone4_mode_raw = parsed.get("zone4_mode_raw")
            self.data.light_zone1_mode = parsed.get("zone1_mode")
            # Mirror zone1_mode into the top-level light_mode field so the select
            # entity shows a real value on cold start. Approximate for multi-zone
            # hardware (which we don't have to test against yet). Read-side updates
            # will overwrite optimistic post-write values once 0x012C is re-fetched.
            # For multi-zone hardware this becomes the per-zone mode source.
            if parsed.get("zone1_mode") in {"Off", "Auto", "On"}:
                self.data.light_mode = parsed.get("zone1_mode")
            self.data.light_zone2_mode = parsed.get("zone2_mode")
            self.data.light_zone3_mode = parsed.get("zone3_mode")
            self.data.light_zone4_mode = parsed.get("zone4_mode")
            self.data.light_zone1_on = parsed.get("zone1_on")
            self.data.light_zone2_on = parsed.get("zone2_on")
            self.data.light_zone3_on = parsed.get("zone3_on")
            self.data.light_zone4_on = parsed.get("zone4_on")
            self.data.light_zone1_active_source = parsed.get("zone1_active_source")
            self.data.light_zone2_active_source = parsed.get("zone2_active_source")
            self.data.light_zone3_active_source = parsed.get("zone3_active_source")
            self.data.light_zone4_active_source = parsed.get("zone4_active_source")
        elif parsed.get("type") == "light_capabilities":
            self.data.lighting_enabled = parsed.get("lighting_enabled")
            self.data.onboard_light_enabled = parsed.get("onboard_light_enabled")
            self.data.lighting_model = parsed.get("lighting_model")
            self.data.lighting_model_label = parsed.get("lighting_model_label")
            self.data.lighting_num_zones_in_use = parsed.get(
                "lighting_num_zones_in_use"
            )
            self.data.zone1_is_multicolour = parsed.get("zone1_is_multicolour")
            self.data.zone2_is_multicolour = parsed.get("zone2_is_multicolour")
            self.data.zone3_is_multicolour = parsed.get("zone3_is_multicolour")
            self.data.zone4_is_multicolour = parsed.get("zone4_is_multicolour")
        elif parsed.get("type") == "timer_pump_speed":
            self.data.timer_pump_speed = parsed.get("timer_pump_speed")
            self.data.timer_pump_speed_code = parsed.get("timer_pump_speed_code")
            self._recompute_current_operating_speed()
        elif parsed.get("type") == "gpo_setup":
            slot = parsed["gpo_slot"]
            self.data.gpo_names[slot] = parsed["name_label"]
            self.data.gpo_enabled[slot] = parsed["enabled"]
            self.data.gpo_use_timers[slot] = parsed["use_timers"]
            self.data.gpo_is_custom_name[slot] = parsed["is_custom_name"]
        elif parsed.get("type") == "valve_setup":
            slot = parsed["valve_slot"]
            self.data.valve_names[slot] = parsed["name_label"]
            self.data.valve_enabled[slot] = parsed["enabled"]
            self.data.valve_use_timers[slot] = parsed["use_timers"]
            self.data.valve_is_custom_name[slot] = parsed["is_custom_name"]
        elif parsed.get("type") == "valve_custom_name_chunk":
            if "error" not in parsed:
                valve_index = parsed["valve_index"]
                message_number = parsed["message_number"]
                buf = self._valve_custom_name_buffer.setdefault(valve_index, {})
                buf[message_number] = parsed["fragment_bytes"]
                buf["length"] = parsed["custom_name_length"]
                name = self._assemble_valve_custom_name(valve_index)
                if name is not None:
                    self.data.valve_custom_names[valve_index] = name
        elif parsed.get("type") == "light_zone_custom_name_chunk":
            if "error" not in parsed:
                zone_index = parsed["zone_index"]
                message_number = parsed["message_number"]
                buf = self._light_zone_name_buffer.setdefault(zone_index, {})
                buf[message_number] = parsed["fragment_bytes"]
                buf["length"] = parsed["custom_name_length"]
                name = self._assemble_light_zone_name(zone_index)
                if name:
                    self.data.light_zone_names[zone_index] = name
        elif parsed.get("type") == "gpo_custom_name_chunk":
            if "error" not in parsed:
                gpo_slot = parsed["gpo_slot"]
                buf = self._gpo_custom_name_buffer.setdefault(gpo_slot, {})
                buf[parsed["message_number"]] = parsed["fragment_bytes"]
                buf["length"] = parsed["custom_name_length"]
                name = _assemble_custom_name(self._gpo_custom_name_buffer.get(gpo_slot))
                if name:
                    self.data.gpo_custom_names[gpo_slot] = name
        elif parsed.get("type") == "relay_custom_name_chunk":
            if "error" not in parsed:
                relay_index = parsed["relay_index"]
                buf = self._relay_custom_name_buffer.setdefault(relay_index, {})
                buf[parsed["message_number"]] = parsed["fragment_bytes"]
                buf["length"] = parsed["custom_name_length"]
                name = _assemble_custom_name(
                    self._relay_custom_name_buffer.get(relay_index)
                )
                if name:
                    self.data.relay_custom_names[relay_index] = name
        elif parsed.get("type") == "statistics_a":
            self.data.cell_reversal_count = parsed.get("cell_reversal_count")
            self.data.cell_running_hours = parsed.get("cell_running_hours")
            self.data.low_salt_cell_running_hours = parsed.get(
                "low_salt_cell_running_hours"
            )
            self.data.previous_days_cell_load_percent = parsed.get(
                "previous_days_cell_load_percent"
            )
            self.data.acid_dosing_seconds_today = parsed.get(
                "acid_dosing_seconds_today"
            )
            self.data.filter_pump_minutes_today = parsed.get(
                "filter_pump_minutes_today"
            )
            self.data.stats_flag_byte = parsed.get("stats_flag_byte")
        elif parsed.get("type") == "probe_statistics":
            self.data.highest_ph_measured = parsed.get("highest_ph_measured")
            self.data.lowest_ph_measured = parsed.get("lowest_ph_measured")
            self.data.highest_orp_measured = parsed.get("highest_orp_measured")
            self.data.lowest_orp_measured = parsed.get("lowest_orp_measured")
        elif parsed.get("type") == "statistics_b":
            self.data.power_board_runtime_hours = parsed.get(
                "power_board_runtime_hours"
            )
        elif parsed.get("type") == "capabilities":
            self.data.ph_control_type = parsed.get("ph_control_type")
            self.data.chlorine_control_type = parsed.get("chlorine_control_type")
            self.data.min_ph_setpoint = parsed.get("min_ph_setpoint")
            self.data.max_ph_setpoint = parsed.get("max_ph_setpoint")
            self.data.min_orp_setpoint = parsed.get("min_orp_setpoint")
            self.data.max_orp_setpoint = parsed.get("max_orp_setpoint")
            self.data.min_manual_chlorine_setpoint = parsed.get(
                "min_manual_chlorine_setpoint"
            )
            self.data.max_manual_chlorine_setpoint = parsed.get(
                "max_manual_chlorine_setpoint"
            )
            self.data.min_manual_acid_setpoint = parsed.get("min_manual_acid_setpoint")
            self.data.max_manual_acid_setpoint = parsed.get("max_manual_acid_setpoint")
            if parsed.get("dosing_capable") is not None:
                self.data.dosing_capable = parsed.get("dosing_capable")

        elif parsed.get("type") == "temperature":
            if parsed.get("water_temp_c") is not None:
                self.data.water_temperature_c = parsed["water_temp_c"]
                self.data.water_temperature_precise = parsed["water_temp_c"]
            self.data.board_temperature_c = parsed.get("board_temp_c")
        elif parsed.get("type") == "setpoint":
            self.data.ph_setpoint = parsed.get("ph_setpoint")
            self.data.orp_setpoint = parsed.get("orp_setpoint")
            self.data.pool_chlorine_setpoint = parsed.get("pool_chlorine_setpoint")
            self.data.acid_setpoint = parsed.get("acid_setpoint")
            self.data.spa_chlorine_setpoint = parsed.get("spa_chlorine_setpoint")
        elif parsed.get("type") == "water_volume":
            self.data.pool_volume_l = parsed.get("pool_volume")
            self.data.pool_left_filter_l = parsed.get("pool_left_filter")
            self.data.spa_enabled = parsed.get("spa_enabled")
        elif parsed.get("type") == "equipment_mode":
            # Live GPO Off/Auto/On readback — populates Blade (GPO3) / Jets
            # (GPO4) modes from the controller (was previously write-only
            # optimistic). None = NotEnabled -> entity unavailable.
            self.data.blade_mode = parsed.get("blade_mode")
            self.data.jets_mode = parsed.get("jets_mode")
        elif parsed.get("type") == "solar_state":
            self.data.solar_roof_temp_c = parsed.get("solar_roof_temp_c")
            self.data.solar_water_temp_c = parsed.get("solar_water_temp_c")
            self.data.solar_temp_c = parsed.get("solar_temp_c")
            self.data.solar_mode = parsed.get("solar_mode")
            self.data.solar_message = parsed.get("solar_message")
            self.data.solar_pump_on = parsed.get("solar_pump_on")
            self.data.solar_flush_active = parsed.get("solar_flush_active")
        elif parsed.get("type") == "heater_state":
            self.data.heater_mode = parsed.get("heater_mode")
            self.data.heater_pump_mode = parsed.get("heater_pump_mode")
            self.data.heater_setpoint_c = parsed.get("heater_setpoint_c")
            self.data.heat_pump_mode = parsed.get("heat_pump_mode")
            self.data.heater_water_temp_c = parsed.get("heater_water_temp_c")
            self.data.heater_on = parsed.get("heater_on", False)
            self.data.heater_error = parsed.get("heater_error")
            self.data.heater_message = parsed.get("heater_message")
            self.data.heater_message_detail = parsed.get("heater_message_detail")
            self.data.heater_flame = parsed.get("heater_flame")
            self.data.heater_pressure = parsed.get("heater_pressure")
            self.data.heater_gas_valve = parsed.get("heater_gas_valve")
            self.data.heater_lockout = parsed.get("heater_lockout")
            self.data.heater_service_required = parsed.get("heater_service_required")
            self.data.heater_cooling_available = parsed.get("heater_cooling_available")
        elif parsed.get("type") == "heat_demand_settings":
            self.data.heat_demand_enabled = parsed.get("heat_demand_enabled")
            self.data.heat_demand_window_enabled = parsed.get(
                "heat_demand_window_enabled"
            )
            self.data.heat_demand_window_start_hour = parsed.get(
                "heat_demand_window_start_hour"
            )
            self.data.heat_demand_window_start_minute = parsed.get(
                "heat_demand_window_start_minute"
            )
            self.data.heat_demand_window_stop_hour = parsed.get(
                "heat_demand_window_stop_hour"
            )
            self.data.heat_demand_window_stop_minute = parsed.get(
                "heat_demand_window_stop_minute"
            )
            self.data.heat_demand_activated = parsed.get("heat_demand_activated")
        elif parsed.get("type") == "controller_time":
            controller_date = (
                self.data.controller_datetime.date()
                if self.data.controller_datetime
                else None
            )
            try:
                if controller_date is not None:
                    tzinfo = datetime.datetime.now().astimezone().tzinfo
                    self.data.controller_datetime = datetime.datetime(
                        controller_date.year,
                        controller_date.month,
                        controller_date.day,
                        parsed.get("controller_hour", 0),
                        parsed.get("controller_minute", 0),
                        parsed.get("controller_second", 0),
                        tzinfo=tzinfo,
                    )
                self.data.controller_weekday = parsed.get("controller_weekday")
            except ValueError:
                LOGGER.debug("Ignoring invalid controller time payload", exc_info=True)
        elif parsed.get("type") == "controller_date":
            existing = self.data.controller_datetime
            try:
                tzinfo = datetime.datetime.now().astimezone().tzinfo
                self.data.controller_datetime = datetime.datetime(
                    parsed.get("controller_year", 2000),
                    parsed.get("controller_month", 1),
                    parsed.get("controller_day", 1),
                    existing.hour if existing else 0,
                    existing.minute if existing else 0,
                    existing.second if existing else 0,
                    tzinfo=tzinfo,
                )
            except ValueError:
                LOGGER.debug("Ignoring invalid controller date payload", exc_info=True)
        elif parsed.get("type") == "timer_capabilities":
            self.data.equipment_timer_slots = parsed.get("equipment_timer_slots")
            self.data.lighting_timer_slots = parsed.get("lighting_timer_slots")
            self.data.timer_capability_flags = parsed.get("flags", [])
        elif parsed.get("type") == "timer_setup":
            self.data.timer_no_timer_model = parsed.get("no_timer_model")
            self.data.timer_master_is_present = parsed.get("timer_master_is_present")
            self.data.timer_dusk_time_hour = parsed.get("dusk_time_hour")
            self.data.timer_dusk_time_mins = parsed.get("dusk_time_mins")
            self.data.timer_dawn_time_hour = parsed.get("dawn_time_hour")
            self.data.timer_dawn_time_mins = parsed.get("dawn_time_mins")
            season = parsed.get("season")
            if season is not None:
                self.data.timer_season = season
                self.data.timer_season_source = "setup"
        elif parsed.get("type") == "timer_state":
            self.data.timer_profile_index = parsed.get("profile_index")
            self.data.timer_next_profile_index = parsed.get("next_profile_index")
            season = parsed.get("season")
            if season is not None:
                self.data.timer_season = season
                self.data.timer_season_source = "state"
            self._recompute_current_operating_speed()
        elif parsed.get("type") == "timer_config":
            slot_index = parsed.get("slot_index")
            if slot_index is not None:
                season = parsed.get("season")
                is_lighting = parsed.get("timer_type") == 1
                if is_lighting:
                    timer_configs = (
                        self.data.timer_configs_light_summer
                        if season == "Summer"
                        else self.data.timer_configs_light_winter
                    )
                else:
                    # Fresh equipment-timer data invalidates the restored cache.
                    self.data.timer_summary_restored = False
                    self.data.timer_summary_restored_from = None
                    self.data.timer_summary_restored_equipment_catalog = None
                    self.data.timer_summary_restored_slot_labels = None
                    timer_configs = (
                        self.data.timer_configs_summer
                        if season == "Summer"
                        else self.data.timer_configs_winter
                    )
                entry = {
                    "timer_type": parsed.get("timer_type"),
                    "season": season,
                    "slot_index": parsed.get("slot_index"),
                    "timer_mode": parsed.get("timer_mode"),
                    "active": parsed.get("active"),
                    "equipment_flags": parsed.get("equipment_flags"),
                    "equipment_enabled": parsed.get("equipment_enabled", []),
                    "has_base_timer_flag": parsed.get("has_base_timer_flag"),
                    "unknown_equipment_flags": parsed.get(
                        "unknown_equipment_flags", []
                    ),
                    "start_mode": parsed.get("start_mode"),
                    "start_time": parsed.get("start_time"),
                    "start_hour": parsed.get("start_hour"),
                    "start_minute": parsed.get("start_minute"),
                    "stop_mode": parsed.get("stop_mode"),
                    "stop_time": parsed.get("stop_time"),
                    "stop_hour": parsed.get("stop_hour"),
                    "stop_minute": parsed.get("stop_minute"),
                    "duration_minutes": parsed.get("duration_minutes"),
                    "overnight": parsed.get("overnight"),
                    "speed": parsed.get("speed"),
                    "speed_code": parsed.get("speed_code"),
                }
                if is_lighting:
                    # Lighting timers reuse the Enables bitmap as a light-zone
                    # mask (low 4 bits -> zones 0-3).
                    flags = parsed.get("equipment_flags") or 0
                    entry["zones_enabled"] = [z for z in range(4) if flags & (1 << z)]
                timer_configs[int(slot_index)] = entry
