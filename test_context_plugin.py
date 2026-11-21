"""Pass the currently running pytest node ID into spawned test processes."""

from __future__ import annotations

import os

import pytest


@pytest.hookimpl(hookwrapper=True, tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    previous = os.environ.get("BLAST_TEST_NODEID")
    os.environ["BLAST_TEST_NODEID"] = item.nodeid
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("BLAST_TEST_NODEID", None)
        else:
            os.environ["BLAST_TEST_NODEID"] = previous
