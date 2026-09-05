import unittest

from http_load_tester.domain.models import HttpRequest, Origin, TestPlan
from http_load_tester.load.scheduler import ClosedLoopScheduler


class SchedulerTests(unittest.TestCase):
    def test_fixed_count_is_unique_and_stops_at_count(self) -> None:
        plan = TestPlan(
            origin=Origin("http", "example.test", 80),
            request=HttpRequest("GET", "/"),
            request_count=3,
            workers=2,
        )
        scheduler = ClosedLoopScheduler(plan)

        attempts = [scheduler.next_attempt() for _ in range(4)]

        self.assertEqual(
            [attempt.request_id for attempt in attempts[:3]],
            ["request-1", "request-2", "request-3"],
        )
        self.assertIsNone(attempts[3])

    def test_stop_prevents_new_attempts(self) -> None:
        plan = TestPlan(
            origin=Origin("http", "example.test", 80),
            request=HttpRequest("GET", "/"),
            request_count=2,
        )
        scheduler = ClosedLoopScheduler(plan)
        scheduler.stop()

        self.assertIsNone(scheduler.next_attempt())
