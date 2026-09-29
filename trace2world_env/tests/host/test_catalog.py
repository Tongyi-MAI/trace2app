"""Catalog loading on a synthetic release."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from host_fixtures import make_environment, write_json

from trace2world_env.catalog import (
    BrowserProfile,
    load_catalog,
    load_environment,
    resolve_start_page,
)


def test_release_loads_tasks_and_environment(release_dir: Path) -> None:
    catalog = load_catalog(release_dir)
    assert list(catalog.environments) == ["demo"]
    environment = catalog.environments["demo"]
    assert environment.runtime_profile == "browser_checkpoint_events"
    assert environment.evaluators_dir == release_dir / "environments" / "demo" / "evaluators"
    assert environment.entry_page == "index.html"
    first, second = catalog.task("t1"), catalog.task("t2")
    assert first.start_page == "phase_pagegen/pages/detail.html"
    assert first.initial_state is None and first.available_apps is None
    # instruction falls back to description; empty start page falls back to the entry page
    assert second.instruction == "Say hello"
    assert second.start_page == "index.html"
    assert second.initial_state == {"collections": {}}
    assert second.available_apps == ["Demo", "demo-app"]
    assert second.source_reference_task_id == "ref_1"
    assert catalog.environment_of(second) is environment
    assert first.summary() == {
        "task_id": "t1",
        "environment": "demo",
        "instruction": "Open the detail page",
        "start_page": "phase_pagegen/pages/detail.html",
        "initial_state": None,
        "runtime_profile": "browser_checkpoint_events",
    }


def test_single_environment_dir_is_a_one_entry_catalog(release_dir: Path) -> None:
    catalog = load_catalog(release_dir / "environments" / "demo")
    assert list(catalog.environments) == ["demo"]
    assert sorted(catalog.tasks) == ["t1", "t2"]


def test_resolve_start_page(tmp_path: Path) -> None:
    (tmp_path / "phase_pagegen" / "pages").mkdir(parents=True)
    (tmp_path / "phase_pagegen" / "pages" / "a.html").write_text("")
    assert resolve_start_page(tmp_path, "/a.html") == "phase_pagegen/pages/a.html"
    assert resolve_start_page(tmp_path, "missing.html") == "missing.html"
    assert resolve_start_page(tmp_path, "/") == "/"
    assert resolve_start_page(tmp_path, "") == ""


def test_browser_profile_defaults_and_validation() -> None:
    assert BrowserProfile.from_manifest({}) == BrowserProfile(430, 884, 2.0, 200)
    profile = BrowserProfile.from_manifest(
        {
            "browser_viewport": {"width": 400, "height": 800},
            "browser_device_scale_factor": 3,
            "browser_scroll_delta": 150,
        }
    )
    assert profile == BrowserProfile(400, 800, 3.0, 150)
    assert profile.viewport == {"width": 400, "height": 800}
    for bad in (
        {"browser_viewport": {"width": True, "height": 800}},
        {"browser_device_scale_factor": 0},
        {"browser_scroll_delta": 1.5},
    ):
        with pytest.raises(ValueError):
            BrowserProfile.from_manifest(bad)


def _edit_json(path: Path, **changes: object) -> None:
    payload = json.loads(path.read_text())
    payload.update(changes)
    path.write_text(json.dumps(payload))


def test_rejects_legacy_resource_format(release_dir: Path) -> None:
    manifest = release_dir / "environments" / "demo" / "resource_manifest.json"
    _edit_json(manifest, format="some_other_resource.v2")
    with pytest.raises(ValueError, match="Unsupported Trace2World environment resource format"):
        load_catalog(release_dir)


def test_rejects_non_pass_validation(release_dir: Path) -> None:
    report = release_dir / "environments" / "demo" / "package_validation_report.json"
    _edit_json(report, status="stale")
    with pytest.raises(ValueError, match="not pass"):
        load_catalog(release_dir)


def test_rejects_count_mismatch(release_dir: Path) -> None:
    _edit_json(release_dir / "environments_manifest.json", task_count=3)
    with pytest.raises(ValueError, match="Release task count mismatch"):
        load_catalog(release_dir)


def test_rejects_task_id_collision(release_dir: Path) -> None:
    make_environment(release_dir / "environments" / "other")
    write_json(
        release_dir / "environments_manifest.json",
        {
            "format": "trace2world.environment_manifest.v1",
            "environments": [{"id": "demo"}, {"id": "other"}],
        },
    )
    with pytest.raises(ValueError, match="Task id collision: t1"):
        load_catalog(release_dir)


def test_rejects_missing_plan_entry(release_dir: Path) -> None:
    plan = release_dir / "environments" / "demo" / "evaluators" / "browser_evaluator_plan.json"
    plan.write_text(json.dumps({"evaluators": {"t1": {"evaluator_file": "t1.js"}}}))
    with pytest.raises(ValueError, match="missing plan entries=t2"):
        load_catalog(release_dir)


def test_rejects_empty_checkpoint_list_without_oracle(release_dir: Path) -> None:
    script = release_dir / "environments" / "demo" / "evaluators" / "t1.js"
    script.write_text("const EXPECTED = [];\nreturn 0;\n")
    with pytest.raises(ValueError, match="empty EXPECTED/REQUIRED"):
        load_catalog(release_dir)
    script.write_text(
        "const EXPECTED = [];\nconst TASK_SPECIFIC_ORACLE = true;\n"
        "const meta = {oracle: 'answer_matches'};\nreturn 0;\n"
    )
    assert load_catalog(release_dir).task("t1")


def test_rejects_disabled_evaluation(tmp_path: Path) -> None:
    root = make_environment(tmp_path / "demo")
    _edit_json(root / "resource_manifest.json", evaluator={"enabled": False})
    with pytest.raises(ValueError, match="disables evaluation"):
        load_environment(root)


def test_rejects_bad_alias_list(release_dir: Path) -> None:
    tasks = release_dir / "environments" / "demo" / "tasks.json"
    payload = json.loads(tasks.read_text())
    payload["tasks"][0]["available_apps"] = ["ok", ""]
    tasks.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="available app aliases"):
        load_catalog(release_dir)
