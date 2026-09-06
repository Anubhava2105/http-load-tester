# http-load-tester

A from-first-principles HTTP/1.1 load tester and client-side chaos client.
It opens real TCP and TLS sockets with the Python standard library only,
runs bounded closed- or open-loop workloads, and reports latency, errors,
and connection reuse. No third-party HTTP clients, no retries, no
redirects.

## How it works

Each responsibility lives in its own package under `src/http_load_tester`:

- `domain`: immutable test plans, result samples, and stable error
  categories. An attempt is one scheduled request execution. A test plan
  is the validated description of a run before any socket opens.
- `http`: URL parsing, request encoding, response framing, and sessions.
- `transport`: blocking TCP and TLS sockets with deadlines.
- `pool`: a bounded per-origin connection pool. Pool wait is the time an
  attempt spends waiting for a connection, reported separately from
  request latency.
- `load`: closed- and open-loop schedulers, the worker executor, and
  deterministic fault injection.
- `observability`: sample collection, metrics, and versioned reports.
- `application`: CLI parsing and the run composition root.

Three latencies stay separate everywhere: request latency starts when the
request bytes begin to leave, end-to-end latency starts at the scheduled
timestamp, and time to first byte covers write end to first response
byte. A reusable connection is one whose complete response was consumed
and whose framing allows another sequential exchange.

## Installation

Requires Python 3.11 or newer. Runtime dependencies are empty.

    pip install .
    pip install -e .   # editable checkout for development

Verify the install:

    http-load-tester --help
    http-load-tester --version
    python -m http_load_tester --help

## Quick start: HTTP

Start the deterministic scenario server in one terminal:

    PYTHONPATH=src:. python -m test_server --scenario fixed

It prints something like `serving fixed at http://127.0.0.1:58873/`.
Run four requests against that URL in a second terminal:

    http-load-tester http://127.0.0.1:58873/ --count 4 --workers 1

A real run looks like this (addresses and timings vary per run):

```text
Target:       http://127.0.0.1:58873/
Mode:         closed_loop
Workers:      1
Connections:  1
Request count: 4
Metrics:      exact

Attempts:     4
Responses:    4
Errors:       0 (0.00%)
Throughput:   88.474 req/s
Reuse ratio:  75.00%

Latency:
  p50        0.157 ms
  p95        0.616 ms
  p99        0.677 ms

End-to-end latency:
  p50        0.189 ms
  p95        37.383 ms
  p99        42.628 ms

Pool wait:
  p50        0.008 ms
  p95        36.727 ms
  p99        41.910 ms

Status codes:
  200                    4
```

## HTTPS example

TLS verifies certificates by default. Point at a real HTTPS origin:

    http-load-tester https://example.test:443/health --count 10

Disable verification only for local testing, and override the server
name when the certificate expects a different one:

    http-load-tester https://127.0.0.1:8443/ --count 10 --insecure --server-name example.test

## Load models

Closed-loop (default) schedules each worker's next attempt only after
its previous attempt completes:

    http-load-tester http://127.0.0.1:58873/ --count 100 --workers 4 --max-connections 4

Send a different method, headers, or body when the target needs it
(`-X` also spells `--method`):

    http-load-tester http://127.0.0.1:58873/items --count 10 -X POST \
        --header "Content-Type: text/plain" --body "payload"

Open-loop schedules attempts against a monotonic timeline at the target
rate, without waiting for earlier attempts:

    http-load-tester http://127.0.0.1:58873/ --duration 10 --load-model open_loop --rate 25

`--load-model` also spells `--mode`. Fixed-count runs stop after the count. Fixed-duration runs stop after
the clock. Warmup seconds (`--warmup`) delay the start without counting
toward results.

## Fault injection

Faults are explicit, seeded, and recorded per attempt. Pick a mode with
`--fault-mode`, tune it with `--fault-probability`, `--fault-delay`,
`--fault-body-bytes`, and `--fault-seed`:

| Mode | Effect |
| --- | --- |
| `none` | No fault (default). |
| `ramp_up` | Labels attempts; reserved decision point, no behavior change. |
| `traffic_spike` | Labels attempts; reserved decision point, no behavior change. |
| `connection_churn` | Forces `Connection: close` so every attempt reconnects. |
| `slow_request_body` | Sleeps `fault-delay` seconds before sending. |
| `abort_after_headers` | Closes the connection instead of sending. |
| `abort_during_response` | Closes the connection after the response arrives. |
| `short_read_timeout` | Caps the attempt deadline at `fault-delay` seconds. |
| `randomized_body` | Replaces the body with seeded random bytes. |

Applied faults show per sample and as totals in both report formats.

## Reports

Terminal reports read as above. JSON reports (`--format json`) carry a
stable `schema_version` of `1.0` with `configuration`, covering target,
counts, workers, timeouts, limits, and fault and metrics settings, plus
`results`, covering `total_attempts`, `successes`, `http_errors`,
`error_rate`, `throughput_requests_per_second`, byte totals,
`connection_reuse_ratio`, `status_codes`, `outcomes`, `error_categories`,
transport, protocol, timeout, cancelled, and pool-acquire counts,
percentile maps for request, end-to-end, first-byte, and pool-wait
latencies, and fault totals. Bounded-mode reports add
`metrics_mode`, `approximate_percentiles`, and `metrics_reservoir_size`
so approximate percentiles are never mistaken for exact ones.

An HTTP 500 is a valid HTTP response, reported under `http_errors` with
its status code, never as a transport failure. Timeouts, resets,
early closes, and framing errors land in `error_categories` with the
stable taxonomy from `domain/errors.py`.

Save any report to a file as well as stdout with `--output`:

    http-load-tester http://127.0.0.1:58873/ --count 100 --format json --output /tmp/run.json

Long runs print live progress to stderr, one line every two seconds with
elapsed time and completed attempts, but only on interactive terminals.
Piped output stays clean for machine reading.

Exit codes:

| Code | Meaning |
| --- | --- |
| 0 | Completed with no errors. |
| 1 | Completed, but some attempts failed. |
| 2 | The run itself failed, or the report file could not be written. |
| 3 | Invalid configuration or CLI usage. |
| 130 | Interrupted; a partial report is rendered first. |

## Safety limits

Plans are validated before any socket opens. Defaults:

| Limit | Default |
| --- | --- |
| `max_workers` | 100 |
| `max_connections` | 100 |
| `max_requests` | 1000000 |
| `max_duration_seconds` | 86400 |
| `max_request_body_bytes` | 10485760 |
| `max_response_body_bytes` | 104857600 |
| `max_header_bytes` | 65536 |
| `max_header_count` | 100 |
| `max_total_runtime_seconds` | 86400 |

Over-limit plans fail fast with a configuration error. Request and pool
timeouts default to 30 seconds (`--request-timeout`, `--pool-timeout`);
DNS, connect, TLS handshake, write, and read phases each default to 5
seconds. `--max-runtime` caps warmup plus duration against the total
runtime ceiling.

## Metrics

Latency percentiles come in two modes. The default keeps every sample and
reports exact percentiles. Bounded mode keeps a fixed recent window per
latency stream and reports approximate percentiles. Both modes track the
same four streams separately: request latency, end-to-end latency, pool
wait, and time to first byte.

- Exact (default): every attempt is retained, so p50, p90, p95, and p99 are
  exact. Memory grows with the number of attempts.
- Bounded: at most `reservoir-size` values are kept per stream, so latency
  memory stays within `4 * reservoir-size` values no matter how long the
  run lasts. Counts, byte totals, status and error breakdowns, and the
  reuse ratio stay exact. Only percentiles are approximate, and reports
  mark them as such.

The window holds the most recent values, so percentiles describe recent
behavior rather than the whole run. A small reservoir reacts fast but
noisy. A large reservoir is smoother but holds more memory. Start with
the default 1024 per stream and lower it only when long runs press on
memory.

Select bounded mode explicitly:

    http-load-tester http://127.0.0.1:8000/ --count 100000 \
        --metrics-mode bounded --metrics-reservoir-size 256

Bounded reports say `approximate (bounded, reservoir 256)` in terminal
output and carry `metrics_mode`, `approximate_percentiles`, and
`metrics_reservoir_size` fields in JSON output. Reservoir size must be
between 1 and 1000000.

## Supported HTTP behavior

- HTTP/1.1 over TCP, and HTTPS over TLS with verification on by default
- `Content-Length` responses
- Chunked responses, including extensions and trailers
- Close-delimited responses (complete but not reusable)
- Persistent connections with keep-alive reuse

## Unsupported behavior

- HTTP/2 and HTTP/3
- Redirects: 3xx responses are reported, never followed
- Retries: every attempt runs once; failures are reported, not retried
- Proxies
- HTTP pipelining
- Distributed load generation
- Unlimited body capture: bodies are counted within the configured
  response-body limit, never retained without bound

## Scenario server

The server under `test_server/` sends deterministic raw HTTP responses
for local tests and demos. It is not a production server.

    PYTHONPATH=src:. python -m test_server --scenario fixed
    PYTHONPATH=src:. python -m test_server --scenario chunked
    PYTHONPATH=src:. python -m test_server --scenario delayed_headers
    PYTHONPATH=src:. python -m test_server --scenario delayed_body
    PYTHONPATH=src:. python -m test_server --scenario forced_close
    PYTHONPATH=src:. python -m test_server --scenario truncated
    PYTHONPATH=src:. python -m test_server --scenario large_response
    PYTHONPATH=src:. python -m test_server --scenario intermittent_500
    PYTHONPATH=src:. python -m test_server --scenario abrupt_reset
    PYTHONPATH=src:. python -m test_server --help

`--port 0` picks an ephemeral port and prints the URL. The server is
HTTP-only by design.

## Benchmarks

Methodology, warmup policy, and interpretation rules live in
`benchmarks/README.md`. Short local measurement:

    PYTHONPATH=src python benchmarks/run_benchmark.py --count 50 --warmup 0

All benchmark numbers are machine- and workload-specific. Never compare
across setups unless the environment and workload are controlled.

## Validation

From a clean checkout, with `PYTHONPATH=src:.` on Windows PowerShell:

    $env:PYTHONPATH="src;."; python3 -m compileall -q src tests test_server benchmarks
    $env:PYTHONPATH="src;."; python3 -m unittest discover -s tests
    $env:PYTHONPATH="src;."; python3 -m http_load_tester --help
    $env:PYTHONPATH="src;."; python3 -m test_server --help

On POSIX shells the same commands use `PYTHONPATH=src:.`. CI runs the
same steps plus a clean `pip install` and the installed
`http-load-tester --help`. See `.github/workflows/ci.yml`.
