"""Shared test fixtures for the core package."""

from pathlib import Path

import pytest


@pytest.fixture
def sqlite_policy_path(tmp_path: Path) -> Path:
    """Give every disk-backed integration test its own authoritative database."""
    return tmp_path / "policies.sqlite3"
