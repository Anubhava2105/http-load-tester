import unittest

from http_load_tester.domain.models import HttpRequest, LoadModel, Origin, TestPlan
from http_load_tester.load.scheduler import ClosedLoopScheduler, OpenLoopScheduler


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

class FakeClock:
    def __init__(self, now_ns: int = 1_000_000_000) -> None:
        self.current_ns = now_ns

    def now_ns(self) -> int:
        return self.current_ns

    def sleep(self, seconds: float) -> None:
        self.current_ns += int(seconds * 1_000_000_000)


class OpenLoopSchedulerTests(unittest.TestCase):
    def test_attempts_follow_rate_timeline_not_worker_completion_time(self) -> None:
        clock = FakeClock()
        plan = TestPlan(
            origin=Origin("http", "example.test", 80),
            request=HttpRequest("GET", "/"),
            request_count=3,
            load_model=LoadModel.OPEN_LOOP,
            target_rate=10,
        )
        scheduler = OpenLoopScheduler(plan, clock=clock, sleeper=clock.sleep)

        first = scheduler.next_attempt()
        clock.current_ns += 250_000_000
        second = scheduler.next_attempt()
        third = scheduler.next_attempt()

        self.assertEqual(first.scheduled_ns, 1_000_000_000)
        self.assertEqual(second.scheduled_ns, 1_100_000_000)
        self.assertEqual(third.scheduled_ns, 1_200_000_000)
        self.assertEqual(clock.current_ns, 1_250_000_000)

    def test_duration_stops_at_the_end_of_the_scheduling_window(self) -> None:
        clock = FakeClock()
        plan = TestPlan(
            origin=Origin("http", "example.test", 80),
            request=HttpRequest("GET", "/"),
            duration_seconds=0.25,
            load_model=LoadModel.OPEN_LOOP,
            target_rate=10,
        )
        scheduler = OpenLoopScheduler(plan, clock=clock, sleeper=clock.sleep)

        attempts = [scheduler.next_attempt() for _ in range(4)]

        self.assertEqual(
            [attempt.request_id for attempt in attempts[:3]],
            ["request-1", "request-2", "request-3"],
        )
        self.assertIsNone(attempts[3])
