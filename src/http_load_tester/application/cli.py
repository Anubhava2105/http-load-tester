"""Command-line entry point and exit-code handling."""

from __future__ import annotations

from collections.abc import Sequence
import sys

from ..domain.errors import ConfigurationError, RawLoadError
from ..observability.report import ExitCode
from .config import load_plan
from .runner import run_plan


def main(argv: Sequence[str] | None = None) -> int:
    try:
        plan = load_plan(argv)
    except (ConfigurationError, RawLoadError, ValueError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return int(ExitCode.INVALID_CONFIGURATION)
    return run_plan(plan)


if __name__ == "__main__":
    raise SystemExit(main())
