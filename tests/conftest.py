"""Session-wide test fixtures for BORG_ROUTE.

Several endpoints call `matrix.save()`, which writes the capability matrix
YAML to disk. Combined with e2e fixtures that flip every row to
``checked=True`` in memory before tests, this causes the on-disk seed to
drift between tests. The autouse fixture below snapshots the file before
every test and restores it afterwards so tests stay isolated regardless of
order.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_MATRIX_PATH = Path("data/capability_matrix.yaml")


@pytest.fixture(autouse=True)
def _preserve_matrix_yaml():
    """Snapshot data/capability_matrix.yaml around every test."""
    original = _MATRIX_PATH.read_text(encoding="utf-8") if _MATRIX_PATH.exists() else None
    try:
        yield
    finally:
        if original is not None:
            _MATRIX_PATH.write_text(original, encoding="utf-8")
