"""Tests for the number platform pool chlorine setpoint control."""

from __future__ import annotations

import unittest
import datetime
from unittest.mock import AsyncMock, MagicMock

from homeassistant.exceptions import HomeAssistantError

from custom_components.astralpool_halo_cloud.number import (
    NUMBER_DESCRIPTIONS,
    HaloCloudSetpointNumber,
)
from custom_components.astralpool_halo_cloud.pychlorinator_cloud.websocket_client import (
    ChlorinatorLiveData,
)


class TestNumberPlatform(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.desc = next(
            d for d in NUMBER_DESCRIPTIONS if d.key == "pool_chlorine_setpoint_control"
        )

    def test_description_attributes(self):
        self.assertEqual(self.desc.key, "pool_chlorine_setpoint_control")
        self.assertEqual(self.desc.name, "Pool Chlorine Setpoint")
        self.assertEqual(self.desc.icon, "mdi:beaker-plus-outline")
        self.assertEqual(self.desc.native_min_value, 0)
        self.assertEqual(self.desc.native_max_value, 8)
        self.assertEqual(self.desc.native_step, 1)

    def test_value_fn(self):
        data = ChlorinatorLiveData()
        self.assertIsNone(self.desc.value_fn(data))
        data.pool_chlorine_setpoint = 5
        self.assertEqual(self.desc.value_fn(data), 5)

    def test_update_value_fn(self):
        data = ChlorinatorLiveData()
        self.desc.update_value_fn(data, 4.0)
        self.assertEqual(data.pool_chlorine_setpoint, 4)

    def test_is_supported_fn(self):
        data = ChlorinatorLiveData()
        self.assertFalse(self.desc.is_supported_fn(data))
        data.pool_chlorine_setpoint = 0
        self.assertTrue(self.desc.is_supported_fn(data))

    def _create_entity(self, data: ChlorinatorLiveData):
        coordinator = MagicMock()
        coordinator.data = data
        coordinator.client.data.connected = True
        coordinator._entry.data = {"serial_number": "123456", "device_name": "Pool"}
        entity = HaloCloudSetpointNumber(coordinator, self.desc)
        entity.async_write_ha_state = MagicMock()
        return entity

    def test_default_bounds(self):
        data = ChlorinatorLiveData()
        data.pool_chlorine_setpoint = 4
        entity = self._create_entity(data)

        self.assertEqual(entity.native_min_value, 0.0)
        self.assertEqual(entity.native_max_value, 8.0)
        self.assertEqual(entity.native_value, 4.0)

    def test_capability_bounds_override(self):
        data = ChlorinatorLiveData()
        data.pool_chlorine_setpoint = 3
        data.min_manual_chlorine_setpoint = 1
        data.max_manual_chlorine_setpoint = 6
        entity = self._create_entity(data)

        self.assertEqual(entity.native_min_value, 1.0)
        self.assertEqual(entity.native_max_value, 6.0)

    def test_capability_zero_min_bound(self):
        data = ChlorinatorLiveData()
        data.pool_chlorine_setpoint = 3
        data.min_manual_chlorine_setpoint = 0
        data.max_manual_chlorine_setpoint = 8
        entity = self._create_entity(data)

        self.assertEqual(entity.native_min_value, 0.0)
        self.assertEqual(entity.native_max_value, 8.0)

    async def test_invalid_values_do_not_write_or_update_state(self):
        data = ChlorinatorLiveData(pool_chlorine_setpoint=3)
        data.min_manual_chlorine_setpoint = 1
        data.max_manual_chlorine_setpoint = 6
        entity = self._create_entity(data)
        entity.coordinator.client.set_pool_chlorine_setpoint = AsyncMock()
        for value in (0, 7, 4.5, True, float("nan"), float("inf"), "4"):
            with self.subTest(value=value), self.assertRaises(HomeAssistantError):
                await entity.async_set_native_value(value)
        entity.coordinator.client.set_pool_chlorine_setpoint.assert_not_awaited()
        self.assertEqual(data.pool_chlorine_setpoint, 3)
        entity.async_write_ha_state.assert_not_called()

    async def test_failed_write_does_not_update_state(self):
        data = ChlorinatorLiveData(pool_chlorine_setpoint=3)
        entity = self._create_entity(data)
        entity.coordinator.client.set_pool_chlorine_setpoint = AsyncMock(
            side_effect=RuntimeError("Missing snapshot")
        )
        with self.assertRaisesRegex(HomeAssistantError, "Missing snapshot"):
            await entity.async_set_native_value(5.0)
        self.assertEqual(data.pool_chlorine_setpoint, 3)
        entity.async_write_ha_state.assert_not_called()

    async def test_availability_and_write_guards(self):
        data = ChlorinatorLiveData(pool_chlorine_setpoint=0)
        data.last_update = datetime.datetime.now(datetime.timezone.utc)
        entity = self._create_entity(data)
        entity.coordinator.client.set_pool_chlorine_setpoint = AsyncMock()
        self.assertTrue(entity.available)
        entity.coordinator.client.data.connected = False
        self.assertFalse(entity.available)
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_native_value(5)
        entity.coordinator.client.data.connected = True
        data.pool_chlorine_setpoint = None
        self.assertFalse(entity.available)
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_native_value(5)
        entity.coordinator.data = None
        self.assertFalse(entity.available)
        self.assertIsNone(entity.native_value)
        with self.assertRaises(HomeAssistantError):
            await entity.async_set_native_value(5)
        entity.coordinator.client.set_pool_chlorine_setpoint.assert_not_awaited()

    def test_invalid_capability_pair_falls_back_together(self):
        data = ChlorinatorLiveData(pool_chlorine_setpoint=3)
        data.min_manual_chlorine_setpoint = 10
        data.max_manual_chlorine_setpoint = 6
        entity = self._create_entity(data)
        self.assertEqual((entity.native_min_value, entity.native_max_value), (0, 8))

    async def test_capability_range_above_default_is_writable(self):
        data = ChlorinatorLiveData(pool_chlorine_setpoint=3)
        data.max_manual_chlorine_setpoint = 10
        entity = self._create_entity(data)
        entity.coordinator.client.set_pool_chlorine_setpoint = AsyncMock()
        self.assertEqual(entity.native_max_value, 10)
        await entity.async_set_native_value(10.0)
        entity.coordinator.client.set_pool_chlorine_setpoint.assert_awaited_once_with(10)

    async def test_async_set_native_value(self):
        data = ChlorinatorLiveData()
        data.pool_chlorine_setpoint = 3
        entity = self._create_entity(data)
        entity.coordinator.client.set_pool_chlorine_setpoint = AsyncMock()

        await entity.async_set_native_value(5.0)

        entity.coordinator.client.set_pool_chlorine_setpoint.assert_awaited_once_with(5)
        self.assertEqual(data.pool_chlorine_setpoint, 5)
        entity.async_write_ha_state.assert_called_once()


if __name__ == "__main__":
    unittest.main()
