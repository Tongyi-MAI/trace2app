

from __future__ import annotations

import argparse
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
from pathlib import Path
from threading import Thread
from typing import Any, Iterator
from urllib.parse import unquote, urlparse


from trace2world_pipeline.verification.contract_instrumentation import (
    copy_env_without_deprecated,
    run_contract_instrumentation,
)
from trace2world_pipeline.verification.executable_evaluator import compile_executable_evaluator
from trace2world_pipeline.verification.negative_probe_executor import execute_negative_probes
from trace2world_pipeline.verification.runtime_bridge_injector import run as inject_runtime_bridge
from trace2world_pipeline.verification.runtime_checkpoint_collector import (
    _collect_runtime_checkpoint_snapshots_parallel,
    _load_trace_ids,
    _runtime_validation_output_path,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Trace2World verification over a result directory")
    parser.add_argument("--config", required=True, help="Path to instrumentation-only config JSON")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = json.loads(config_path.read_text())

    source_env_dir = _resolve_path(config["source_env_dir"])
    output_dir = _resolve_path(config["output_dir"])
    if config.get("copy_runtime_and_pages", False):
        copy_env_without_deprecated(source_env_dir, output_dir)

    output_flags = _contract_output_flags(config)
    result = run_contract_instrumentation(
        output_dir,
        source_env_dir=source_env_dir,
        terminal_contract_root=config.get("terminal_contract_root"),
        clear_existing=config.get("clear_existing_instrumentation", True),
        semantic_checkpoint_planner=config.get("semantic_checkpoint_planner") or config.get("instrumentation", {}).get("semantic_checkpoint_planner"),
        ignore_semantic_obligation_matrix=config.get("ignore_semantic_obligation_matrix", False)
        or config.get("instrumentation", {}).get("ignore_semantic_obligation_matrix", False),
        **output_flags,
    )
    reward_oracle_result = _run_reward_oracle_steps(output_dir, config)
    payload: dict[str, Any] = result.__dict__
    if reward_oracle_result:
        payload["reward_oracle_artifacts"] = reward_oracle_result
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def _resolve_path(value: str) -> Path:
    # Relative paths in the config resolve against the current working directory.
    return Path(value).expanduser().resolve()


def _instrumentation_artifacts_only(config: dict[str, Any]) -> bool:
    instrumentation_cfg = config.get("instrumentation") if isinstance(config.get("instrumentation"), dict) else {}
    scope = (
        instrumentation_cfg.get("output_scope")
        or instrumentation_cfg.get("artifact_scope")
        or config.get("instrumentation_output_scope")
    )
    return str(scope or "").strip().lower() in {
        "instrumentation",
        "instrumentation_only",
        "instrumentation_artifacts",
        "instrumentation_artifacts_only",
    }


def _contract_output_flags(config: dict[str, Any]) -> dict[str, bool]:
    instrumentation_cfg = config.get("instrumentation") if isinstance(config.get("instrumentation"), dict) else {}
    if _instrumentation_artifacts_only(config):
        return {
            "generate_evaluators": False,
            "generate_test_logic": False,
            "generate_instrumented_business_logic": bool(
                instrumentation_cfg.get("generate_instrumented_business_logic", False)
            ),
        }
    return {
        "generate_evaluators": bool(instrumentation_cfg.get("generate_evaluators", True)),
        "generate_test_logic": bool(instrumentation_cfg.get("generate_test_logic", True)),
        "generate_instrumented_business_logic": bool(
            instrumentation_cfg.get("generate_instrumented_business_logic", True)
        ),
    }


def _run_reward_oracle_steps(output_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    instrumentation_cfg = config.get("instrumentation") if isinstance(config.get("instrumentation"), dict) else {}
    reward_cfg = config.get("reward_oracle") if isinstance(config.get("reward_oracle"), dict) else {}
    merged_cfg = {**instrumentation_cfg, **reward_cfg}
    if _instrumentation_artifacts_only(config):
        merged_cfg["generate_executable_evaluator"] = False

    result: dict[str, Any] = {}
    if _enabled(merged_cfg, "generate_executable_evaluator"):
        manifest = compile_executable_evaluator(output_dir)
        result["executable_evaluator_manifest"] = {
            "trace_count": len(manifest.get("evaluators") or {}),
            "checkpoint_count": sum(
                len((entry.get("checkpoints") or {})) + (1 if entry.get("outcome") else 0)
                for entry in (manifest.get("evaluators") or {}).values()
                if isinstance(entry, dict)
            ),
            "reward_oracle_mode": manifest.get("reward_oracle_mode"),
        }

    if _enabled(merged_cfg, "run_negative_probe_executor"):
        probe_result = execute_negative_probes(output_dir)
        probe_summary = probe_result.get("summary") if isinstance(probe_result.get("summary"), dict) else probe_result
        result["negative_probes"] = {
            "total_probes": probe_summary.get("total_probes", 0),
            "passed_probes": probe_summary.get("passed_probes", 0),
            "failed_probes": probe_summary.get("failed_probes", 0),
        }

    if _enabled(merged_cfg, "generate_runtime_checkpoint_bridge"):
        bridge_result = inject_runtime_bridge(output_dir)
        result["runtime_bridge"] = {
            "pages_scanned": bridge_result.get("pages_scanned", 0),
            "pages_injected": bridge_result.get("pages_injected", 0),
            "checkpoints_wired": bridge_result.get("checkpoints_wired", 0),
        }

    if _enabled(merged_cfg, "run_runtime_checkpoint_collector"):
        result["runtime_checkpoint_validation"] = _run_runtime_checkpoint_collector(output_dir, merged_cfg)

    return result


def _enabled(config: dict[str, Any], key: str) -> bool:
    return bool(config.get(key, False))


def _run_runtime_checkpoint_collector(output_dir: Path, config: dict[str, Any]) -> dict[str, Any]:
    collector_cfg = config.get("runtime_checkpoint_collector")
    if not isinstance(collector_cfg, dict):
        collector_cfg = {}

    trace_id = collector_cfg.get("trace_id") or config.get("trace_id")
    trace_ids = _load_trace_ids(output_dir, str(trace_id) if trace_id else None)
    base_url = collector_cfg.get("base_url") or config.get("runtime_checkpoint_base_url")
    workers = 1 if trace_id else int(collector_cfg.get("workers", config.get("runtime_checkpoint_workers", 32)))
    block_assets = bool(collector_cfg.get("block_assets", config.get("runtime_checkpoint_block_assets", False)))
    timeout_value = collector_cfg.get("trace_timeout_ms", config.get("runtime_checkpoint_trace_timeout_ms"))
    trace_timeout_ms = int(timeout_value) if timeout_value else None
    shard_output_dir = output_dir / "phase_post_processing" / "evaluators" / "checkpoint_runtime_validation.shards"

    if base_url:
        validation = _collect_runtime_checkpoint_snapshots_parallel(
            result_dir=str(output_dir),
            trace_ids=trace_ids,
            base_url=str(base_url),
            workers=max(1, workers),
            block_assets=block_assets,
            trace_timeout_ms=trace_timeout_ms,
            shard_output_dir=shard_output_dir,
            clear_existing_shards=not bool(trace_id),
        )
    else:
        with _temporary_pagegen_server(output_dir) as served_base_url:
            validation = _collect_runtime_checkpoint_snapshots_parallel(
                result_dir=str(output_dir),
                trace_ids=trace_ids,
                base_url=served_base_url,
                workers=max(1, workers),
                block_assets=block_assets,
                trace_timeout_ms=trace_timeout_ms,
                shard_output_dir=shard_output_dir,
                clear_existing_shards=not bool(trace_id),
            )

    output_path = _runtime_validation_output_path(output_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")

    total = 0
    passed = 0
    bridge_missing = 0
    for trace_result in (validation.get("traces") or {}).values():
        for checkpoint in (trace_result.get("checkpoints") or {}).values():
            total += 1
            if checkpoint.get("all_passed"):
                passed += 1
            if checkpoint.get("bridge_missing"):
                bridge_missing += 1

    return {
        "trace_count": len(validation.get("traces") or {}),
        "checkpoint_count": total,
        "passed_checkpoints": passed,
        "failed_checkpoints": total - passed,
        "bridge_missing": bridge_missing,
        "output_path": str(output_path),
    }


@contextmanager
def _temporary_pagegen_server(output_dir: Path) -> Iterator[str]:
    pagegen_dir = output_dir / "phase_pagegen"
    if not pagegen_dir.exists():
        raise FileNotFoundError(f"phase_pagegen directory not found: {pagegen_dir}")

    class GhostwebResultHandler(SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:
            parsed_path = unquote(urlparse(path).path)
            if parsed_path.startswith("/pages/") or parsed_path.startswith("/images/"):
                root = pagegen_dir
                rel_path = parsed_path.lstrip("/")
            else:
                root = output_dir
                rel_path = parsed_path.lstrip("/")
            parts = [part for part in rel_path.split("/") if part and part not in {".", ".."}]
            return str(root.joinpath(*parts))

    handler = GhostwebResultHandler
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}/pages"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


if __name__ == "__main__":
    raise SystemExit(main())
