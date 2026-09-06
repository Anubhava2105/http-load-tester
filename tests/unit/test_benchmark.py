"""Argument validation and smoke coverage for the benchmark harness."""

import json
import unittest

from benchmarks.run_benchmark import (
    BENCHMARK_VERSION,
    collect_environment,
    parse_args,
    run_benchmark,
)
from http_load_tester.domain.errors import ConfigurationError


class BenchmarkArgsTests(unittest.TestCase):
    def test_defaults_describe_a_short_local_run(self) -> None:
        args = parse_args([])
        self.assertIsNone(args.target_url)
        self.assertEqual(args.scenario, "fixed")
        self.assertIsNone(args.request_count)
        self.assertIsNone(args.duration_seconds)
        self.assertEqual(args.warmup, 1.0)
        self.assertEqual(args.workers, 2)
        self.assertEqual(args.max_connections, 2)

    def test_count_and_duration_are_mutually_exclusive(self) -> None:
        with self.assertRaises(ConfigurationError):
            parse_args(["--count", "10", "--duration", "5"])

    def test_non_positive_workers_are_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            parse_args(["--workers", "0", "--count", "4"])

    def test_unknown_scenario_is_rejected(self) -> None:
        with self.assertRaises(ConfigurationError):
            parse_args(["--scenario", "slowloris", "--count", "4"])

    def test_open_loop_without_rate_is_rejected_at_build(self) -> None:
        args = parse_args(["--load-model", "open_loop", "--count", "4"])
        with self.assertRaises(ConfigurationError):
            run_benchmark(args)


class BenchmarkEnvironmentTests(unittest.TestCase):
    def test_environment_carries_required_metadata(self) -> None:
        environment = collect_environment()
        for key in (
            "python_version",
            "python_implementation",
            "os_system",
            "os_release",
            "machine",
            "cpu_count",
        ):
            self.assertIn(key, environment)
        self.assertTrue(environment["python_version"])


class BenchmarkSmokeTests(unittest.TestCase):
    def test_short_local_benchmark_completes_with_full_schema(self) -> None:
        args = parse_args(["--count", "4", "--warmup", "0", "--workers", "1"])
        result = run_benchmark(args)
        self.assertEqual(result["benchmark_version"], BENCHMARK_VERSION)
        self.assertIn("machine", result["environment"])
        workload = result["workload"]
        for key in (
            "target_url",
            "target_kind",
            "scenario",
            "method",
            "path",
            "request_body_bytes",
            "workers",
            "max_connections",
            "warmup_seconds",
            "request_count",
            "duration_seconds",
            "load_model",
            "target_rate",
            "metrics_mode",
            "metrics_reservoir_size",
            "request_timeout_seconds",
            "pool_timeout_seconds",
        ):
            self.assertIn(key, workload)
        self.assertEqual(workload["target_kind"], "local")
        self.assertTrue(workload["target_url"].startswith("http://127.0.0.1:"))
        results = result["results"]
        self.assertEqual(results["total_attempts"], 4)
        for stream in (
            "request_latency_ns",
            "end_to_end_latency_ns",
            "pool_wait_ns",
            "time_to_first_byte_ns",
        ):
            self.assertIn("p50", results[stream])
            self.assertIn("p95", results[stream])
            self.assertIn("p99", results[stream])
        self.assertIn("connection_reuse_ratio", results)
        self.assertIn("memory", result)
        json.dumps(result)


if __name__ == "__main__":
    unittest.main()
