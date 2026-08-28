import unittest

from app.store.lenta.api import AdaptiveThrottle


class AdaptiveThrottleTests(unittest.TestCase):
    def test_starts_neutral(self) -> None:
        throttle = AdaptiveThrottle()
        self.assertEqual(throttle.factor, 1.0)
        self.assertEqual(throttle.delay(1.2, 8.0), 1.2)

    def test_429_doubles_until_cap(self) -> None:
        throttle = AdaptiveThrottle(factor_cap=4.0)
        throttle.on_throttled()
        self.assertEqual(throttle.factor, 2.0)
        throttle.on_throttled()
        self.assertEqual(throttle.factor, 4.0)
        throttle.on_throttled()
        self.assertEqual(throttle.factor, 4.0)

    def test_delay_respects_max(self) -> None:
        throttle = AdaptiveThrottle()
        throttle.on_throttled()
        throttle.on_throttled()
        self.assertEqual(throttle.delay(1.2, 8.0), 4.8)
        self.assertEqual(throttle.delay(3.0, 8.0), 8.0)

    def test_success_streak_recovers_gradually(self) -> None:
        throttle = AdaptiveThrottle(recovery_successes=3)
        throttle.on_throttled()
        throttle.on_throttled()
        self.assertEqual(throttle.factor, 4.0)
        for _ in range(3):
            throttle.on_success()
        self.assertEqual(throttle.factor, 2.0)
        for _ in range(3):
            throttle.on_success()
        self.assertEqual(throttle.factor, 1.0)
        for _ in range(3):
            throttle.on_success()
        self.assertEqual(throttle.factor, 1.0)

    def test_429_resets_success_streak(self) -> None:
        throttle = AdaptiveThrottle(recovery_successes=3)
        throttle.on_throttled()
        throttle.on_success()
        throttle.on_success()
        throttle.on_throttled()
        for _ in range(2):
            throttle.on_success()
        self.assertEqual(throttle.factor, 4.0)


if __name__ == "__main__":
    unittest.main()
