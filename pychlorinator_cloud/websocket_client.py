"""WebSocket-only cloud client for Halo protocol v2.0."""

from __future__ import annotations

import asyncio
import base64
import datetime
import json
import logging
import ssl
import struct
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import websockets

from .const import (
    SIGNALLING_AUTH_PASSWORD,
    SIGNALLING_AUTH_USERNAME,
    SIGNALLING_FAIL_REASON_MAP,
    SIGNALLING_WS_URL,
)
from .exceptions import (
    SignallingAuthenticationError,
    SignallingBusyError,
    SignallingDosProtectionError,
    SignallingError,
    SignallingRateLimitedError,
    SignallingUnavailableError,
)
from .setpoints import build_setpoint_command

LOGGER = logging.getLogger(__name__)


@dataclass
class ChlorinatorLiveData:
    """Aggregated live data from the chlorinator."""

    connected: bool = False
    access_level: int = 0
    protocol_version: str = ""
    last_update: Optional[datetime.datetime] = None

    # State (cmd 0x0068) — mapped from BLE StateCharacteristic3
    mode: Optional[str] = None  # Off, ManualOn, Auto
    pump_speed: Optional[str] = None  # Low, Medium, High, AI (from 0x0324 sub 0x03)
    pump_is_operating: bool = False
    cell_is_operating: bool = False
    cell_is_reversed: bool = False
    cell_is_reversing: bool = False
    cooling_fan_on: bool = False
    dosing_pump_on: bool = False
    ai_mode_active: bool = False
    ph_measurement: Optional[float] = None
    ph_control_status: Optional[str] = None  # PHIsGreen, PHIsYellow, etc.
    chlorine_control_status: Optional[str] = None  # ORPIsGreen, ChlorineIsLow, etc.
    info_message: Optional[str] = None  # MainText enum
    error_message: Optional[str] = None  # SubText4ErrorInfo
    timer_info: Optional[str] = None  # SubText3
    spa_selection: bool = False
    orp_mv: Optional[int] = None  # ORP in millivolts
    water_temperature_c: Optional[float] = None  # precise from 0x0259
    cell_current_ma: Optional[int] = None
    cell_level: Optional[int] = None  # RealCelllevel (0-10)

    # Measurements (cmd 0x0259)
    water_temperature_precise: Optional[float] = None

    # Config (cmd 0x0324)
    ph_control_type: Optional[str] = None  # None, Manual, Automatic
    orp_control_type: Optional[str] = None  # None, Manual, Automatic

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
    salt_error_raw: Optional[int] = None  # Raw error code (702=LowSalt, 701=HighSalt, etc.)

    # Heater (cmd 0x044e — HeaterStateCharacteristic)
    heater_mode: Optional[str] = None  # Off, On
    heater_pump_mode: Optional[str] = None  # Off, Auto, On
    heater_setpoint_c: Optional[int] = None
    heat_pump_mode: Optional[str] = None  # Cooling, Heating, Auto
    heater_water_temp_c: Optional[float] = None
    heater_on: bool = False
    heater_error: Optional[int] = None

    # Raw payloads for debugging
    raw_payloads: dict[int, bytes] = field(default_factory=dict)


# Enums from pychlorinator halo_parsers.py — authoritative BLE definitions
SPEED_LEVELS = {0: "Low", 1: "Medium", 2: "High", 3: "AI"}

# StateCharacteristic3.MainTextValues (info_message / MainText)
MAIN_TEXT_VALUES = {
    0: "Off", 1: "Sanitising", 2: "AIModeSanitising", 3: "AIModeSampling",
    4: "Sampling", 5: "Standby", 6: "PrePurge", 7: "PostPurg",
    8: "SanitisingUntilFirstTimer", 9: "Filtering", 10: "FilteringAndCleaning",
    11: "CalibratingSensor", 12: "Backwashing", 13: "PrimingAcidPump",
    14: "ManualAcidDose", 15: "LowSpeedNoChlorinating", 16: "SanitisingForPeriod",
    17: "SanitisingAndCleaningForPeriod", 18: "LowTemperatureReducedOutput",
    19: "HeaterCooldownInProgress",
}

# StateCharacteristic3.SubText1Values (chlorine status)
SUBTEXT1_CHLORINE = {
    0: "None", 1: "ORPIsYellow", 2: "ORPWasYellow", 3: "ORPIsGreen",
    4: "ORPWasGreen", 5: "ORPIsRed", 6: "ORPWasRed", 7: "ChlorineIsLow",
    8: "ChlorineWasLow", 9: "ChlorineIsOK", 10: "ChlorineWasOK",
    11: "ChlorineIsHigh", 12: "ChlorineWasHigh",
}

# StateCharacteristic3.SubText2Values (pH status)
SUBTEXT2_PH = {
    0: "None", 1: "PHIsYellow", 2: "PHWasYellow", 3: "PHIsGreen",
    4: "PHWasGreen", 5: "PHIsRed", 6: "PHWasRed", 7: "PHIsLow",
    8: "PHWasLow", 9: "PHIsOK", 10: "PHWasOK", 11: "PHIsHigh", 12: "PHWasHigh",
}

# StateCharacteristic3.SubText3Values (timer info)
SUBTEXT3_TIMER = {
    0: "None", 1: "SanitisingPoolOff", 2: "SanitisingPoolUntil",
    3: "SanitisingSpaOff", 4: "SanitisingSpaUntil", 5: "SanitisingOff",
    6: "SanitisingUntil", 7: "PrimingFor", 8: "HeaterCooldownTimeRemaining",
}

# StateCharacteristic3.FlagsValues
FLAG_SPA_MODE = 0x01
FLAG_CELL_ON = 0x02
FLAG_CELL_REVERSED = 0x04
FLAG_COOLING_FAN_ON = 0x08
FLAG_LIGHT_OUTPUT_ON = 0x10
FLAG_DOSING_PUMP_ON = 0x20
FLAG_CELL_IS_REVERSING = 0x40
FLAG_AI_MODE_ACTIVE = 0x80

# EquipmentModeCharacteristic mode values
MODES = {0: "Off", 1: "Auto", 2: "On"}

# ChlorinatorActions — the action enum for cloud writes
# Confirmed command ID: 0x01F4 (500)
ACTION_CMD_ID = 0x01F4


def _map_signalling_failure(reason_code: int) -> SignallingError:
    """Map a signalling failure code to a typed exception."""
    reason_name = SIGNALLING_FAIL_REASON_MAP.get(reason_code, "unknown_error")
    message = f"Signalling failed with reason {reason_code} ({reason_name})"
    if reason_code == 1:
        return SignallingUnavailableError(message)
    if reason_code == 2:
        return SignallingAuthenticationError(message)
    if reason_code == 3:
        return SignallingBusyError(message)
    if reason_code == 4:
        return SignallingRateLimitedError(message)
    if reason_code == 5:
        return SignallingDosProtectionError(message)
    return SignallingError(message)


def _parse_state(data: bytes) -> dict[str, Any]:
    """Parse state characteristic (cmd 0x0068).

    Confirmed mapping to BLE StateCharacteristic3 (<BBHBBHBBB2sHB):
      byte[0]:    Flags bitfield
      byte[1]:    RealCelllevel
      byte[2:4]:  CellCurrentmA (uint16 LE)
      byte[4]:    MainText — info/state enum (Off, Sanitising, AIModeSanitising, etc.)
      byte[5]:    SubText1Chlorine — chlorine status enum
      byte[6:8]:  ORPMeasurement (uint16 LE, mV)
      byte[8]:    SubText2Ph — pH status enum
      byte[9]:    PhMeasurement (raw / 10 = pH)
      byte[10]:   SubText3TimerInfo
      byte[11:13]: SubText3BytesData
      byte[13:15]: SubText4ErrorInfo (uint16 LE)
      byte[15]:   Flag (extra flags byte)
      byte[16]:   (cloud extra byte, not in BLE struct)
    """
    if len(data) < 10:
        return {"type": "state", "raw": data.hex(), "error": "too short for state"}

    flags = data[0]
    cell_level = data[1]
    cell_current_ma = struct.unpack_from("<H", data, 2)[0]
    main_text = data[4]
    sub1_chlorine = data[5]
    orp_mv = struct.unpack_from("<H", data, 6)[0] if len(data) > 7 else 0
    sub2_ph = data[8] if len(data) > 8 else 0
    ph_raw = data[9] if len(data) > 9 else 0
    sub3_timer = data[10] if len(data) > 10 else 0
    error_info = struct.unpack_from("<H", data, 13)[0] if len(data) > 14 else 0

    info_message = MAIN_TEXT_VALUES.get(main_text, f"Unknown({main_text})")

    return {
        "type": "state",
        "flags_raw": flags,
        "spa_mode": bool(flags & FLAG_SPA_MODE),
        "cell_is_operating": bool(flags & FLAG_CELL_ON),
        "cell_is_reversed": bool(flags & FLAG_CELL_REVERSED),
        "cooling_fan_on": bool(flags & FLAG_COOLING_FAN_ON),
        "light_output_on": bool(flags & FLAG_LIGHT_OUTPUT_ON),
        "dosing_pump_on": bool(flags & FLAG_DOSING_PUMP_ON),
        "cell_is_reversing": bool(flags & FLAG_CELL_IS_REVERSING),
        "ai_mode_active": bool(flags & FLAG_AI_MODE_ACTIVE),
        "cell_level": cell_level,
        "cell_current_ma": cell_current_ma,
        "info_message": info_message,
        "info_message_code": main_text,
        "chlorine_control_status": SUBTEXT1_CHLORINE.get(sub1_chlorine, f"Unknown({sub1_chlorine})"),
        "orp_mv": orp_mv,
        "ph_control_status": SUBTEXT2_PH.get(sub2_ph, f"Unknown({sub2_ph})"),
        "ph_measurement": ph_raw / 10.0 if ph_raw > 0 else None,
        "timer_info": SUBTEXT3_TIMER.get(sub3_timer, f"Unknown({sub3_timer})"),
        "error_info": error_info,
    }


def _parse_setpoint(data: bytes) -> dict[str, Any]:
    """Parse setpoint characteristic (cmd 0x0066).

    Confirmed mapping to BLE SetPointCharacteristic: <BHBBB
      byte[0]:   PhControlSetpoint (raw / 10 = pH)
      byte[1:3]: OrpControlSetpoint (uint16 LE, mV)
      byte[3]:   PoolChlorineControlSetpoint
      byte[4]:   AcidControlSetpoint
      byte[5]:   SpaChlorineControlSetpoint
    """
    if len(data) < 5:
        return {"type": "setpoint", "raw": data.hex(), "error": "too short"}

    vals = struct.unpack_from("<BHBBB", data)
    return {
        "type": "setpoint",
        "ph_setpoint": vals[0] / 10.0,
        "orp_setpoint": vals[1],
        "pool_chlorine_setpoint": vals[2],
        "acid_setpoint": vals[3],
        "spa_chlorine_setpoint": vals[4],
    }


def _parse_measurements(data: bytes) -> dict[str, Any]:
    """Parse measurements characteristic (cmd 0x0259)."""
    if len(data) < 4:
        return {"type": "measurements", "raw": data.hex(), "error": "too short"}

    temp_raw = struct.unpack_from("<H", data, 0)[0]
    cell_current = struct.unpack_from("<H", data, 2)[0]

    return {
        "type": "measurements",
        "water_temperature_c": round(temp_raw / 50.0, 1),
        "cell_current_ma": cell_current,
    }


def _parse_settings(data: bytes) -> dict[str, Any]:
    """Parse settings characteristic (cmd 0x0064)."""
    if len(data) < 7:
        return {"type": "settings", "raw": data.hex(), "error": "too short"}

    vals = struct.unpack_from("<HBBBBBB", data)
    general = vals[0]

    return {
        "type": "settings",
        "general_flags": general,
        "cell_model": vals[1],
        "reversal_period": vals[2],
        "ai_water_turns": vals[3],
        "acid_pump_size": vals[4],
        "filter_pump_size": vals[5],
        "default_manual_speed": vals[6],
        "dosing_enabled": bool(general & 64),
        "three_speed_pump": bool(general & 128),
        "ai_enabled": bool(general & 8),
        "display_orp": bool(general & 32),
        "display_ph": bool(general & 4096),
    }


def _parse_water_volume(data: bytes) -> dict[str, Any]:
    """Parse water volume characteristic (cmd 0x0065)."""
    if len(data) < 14:
        return {"type": "water_volume", "raw": data.hex(), "error": "too short"}

    vals = struct.unpack_from("<BIHIHB", data)
    units = {0: "Litres", 1: "USGallons", 2: "ImperialGallons"}

    return {
        "type": "water_volume",
        "volume_units": units.get(vals[0], f"Unknown({vals[0]})"),
        "pool_volume": vals[1],
        "spa_volume": vals[2],
        "pool_left_filter": vals[3],
        "spa_left_filter": vals[4],
        "pool_enabled": bool(vals[5] & 1),
        "spa_enabled": bool(vals[5] & 2),
    }


def _parse_temperature(data: bytes) -> dict[str, Any]:
    """Parse temperature characteristic (cmd 0x0009)."""
    if len(data) < 11:
        return {"type": "temperature", "raw": data.hex(), "error": "too short"}

    vals = struct.unpack_from("<BBHHHHBHHB", data)
    return {
        "type": "temperature",
        "is_fahrenheit": bool(vals[0]),
        "board_temp_c": round(vals[2] / 10.0, 1),
        "water_temp_c": round(vals[3] / 10.0, 1),
        "chloro_water_temp_c": round(vals[4] / 10.0, 1),
        "solar_water_temp_c": round(vals[5] / 10.0, 1),
        "water_temp_valid": vals[6],
        "solar_roof_temp_c": round(vals[7] / 10.0, 1),
        "heater_temp_c": round(vals[8] / 10.0, 1),
    }


def _parse_heater_state(data: bytes) -> dict[str, Any]:
    """Parse heater state characteristic (cmd 0x044e / BLE 1102)."""
    if len(data) < 11:
        return {"type": "heater_state", "raw": data.hex(), "error": "too short"}

    vals = struct.unpack_from("<BBBBBBBBBHB", data)
    pump_modes = {0: "Off", 1: "Auto", 2: "On"}
    heater_modes = {0: "Off", 1: "On"}
    heatpump_modes = {0: "Cooling", 1: "Heating", 2: "Auto"}

    status = vals[0]
    return {
        "type": "heater_state",
        "heater_on": bool(status & 1),
        "heater_pressure": bool(status & 2),
        "heater_gas_valve": bool(status & 4),
        "heater_flame": bool(status & 8),
        "heater_lockout": bool(status & 16),
        "heater_pump_mode": pump_modes.get(vals[1], f"Unknown({vals[1]})"),
        "heater_mode": heater_modes.get(vals[2], f"Unknown({vals[2]})"),
        "heater_setpoint_c": vals[3],
        "heat_pump_mode": heatpump_modes.get(vals[4], f"Unknown({vals[4]})"),
        "heater_forced": vals[5],
        "heater_water_temp_valid": vals[8],
        "heater_water_temp_c": round(vals[9] / 10.0, 1),
        "heater_error": vals[10],
    }


def _parse_timer_capabilities(data: bytes) -> dict[str, Any]:
    """Parse timer capabilities (cmd 0x0190 / BLE 400).

    Confirmed so far:
    - byte[0] = equipment timer slot count (8 on Rob's unit)
    - byte[1] = lighting timer slot count (2 on Rob's unit)
    - later bytes are still only partially understood feature flags
    """
    if len(data) < 2:
        return {"type": "timer_capabilities", "raw": data.hex(), "error": "too short"}

    return {
        "type": "timer_capabilities",
        "equipment_timer_slots": data[0],
        "lighting_timer_slots": data[1],
        "flags": list(data[2:]),
    }


def _parse_timer_setup(data: bytes) -> dict[str, Any]:
    """Parse timer setup/profile state (cmd 0x0191 / BLE 401).

    Live-confirmed on Rob's unit:
    - byte[2] flips with Summer/Winter selection
      - 1 = Summer
      - 0 = Winter
    Other bytes remain under investigation.
    """
    if len(data) < 3:
        return {"type": "timer_setup", "raw": data.hex(), "error": "too short"}

    season_byte = data[2]
    return {
        "type": "timer_setup",
        "season_byte": season_byte,
        "season": {0: "Winter", 1: "Summer"}.get(season_byte, f"Unknown({season_byte})"),
        "raw_bytes": list(data),
    }


def _parse_timer_state(data: bytes) -> dict[str, Any]:
    """Parse timer state/profile pointer (cmd 0x0192 / BLE 402).

    Live-confirmed on Rob's unit:
    - byte[0] changes with season/profile selection (Summer=2, Winter=1 in current tests)
    - remaining bytes are still under investigation
    """
    if len(data) < 1:
        return {"type": "timer_state", "raw": data.hex(), "error": "too short"}

    return {
        "type": "timer_state",
        "profile_index": data[0],
        "raw_bytes": list(data),
    }


def _parse_timer_config(data: bytes) -> dict[str, Any]:
    """Parse per-slot timer config records (cmd 0x0193 / BLE 403).

    Live-confirmed on Rob's unit for slot 0:
    - byte[0]  = slot index
    - byte[3]  = active/enabled flag
    - byte[4]  = equipment bitmask
        - bit 0x04 = Heater
        - bit 0x80 = Blade
        - bit 0x02 = still present as a base/fixed timer-class bit
    - byte[7]  = start hour
    - byte[8]  = start minute
    - byte[10] = stop hour
    - byte[11] = stop minute
    - byte[12] = speed enum (0x01 observed for Medium)
    Unused/unknown bytes remain intentionally undecoded for now.
    """
    if len(data) < 13:
        return {"type": "timer_config", "raw": data.hex(), "error": "too short"}

    equipment_flags = data[4]
    return {
        "type": "timer_config",
        "slot_index": data[0],
        "active": bool(data[3]),
        "equipment_flags": equipment_flags,
        "heater_enabled": bool(equipment_flags & 0x04),
        "blade_enabled": bool(equipment_flags & 0x80),
        "start_hour": data[7],
        "start_minute": data[8],
        "stop_hour": data[10],
        "stop_minute": data[11],
        "speed_code": data[12],
        "raw_bytes": list(data),
    }


def parse_data_payload(raw: bytes) -> dict[str, Any]:
    """Parse a dataexchange payload.

    Format: byte[0]=prefix(0x01), byte[1:3]=cmd_id(uint16 LE), byte[3:]=data
    """
    if len(raw) < 3:
        return {"error": "payload too short", "raw": raw.hex()}

    prefix = raw[0]
    cmd_id = struct.unpack_from("<H", raw, 1)[0]
    data = raw[3:]

    result: dict[str, Any] = {
        "cmd_id": cmd_id,
        "cmd_hex": f"0x{cmd_id:04x}",
        "prefix": prefix,
        "data_len": len(data),
        "data_hex": data.hex(),
    }

    if cmd_id == 0x0068:
        result.update(_parse_state(data))
    elif cmd_id == 0x0066:
        result.update(_parse_setpoint(data))
    elif cmd_id == 0x0259:
        result.update(_parse_measurements(data))
    elif cmd_id == 0x0009:
        result.update(_parse_temperature(data))
    elif cmd_id == 0x0324:
        sub = data[0] if data else 0
        result["type"] = "config"
        result["sub_command"] = sub
        if sub == 0x03 and len(data) >= 3:
            result["pump_speed_code"] = data[2]
            result["pump_speed"] = SPEED_LEVELS.get(data[2], f"Unknown({data[2]})")
    elif cmd_id == 0x0190:
        result.update(_parse_timer_capabilities(data))
    elif cmd_id == 0x0191:
        result.update(_parse_timer_setup(data))
    elif cmd_id == 0x0192:
        result.update(_parse_timer_state(data))
    elif cmd_id == 0x0193:
        result.update(_parse_timer_config(data))
    elif cmd_id == 0x0064:
        result.update(_parse_settings(data))
    elif cmd_id == 0x0065:
        result.update(_parse_water_volume(data))
    elif cmd_id == 0x0002:
        result["type"] = "device_info"
    elif cmd_id == 0x0003:
        result["type"] = "device_info_2"
    elif cmd_id == 0x044E:
        result.update(_parse_heater_state(data))
    elif cmd_id == 0x0019:
        result["type"] = "unknown_0x0019"
    else:
        result["type"] = f"unknown_0x{cmd_id:04x}"

    return result


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
        self._request_all_data_task: Optional[asyncio.Task] = None
        self._running = False
        self._ssl_context: ssl.SSLContext | None = None
        self._ssl_context_lock = asyncio.Lock()
        self.on_data: Optional[Callable[[dict[str, Any]], None]] = None
        self.on_disconnect: Optional[Callable[[], None]] = None

    def _auth_headers(self) -> dict[str, str]:
        creds = base64.b64encode(
            f"{SIGNALLING_AUTH_USERNAME}:{SIGNALLING_AUTH_PASSWORD}".encode()
        ).decode()
        return {"Authorization": f"Basic {creds}"}

    async def _get_ssl_context(self) -> ssl.SSLContext:
        """Build the SSL context lazily off the event loop."""
        if self._ssl_context is None:
            async with self._ssl_context_lock:
                if self._ssl_context is None:
                    self._ssl_context = await asyncio.to_thread(ssl.create_default_context)
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

    async def connect(self) -> None:
        """Connect to the chlorinator via cloud WebSocket."""
        LOGGER.info("Connecting to %s for SN %s", self.url, self.serial_number)

        ssl_context = await self._get_ssl_context()

        try:
            websocket = await websockets.connect(
                self.url,
                additional_headers=self._auth_headers(),
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
            await self._ws.send(json.dumps(connect_msg))
            LOGGER.debug("Sent connect message")

            try:
                resp_raw = await asyncio.wait_for(self._ws.recv(), timeout=15)
            except asyncio.TimeoutError as exc:
                raise SignallingError("Timed out waiting for connect response") from exc

            try:
                resp = json.loads(resp_raw)
            except json.JSONDecodeError as exc:
                raise SignallingError(f"Invalid connect response JSON: {resp_raw!r}") from exc

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
                raise _map_signalling_failure(reason_code)

            payload = resp.get("payload") or {}
            self.data.connected = True
            self.data.access_level = int(
                payload.get("accesslevel", payload.get("accessLevel", 0))
            )
            build_info = payload.get("buildinfo", {}) or {}
            self.data.protocol_version = str(build_info.get("protocol", "unknown"))

            LOGGER.info(
                "Connected! Protocol v%s, access level %d",
                self.data.protocol_version,
                self.data.access_level,
            )

            self._running = True
            self._receive_task = asyncio.create_task(self._receive_loop())
            self._keepalive_task = asyncio.create_task(self._keepalive_loop())
            self._request_all_data_task = asyncio.create_task(self._request_all_data())
        except Exception:
            self.data.connected = False
            self._running = False
            await self._close_websocket()
            raise

    async def _request_all_data(self) -> None:
        """Send ReadForCatchAll requests for data types that don't stream automatically."""
        await asyncio.sleep(5)

        vomit_cmds = [
            9,
            100,
            101,
            102,
            104,
            105,
            106,
            600,
            601,
            602,
            1100,
            1102,
        ]

        LOGGER.info("Requesting all data types (cloud vomit)...")
        for cmd_id in vomit_cmds:
            if not self._running:
                break
            try:
                read_cmd = bytes([0x02]) + struct.pack("<H", cmd_id) + bytes(17)
                await self.send_command(read_cmd)
                await asyncio.sleep(0.3)
            except asyncio.CancelledError:
                raise
            except Exception as err:
                LOGGER.debug("ReadForCatchAll(%d) failed: %s", cmd_id, err)

        LOGGER.info("Cloud vomit complete")

    async def query_availability(self) -> dict[str, Any]:
        """Check chlorinator availability without connecting."""
        ssl_context = await self._get_ssl_context()
        async with websockets.connect(
            self.url,
            additional_headers=self._auth_headers(),
            open_timeout=10,
            ssl=ssl_context,
        ) as ws:
            await ws.send(json.dumps({"type": "query", "name": self.serial_number}))
            resp = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
            return resp

    async def send_command(self, command_bytes: bytes) -> None:
        """Send raw command bytes to the chlorinator."""
        if not self._ws or not self.data.connected:
            raise RuntimeError("Not connected")

        msg = {
            "type": "dataexchange",
            "payload": {
                "data": base64.b64encode(command_bytes).decode("ascii"),
            },
        }
        await self._ws.send(json.dumps(msg))

    async def send_action(self, action: int, period_minutes: int = 0) -> None:
        """Send a chlorinator action command."""
        payload = struct.pack("<Bi12x", action, period_minutes)
        command = bytes([0x01]) + struct.pack("<H", ACTION_CMD_ID) + payload
        await self.send_command(command)

    async def set_mode_off(self) -> None:
        await self.send_action(1)

    async def set_mode_auto(self) -> None:
        await self.send_action(2)

    async def set_mode_manual(self) -> None:
        await self.send_action(3)

    async def set_pump_speed_low(self) -> None:
        await self.send_action(4)

    async def set_pump_speed_medium(self) -> None:
        await self.send_action(5)

    async def set_pump_speed_high(self) -> None:
        await self.send_action(6)

    async def select_pool(self) -> None:
        await self.send_action(7)

    async def select_spa(self) -> None:
        await self.send_action(8)

    async def dismiss_info_message(self) -> None:
        await self.send_action(9)

    async def disable_acid_dosing(self, minutes: int = 0) -> None:
        if minutes > 0:
            await self.send_action(11, minutes)
        else:
            await self.send_action(10)

    async def enable_acid_dosing(self) -> None:
        await self.send_action(11, 0)

    def _require_known_setpoint_value(self, name: str, value: Optional[int | float]) -> int | float:
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
                else self._require_known_setpoint_value("ph_setpoint", self.data.ph_setpoint)
            ),
            orp_setpoint=(
                orp_setpoint
                if orp_setpoint is not None
                else self._require_known_setpoint_value("orp_setpoint", self.data.orp_setpoint)
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
                else self._require_known_setpoint_value("acid_setpoint", self.data.acid_setpoint)
            ),
            spa_chlorine_setpoint=(
                spa_chlorine_setpoint
                if spa_chlorine_setpoint is not None
                else self._require_known_setpoint_value(
                    "spa_chlorine_setpoint", self.data.spa_chlorine_setpoint
                )
            ),
        )
        await self.send_command(command)

    async def set_ph_setpoint(self, value: float) -> None:
        await self.write_setpoints(ph_setpoint=value)

    async def set_orp_setpoint(self, value: int) -> None:
        await self.write_setpoints(orp_setpoint=value)

    async def disconnect(self) -> None:
        """Disconnect cleanly."""
        self._running = False

        for task in (self._request_all_data_task, self._receive_task, self._keepalive_task):
            if task:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    LOGGER.debug("Ignoring task shutdown failure", exc_info=True)

        self._request_all_data_task = None
        self._receive_task = None
        self._keepalive_task = None

        if self._ws:
            try:
                await self._ws.send(json.dumps({"type": "disconnect"}))
            except Exception:
                pass

        await self._close_websocket()
        self.data.connected = False

    async def _keepalive_loop(self) -> None:
        """Send keepalive messages every 2 seconds to maintain the connection."""
        try:
            while self._running and self._ws:
                await asyncio.sleep(2)
                if self._ws and self._running:
                    try:
                        await self._ws.send(json.dumps({"type": "keepalive"}))
                    except Exception:
                        break
        except asyncio.CancelledError:
            pass

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
                msg_type = msg.get("type")

                if msg_type == "dataexchange":
                    payload = msg.get("payload", {})
                    data_b64 = payload.get("data", "")
                    if data_b64:
                        data_bytes = base64.b64decode(data_b64)
                        parsed = parse_data_payload(data_bytes)
                        self._update_data(parsed, data_bytes)
                        if self.on_data:
                            try:
                                self.on_data(parsed)
                            except Exception:
                                LOGGER.exception("on_data callback failed")

                elif msg_type == "keepalive":
                    LOGGER.debug("Keepalive received")

                elif msg_type == "disconnect":
                    LOGGER.info("Server sent disconnect")
                    break

                else:
                    LOGGER.debug("Unknown message type: %s", msg_type)

        except websockets.ConnectionClosed:
            LOGGER.info("WebSocket connection closed")
        except asyncio.CancelledError:
            pass
        except Exception as err:
            LOGGER.error("Receive loop error: %s", err)
        finally:
            self.data.connected = False
            self._running = False
            await self._close_websocket()
            if self.on_disconnect:
                try:
                    self.on_disconnect()
                except Exception:
                    LOGGER.exception("on_disconnect callback failed")

    def _update_data(self, parsed: dict[str, Any], raw: bytes) -> None:
        """Update the live data model from a parsed payload."""
        cmd_id = parsed.get("cmd_id", 0)
        self.data.raw_payloads[cmd_id] = raw

        if parsed.get("error") is None:
            self.data.last_update = datetime.datetime.now(tz=datetime.timezone.utc)

        if parsed.get("type") == "state":
            self.data.info_message = parsed.get("info_message")
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
            error_code = parsed.get("error_info", 0)
            self.data.salt_error_raw = error_code
            error_codes = {
                0: "NoError",
                700: "NoFlow",
                701: "HighSalt",
                702: "LowSalt",
                703: "WaterTooCold",
                705: "DownRate2",
                706: "DownRate1",
                707: "SamplingOnly",
                708: "DosingDisabled",
                709: "DlyAcidDoseLimit",
                710: "CellDis",
            }
            self.data.error_message = error_codes.get(
                error_code,
                f"Error({error_code})" if error_code != 0 else "NoError",
            )
            info_code = parsed.get("info_message_code", -1)
            if info_code == 0:
                self.data.mode = "Off"
            elif info_code in (2, 3):
                self.data.mode = "Auto"
            elif info_code == 5:
                self.data.mode = "Auto"
            elif info_code >= 1:
                self.data.mode = "Auto"
            self.data.pump_is_operating = info_code not in (0, 5, None)
        elif parsed.get("type") == "config":
            if parsed.get("pump_speed") is not None:
                self.data.pump_speed = parsed["pump_speed"]
        elif parsed.get("type") == "measurements":
            if parsed.get("water_temperature_c") is not None:
                self.data.water_temperature_c = parsed["water_temperature_c"]
                self.data.water_temperature_precise = parsed["water_temperature_c"]
            if parsed.get("cell_current_ma") is not None:
                self.data.cell_current_ma = parsed["cell_current_ma"]
        elif parsed.get("type") == "temperature":
            if parsed.get("water_temp_c") is not None and self.data.water_temperature_precise is None:
                self.data.water_temperature_c = parsed["water_temp_c"]
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
        elif parsed.get("type") == "heater_state":
            self.data.heater_mode = parsed.get("heater_mode")
            self.data.heater_pump_mode = parsed.get("heater_pump_mode")
            self.data.heater_setpoint_c = parsed.get("heater_setpoint_c")
            self.data.heat_pump_mode = parsed.get("heat_pump_mode")
            self.data.heater_water_temp_c = parsed.get("heater_water_temp_c")
            self.data.heater_on = parsed.get("heater_on", False)
            self.data.heater_error = parsed.get("heater_error")
