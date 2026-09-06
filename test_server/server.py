"""Threaded standard-library HTTP scenario server."""

from __future__ import annotations

import socket
import socketserver
import struct
import threading
import time

from .scenarios import Scenario, ScenarioConfig, chunked_response, response_headers


# Abrupt-reset shape: declare more than we send, flush a prefix, then RST.
# The oversize keeps a valid client blocked in body read so the reset
# lands mid-response instead of looking like a clean EOF after full body.
_RESET_DECLARED_OVERHEAD = 64
_RESET_FLUSH_DELAY = 0.02


class ScenarioServer:
    """Serve deterministic raw HTTP responses on a local TCP port."""

    def __init__(
        self,
        config: ScenarioConfig | None = None,
        *,
        host: str = "127.0.0.1",
    ) -> None:
        self.config = config or ScenarioConfig()
        self._server = _ThreadingServer((host, 0), _ScenarioHandler, self.config)
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address

    @property
    def url(self) -> str:
        host, port = self.address
        return f"http://{host}:{port}/"

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="http-scenario-server",
            daemon=True,
        )
        self._thread.start()

    def close(self) -> None:
        if self._thread is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=2)
        self._thread = None

    def __enter__(self) -> ScenarioServer:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class _ThreadingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[socketserver.BaseRequestHandler],
        config: ScenarioConfig,
    ) -> None:
        self.config = config
        self.request_count = 0
        self.request_count_lock = threading.Lock()
        super().__init__(address, handler)


class _ScenarioHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        while True:
            try:
                if self._read_request() is None:
                    return
            except (OSError, ValueError):
                # Client went away mid-request: timeout, cancel, or RST
                # from delay and reset tests. Test server only, so stop
                # this connection without logging a traceback.
                return
            with self.server.request_count_lock:
                self.server.request_count += 1
                request_number = self.server.request_count
            try:
                if not self._write_response(request_number):
                    return
            except OSError:
                # Client timed out or reset while we were sending.
                # Same policy as above: drop this connection quietly.
                return

    def _read_request(self) -> bytes | None:
        self.request.settimeout(2)
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return None
            data.extend(chunk)
            if len(data) > 64 * 1024:
                return None
        header_end = data.index(b"\r\n\r\n") + 4
        content_length = 0
        for line in data[:header_end].split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":", 1)[1].strip())
        remaining = content_length - (len(data) - header_end)
        while remaining > 0:
            chunk = self.request.recv(min(4096, remaining))
            if not chunk:
                return None
            remaining -= len(chunk)
        return bytes(data)

    def _write_response(self, request_number: int) -> bool:
        config = self.server.config
        scenario = config.scenario
        if scenario is Scenario.ABRUPT_RESET:
            prefix = config.body[: len(config.body) // 2]
            try:
                self.request.sendall(
                    response_headers(
                        200, len(config.body) + _RESET_DECLARED_OVERHEAD, close=False
                    )
                    + prefix
                )
                # Let the partial body reach the client before the RST
                # so the fault reads as mid-response, not clean EOF.
                time.sleep(_RESET_FLUSH_DELAY)
            except OSError:
                pass
            _send_tcp_reset(self.request)
            return False
        if scenario is Scenario.DELAYED_HEADERS:
            time.sleep(config.delay_seconds)
        if scenario is Scenario.INTERMITTENT_500 and request_number % 2 == 0:
            self.request.sendall(response_headers(500, 0))
            return True
        body = (
            b"x" * config.large_body_bytes
            if scenario is Scenario.LARGE_RESPONSE
            else config.body
        )
        close = scenario in {Scenario.FORCED_CLOSE, Scenario.TRUNCATED}
        if scenario is Scenario.CHUNKED:
            self.request.sendall(
                chunked_response(body, close=close, chunk_size=config.chunk_size)
            )
        elif scenario is Scenario.TRUNCATED:
            self.request.sendall(response_headers(200, len(body) + 1, close=True) + body)
            return False
        elif scenario is Scenario.DELAYED_BODY:
            self.request.sendall(response_headers(200, len(body), close=False))
            for offset in range(0, len(body), config.chunk_size):
                self.request.sendall(body[offset : offset + config.chunk_size])
                if offset + config.chunk_size < len(body):
                    time.sleep(config.delay_seconds)
        else:
            self.request.sendall(response_headers(200, len(body), close=close) + body)
        return not close


def _send_tcp_reset(connection: socket.socket) -> None:
    """Abort the connection with a TCP RST where the OS permits it."""
    try:
        connection.setsockopt(
            socket.SOL_SOCKET,
            socket.SO_LINGER,
            struct.pack("ii", 1, 0),
        )
    except OSError:
        pass
    try:
        connection.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
