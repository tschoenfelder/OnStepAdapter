import unittest

from onstep_adapter.meridian_policy import derive_meridian_policy


class MeridianPolicyTests(unittest.TestCase):
    def test_current_controller_guards_leave_flip_before_firmware_stop(self):
        policy = derive_meridian_policy(east_guard_minutes=12, west_guard_minutes=8)

        self.assertEqual(policy.firmware_guard_deg, 2.0)
        self.assertEqual(policy.flip_request_deg, 1.0)
        self.assertEqual(policy.hard_stop_deg, 1.75)
        self.assertTrue(175 < policy.seconds_flip_to_stop < 185)
        self.assertTrue(25 < policy.max_exposure_seconds_at_flip < 35)

    def test_missing_or_too_narrow_firmware_guard_refuses_policy(self):
        guards = (
            {"east_guard_minutes": 0, "west_guard_minutes": 8},
            {"east_guard_minutes": 12, "west_guard_minutes": float("nan")},
            {"east_guard_minutes": 4, "west_guard_minutes": 8},
        )
        for guard in guards:
            with self.subTest(guard=guard), self.assertRaises(ValueError):
                derive_meridian_policy(**guard)


if __name__ == "__main__":
    unittest.main()
