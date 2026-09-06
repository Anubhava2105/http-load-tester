"""CLI quality of life: version, report files, live progress."""

import io
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from http_load_tester.application.cli import main
from http_load_tester.application.config import load_plan
from http_load_tester.application.runner import ProgressTracker, run_plan
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


def run_with_mocks(plan, *, stdout=None, stderr=None):
    fake_pool = Mock()
    fake_executor = Mock()

    def make_executor(*args: object, **kwargs: object) -> Mock:
        kwargs["sample_sink"](success_sample())
        return fake_executor

    fake_executor.run.return_value = (success_sample(),)
    return run_plan(
        plan,
        stdout=stdout or io.StringIO(),
        stderr=stderr or io.StringIO(),
        pool_factory=lambda _: fake_pool,
        executor_factory=make_executor,
    )


class VersionTests(unittest.TestCase):
    def test_version_prints_and_exits_zero(self) -> None:
        import http_load_tester

        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as raised:
            with patch("sys.stdout", stdout):
                main(["--version"])
        self.assertEqual(raised.exception.code, 0)
        self.assertIn(http_load_tester.__version__, stdout.getvalue())


class OutputFileTests(unittest.TestCase):
    def test_report_is_written_to_file_and_stdout(self) -> None:
        with TemporaryDirectory() as home:
            target = Path(home) / "run.json"
            plan = load_plan(
                [
                    "http://example.test/",
                    "--count",
                    "1",
                    "--format",
                    "json",
                    "--output",
                    str(target),
                ]
            )
            output = io.StringIO()
            code = run_with_mocks(plan, stdout=output)
            self.assertEqual(code, 0)
            self.assertEqual(target.read_text(encoding="utf-8"), output.getvalue())
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload["results"]["successes"], 1)

    def test_unwritable_report_file_is_an_execution_failure(self) -> None:
        with TemporaryDirectory() as home:
            plan = load_plan(
                [
                    "http://example.test/",
                    "--count",
                    "1",
                    "--output",
                    str(Path(home)),
                ]
            )
            code = run_with_mocks(plan)
            self.assertEqual(code, 2)

    def test_output_path_is_recorded_in_the_report(self) -> None:
        plan = load_plan(
            ["http://example.test/", "--count", "1", "--output", "run.json"]
        )
        self.assertEqual(plan.output_path, "run.json")


class ProgressTests(unittest.TestCase):
    def test_counter_and_lines(self) -> None:
        tracker = ProgressTracker()
        self.assertEqual(tracker.completed, 0)
        tracker.record(success_sample())
        tracker.record(success_sample())
        self.assertEqual(tracker.completed, 2)
        self.assertEqual(tracker.line(7.4, 10), "[7s] 2/10 attempts")
        self.assertEqual(tracker.line(3.2, None), "[3s] 2 attempts")

    def test_non_terminal_stderr_stays_quiet(self) -> None:
        plan = load_plan(["http://example.test/", "--count", "1"])
        errors = io.StringIO()
        self.assertFalse(errors.isatty())
        code = run_with_mocks(plan, stderr=errors)
        self.assertEqual(code, 0)
        self.assertEqual(errors.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
