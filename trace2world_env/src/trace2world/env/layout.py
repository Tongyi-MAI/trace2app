
import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


_RESOURCE_FORMAT = "ghostweb_agentforge_webenv_resource.v2"
_PORTFOLIO_FORMATS = frozenset({
    "trace2world.environment_manifest.v1",
})
_SAFE_APP_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


@dataclass(frozen=True)
class WebEnvLayout:

    static_dir: Path
    tasks_file: Path | None
    evaluators_dir: Path | None
    entry_page: str
    resource_dir: Path | None = None
    resource_manifest: dict[str, Any] | None = None
    evaluator_plan: Path | None = None
    strict_evaluation_ready: bool = False
    strict_evaluation_gaps: tuple[str, ...] = ()


@dataclass(frozen=True)
class WebEnvPortfolioLayout:

    root: Path
    apps: dict[str, WebEnvLayout]
    tasks: list[dict[str, Any]]
    task_to_app: dict[str, str]
    manifest: dict[str, Any]


def _task_id(task: dict[str, Any]) -> str:
    return str(task.get("task_id") or task.get("id") or "").strip()


def resolve_start_page(static_dir: Path, start_page: str) -> str:
    raw_value = str(start_page or "").strip()
    if raw_value == "/":
        return "/"
    value = raw_value.lstrip("/")
    candidates = [value] if value else []
    if value and not value.startswith("phase_pagegen/pages/"):
        candidates.append(f"phase_pagegen/pages/{Path(value).name}")
    for candidate in candidates:
        if (static_dir / candidate).is_file():
            return candidate
    return value


def load_and_normalize_tasks(
    tasks_file: Path | None,
    static_dir: Path,
) -> list[dict[str, Any]]:
    if tasks_file is None or not tasks_file.is_file():
        return []
    payload = json.loads(tasks_file.read_text(encoding="utf-8"))
    raw_tasks = payload.get("tasks", []) if isinstance(payload, dict) else []
    tasks: list[dict[str, Any]] = []
    for raw in raw_tasks:
        if not isinstance(raw, dict):
            continue
        task_id = _task_id(raw)
        if not task_id:
            continue
        task = dict(raw)
        task["task_id"] = task_id
        task.setdefault("id", task_id)
        task["instruction"] = str(
            task.get("instruction")
            or task.get("name")
            or task.get("description")
            or ""
        )
        start_page = str(task.get("start_page") or task.get("start_filename") or "")
        task["start_page"] = resolve_start_page(static_dir, start_page)
        tasks.append(task)
    return tasks


def resolve_resource_layout(
    resource_dir: str | Path,
    *,
    allowed_validation_statuses: set[str] | frozenset[str] | None = None,
) -> WebEnvLayout:
    root = Path(resource_dir).expanduser().resolve()
    manifest_path = root / "resource_manifest.json"
    if not manifest_path.is_file():
        raise ValueError(f"Trace2World resource manifest is missing: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    resource_format = manifest.get("format")
    if resource_format != _RESOURCE_FORMAT:
        raise ValueError(f"Unsupported Trace2World WebEnv resource format: {manifest.get('format')}")
    validation_path = root / "package_validation_report.json"
    if not validation_path.is_file():
        raise ValueError(f"Trace2World package validation report is missing: {validation_path}")
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    accepted_statuses = allowed_validation_statuses or frozenset({"pass"})
    validation_status = str(validation.get("status") or "")
    if validation_status not in accepted_statuses:
        raise ValueError(
            "Trace2World package validation status is not allowed: "
            f"status={validation_status!r}, allowed={sorted(accepted_statuses)}, "
            f"report={validation_path}"
        )
    if validation_status != "pass":
        logger.warning(
            "Hosting package with explicit non-pass validation override: "
            "status=%s report=%s",
            validation_status,
            validation_path,
        )
    app_root = root / str(manifest.get("app_root") or "app")
    tasks_file = root / str(manifest.get("tasks_file") or "tasks.json")
    entry_page = str(manifest.get("entry_page") or "").lstrip("/")
    evaluator = manifest.get("evaluator") if isinstance(manifest.get("evaluator"), dict) else {}
    runtime_profile = str(manifest.get("runtime_profile") or "")
    evaluation_enabled = bool(evaluator.get("enabled", True))
    evaluator_plan = (
        root / str(evaluator.get("plan") or "evaluators/state_evaluator_plan.json")
        if evaluation_enabled
        else None
    )
    evaluator_directory = root / str(evaluator.get("directory") or "evaluators")
    required = {
        "app root": app_root,
        "tasks file": tasks_file,
        "entry page": app_root / entry_page,
    }
    if evaluation_enabled:
        required["evaluator plan"] = evaluator_plan
    missing = [f"{label}: {path}" for label, path in required.items() if not path.exists()]
    if missing:
        raise ValueError("Invalid Trace2World WebEnv resource; missing " + "; ".join(missing))
    if runtime_profile == "state_kernel_sqlite":
        state_service = manifest.get("state_service")
        if not isinstance(state_service, dict):
            raise ValueError("state_kernel_sqlite resource requires state_service manifest")
        entrypoint = root / str(state_service.get("entrypoint") or "")
        if not entrypoint.is_file():
            raise ValueError(f"Packaged state-service entrypoint is missing: {entrypoint}")
        strict_evaluation_ready = False
        strict_evaluation_gaps: tuple[str, ...] = ()
        if evaluation_enabled:
            from trace2world.env.evaluator_contract import (
                validate_state_kernel_evaluator_plan,
            )

            tasks = load_and_normalize_tasks(tasks_file, app_root)
            validation = validate_state_kernel_evaluator_plan(evaluator_plan, tasks)
            strict_evaluation_ready = bool(validation["portfolio_release_ready"])
            strict_evaluation_gaps = tuple(validation["strict_evaluation_gaps"])
        evaluators_dir = None
    elif runtime_profile == "browser_checkpoint_events":
        if evaluation_enabled and not evaluator_directory.is_dir():
            raise ValueError(
                "Packaged browser evaluator directory is missing: "
                f"{evaluator_directory}"
            )
        if evaluation_enabled:
            from trace2world.env.evaluator_contract import (
                validate_browser_evaluator_directory,
                validate_browser_evaluator_plan,
            )

            validate_browser_evaluator_directory(evaluator_directory)
            tasks = load_and_normalize_tasks(tasks_file, app_root)
            validate_browser_evaluator_plan(
                evaluator_plan,
                evaluator_directory,
                tasks,
            )
        evaluators_dir = evaluator_directory if evaluation_enabled else None
    else:
        raise ValueError(f"Unsupported Trace2World runtime profile: {runtime_profile}")
    if runtime_profile != "state_kernel_sqlite":
        strict_evaluation_ready = False
        strict_evaluation_gaps = ()
    return WebEnvLayout(
        static_dir=app_root,
        tasks_file=tasks_file,
        evaluators_dir=evaluators_dir,
        entry_page=entry_page,
        resource_dir=root,
        resource_manifest=manifest,
        evaluator_plan=evaluator_plan,
        strict_evaluation_ready=strict_evaluation_ready,
        strict_evaluation_gaps=strict_evaluation_gaps,
    )


def resolve_portfolio_layout(
    portfolio_dir: str | Path,
    *,
    allowed_validation_statuses: set[str] | frozenset[str] | None = None,
) -> WebEnvPortfolioLayout:
    root = Path(portfolio_dir).expanduser().resolve()
    manifest_path = next(
        (
            candidate
            for candidate in (
                root / "environments_manifest.json",
                root / "portfolio_manifest.json",
            )
            if candidate.is_file()
        ),
        None,
    )
    if manifest_path is None:
        raise ValueError(
            f"Trace2World environment manifest is missing: {root / 'environments_manifest.json'}"
        )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") not in _PORTFOLIO_FORMATS:
        raise ValueError(
            f"Unsupported Trace2World environment manifest format: {manifest.get('format')}"
        )
    rows = manifest.get("environments") or manifest.get("apps")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Trace2World environment list must be non-empty")

    apps: dict[str, WebEnvLayout] = {}
    tasks: list[dict[str, Any]] = []
    task_to_app: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Trace2World environment row must be an object")
        slug = str(row.get("id") or row.get("app") or "").strip()
        if not _SAFE_APP_SLUG.fullmatch(slug):
            raise ValueError(f"Invalid Trace2World environment id: {slug!r}")
        if slug in apps:
            raise ValueError(f"Duplicate Trace2World environment id: {slug}")
        environment_dir = next(
            (
                candidate
                for candidate in (
                    root / "environments" / slug,
                    root / "resources" / slug,
                )
                if candidate.is_dir()
            ),
            root / "environments" / slug,
        )
        layout = resolve_resource_layout(
            environment_dir,
            allowed_validation_statuses=allowed_validation_statuses,
        )
        apps[slug] = layout
        app_tasks = load_and_normalize_tasks(layout.tasks_file, layout.static_dir)
        declared_count = row.get("task_count")
        if declared_count is not None and int(declared_count) != len(app_tasks):
            raise ValueError(
                f"Environment task count mismatch for {slug}: "
                f"declared={declared_count}, actual={len(app_tasks)}"
            )
        for task in app_tasks:
            task_id = str(task["task_id"])
            previous = task_to_app.get(task_id)
            if previous is not None:
                raise ValueError(
                    f"Task id collision: {task_id} in {previous} and {slug}"
                )
            task_to_app[task_id] = slug
            tasks.append(task)

    declared_envs = manifest.get("environment_count", manifest.get("package_count"))
    declared_tasks = manifest.get("task_count")
    if declared_envs is not None and int(declared_envs) != len(apps):
        raise ValueError(
            f"Environment count mismatch: declared={declared_envs}, actual={len(apps)}"
        )
    if declared_tasks is not None and int(declared_tasks) != len(tasks):
        raise ValueError(
            f"Portfolio task count mismatch: declared={declared_tasks}, actual={len(tasks)}"
        )
    return WebEnvPortfolioLayout(
        root=root,
        apps=apps,
        tasks=tasks,
        task_to_app=task_to_app,
        manifest=manifest,
    )
