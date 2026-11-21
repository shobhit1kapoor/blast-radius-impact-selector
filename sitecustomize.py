"""Keep pytest's test context when coverage starts in a child process."""

from __future__ import annotations

import os

try:
    import coverage

    current = coverage.Coverage.current()
    nodeid = os.environ.get("BLAST_TEST_NODEID")
    if current is not None and nodeid:
        current.switch_context(f"{nodeid}|subprocess")
except Exception:
    pass
