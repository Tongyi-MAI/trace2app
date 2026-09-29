"""Load a Trace2World release (or a single environment) from disk.

A release directory holds ``environments_manifest.json`` and ``environments/<id>/``. Each
environment directory holds ``resource_manifest.json`` (format
``trace2world_environment_resource.v2``), ``package_validation_report.json`` (status ``pass``),
``tasks.json``, the static app, and its evaluators. Loading validates the manifests, task
coverage of the evaluator plans, and the declared counts, so a host never starts on a partial
release.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from trace2world_env.actions.vocabulary import SCROLL_DELTA
from trace2world_env.scoring.plans import (
    validate_browser_evaluator_directory,
    validate_browser_evaluator_plan,
    validate_state_kernel_evaluator_plan,
)

RESOURCE_FORMAT = "trace2world_environment_resource.v2"
RELEASE_FORMAT = "trace2world.environment_manifest.v1"
RELEASE_MANIFEST = "environments_manifest.json"
BROWSER_CHECKPOINT_EVENTS = "browser_checkpoint_events"
STATE_KERNEL_SQLITE = "state_kernel_sqlite"
_SAFE_ENVIRONMENT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")

# Every shipped environment declares this phone-sized profile; the fallback matches it so an
# environment that omits the declaration cannot change the screenshot size a policy sees.
DEFAULT_VIEWPORT = (430, 884)
DEFAULT_DEVICE_SCALE_FACTOR = 2.0


@dataclass(frozen=True)
class BrowserProfile:
    """Viewport (CSS pixels), device scale factor, and scroll step of an environment."""

    width: int = DEFAULT_VIEWPORT[0]
    height: int = DEFAULT_VIEWPORT[1]
    device_scale_factor: float = DEFAULT_DEVICE_SCALE_FACTOR
    scroll_delta: int = SCROLL_DELTA

    @classmethod
    def from_manifest(cls, manifest: dict[str, Any]) -> BrowserProfile:
        """Read ``browser_viewport``, ``browser_device_scale_factor``, ``browser_scroll_delta``."""
        width, height = DEFAULT_VIEWPORT
        if "browser_viewport" in manifest:
            viewport = manifest["browser_viewport"]
            if not isinstance(viewport, dict):
                raise ValueError("resource_manifest.browser_viewport must be an object")
            width = viewport.get("width")
            height = viewport.get("height")
            if (
                isinstance(width, bool)
                or isinstance(height, bool)
                or not isinstance(width, int)
                or not isinstance(height, int)
                or width <= 0
                or height <= 0
            ):
                raise ValueError(
                    "resource_manifest.browser_viewport must contain positive integer "
                    "width and height"
                )
        scale = DEFAULT_DEVICE_SCALE_FACTOR
        if "browser_device_scale_factor" in manifest:
            raw_scale = manifest["browser_device_scale_factor"]
            if isinstance(raw_scale, bool):
                raise ValueError("resource_manifest.browser_device_scale_factor must be positive")
            try:
                scale = float(raw_scale)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "resource_manifest.browser_device_scale_factor must be positive"
                ) from exc
            if scale <= 0:
                raise ValueError("resource_manifest.browser_device_scale_factor must be positive")
        scroll_delta = SCROLL_DELTA
        if "browser_scroll_delta" in manifest:
            scroll_delta = manifest["browser_scroll_delta"]
            if (
                isinstance(scroll_delta, bool)
                or not isinstance(scroll_delta, int)
                or scroll_delta <= 0
            ):
                raise ValueError(
                    "resource_manifest.browser_scroll_delta must be a positive integer"
                )
        return cls(
            width=width, height=height, device_scale_factor=scale, scroll_delta=scroll_delta
        )

    @property
    def viewport(self) -> dict[str, int]:
        """The viewport as ``{"width", "height"}``."""
        return {"width": self.width, "height": self.height}


@dataclass(frozen=True)
class Task:
    """One task of an environment."""

    task_id: str
    environment: str
    instruction: str
    start_page: str
    initial_state: dict[str, Any] | None
    source_reference_task_id: str | None
    available_apps: list[str] | None
    runtime_profile: str

    def summary(self) -> dict[str, Any]:
        """The public task row served by ``GET /tasks``."""
        return {
            "task_id": self.task_id,
            "environment": self.environment,
            "instruction": self.instruction,
            "start_page": self.start_page,
            "initial_state": self.initial_state,
            "runtime_profile": self.runtime_profile,
        }


@dataclass(frozen=True)
class Environment:
    """One validated environment directory."""

    environment_id: str
    root: Path
    app_dir: Path
    entry_page: str
    manifest: dict[str, Any]
    runtime_profile: str
    evaluator_plan: Path
    evaluators_dir: Path | None
    browser_profile: BrowserProfile
    tasks: list[Task] = field(default_factory=list)

    @property
    def uses_state_kernel(self) -> bool:
        """Whether episodes run against the packaged SQLite state service."""
        return self.runtime_profile == STATE_KERNEL_SQLITE


@dataclass(frozen=True)
class Catalog:
    """All hosted environments and a task index."""

    environments: dict[str, Environment]
    tasks: dict[str, Task]

    def task(self, task_id: str) -> Task:
        """The task with ``task_id``; raises ``KeyError`` if unknown."""
        return self.tasks[task_id]

    def environment_of(self, task: Task) -> Environment:
        """The environment a task belongs to."""
        return self.environments[task.environment]


def resolve_start_page(app_dir: Path, start_page: str) -> str:
    """Resolve a task start page relative to the app root.

    Accepts a path under the app root or a bare page name under ``phase_pagegen/pages/``;
    unresolved values are returned without their leading slash.
    """
    raw_value = str(start_page or "").strip()
    if raw_value == "/":
        return "/"
    value = raw_value.lstrip("/")
    candidates = [value] if value else []
    if value and not value.startswith("phase_pagegen/pages/"):
        candidates.append(f"phase_pagegen/pages/{Path(value).name}")
    for candidate in candidates:
        if (app_dir / candidate).is_file():
            return candidate
    return value


def load_task_rows(tasks_file: Path, app_dir: Path) -> list[dict[str, Any]]:
    """Read ``tasks.json`` rows with ``task_id``, ``instruction``, and resolved ``start_page``."""
    if not tasks_file.is_file():
        return []
    payload = json.loads(tasks_file.read_text(encoding="utf-8"))
    raw_tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
    tasks: list[dict[str, Any]] = []
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            continue
        task_id = str(raw.get("task_id") or raw.get("id") or "").strip()
        if not task_id:
            continue
        task = dict(raw)
        task["task_id"] = task_id
        task["instruction"] = str(
            task.get("instruction") or task.get("name") or task.get("description") or ""
        )
        task["start_page"] = resolve_start_page(app_dir, str(task.get("start_page") or ""))
        tasks.append(task)
    return tasks


def _build_task(
    row: dict[str, Any],
    environment_id: str,
    manifest: dict[str, Any],
    runtime_profile: str,
    entry_page: str,
) -> Task:
    task_id = row["task_id"]
    aliases = row.get("available_apps") or row.get("apps") or manifest.get("available_apps")
    if aliases is not None:
        if (
            not isinstance(aliases, list)
            or not aliases
            or not all(isinstance(alias, str) and alias.strip() for alias in aliases)
        ):
            raise ValueError(
                f"task {task_id!r} available app aliases must be a non-empty string list"
            )
        aliases = [alias.strip() for alias in aliases]
    initial_state = row.get("initial_state")
    source_reference_task_id = row.get("source_reference_task_id")
    return Task(
        task_id=task_id,
        environment=environment_id,
        instruction=row["instruction"],
        start_page=row.get("start_page") or entry_page,
        initial_state=initial_state if isinstance(initial_state, dict) else None,
        source_reference_task_id=(
            source_reference_task_id
            if isinstance(source_reference_task_id, str) and source_reference_task_id
            else None
        ),
        available_apps=aliases,
        runtime_profile=runtime_profile,
    )


def load_environment(
    environment_dir: str | Path,
    environment_id: str | None = None,
) -> Environment:
    """Validate and load one environment directory (``environment_id`` defaults to its name)."""
    root = Path(environment_dir).expanduser().resolve()
    environment_id = environment_id or root.name
    if not _SAFE_ENVIRONMENT_ID.fullmatch(environment_id):
        raise ValueError(f"Invalid Trace2World environment id: {environment_id!r}")
    manifest_path = root / "resource_manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Trace2World resource manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != RESOURCE_FORMAT:
        raise ValueError(
            f"Unsupported Trace2World environment resource format: {manifest.get('format')}"
        )
    validation_path = root / "package_validation_report.json"
    if not validation_path.is_file():
        raise ValueError(f"Trace2World package validation report is missing: {validation_path}")
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation_status = str(validation.get("status") or "")
    if validation_status != "pass":
        raise ValueError(
            "Trace2World package validation status is not pass: "
            f"status={validation_status!r}, report={validation_path}"
        )
    app_dir = root / str(manifest.get("app_root") or "app")
    tasks_file = root / str(manifest.get("tasks_file") or "tasks.json")
    entry_page = str(manifest.get("entry_page") or "").lstrip("/")
    evaluator = manifest.get("evaluator") if isinstance(manifest.get("evaluator"), dict) else {}
    if not bool(evaluator.get("enabled", True)):
        raise ValueError(f"Environment {environment_id} disables evaluation; not supported")
    runtime_profile = str(manifest.get("runtime_profile") or "")
    evaluator_plan = root / str(evaluator.get("plan") or "evaluators/state_evaluator_plan.json")
    required = {
        "app root": app_dir,
        "tasks file": tasks_file,
        "entry page": app_dir / entry_page,
        "evaluator plan": evaluator_plan,
    }
    missing = [f"{label}: {path}" for label, path in required.items() if not path.exists()]
    if missing:
        raise ValueError("Invalid Trace2World environment resource; missing " + "; ".join(missing))

    task_rows = load_task_rows(tasks_file, app_dir)
    evaluators_dir: Path | None = None
    if runtime_profile == STATE_KERNEL_SQLITE:
        state_service = manifest.get("state_service")
        if not isinstance(state_service, dict):
            raise ValueError("state_kernel_sqlite resource requires state_service manifest")
        entrypoint = root / str(state_service.get("entrypoint") or "")
        if not entrypoint.is_file():
            raise ValueError(f"Packaged state-service entrypoint is missing: {entrypoint}")
        validate_state_kernel_evaluator_plan(evaluator_plan, task_rows)
    elif runtime_profile == BROWSER_CHECKPOINT_EVENTS:
        evaluators_dir = root / str(evaluator.get("directory") or "evaluators")
        if not evaluators_dir.is_dir():
            raise ValueError(f"Packaged browser evaluator directory is missing: {evaluators_dir}")
        validate_browser_evaluator_directory(evaluators_dir)
        validate_browser_evaluator_plan(evaluator_plan, evaluators_dir, task_rows)
    else:
        raise ValueError(f"Unsupported Trace2World runtime profile: {runtime_profile}")

    return Environment(
        environment_id=environment_id,
        root=root,
        app_dir=app_dir,
        entry_page=entry_page,
        manifest=manifest,
        runtime_profile=runtime_profile,
        evaluator_plan=evaluator_plan,
        evaluators_dir=evaluators_dir,
        browser_profile=BrowserProfile.from_manifest(manifest),
        tasks=[
            _build_task(row, environment_id, manifest, runtime_profile, entry_page)
            for row in task_rows
        ],
    )


def _catalog(environments: list[Environment]) -> Catalog:
    tasks: dict[str, Task] = {}
    for environment in environments:
        for task in environment.tasks:
            previous = tasks.get(task.task_id)
            if previous is not None:
                raise ValueError(
                    f"Task id collision: {task.task_id} in {previous.environment} "
                    f"and {environment.environment_id}"
                )
            tasks[task.task_id] = task
    return Catalog({env.environment_id: env for env in environments}, tasks)


def load_release(release_dir: str | Path) -> Catalog:
    """Load every environment listed in ``<release_dir>/environments_manifest.json``."""
    root = Path(release_dir).expanduser().resolve()
    manifest_path = root / RELEASE_MANIFEST
    if not manifest_path.is_file():
        raise ValueError(f"Trace2World environment manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != RELEASE_FORMAT:
        raise ValueError(
            f"Unsupported Trace2World environment manifest format: {manifest.get('format')}"
        )
    rows = manifest.get("environments")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Trace2World environment list must be non-empty")
    environments: list[Environment] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Trace2World environment row must be an object")
        environment_id = str(row.get("id") or "").strip()
        if not _SAFE_ENVIRONMENT_ID.fullmatch(environment_id):
            raise ValueError(f"Invalid Trace2World environment id: {environment_id!r}")
        if environment_id in seen:
            raise ValueError(f"Duplicate Trace2World environment id: {environment_id}")
        seen.add(environment_id)
        environment = load_environment(root / "environments" / environment_id, environment_id)
        declared_count = row.get("task_count")
        if declared_count is not None and int(declared_count) != len(environment.tasks):
            raise ValueError(
                f"Environment task count mismatch for {environment_id}: "
                f"declared={declared_count}, actual={len(environment.tasks)}"
            )
        environments.append(environment)
    catalog = _catalog(environments)
    declared_environments = manifest.get("environment_count")
    declared_tasks = manifest.get("task_count")
    if declared_environments is not None and int(declared_environments) != len(environments):
        raise ValueError(
            f"Environment count mismatch: declared={declared_environments}, "
            f"actual={len(environments)}"
        )
    if declared_tasks is not None and int(declared_tasks) != len(catalog.tasks):
        raise ValueError(
            f"Release task count mismatch: declared={declared_tasks}, actual={len(catalog.tasks)}"
        )
    return catalog


def load_catalog(path: str | Path) -> Catalog:
    """Load a release directory, or a single environment directory as a one-entry catalog."""
    root = Path(path).expanduser().resolve()
    if (root / RELEASE_MANIFEST).is_file():
        return load_release(root)
    return _catalog([load_environment(root)])
