import json
import unittest

from http_load_tester.domain.errors import ErrorCategory
from http_load_tester.domain.models import HttpRequest, Origin, Outcome, ResultSample, TestPlan
from http_load_tester.observability.metrics import MetricsCollector
from http_load_tester.observability.renderers import render_json, render_terminal
from http_load_tester.observability.report import ExitCode, Report, exit_code_for


def sample(
    request_id: str,
    *,
    outcome: Outcome,
    status_code: int | None = None,
    error_category: ErrorCategory | None = None,
    reused: bool = False,
) -> ResultSample:
    return ResultSample(
        request_id=request_id,
        scheduled_ns=0,
        worker_start_ns=1,
        pool_acquire_start_ns=2,
        pool_acquire_end_ns=3,
        write_start_ns=4 if status_code is not None else None,
        write_end_ns=5 if status_code is not None else None,
        first_byte_ns=6 if status_code is not None else None,
        completion_ns=10,
        status_code=status_code,
        outcome=outcome,
        error_category=error_category,
        connection_reused=reused,
        bytes_sent=10 if status_code is not None else 0,
        bytes_received=20 if status_code is not None else 0,
    )


class ReportingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.plan = TestPlan(
            origin=Origin("http", "example.test", 80),
            request=HttpRequest("GET", "/health"),
            request_count=2,
            workers=1,
            max_connections=1,
        )
        metrics = MetricsCollector()
        metrics.add(sample("request-1", outcome=Outcome.SUCCESS, status_code=200))
        metrics.add(
            sample(
                "request-2",
                outcome=Outcome.TIMEOUT,
                error_category=ErrorCategory.POOL_ACQUIRE_TIMEOUT,
            )
        )
        self.report = Report.from_plan(metrics=metrics.snapshot(run_duration_ns=1_000_000_000), plan=self.plan)

    def test_json_is_versioned_and_distinguishes_error_types(self) -> None:
        rendered = json.loads(render_json(self.report))
        self.assertEqual(rendered["schema_version"], "1.0")
        self.assertEqual(rendered["configuration"]["target"], "http://example.test:80/health")
        self.assertEqual(rendered["results"]["responses"], 1)
        self.assertEqual(rendered["results"]["transport_error_count"], 0)
        self.assertEqual(rendered["results"]["pool_acquire_timeout_count"], 1)
        self.assertEqual(rendered["results"]["status_codes"], {"200": 1})
        self.assertEqual(rendered["results"]["error_categories"], {"pool_acquire_timeout": 1})

    def test_terminal_contains_configuration_and_key_sections(self) -> None:
        rendered = render_terminal(self.report)
        self.assertIn("Target:       http://example.test:80/health", rendered)
        self.assertIn("Attempts:     2", rendered)
        self.assertIn("Pool acquisition errors:", rendered)
        self.assertIn("pool_acquire_timeout", rendered)

    def test_exit_codes_prioritize_configuration_and_execution_failures(self) -> None:
        self.assertEqual(exit_code_for(self.report), ExitCode.COMPLETED_WITH_ERRORS)
        self.assertEqual(
            exit_code_for(self.report, execution_failed=True),
            ExitCode.EXECUTION_FAILURE,
        )
        self.assertEqual(
            exit_code_for(self.report, configuration_failed=True),
            ExitCode.INVALID_CONFIGURATION,
        )
        success_metrics = MetricsCollector()
        success_metrics.add(sample("request-1", outcome=Outcome.SUCCESS, status_code=200))
        success_report = Report.from_plan(self.plan, success_metrics.snapshot())
        self.assertEqual(exit_code_for(success_report), ExitCode.SUCCESS)


    def test_json_report_includes_fault_counts(self) -> None:
        from http_load_tester.domain.models import FaultMode, FaultPolicy
        from dataclasses import replace as dc_replace
        plan_with_fault = dc_replace(
            self.plan,
            fault_policy=FaultPolicy(mode=FaultMode.CONNECTION_CHURN, seed=1),
        )
        metrics = MetricsCollector()
        metrics.add(sample("request-1", outcome=Outcome.SUCCESS, status_code=200))
        report = Report.from_plan(plan_with_fault, metrics.snapshot(run_duration_ns=1_000_000_000))
        payload = report.to_dict()
        self.assertIn("fault_applied_count", payload["results"])
        self.assertIn("fault_mode_counts", payload["results"])
        self.assertEqual(
            payload["configuration"]["fault_mode"],
            "connection_churn",
        )
