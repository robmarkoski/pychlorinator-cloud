"""Lightweight Home Assistant stubs for isolated unit tests.

These tests exercise the entity and protocol logic, not a running HA instance.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
import sys
from typing import Any
from unittest.mock import MagicMock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class NumberMode(str, Enum):
    AUTO = "auto"
    BOX = "box"
    SLIDER = "slider"


@dataclass(frozen=True, kw_only=True)
class NumberEntityDescription:
    key: str
    name: str | None = None
    icon: str | None = None
    native_unit_of_measurement: str | None = None
    native_min_value: float | None = None
    native_max_value: float | None = None
    native_step: float | None = None
    mode: NumberMode = NumberMode.AUTO
    entity_category: Any = None


class NumberEntity:
    entity_description: NumberEntityDescription
    _attr_native_min_value: float | None = None
    _attr_native_max_value: float | None = None
    _attr_native_step: float | None = None
    _attr_native_unit_of_measurement: str | None = None
    _attr_mode: NumberMode = NumberMode.AUTO

    @property
    def native_min_value(self) -> float:
        if self._attr_native_min_value is not None:
            return self._attr_native_min_value
        return float(self.entity_description.native_min_value or 0)

    @property
    def native_max_value(self) -> float:
        if self._attr_native_max_value is not None:
            return self._attr_native_max_value
        return float(self.entity_description.native_max_value or 0)

    @property
    def native_step(self) -> float:
        if self._attr_native_step is not None:
            return self._attr_native_step
        return float(self.entity_description.native_step or 1)


class HomeAssistantError(Exception):
    pass


class CoordinatorEntity:
    def __init__(self, coordinator: Any) -> None:
        self.coordinator = coordinator

    def __class_getitem__(cls, item):
        return cls

    @property
    def available(self) -> bool:
        return True


def _mock_pkg():
    m = MagicMock()
    m.__path__ = []
    return m

class EntityCategory(str, Enum):
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"

_HA_MODULES = {
    "homeassistant": _mock_pkg(),
    "homeassistant.components": _mock_pkg(),
    "homeassistant.components.number": _mock_pkg(),
    "homeassistant.components.select": _mock_pkg(),
    "homeassistant.components.sensor": _mock_pkg(),
    "homeassistant.components.binary_sensor": _mock_pkg(),
    "homeassistant.config_entries": MagicMock(),
    "homeassistant.const": MagicMock(),
    "homeassistant.core": MagicMock(),
    "homeassistant.exceptions": MagicMock(),
    "homeassistant.helpers": _mock_pkg(),
    "homeassistant.helpers.device_registry": MagicMock(),
    "homeassistant.helpers.entity_platform": MagicMock(),
    "homeassistant.helpers.event": MagicMock(),
    "homeassistant.helpers.update_coordinator": MagicMock(),
    "homeassistant.util": _mock_pkg(),
    "homeassistant.util.dt": MagicMock(),
}
for _mod_name, _mock_val in _HA_MODULES.items():
    if _mod_name not in sys.modules:
        sys.modules[_mod_name] = _mock_val

# Populate mocked modules with our concrete classes
ha_number = sys.modules["homeassistant.components.number"]
ha_number.NumberEntity = NumberEntity
ha_number.NumberEntityDescription = NumberEntityDescription
ha_number.NumberMode = NumberMode

ha_const = sys.modules["homeassistant.const"]
ha_const.EntityCategory = EntityCategory
ha_const.EVENT_HOMEASSISTANT_STOP = "homeassistant_stop"

sys.modules["homeassistant.exceptions"].HomeAssistantError = HomeAssistantError

ha_coordinator = sys.modules["homeassistant.helpers.update_coordinator"]
ha_coordinator.CoordinatorEntity = CoordinatorEntity
