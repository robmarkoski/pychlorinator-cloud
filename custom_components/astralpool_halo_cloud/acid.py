"""Acid reservoir tracking for the AstralPool Halo Cloud integration.

The controller has no acid tank-level sensor. It reports a daily acid-dosing
counter in 0x0259. This resets at the controller's day rollover. This module
tracks a user-managed reservoir HA-side: the user logs a refill (sets
remaining = bottle size), and we decrement the remaining volume as the
controller doses.
The interpretation of the raw 0x0259 counter is firmware-specific:

Firmware 2.7:
  The 0x0259 value has been observed to match the Halo panel's
  "Dosing Pump: N ml today" value directly. For firmware 2.7, the raw value
  is therefore treated as millilitres.
Other / unknown firmware:
  The raw value is treated as pump-seconds and converted to millilitres using
  the acid pump dose rate (mL/min, ``acid_pump_size`` from 0x0064/0x0069).

Units caveat: the vendor field is named DosingPumpSecs and the 1.5.x app
renders the raw value as "ml today", while the 2.x app carries both seconds
and mL plus a mL/min flow rate. This makes the historical interpretation of
the raw value ambiguous and strongly suggests that, where the value
represents seconds, mL = seconds x rate / 60.

Firmware 2.7 testing has shown that the raw 0x0259 value can instead be used
directly as millilitres, so the firmware-specific handling above is
intentional. AstralPool has not formally documented the units of this field,
and the existing reverse-engineered documentation was developed against older
firmware. The raw counter is also exposed so the derived dosing volume can be
sanity-checked against a live dose.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

_STORE_VERSION = 1
DEFAULT_BOTTLE_LITRES = 20.0

# Firmware versions for which 0x0259 has been directly verified to report
# millilitres rather than pump seconds.
_RAW_DOSING_VALUE_IS_ML_FIRMWARE = {
    "2.7",
}

# Vendor default acid pump dose rate when the capability is unknown (mL/min).
_DEFAULT_PUMP_ML_PER_MIN = 5


class AcidReservoirTracker:
    """Persistent estimate of remaining acid, driven by daily dosing."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        self._hass = hass
        self._store: Store = Store(
            hass,
            _STORE_VERSION,
            f"astralpool_halo_cloud_acid_{entry_id}",
        )
        self.bottle_size_l: float = DEFAULT_BOTTLE_LITRES
        self.remaining_ml: float = DEFAULT_BOTTLE_LITRES * 1000.0
        self.last_refill: Optional[Any] = None  # aware datetime

        # This stores the raw 0x0259 daily counter regardless of its
        # firmware-specific units.
        self._last_dosing_today: Optional[int] = None

        self._loaded = False

    async def async_load(self) -> None:
        try:
            data = await self._store.async_load()
        except Exception:  # noqa: BLE001
            # A corrupt/unreadable acid store must not fail integration setup;
            # fall back to defaults (a fresh, full bottle).
            _LOGGER.warning(
                "Could not load acid reservoir state; starting from defaults",
                exc_info=True,
            )
            data = None

        if data:
            self.bottle_size_l = float(
                data.get("bottle_size_l", DEFAULT_BOTTLE_LITRES)
            )
            self.remaining_ml = float(
                data.get(
                    "remaining_ml",
                    self.bottle_size_l * 1000.0,
                )
            )

            raw = data.get("last_refill")
            self.last_refill = (
                dt_util.parse_datetime(raw) if raw else None
            )

            # Read the current key first. Fall back to the legacy
            # ``last_seconds_today`` key so existing stored reservoir state from
            # earlier versions survives the migration to the firmware-independent
            # ``last_dosing_today`` name.
            self._last_dosing_today = data.get(
                "last_dosing_today",
                data.get("last_seconds_today"),
            )

        self._loaded = True

    async def _async_save(self) -> None:
        await self._store.async_save(
            {
                "bottle_size_l": self.bottle_size_l,
                "remaining_ml": self.remaining_ml,
                "last_refill": (
                    self.last_refill.isoformat()
                    if self.last_refill
                    else None
                ),
                "last_dosing_today": self._last_dosing_today,
            }
        )

    @staticmethod
    def _normalise_firmware_version(
        firmware_version: Optional[str],
    ) -> Optional[str]:
        """Return the firmware major.minor version."""
        if not firmware_version:
            return None

        parts = str(firmware_version).strip().split(".")

        if len(parts) < 2:
            return str(firmware_version).strip()

        return f"{parts[0]}.{parts[1]}"

    @classmethod
    def _raw_value_is_ml(
        cls,
        firmware_version: Optional[str],
    ) -> bool:
        """Return True when 0x0259 is known to be a millilitre counter."""
        version = cls._normalise_firmware_version(firmware_version)
        return version in _RAW_DOSING_VALUE_IS_ML_FIRMWARE

    @staticmethod
    def _ml_per_second(
        pump_ml_per_min: Optional[int],
    ) -> float:
        rate = (
            pump_ml_per_min
            if pump_ml_per_min
            else _DEFAULT_PUMP_ML_PER_MIN
        )
        return float(rate) / 60.0

    @classmethod
    def _dosing_counter_to_ml(
        cls,
        dosing_today: int,
        pump_ml_per_min: Optional[int],
        firmware_version: Optional[str],
    ) -> float:
        """Convert the raw 0x0259 daily counter to millilitres."""

        if cls._raw_value_is_ml(firmware_version):
            # Firmware 2.7: raw 0x0259 value is the Halo panel's
            # "Dosing Pump: N ml today" value.
            return float(dosing_today)

        # Older / unknown firmware: retain the original seconds-based
        # interpretation.
        return dosing_today * cls._ml_per_second(pump_ml_per_min)

    async def async_ingest(
        self,
        dosing_today: Optional[int],
        pump_ml_per_min: Optional[int],
        firmware_version: Optional[str],
    ) -> bool:
        """Ingest a fresh 0x0259 daily dosing reading."""

        if not self._loaded or dosing_today is None:
            return False

        raw_is_ml = self._raw_value_is_ml(firmware_version)

        prev = self._last_dosing_today

        if prev is None:
            self._last_dosing_today = dosing_today
            await self._async_save()
            return False

        # The counter resets at the controller's day rollover.
        delta = (
            dosing_today - prev
            if dosing_today >= prev
            else dosing_today
        )

        changed = False

        if delta > 0:
            used_ml = self._dosing_counter_to_ml(
                delta,
                pump_ml_per_min,
                firmware_version,
            )

            new_remaining = max(
                0.0,
                self.remaining_ml - used_ml,
            )

            if new_remaining != self.remaining_ml:
                self.remaining_ml = new_remaining
                changed = True

        if dosing_today != prev:
            self._last_dosing_today = dosing_today
            changed = True

        if changed:
            await self._async_save()

        return changed

    async def async_log_refill(self) -> None:
        """Record a fresh bottle: reset remaining to full and stamp the time."""
        self.remaining_ml = self.bottle_size_l * 1000.0
        self.last_refill = dt_util.utcnow()

        await self._async_save()

        _LOGGER.info(
            "Acid refill logged: reservoir reset to %.1f L",
            self.bottle_size_l,
        )

    async def async_set_bottle_size(self, litres: float) -> None:
        self.bottle_size_l = max(0.1, float(litres))
        # Keep remaining within the new capacity.
        self.remaining_ml = min(
            self.remaining_ml,
            self.bottle_size_l * 1000.0,
        )
        await self._async_save()

    # ---- Derived values for sensors ----

    @property
    def remaining_l(self) -> float:
        return round(self.remaining_ml / 1000.0, 2)

    @property
    def percent(self) -> Optional[float]:
        full = self.bottle_size_l * 1000.0

        if full <= 0:
            return None

        return round(
            min(100.0, 100.0 * self.remaining_ml / full),
            1,
        )

    def used_today_ml(
        self,
        dosing_today: Optional[int],
        pump_ml_per_min: Optional[int],
        firmware_version: Optional[str],
    ) -> Optional[float]:
        """Return today's acid use in millilitres."""

        if dosing_today is None:
            return None

        used_ml = self._dosing_counter_to_ml(
            dosing_today,
            pump_ml_per_min,
            firmware_version,
        )
        return round(used_ml, 0)

    def _days_since_refill(self) -> Optional[float]:
        if self.last_refill is None:
            return None

        return (
            dt_util.utcnow() - self.last_refill
        ).total_seconds() / 86400.0

    def avg_daily_ml(self) -> Optional[float]:
        days = self._days_since_refill()

        if days is None or days < 0.5:
            return None

        consumed = (
            self.bottle_size_l * 1000.0
            - self.remaining_ml
        )

        if consumed <= 0:
            return None

        return consumed / days

    def days_remaining(self) -> Optional[float]:
        avg = self.avg_daily_ml()

        if not avg or avg <= 0:
            return None

        return round(
            self.remaining_ml / avg,
            1,
        )