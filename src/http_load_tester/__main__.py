"""Run the http-load-tester command with python -m."""

from .application.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
