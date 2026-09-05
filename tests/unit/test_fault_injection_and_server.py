import socket
import unittest

from http_load_tester.domain.models import HttpRequest
from http_load_tester.load.fault_injection import FaultInjector, FaultMode, FaultPolicy
from test_server import Scenario, ScenarioConfig, ScenarioServer


class FaultInjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = HttpRequest(
            "POST",
            "/",
            headers=(("Content-Length", "3"),),
            body=b"old",
        )

    def test_connection_churn_is_explicit_and_deterministic(self) -> None:
        decision = FaultInjector(
            FaultPolicy(FaultMode.CONNECTION_CHURN, seed=44)
        ).apply(self.request, 1)

        self.assertTrue(decision.applied)
        self.assertTrue(decision.force_connection_close)
        self.assertIn(("Connection", "close"), decision.request.headers)

    def test_randomized_body_uses_seed_and_rewrites_length(self) -> None:
        policy = FaultPolicy(FaultMode.RANDOMIZED_BODY, randomized_body_bytes=8, seed=9)
        first = FaultInjector(policy).apply(self.request, 1)
        second = FaultInjector(policy).apply(self.request, 1)

        self.assertEqual(first.request.body, second.request.body)
        self.assertEqual(len(first.request.body), 8)
        self.assertNotIn(("Content-Length", "3"), first.request.headers)

    def test_probability_can_disable_an_attempt(self) -> None:
        decision = FaultInjector(
            FaultPolicy(FaultMode.SLOW_REQUEST_BODY, probability=0, seed=1)
        ).apply(self.request, 1)
        self.assertFalse(decision.applied)


class ScenarioServerTests(unittest.TestCase):
    def test_fixed_server_responds_and_can_be_used_twice(self) -> None:
        with ScenarioServer(ScenarioConfig(body=b"hello")) as server:
            with socket.create_connection(server.address, timeout=2) as connection:
                connection.sendall(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
                first = connection.recv(1024)
                self.assertIn(b"Content-Length: 5", first)
                self.assertIn(b"hello", first)
                connection.sendall(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
                second = connection.recv(1024)
                self.assertIn(b"hello", second)

    def test_chunked_scenario_contains_chunk_terminator(self) -> None:
        with ScenarioServer(
            ScenarioConfig(scenario=Scenario.CHUNKED, body=b"hello")
        ) as server:
            with socket.create_connection(server.address, timeout=2) as connection:
                connection.sendall(b"GET / HTTP/1.1\r\nHost: test\r\n\r\n")
                response = connection.recv(1024)
                self.assertIn(b"Transfer-Encoding: chunked", response)
                self.assertIn(b"0\r\n\r\n", response)
