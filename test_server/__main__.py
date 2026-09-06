"""Run the deterministic scenario server until interrupted."""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Sequence

from .scenarios import Scenario, ScenarioConfig
from .server import ScenarioServer


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="test_server",
        description="Serve deterministic HTTP responses for local tests.",
    )
    parser.add_argument(
        "--scenario",
        choices=tuple(scenario.value for scenario in Scenario),
        default=Scenario.FIXED.value,
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = create_parser().parse_args(argv)
    if not 0 <= args.port <= 65535:
        print("port must be between 0 and 65535", file=sys.stderr)
        return 2
    config = ScenarioConfig(scenario=Scenario(args.scenario))
    server = ScenarioServer(config, host=args.host, port=args.port)
    server.start()
    print(f"serving {config.scenario.value} at {server.url}", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        server.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
