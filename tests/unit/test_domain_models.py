import unittest

from http_load_tester.domain.errors import ConfigurationError, ErrorCategory, ReadTimeout
from http_load_tester.domain.models import (
    HttpRequest,
    HttpResponseSummary,
    LoadModel,
    Origin,
    Outcome,
    ResultSample,
    SafetyLimits,
    TestPlan,
    TimeoutConfig,
)


class DomainModelTests(unittest.TestCase):
    def test_origin_normalizes_scheme_and_ipv6_brackets(self) -> None:
        origin = Origin("HTTPS", "[::1]", 8443)
        self.assertEqual(origin.scheme, "https")
        self.assertEqual(origin.hostname, "::1")

    def test_request_is_immutable_and_headers_are_tupled(self) -> None:
        request = HttpRequest("get", "/health", {"Accept": "identity"})
        self.assertEqual(request.method, "GET")
        self.assertEqual(request.headers, (("Accept", "identity"),))
        with self.assertRaises(AttributeError):
            request.method = "POST"

    def test_plan_requires_exactly_one_workload_bound(self) -> None:
        origin = Origin("http", "localhost", 8080)
        request = HttpRequest("GET", "/")
        with self.assertRaises(ConfigurationError):
            TestPlan(origin, request)
        with self.assertRaises(ConfigurationError):
            TestPlan(origin, request, request_count=1, duration_seconds=1.0)

    def test_plan_validates_open_loop_rate_and_limits(self) -> None:
        origin = Origin("http", "localhost", 8080)
        request = HttpRequest("POST", "/", body=b"payload")
        limits = SafetyLimits(max_workers=2, max_connections=2, max_requests=5)
        plan = TestPlan(
            origin,
            request,
            request_count=3,
            workers=2,
            max_connections=2,
            load_model=LoadModel.OPEN_LOOP,
            target_rate=10,
            limits=limits,
        )
        self.assertEqual(plan.request_count, 3)
        with self.assertRaises(ConfigurationError):
            TestPlan(
                origin,
                request,
                request_count=6,
                limits=limits,
            )

    def test_result_sample_exposes_separate_duration_definitions(self) -> None:
        sample = ResultSample(
            request_id="request-1",
            scheduled_ns=10,
            worker_start_ns=20,
            pool_acquire_start_ns=20,
            pool_acquire_end_ns=30,
            write_start_ns=40,
            write_end_ns=50,
            first_byte_ns=60,
            completion_ns=80,
            status_code=200,
            outcome=Outcome.SUCCESS,
            error_category=None,
            bytes_sent=10,
            bytes_received=20,
        )
        self.assertEqual(sample.request_latency_ns, 40)
        self.assertEqual(sample.end_to_end_latency_ns, 70)
        self.assertEqual(sample.pool_wait_ns, 10)
        self.assertEqual(sample.time_to_first_byte_ns, 10)

    def test_incomplete_response_cannot_be_reusable(self) -> None:
        with self.assertRaises(ConfigurationError):
            HttpResponseSummary("HTTP/1.1", 200, body_complete=False, connection_reusable=True)

    def test_error_category_is_stable(self) -> None:
        error = ReadTimeout("read deadline expired")
        self.assertEqual(error.category, ErrorCategory.READ_TIMEOUT)

    def test_invalid_headers_are_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            HttpRequest("GET", "/", {"X-Bad\r\n": "value"})


    def test_timeout_values_must_be_finite(self) -> None:
        with self.assertRaises(ConfigurationError):
            TimeoutConfig(read_seconds=float("inf"))

    def test_open_loop_rate_type_is_validated_as_configuration(self) -> None:
        origin = Origin("http", "localhost", 8080)
        request = HttpRequest("GET", "/")
        with self.assertRaises(ConfigurationError):
            TestPlan(
                origin,
                request,
                request_count=1,
                load_model=LoadModel.OPEN_LOOP,
                target_rate="as-fast-as-possible",
            )

    def test_result_sample_completion_cannot_precede_write_end(self) -> None:
        with self.assertRaises(ConfigurationError):
            ResultSample(
                request_id="request-1",
                scheduled_ns=0,
                worker_start_ns=0,
                pool_acquire_start_ns=0,
                pool_acquire_end_ns=0,
                write_start_ns=0,
                write_end_ns=10,
                first_byte_ns=None,
                completion_ns=5,
                status_code=None,
                outcome=Outcome.TRANSPORT_ERROR,
                error_category=ErrorCategory.WRITE_TIMEOUT,
            )
if __name__ == "__main__":
    unittest.main()
