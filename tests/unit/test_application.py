import io
import json
import unittest
from unittest.mock import Mock
from http_load_tester.domain.errors import ConfigurationError

from http_load_tester.application.config import load_plan
from http_load_tester.application.runner import run_plan
from http_load_tester.domain.models import Outcome, ResultSample


def success_sample() -> ResultSample:
    return ResultSample(
        request_id="request-1",
        scheduled_ns=0,
        worker_start_ns=1,
        pool_acquire_start_ns=2,
        pool_acquire_end_ns=3,
        write_start_ns=4,
        write_end_ns=5,
        first_byte_ns=6,
        completion_ns=10,
        status_code=200,
        outcome=Outcome.SUCCESS,
        error_category=None,
        bytes_sent=10,
        bytes_received=20,
    )


class ConfigTests(unittest.TestCase):
    def test_load_plan_builds_closed_loop_plan(self) -> None:
        plan = load_plan(
            [
                "https://[::1]:8443/health?full=1#ignored",
                "--count",
                "4",
                "--method",
                "post",
                "--header",
                "X-Test: yes",
                "--body",
                "payload",
                "--workers",
                "2",
                "--max-connections",
                "2",
                "--insecure",
                "--format",
                "json",
            ]
        )
        self.assertEqual(plan.origin.hostname, "::1")
        self.assertEqual(plan.origin.port, 8443)
        self.assertFalse(plan.origin.tls_verify)
        self.assertEqual(plan.request.target, "/health?full=1")
        self.assertEqual(plan.request.method, "POST")
        self.assertEqual(plan.request.body, b"payload")
        self.assertEqual(plan.request.headers, (("X-Test", "yes"),))
        self.assertEqual(plan.request_count, 4)
        self.assertEqual(plan.report_format.value, "json")

    def test_count_and_duration_are_required_but_mutually_exclusive(self) -> None:
        with self.assertRaises(ConfigurationError):
            load_plan(["http://example.test/", "--workers", "1"])


class RunnerTests(unittest.TestCase):
    def test_runner_wires_samples_and_renders_json(self) -> None:
        plan = load_plan(
            [
                "http://example.test/",
                "--count",
                "1",
                "--format",
                "json",
            ]
        )
        fake_pool = Mock()
        fake_executor = Mock()

        def make_executor(*args: object, **kwargs: object) -> Mock:
            kwargs["sample_sink"](success_sample())
            return fake_executor

        fake_executor.run.return_value = (success_sample(),)
        output = io.StringIO()
        error = io.StringIO()
        code = run_plan(
            plan,
            stdout=output,
            stderr=error,
            pool_factory=lambda _: fake_pool,
            executor_factory=make_executor,
        )

        payload = json.loads(output.getvalue())
        self.assertEqual(code, 0)
        self.assertEqual(payload["schema_version"], "1.0")
        self.assertEqual(payload["results"]["responses"], 1)
        self.assertEqual(error.getvalue(), "")
