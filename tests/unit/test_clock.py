import unittest

from http_load_tester.domain.clock import Clock, MonotonicClock


class ClockTests(unittest.TestCase):
    def test_production_clock_is_monotonic_for_successive_reads(self) -> None:
        clock: Clock = MonotonicClock()
        self.assertLessEqual(clock.now_ns(), clock.now_ns())


if __name__ == "__main__":
    unittest.main()
