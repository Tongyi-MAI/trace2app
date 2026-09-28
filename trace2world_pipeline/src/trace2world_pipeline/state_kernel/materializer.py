from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Mapping

from trace2world_pipeline.state_kernel.backends.sqlite import materialize_sqlite_databases
from trace2world_pipeline.state_kernel.canonical_json import canonical_hash

# Import prefix of this package and the name it is vendored under inside an
# exported environment (`backend/python/environment_state/`).
_SOURCE_PACKAGE = "trace2world_pipeline.state_kernel"
_VENDORED_PACKAGE = "environment_state"
_FRONTEND_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates" / "frontend"


def materialize_environment_state_runtime(
    *,
    output_dir: str | Path,
    artifacts: Mapping[str, Any],
    world: Mapping[str, list[Any]],
) -> dict[str, Any]:
    output = Path(output_dir)
    runtime_dir = output / "phase_sdk_generation" / "runtime"
    backend_dir = runtime_dir / "backend"
    contracts_dir = backend_dir / "contracts"
    contracts_dir.mkdir(parents=True, exist_ok=True)
    expected_world_hash = artifacts["storage_plan"]["world_hash"]
    actual_world_hash = canonical_hash(dict(world))
    if actual_world_hash != expected_world_hash:
        raise ValueError(
            "world materialization hash mismatch: "
            f"expected={expected_world_hash}, actual={actual_world_hash}"
        )
    for name, payload in artifacts.items():
        (contracts_dir / f"{name}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    report = materialize_sqlite_databases(
        world_db_path=backend_dir / "world.sqlite",
        state_template_path=backend_dir / "state.sqlite.template",
        artifacts=artifacts,
        world=world,
    )
    source_root = Path(__file__).resolve().parent
    vendored_root = backend_dir / "python" / "environment_state"
    if vendored_root.exists():
        deprecated_root = backend_dir / "deprecated"
        deprecated_root.mkdir(parents=True, exist_ok=True)
        target = deprecated_root / "environment_state_previous"
        if target.exists():
            target = deprecated_root / f"environment_state_previous_{len(list(deprecated_root.iterdir()))}"
        shutil.move(str(vendored_root), str(target))
    # The state kernel is vendored into the environment as a self-contained
    # `environment_state` package so the exported environment runs without the
    # constructor installed; rewrite the package-qualified imports accordingly.
    shutil.copytree(
        source_root,
        vendored_root,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "drivers"),
    )
    for vendored_file in vendored_root.rglob("*.py"):
        text = vendored_file.read_text(encoding="utf-8")
        rewritten = text.replace(_SOURCE_PACKAGE, _VENDORED_PACKAGE)
        if rewritten != text:
            vendored_file.write_text(rewritten, encoding="utf-8")
    runner = backend_dir / "run_state_service.py"
    runner.write_text(
        """from pathlib import Path
import os
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "python"))
from environment_state.http_server import run_server

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--result-root", default=str(HERE.parents[2]))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-db-path", default=os.environ.get("T2W_STATE_DB_PATH"))
    args = parser.parse_args()
    run_server(
        result_root=args.result_root,
        runtime_root=HERE,
        host=args.host,
        port=args.port,
        state_db_path=args.state_db_path,
    )
""",
        encoding="utf-8",
    )
    prepare = backend_dir / "prepare_session.py"
    prepare.write_text(
        """from pathlib import Path
import argparse
import json
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "python"))
from environment_state.service import EnvironmentStateService

parser = argparse.ArgumentParser()
parser.add_argument("--sid", required=True)
parser.add_argument("--patch-file")
args = parser.parse_args()
service = EnvironmentStateService(HERE)
service.create(args.sid)
patch = {}
if args.patch_file:
    patch = json.loads(Path(args.patch_file).read_text(encoding="utf-8"))
if patch:
    service.dispatch("inject", sid=args.sid, payload=patch)
service.dispatch("seal_initial", sid=args.sid)
""",
        encoding="utf-8",
    )
    client_source = _FRONTEND_TEMPLATES_DIR / "environment_state_client.js"
    shutil.copy2(client_source, runtime_dir / "environment_state_client.js")
    manifest = {
        "format": "ghostweb_environment_state_service_manifest",
        "schema_revision": 1,
        "runtime_profile": "state_kernel_sqlite",
        "backend": "sqlite",
        "entrypoint": "backend/run_state_service.py",
        "world_db": "backend/world.sqlite",
        "state_db_template": "backend/state.sqlite.template",
        "state_db_runtime": "backend/state.sqlite",
        "state_db_path_configurable": True,
        "healthcheck": "/api/v1/health",
        "static_root": "../../..",
        "schema_hash": artifacts["storage_plan"]["schema_hash"],
        "world_hash": artifacts["storage_plan"]["world_hash"],
        "bundle_hash": artifacts["storage_plan"]["bundle_hash"],
        "redis_runtime_state_dependency": False,
    }
    (backend_dir / "service_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report["service_manifest"] = str(backend_dir / "service_manifest.json")
    (backend_dir / "materialization_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"manifest": manifest, "materialization_report": report, "runtime_dir": str(backend_dir)}
