"""Токен-бакет для входа (TZ-M6R A6 / AUDIT S2)."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.web.ratelimit import RateLimiter  # noqa: E402
from fakes import StubClock  # noqa: E402


class RateLimiterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = StubClock()
        self.limiter = RateLimiter(capacity=5, window_seconds=60.0, clock=self.clock)

    def test_s2_allows_capacity_then_blocks(self) -> None:
        for attempt in range(5):
            self.assertEqual(self.limiter.hit("ip|login"), 0, f"попытка {attempt + 1}")
        self.assertGreater(self.limiter.hit("ip|login"), 0, "шестая попытка должна быть отклонена")

    def test_s2_retry_after_is_positive_whole_seconds(self) -> None:
        for _ in range(5):
            self.limiter.hit("ip|login")
        retry_after = self.limiter.hit("ip|login")
        self.assertIsInstance(retry_after, int)
        self.assertGreaterEqual(retry_after, 1)
        self.assertLessEqual(retry_after, 60)

    def test_s2_refills_after_window(self) -> None:
        for _ in range(5):
            self.limiter.hit("ip|login")
        self.assertGreater(self.limiter.hit("ip|login"), 0)
        self.clock.advance(60)
        for _ in range(5):
            self.assertEqual(self.limiter.hit("ip|login"), 0)

    def test_s2_partial_refill_returns_one_token(self) -> None:
        for _ in range(5):
            self.limiter.hit("ip|login")
        self.clock.advance(12)  # ровно один токен при 5/60 с
        self.assertEqual(self.limiter.hit("ip|login"), 0)
        self.assertGreater(self.limiter.hit("ip|login"), 0)

    def test_s2_keys_are_independent(self) -> None:
        for _ in range(5):
            self.limiter.hit("ip|pervyy")
        self.assertGreater(self.limiter.hit("ip|pervyy"), 0)
        self.assertEqual(self.limiter.hit("ip|vtoroy"), 0)

    def test_s2_reset_clears_bucket(self) -> None:
        for _ in range(5):
            self.limiter.hit("ip|login")
        self.assertGreater(self.limiter.hit("ip|login"), 0)
        self.limiter.reset("ip|login")
        self.assertEqual(self.limiter.hit("ip|login"), 0)

    def test_s2_prunes_stale_keys(self) -> None:
        limiter = RateLimiter(capacity=1, window_seconds=10.0, clock=self.clock, max_keys=3)
        for index in range(3):
            limiter.hit(f"key-{index}")
        self.clock.advance(100)
        for index in range(3, 8):
            limiter.hit(f"key-{index}")
        self.assertLessEqual(len(limiter._buckets), 6)


if __name__ == "__main__":
    unittest.main()
