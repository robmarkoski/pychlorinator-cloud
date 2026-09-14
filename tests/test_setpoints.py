"""Tests for pychlorinator_cloud.setpoints."""

from __future__ import annotations

import struct
import unittest

from custom_components.astralpool_halo_cloud.pychlorinator_cloud.setpoints import (
    ORP_SETPOINT_MAX_MV,
    ORP_SETPOINT_MIN_MV,
    PH_SETPOINT_MAX,
    PH_SETPOINT_MIN,
    POOL_CHLORINE_SETPOINT_MAX,
    POOL_CHLORINE_SETPOINT_MIN,
    POOL_CHLORINE_SETPOINT_STEP,
    SETPOINT_CMD_ID,
    SetpointValidationError,
    build_setpoint_command,
    build_setpoint_payload,
    ph_setpoint_to_raw,
    pool_chlorine_setpoint_bounds,
    validate_orp_setpoint,
    validate_ph_setpoint,
    validate_pool_chlorine_setpoint,
)


class TestValidatePoolChlorineSetpoint(unittest.TestCase):
    def test_capability_bounds(self):
        for low, high, expected in (
            (None, None, (0, 8)), (0, 8, (0, 8)), (1, 6, (1, 6)),
            (0, 10, (0, 10)), (None, 6, (0, 6)), (1, None, (1, 8)),
            (0, 0, (0, 8)), (9, 6, (0, 8)), (10, None, (0, 8)),
            (-1, 8, (0, 8)), (0, 256, (0, 8)), (False, 8, (0, 8)),
        ):
            with self.subTest(low=low, high=high):
                self.assertEqual(pool_chlorine_setpoint_bounds(low, high), expected)

    def test_validation_uses_capability_bounds(self):
        self.assertEqual(validate_pool_chlorine_setpoint(10, maximum=10), 10)
        for value in (0, 7):
            with self.subTest(value=value), self.assertRaises(SetpointValidationError):
                validate_pool_chlorine_setpoint(value, minimum=1, maximum=6)

    def test_constants(self):
        self.assertEqual(POOL_CHLORINE_SETPOINT_MIN, 0)
        self.assertEqual(POOL_CHLORINE_SETPOINT_MAX, 8)
        self.assertEqual(POOL_CHLORINE_SETPOINT_STEP, 1)

    def test_valid_bounds(self):
        self.assertEqual(validate_pool_chlorine_setpoint(0), 0)
        self.assertEqual(validate_pool_chlorine_setpoint(8), 8)
        self.assertEqual(validate_pool_chlorine_setpoint(4), 4)

    def test_below_min_raises(self):
        with self.assertRaises(SetpointValidationError):
            validate_pool_chlorine_setpoint(-1)

    def test_above_max_raises(self):
        with self.assertRaises(SetpointValidationError):
            validate_pool_chlorine_setpoint(9)

    def test_bool_raises(self):
        with self.assertRaises(SetpointValidationError):
            validate_pool_chlorine_setpoint(True)
        with self.assertRaises(SetpointValidationError):
            validate_pool_chlorine_setpoint(False)

    def test_float_raises(self):
        with self.assertRaises(SetpointValidationError):
            validate_pool_chlorine_setpoint(4.0)  # type: ignore[arg-type]

    def test_string_raises(self):
        with self.assertRaises(SetpointValidationError):
            validate_pool_chlorine_setpoint("4")  # type: ignore[arg-type]


class TestValidatePHSetpoint(unittest.TestCase):
    def test_valid_midrange(self):
        self.assertAlmostEqual(validate_ph_setpoint(7.4), 7.4)

    def test_valid_min_boundary(self):
        self.assertAlmostEqual(validate_ph_setpoint(PH_SETPOINT_MIN), PH_SETPOINT_MIN)

    def test_valid_max_boundary(self):
        self.assertAlmostEqual(validate_ph_setpoint(PH_SETPOINT_MAX), PH_SETPOINT_MAX)

    def test_below_min_raises(self):
        with self.assertRaises(SetpointValidationError):
            validate_ph_setpoint(6.7)

    def test_above_max_raises(self):
        with self.assertRaises(SetpointValidationError):
            validate_ph_setpoint(10.1)

    def test_non_tenth_step_raises(self):
        with self.assertRaises(SetpointValidationError):
            validate_ph_setpoint(7.45)

    def test_non_numeric_raises(self):
        with self.assertRaises(SetpointValidationError):
            validate_ph_setpoint("7.4")  # type: ignore[arg-type]

    def test_integer_input_accepted(self):
        result = validate_ph_setpoint(8)
        self.assertAlmostEqual(result, 8.0)


class TestValidateORPSetpoint(unittest.TestCase):
    def test_valid_midrange(self):
        self.assertEqual(validate_orp_setpoint(700), 700)

    def test_valid_min_boundary(self):
        self.assertEqual(validate_orp_setpoint(ORP_SETPOINT_MIN_MV), ORP_SETPOINT_MIN_MV)

    def test_valid_max_boundary(self):
        self.assertEqual(validate_orp_setpoint(ORP_SETPOINT_MAX_MV), ORP_SETPOINT_MAX_MV)

    def test_below_min_raises(self):
        with self.assertRaises(SetpointValidationError):
            validate_orp_setpoint(ORP_SETPOINT_MIN_MV - 1)

    def test_above_max_raises(self):
        with self.assertRaises(SetpointValidationError):
            validate_orp_setpoint(ORP_SETPOINT_MAX_MV + 1)

    def test_bool_raises(self):
        with self.assertRaises(SetpointValidationError):
            validate_orp_setpoint(True)

    def test_non_int_raises(self):
        with self.assertRaises(SetpointValidationError):
            validate_orp_setpoint(700.5)  # type: ignore[arg-type]


class TestPHSetpointToRaw(unittest.TestCase):
    def test_7_4(self):
        self.assertEqual(ph_setpoint_to_raw(7.4), 74)

    def test_min(self):
        self.assertEqual(ph_setpoint_to_raw(PH_SETPOINT_MIN), int(PH_SETPOINT_MIN * 10))

    def test_max(self):
        self.assertEqual(ph_setpoint_to_raw(PH_SETPOINT_MAX), int(PH_SETPOINT_MAX * 10))


class TestBuildSetpointPayload(unittest.TestCase):
    def test_known_values(self):
        payload = build_setpoint_payload(
            ph_setpoint=7.4,
            orp_setpoint=700,
            pool_chlorine_setpoint=5,
            acid_setpoint=2,
            spa_chlorine_setpoint=3,
        )
        self.assertIsInstance(payload, bytes)
        self.assertEqual(len(payload), 6)  # B H B B B = 1+2+1+1+1
        ph_raw, orp_raw, pool_cl, acid, spa_cl = struct.unpack("<BHBBB", payload)
        self.assertEqual(ph_raw, 74)
        self.assertEqual(orp_raw, 700)
        self.assertEqual(pool_cl, 5)
        self.assertEqual(acid, 2)
        self.assertEqual(spa_cl, 3)

    def test_preserves_controller_chlorine_above_default_range(self):
        payload = build_setpoint_payload(
            ph_setpoint=7.4, orp_setpoint=700, pool_chlorine_setpoint=10,
            acid_setpoint=2, spa_chlorine_setpoint=3,
        )
        self.assertEqual(struct.unpack("<BHBBB", payload)[2], 10)

    def test_unencodable_pool_chlorine_raises(self):
        with self.assertRaises(SetpointValidationError):
            build_setpoint_payload(
                ph_setpoint=7.4,
                orp_setpoint=700,
                pool_chlorine_setpoint=256,
                acid_setpoint=2,
                spa_chlorine_setpoint=3,
            )


class TestBuildSetpointCommand(unittest.TestCase):
    def test_prefix_and_cmd_id(self):
        cmd = build_setpoint_command(
            ph_setpoint=7.4,
            orp_setpoint=700,
            pool_chlorine_setpoint=5,
            acid_setpoint=2,
            spa_chlorine_setpoint=3,
        )
        self.assertEqual(cmd[0], 0x03)  # prefix
        cmd_id = struct.unpack_from("<H", cmd, 1)[0]
        self.assertEqual(cmd_id, SETPOINT_CMD_ID)  # 0x0066

    def test_payload_matches_build_setpoint_payload(self):
        kwargs = dict(
            ph_setpoint=7.2,
            orp_setpoint=650,
            pool_chlorine_setpoint=4,
            acid_setpoint=1,
            spa_chlorine_setpoint=2,
        )
        cmd = build_setpoint_command(**kwargs)
        expected_payload = build_setpoint_payload(**kwargs)
        self.assertEqual(len(cmd), 20)
        self.assertEqual(cmd[3:9], expected_payload)
        self.assertEqual(cmd[9:], b"\x00" * 11)


if __name__ == "__main__":
    unittest.main()
