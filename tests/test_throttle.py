"""Request pacing: global rate, per-endpoint ceilings, rate-limit headers."""

from __future__ import annotations

import unittest

from frontegg_data_export.client import Throttle
from frontegg_data_export.config import parse_rate
from tests.test_client import make_client


class FakeClock:
    def __init__(self):
        self.now = 1000.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.slept.append(round(s, 6))
        self.now += s


class ThrottleTests(unittest.TestCase):
    def test_evenly_spaced_at_the_rate(self):
        clk = FakeClock()
        t = Throttle(4.0, {}, clock=clk, sleep=clk.sleep)
        for _ in range(4):
            t.wait("/a")
        self.assertEqual(clk.slept, [0.25, 0.25, 0.25])

    def test_no_wait_when_requests_are_already_slow(self):
        clk = FakeClock()
        t = Throttle(4.0, {}, clock=clk, sleep=clk.sleep)
        t.wait("/a")
        clk.now += 1.0               # the request itself took a second
        t.wait("/a")
        self.assertEqual(clk.slept, [])

    def test_endpoint_ceiling_applies_only_to_that_path(self):
        clk = FakeClock()
        t = Throttle(10.0, {"/users": 30}, clock=clk, sleep=clk.sleep)   # 30/min = one per 2 s
        t.wait("/users")
        t.wait("/other")             # global interval only
        t.wait("/users")             # held until 2 s after the first /users
        self.assertEqual(clk.slept, [0.1, 1.9])

    def test_pause_holds_every_request(self):
        clk = FakeClock()
        t = Throttle(4.0, {}, clock=clk, sleep=clk.sleep)
        t.wait("/a")
        t.pause(5.0)
        t.wait("/b")
        self.assertEqual(clk.slept, [5.0])


class RateParsingTests(unittest.TestCase):
    def test_presets(self):
        self.assertEqual(parse_rate("gentle"), 1.5)
        self.assertEqual(parse_rate("normal"), 4.0)
        self.assertEqual(parse_rate("FAST"), 12.0)
        self.assertEqual(parse_rate(None), 4.0)

    def test_numbers(self):
        self.assertEqual(parse_rate("3"), 3.0)
        self.assertEqual(parse_rate(0.5), 0.5)

    def test_out_of_range_or_junk(self):
        for v in ("0", "0.1", "17", "100", "quick", "-1"):
            with self.subTest(v=v), self.assertRaises(ValueError):
                parse_rate(v)


class RateLimitHeaderTests(unittest.TestCase):
    def test_exhausted_window_pauses_until_reset(self):
        clk = FakeClock()
        headers = {"x-rate-limit-limit": "100", "x-rate-limit-remaining": "0", "x-rate-limit-reset": "7"}
        c, _, _ = make_client(self, {("GET", "/identity/x"): [(200, {}, headers)], ("GET", "/y"): [200]},
                              clock=clk, rate=100.0)
        c.throttle.sleep = clk.sleep
        c.get("/identity/x")
        c.get("/y")
        self.assertIn(7.0, clk.slept)
        self.assertEqual(c.rate_limit_headers_seen, 1)

    def test_remaining_budget_does_not_pause(self):
        clk = FakeClock()
        headers = {"x-rate-limit-limit": "100", "x-rate-limit-remaining": "40", "x-rate-limit-reset": "7"}
        c, _, _ = make_client(self, {("GET", "/identity/x"): [(200, {}, headers)], ("GET", "/y"): [200]},
                              clock=clk, rate=100.0)
        c.throttle.sleep = clk.sleep
        c.get("/identity/x")
        c.get("/y")
        self.assertTrue(all(s < 1 for s in clk.slept), clk.slept)


if __name__ == "__main__":
    unittest.main()
