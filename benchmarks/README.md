# Benchmarks

This folder measures the blocking load tester. It never checks correctness.
Correctness checks live in `tests/`. Nothing here compares against other
tools, and nothing here prints a number that was not measured in the run
it describes.

Every result is machine- and workload-specific. A number from one laptop
says nothing about another machine, another target, or another workload
unless the environment and workload are controlled and recorded alongside.

## Environment requirements

- Python 3.11 or newer, standard library only. No third-party runtime
  dependencies; the harness imports `resource` only when present.
- A quiet local machine for the default path. Close competing load first.
- The local scenario server needs loopback TCP only. No external network
  unless you pass `--target-url` explicitly.

## Commands

Short local smoke measurement (fast, loopback only):

    PYTHONPATH=src python benchmarks/run_benchmark.py --count 50 --warmup 0

Default five-second local run:

    PYTHONPATH=src python benchmarks/run_benchmark.py --duration 5 --workers 2 --max-connections 2

Explicit external target (only when you mean it):

    PYTHONPATH=src python benchmarks/run_benchmark.py --target-url http://192.0.2.10:8000/ --duration 10

Save the JSON document as well:

    PYTHONPATH=src python benchmarks/run_benchmark.py --count 1000 --output /tmp/bench.json

## Workload definition

The JSON document records the full workload, so a run can be repeated:

- target URL, target kind (`local` or `external`), and local scenario
  (`fixed`, `chunked`, `large_response`)
- request shape: method, path, and request body size in bytes
- payload size: the served body for local scenarios is the scenario
  default; record any change to the scenario config with the result
- worker count and pool size (`--workers`, `--max-connections`)
- test duration or request count, plus the target rate for open-loop runs
- warmup seconds, request and pool timeouts, metrics mode and reservoir

## Warmup policy

Each run starts with a warmup phase using the same request shape,
workers, and pool size. Warmup attempts are discarded and never enter
the measured percentiles. Default warmup is one second. Use `--warmup 0`
only for smoke checks, not for numbers you keep.

## Output

The harness prints one JSON document to stdout. Fields:

- `environment`: Python version and implementation, OS, machine, CPU count
- `workload`: everything listed above
- `results`: attempts, successes, HTTP errors, error rate, throughput,
  p50, p95, and p99 for request, end-to-end, pool-wait, and first-byte
  latencies, connection reuse ratio, bytes sent and received
- `memory`: peak RSS where the platform reports it, plus the traced
  Python allocation peak for the measured phase; either may be null
  where unavailable

## Interpretation

- Read p95 and p99 together with the error rate and attempt count. A
  fast p99 over a run with errors is not a good result.
- The reuse ratio tells whether the pool did its job. Near zero on a
  keep-alive target means connections churned.
- Memory fields describe the benchmark process during the measured
  phase, not the target server.
- For long or high-rate runs, prefer `--metrics-mode bounded`. Exact
  mode retains every sample; bounded mode keeps a fixed recent window
  and marks percentiles approximate. See the metrics section in the
  top-level README.

## Comparison rules

Do not compare against other tools or other runs unless the workload
and the environment are identical and recorded. Same target, same
request shape, same workers, same pool, same duration, same machine
state. Otherwise the comparison measures the setup, not the code.
