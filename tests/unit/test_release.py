"""Release polish: docs match code, entry points work, clean install holds."""

import os
import re
import subprocess
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run(*argv: str, timeout: int = 60) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = "src" + os.pathsep + str(ROOT)
    return subprocess.run(
        argv, cwd=ROOT, env=env, capture_output=True, text=True, timeout=timeout
    )


class PackageMetadataTests(unittest.TestCase):
    def test_pyproject_matches_the_package(self) -> None:
        with open(ROOT / "pyproject.toml", "rb") as handle:
            metadata = tomllib.load(handle)["project"]
        import http_load_tester

        self.assertEqual(metadata["name"], "http-load-tester")
        self.assertEqual(metadata["version"], http_load_tester.__version__)
        self.assertIn("3.11", metadata["requires-python"])
        self.assertEqual(metadata["dependencies"], [])
        self.assertTrue((ROOT / metadata["readme"]).exists())
        self.assertEqual(
            metadata["scripts"]["http-load-tester"],
            "http_load_tester.application.cli:main",
        )


class ReadmeMatchesCodeTests(unittest.TestCase):
    def test_every_cli_flag_is_documented(self) -> None:
        from http_load_tester.application.config import create_parser

        documented = (ROOT / "README.md").read_text(encoding="utf-8")
        missing = [
            option
            for action in create_parser()._actions
            for option in action.option_strings
            if option.startswith("--") and option not in documented
        ]
        self.assertEqual(missing, [])

    def test_report_fields_are_documented(self) -> None:
        documented = (ROOT / "README.md").read_text(encoding="utf-8")
        for field in (
            "schema_version",
            "throughput_requests_per_second",
            "connection_reuse_ratio",
            "error_categories",
            "approximate_percentiles",
            "metrics_reservoir_size",
        ):
            self.assertIn(field, documented)


class EntryPointTests(unittest.TestCase):
    def test_module_execution_shows_help(self) -> None:
        completed = _run(sys.executable, "-m", "http_load_tester", "--help")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("usage:", completed.stdout)

    def test_scenario_server_help_works(self) -> None:
        completed = _run(sys.executable, "-m", "test_server", "--help")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--scenario", completed.stdout)

    def test_scenario_server_example_serves(self) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = "src" + os.pathsep + str(ROOT)
        server = subprocess.Popen(
            [sys.executable, "-m", "test_server", "--scenario", "fixed"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            line = server.stdout.readline()
            self.assertTrue(line.startswith("serving fixed at http://127.0.0.1:"))
        finally:
            server.terminate()
            server.wait(timeout=10)


class CleanInstallTests(unittest.TestCase):
    def test_install_exposes_the_console_script(self) -> None:
        with tempfile.TemporaryDirectory() as home:
            venv = Path(home) / "release-check"
            subprocess.run(
                [sys.executable, "-m", "venv", str(venv)],
                check=True,
                capture_output=True,
                timeout=120,
            )
            scripts = venv / ("Scripts" if os.name == "nt" else "bin")
            python = scripts / ("python.exe" if os.name == "nt" else "python")
            try:
                subprocess.run(
                    [str(python), "-m", "pip", "install", str(ROOT)],
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                script = scripts / (
                    "http-load-tester.exe" if os.name == "nt" else "http-load-tester"
                )
                completed = subprocess.run(
                    [str(script), "--help"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(completed.returncode, 0)
                self.assertIn("usage:", completed.stdout)
                completed = subprocess.run(
                    [str(python), "-m", "http_load_tester", "--help"],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(completed.returncode, 0)
            finally:
                self._remove_build_artifacts()

    @staticmethod
    def _remove_build_artifacts() -> None:
        import shutil

        for name in ("build", "dist", "src/http_load_tester.egg-info"):
            target = ROOT / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
