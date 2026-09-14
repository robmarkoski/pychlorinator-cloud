"""Tests for WebSocket client setpoint writes."""

from __future__ import annotations

import struct
import unittest
from unittest.mock import AsyncMock

from custom_components.astralpool_halo_cloud.pychlorinator_cloud.setpoints import (
    SETPOINT_CMD_ID,
    SetpointValidationError,
)
from custom_components.astralpool_halo_cloud.pychlorinator_cloud.websocket_client import (
    HaloWebSocketClient,
)


class TestWebSocketClientSetpoints(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = HaloWebSocketClient("TEST_SERIAL", "test_user", "test_pass")
        self.client.send_command = AsyncMock()
        # Pre-populate client data with known setpoints
        self.client.data.ph_setpoint = 7.4
        self.client.data.orp_setpoint = 700
        self.client.data.pool_chlorine_setpoint = 3
        self.client.data.acid_setpoint = 2
        self.client.data.spa_chlorine_setpoint = 4

    async def test_set_pool_chlorine_setpoint_success(self):
        await self.client.set_pool_chlorine_setpoint(6)

        self.client.send_command.assert_awaited_once()
        args, kwargs = self.client.send_command.call_args
        command = args[0]
        self.assertEqual(kwargs.get("source"), "setpoints")

        # Verify command prefix and ID
        self.assertEqual(command[0], 0x03)
        cmd_id = struct.unpack_from("<H", command, 1)[0]
        self.assertEqual(cmd_id, SETPOINT_CMD_ID)

        # Unpack payload (<BHBBB: ph*10, orp, pool_cl, acid, spa_cl)
        payload = command[3:9]
        ph_raw, orp, pool_cl, acid, spa_cl = struct.unpack("<BHBBB", payload)
        self.assertEqual(ph_raw, 74)  # 7.4 * 10 (preserved)
        self.assertEqual(orp, 700)    # preserved
        self.assertEqual(pool_cl, 6)  # updated value!
        self.assertEqual(acid, 2)     # preserved
        self.assertEqual(spa_cl, 4)   # preserved

    async def test_set_pool_chlorine_setpoint_bounds_min_and_max(self):
        # 0 is valid minimum
        await self.client.set_pool_chlorine_setpoint(0)
        command = self.client.send_command.call_args[0][0]
        ph_raw, orp, pool_cl, acid, spa_cl = struct.unpack("<BHBBB", command[3:9])
        self.assertEqual(pool_cl, 0)

        # 8 is valid maximum
        await self.client.set_pool_chlorine_setpoint(8)
        command = self.client.send_command.call_args[0][0]
        ph_raw, orp, pool_cl, acid, spa_cl = struct.unpack("<BHBBB", command[3:9])
        self.assertEqual(pool_cl, 8)

    async def test_set_pool_chlorine_setpoint_out_of_bounds_raises(self):
        with self.assertRaises(SetpointValidationError):
            await self.client.set_pool_chlorine_setpoint(9)

        with self.assertRaises(SetpointValidationError):
            await self.client.set_pool_chlorine_setpoint(-1)
        self.client.send_command.assert_not_awaited()

    async def test_device_capabilities_apply_to_all_explicit_writes(self):
        self.client.data.min_manual_chlorine_setpoint = 1
        self.client.data.max_manual_chlorine_setpoint = 10
        await self.client.set_pool_chlorine_setpoint(10)
        self.assertEqual(self.client.send_command.call_args.args[0][6], 10)
        self.client.send_command.reset_mock()
        for value in (0, 11, True, 4.5):
            with self.subTest(value=value), self.assertRaises(SetpointValidationError):
                await self.client.write_setpoints(pool_chlorine_setpoint=value)
        self.client.send_command.assert_not_awaited()

    async def test_existing_ph_orp_writes_preserve_chlorine_above_default_limit(self):
        self.client.data.pool_chlorine_setpoint = 10
        for method, value in ((self.client.set_ph_setpoint, 7.2),
                              (self.client.set_orp_setpoint, 650)):
            await method(value)
            self.assertEqual(self.client.send_command.call_args.args[0][6], 10)

    async def test_each_missing_preserved_field_prevents_send(self):
        for field in ("ph_setpoint", "orp_setpoint", "acid_setpoint",
                      "spa_chlorine_setpoint"):
            saved = getattr(self.client.data, field)
            setattr(self.client.data, field, None)
            with self.subTest(field=field), self.assertRaisesRegex(RuntimeError, field):
                await self.client.set_pool_chlorine_setpoint(5)
            setattr(self.client.data, field, saved)
        self.client.send_command.assert_not_awaited()

    async def test_failed_send_preserves_snapshot(self):
        self.client.send_command.side_effect = RuntimeError("Not connected")
        with self.assertRaisesRegex(RuntimeError, "Not connected"):
            await self.client.set_pool_chlorine_setpoint(5)
        self.assertEqual(self.client.data.pool_chlorine_setpoint, 3)

    async def test_missing_live_setpoint_raises_runtime_error(self):
        self.client.data.orp_setpoint = None
        with self.assertRaises(RuntimeError) as ctx:
            await self.client.set_pool_chlorine_setpoint(5)
        self.assertIn("orp_setpoint is not known yet", str(ctx.exception))

    async def test_write_setpoints_explicit_all_fields(self):
        await self.client.write_setpoints(
            ph_setpoint=7.2,
            orp_setpoint=650,
            pool_chlorine_setpoint=5,
            acid_setpoint=1,
            spa_chlorine_setpoint=2,
        )
        self.client.send_command.assert_awaited_once()
        command = self.client.send_command.call_args[0][0]
        ph_raw, orp, pool_cl, acid, spa_cl = struct.unpack("<BHBBB", command[3:9])
        self.assertEqual(ph_raw, 72)
        self.assertEqual(orp, 650)
        self.assertEqual(pool_cl, 5)
        self.assertEqual(acid, 1)
        self.assertEqual(spa_cl, 2)


if __name__ == "__main__":
    unittest.main()
