"""Run the pinned suite once to record per-test coverage for the impact map."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path("/opt/sqlglot").resolve()
PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"

actual = (ROOT / ".source-revision").read_text(encoding="ascii").strip()
if actual != PIN:
    raise SystemExit(f"wrong SQLGlot revision: {actual}")
env = os.environ.copy()
env["PYTHONPATH"] = f"/app:{ROOT}"
env["SKIP_INTEGRATION"] = "0"
env["COVERAGE_CORE"] = "ctrace"
subprocess.run(
    [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "--cov=sqlglot",
        "--cov-context=test",
        "--cov-report=",
        "--disable-warnings",
        "--cov-config=/app/.coveragerc",
        "-p",
        "test_context_plugin",
        "tests",
    ],
    cwd=ROOT,
    env=env,
    check=True,
)
