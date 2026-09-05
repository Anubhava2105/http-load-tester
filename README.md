# http-load-tester

http-load-tester is a from-first-principles HTTP/1.1 load tester and client-side
fault-injection tool.

The project is being built incrementally to make the networking behavior
reviewable. It will use blocking sockets and the Python standard library for
runtime behavior. HTTP request serialization, response framing, connection
reuse, bounded pooling, scheduling, metrics, and reporting will remain
separate responsibilities.

## Constraints

- Python 3.11 or newer.
- Zero third-party runtime dependencies.
- HTTP/1.1 over TCP and TLS.
- No requests, httpx, urllib3, aiohttp, or equivalent HTTP client libraries.
- No automatic retries, redirects, HTTP pipelining, or distributed execution.
- Response bodies are counted or explicitly bounded when captured; they are not
  retained without a limit.

The implementation starts with a blocking, thread-based design. An asyncio
adapter is a possible future extension only after the blocking implementation
has been tested and benchmarked.

## Development

The baseline test suite uses Python's built-in unittest module:

    PYTHONPATH=src python -m unittest discover -s tests -v

Runtime dependencies are intentionally empty. Build tooling may be added
separately from runtime dependencies when packaging is introduced.

## Status

This repository currently contains the project skeleton. Protocol and load
generation behavior will be added in small, tested checkpoints.
