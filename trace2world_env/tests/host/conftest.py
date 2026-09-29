"""Shared fixtures: a tiny synthetic release with one browser-evaluated environment."""

from __future__ import annotations

from pathlib import Path

import pytest
from host_fixtures import make_environment, write_json


@pytest.fixture
def release_dir(tmp_path: Path) -> Path:
    """A release with one environment ``demo`` and two tasks."""
    make_environment(tmp_path / "environments" / "demo")
    write_json(
        tmp_path / "environments_manifest.json",
        {
            "format": "trace2world.environment_manifest.v1",
            "environment_count": 1,
            "task_count": 2,
            "environments": [{"id": "demo", "task_count": 2}],
        },
    )
    return tmp_path
