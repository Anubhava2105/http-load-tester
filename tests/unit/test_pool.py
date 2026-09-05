import threading
import time
import unittest

from http_load_tester.domain.errors import ConnectionClosedEarly, PoolAcquireTimeout
from http_load_tester.domain.models import Origin, TimeoutConfig
from http_load_tester.pool.connection_pool import ConnectionPool


class FakeSession:
    def __init__(self, identifier: int) -> None:
        self.identifier = identifier
        self.reusable = True
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1
        self.reusable = False


class SessionFactory:
    def __init__(self) -> None:
        self.created: list[FakeSession] = []
        self._next_identifier = 1

    def __call__(self) -> FakeSession:
        session = FakeSession(self._next_identifier)
        self._next_identifier += 1
        self.created.append(session)
        return session


class ConnectionPoolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.origin = Origin("http", "example.test", 80)
        self.timeouts = TimeoutConfig()
    
    def test_pool_size_one_reuses_an_idle_session(self) -> None:
        factory = SessionFactory()
        pool = ConnectionPool(self.origin, 1, self.timeouts, session_factory=factory)

        first = pool.acquire()
        session = first.connection
        first.release(True)
        second = pool.acquire()

        self.assertIs(second.connection, session)
        self.assertEqual(len(factory.created), 1)
        self.assertEqual(pool.live_connections, 1)
        second.release(True)
        pool.close_all()

    def test_pool_size_greater_than_one_has_a_hard_bound(self) -> None:
        factory = SessionFactory()
        pool = ConnectionPool(self.origin, 2, self.timeouts, session_factory=factory)

        first = pool.acquire()
        second = pool.acquire()

        self.assertEqual(pool.live_connections, 2)
        self.assertEqual(pool.leased_connections, 2)
        self.assertEqual(len(factory.created), 2)

        third_result: list[object] = []
        entered = threading.Event()

        def wait_for_third() -> None:
            entered.set()
            try:
                third_result.append(pool.acquire(time.monotonic_ns() + 1_000_000_000))
            except BaseException as exc:
                third_result.append(exc)

        thread = threading.Thread(target=wait_for_third)
        thread.start()
        self.assertTrue(entered.wait(1))
        time.sleep(0.01)
        self.assertEqual(pool.live_connections, 2)
        self.assertEqual(len(factory.created), 2)

        first.release(True)
        thread.join(timeout=1)
        self.assertEqual(len(third_result), 1)
        third = third_result[0]
        self.assertFalse(isinstance(third, BaseException))
        assert not isinstance(third, BaseException)
        self.assertIs(third.connection, first.connection)
        second.release(True)
        third.release(True)
        pool.close_all()

    def test_expired_deadline_does_not_create_a_connection(self) -> None:
        factory = SessionFactory()
        pool = ConnectionPool(self.origin, 1, self.timeouts, session_factory=factory)

        with self.assertRaises(PoolAcquireTimeout):
            pool.acquire(time.monotonic_ns() - 1)

        self.assertEqual(factory.created, [])
        pool.close_all()
    def test_pool_acquire_timeout_does_not_create_a_connection(self) -> None:
        factory = SessionFactory()
        pool = ConnectionPool(self.origin, 1, self.timeouts, session_factory=factory)
        lease = pool.acquire()

        with self.assertRaises(PoolAcquireTimeout):
            pool.acquire(time.monotonic_ns() + 10_000_000)

        self.assertEqual(len(factory.created), 1)
        lease.release(True)
        pool.close_all()

    def test_failed_session_is_discarded(self) -> None:
        factory = SessionFactory()
        pool = ConnectionPool(self.origin, 1, self.timeouts, session_factory=factory)
        lease = pool.acquire()
        session = lease.connection
        session.reusable = False
        lease.release(False)

        replacement = pool.acquire()
        self.assertIsNot(replacement.connection, session)
        self.assertEqual(session.close_count, 1)
        self.assertEqual(len(factory.created), 2)
        replacement.release(True)
        pool.close_all()

    def test_shutdown_wakes_blocked_acquire_and_closes_active_sessions(self) -> None:
        factory = SessionFactory()
        pool = ConnectionPool(self.origin, 1, self.timeouts, session_factory=factory)
        active = pool.acquire()
        result: list[object] = []
        started = threading.Event()

        def wait_for_connection() -> None:
            started.set()
            try:
                result.append(pool.acquire())
            except BaseException as exc:
                result.append(exc)

        thread = threading.Thread(target=wait_for_connection)
        thread.start()
        self.assertTrue(started.wait(1))
        time.sleep(0.01)

        pool.close_all()
        thread.join(timeout=1)

        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], ConnectionClosedEarly)
        self.assertTrue(factory.created[0].close_count >= 1)
        self.assertEqual(pool.live_connections, 0)
        active.release(False)


if __name__ == "__main__":
    unittest.main()
