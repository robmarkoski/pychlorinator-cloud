"""Setpoint bounds and helpers for Halo pH/ORP writes.

Bounds note:
- The decompiled app in this repo confirms that pH/ORP changes use a dedicated
  SetPointCharacteristic / WriteSetPoint path.
- I could not recover a direct hard-coded min/max pair from the available
  decompiled source bundle, so the pH/ORP bounds below use the best-supported
  public Halo documentation currently available in this repo's research context:
  official/public Halo manuals consistently describe operating setpoints in the
  7.2-7.6 pH band and 650-800 mV ORP band, with 700 mV as the usual starting
  point.
- Keep these constants close to the write path so future/live write surfaces do
  not silently send obviously out-of-family values.
"""

from __future__ import annotations

import math
import struct
from typing import Final

SETPOINT_CMD_ID: Final[int] = 0x0066

# Rob confirmed on real hardware that the controller accepts a broader range
# than the initial conservative documentation-based bounds.
PH_SETPOINT_MIN: Final[float] = 6.8
PH_SETPOINT_MAX: Final[float] = 10.0
PH_SETPOINT_STEP: Final[float] = 0.1

ORP_SETPOINT_MIN_MV: Final[int] = 200
ORP_SETPOINT_MAX_MV: Final[int] = 800


class SetpointValidationError(ValueError):
    """Raised when a setpoint value is out of bounds or not encodable."""


def _require_byte(name: str, value: int) -> int:
    if not isinstance(value, int):
        raise SetpointValidationError(f"{name} must be an integer byte value")
    if not 0 <= value <= 255:
        raise SetpointValidationError(f"{name} must be between 0 and 255")
    return value


def _is_tenth_step(value: float) -> bool:
    return math.isclose(value * 10, round(value * 10), abs_tol=1e-9)


def validate_ph_setpoint(value: float) -> float:
    """Validate and normalize a pH setpoint.

    Halo encodes pH as a single byte storing pH × 10, so only 0.1 increments are
    representable.
    """
    if not isinstance(value, (int, float)):
        raise SetpointValidationError("pH setpoint must be numeric")

    normalized = float(value)
    if not _is_tenth_step(normalized):
        raise SetpointValidationError(
            "pH setpoint must be representable in 0.1 increments"
        )
    if not PH_SETPOINT_MIN <= normalized <= PH_SETPOINT_MAX:
        raise SetpointValidationError(
            f"pH setpoint must be between {PH_SETPOINT_MIN:.1f} and {PH_SETPOINT_MAX:.1f}"
        )
    return round(normalized, 1)


def ph_setpoint_to_raw(value: float) -> int:
    return int(round(validate_ph_setpoint(value) * 10))


def validate_orp_setpoint(value: int) -> int:
    if not isinstance(value, int):
        raise SetpointValidationError("ORP setpoint must be an integer in millivolts")
    if not ORP_SETPOINT_MIN_MV <= value <= ORP_SETPOINT_MAX_MV:
        raise SetpointValidationError(
            f"ORP setpoint must be between {ORP_SETPOINT_MIN_MV} and {ORP_SETPOINT_MAX_MV} mV"
        )
    return value


def build_setpoint_payload(
    *,
    ph_setpoint: float,
    orp_setpoint: int,
    pool_chlorine_setpoint: int,
    acid_setpoint: int,
    spa_chlorine_setpoint: int,
) -> bytes:
    """Build cmd 0x0066 SetPointCharacteristic payload (<BHBBB>)."""
    return struct.pack(
        "<BHBBB",
        ph_setpoint_to_raw(ph_setpoint),
        validate_orp_setpoint(orp_setpoint),
        _require_byte("pool_chlorine_setpoint", pool_chlorine_setpoint),
        _require_byte("acid_setpoint", acid_setpoint),
        _require_byte("spa_chlorine_setpoint", spa_chlorine_setpoint),
    )


def build_setpoint_command(**kwargs: int | float) -> bytes:
    payload = build_setpoint_payload(**kwargs)
    return bytes([0x03]) + struct.pack("<H", SETPOINT_CMD_ID) + payload
