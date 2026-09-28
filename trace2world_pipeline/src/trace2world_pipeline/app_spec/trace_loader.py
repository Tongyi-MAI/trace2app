
import os
import json
import re
import hashlib
from functools import lru_cache
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field

from trace2world_pipeline.app_spec.step_disposition import validate_step_authority_consistency


@dataclass
class TraceStep:
    screenshot_path: str
    action: str
    thought: str
    step_index: int
    raw_action: str = ""
    raw_action_payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TraceData:
    trace_id: str
    task: str
    website_url: str
    level: str
    steps: List[TraceStep]
    skipped_steps: List[Dict[str, Any]] = field(default_factory=list)
    step_dispositions: List[Dict[str, Any]] = field(default_factory=list)
    terminal_observations: List[Dict[str, Any]] = field(default_factory=list)
    terminal_contract: Dict[str, Any] = field(default_factory=dict)


class TraceLoader:


    @staticmethod
    def is_mt_resource(path: str) -> bool:
        if not os.path.isdir(path):
            return False
        index_path = os.path.join(path, "index.json")
        if not os.path.isfile(index_path):
            return False
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                root_index = json.load(f)
        except Exception:
            return False
        trace_entries = root_index.get("traces", [])
        if not isinstance(trace_entries, list) or not trace_entries:
            return False
        for entry in trace_entries:
            if not isinstance(entry, dict):
                continue
            trace_id = entry.get("id", "")
            entry_path = entry.get("path", "")
            candidates = []
            if entry_path:
                candidates.append(os.path.join(path, entry_path))
                candidates.append(os.path.join(path, os.path.basename(entry_path)))
            if trace_id:
                candidates.append(os.path.join(path, trace_id))
                candidates.append(os.path.join(path, "traces", trace_id))
            for candidate in candidates:
                if os.path.isfile(os.path.join(candidate, "index.json")):
                    return True
        return False

    @staticmethod
    def _resolve_mt_trace_dir(resource_dir: str, entry: Dict[str, Any]) -> Optional[str]:
        trace_id = entry.get("id", "")
        entry_path = entry.get("path", "")
        candidates: List[str] = []
        if entry_path:
            candidates.append(os.path.join(resource_dir, entry_path))
            candidates.append(os.path.join(resource_dir, os.path.basename(entry_path)))
        if trace_id:
            candidates.append(os.path.join(resource_dir, trace_id))
            candidates.append(os.path.join(resource_dir, "traces", trace_id))
        for candidate in candidates:
            if os.path.isfile(os.path.join(candidate, "index.json")):
                return candidate
        return None

    @staticmethod
    def is_meituan_session(path: str) -> bool:
        if not os.path.isdir(path):
            return False
        if not os.path.isfile(os.path.join(path, "traj.json")):
            return False
        screenshots_dir = os.path.join(path, "screenshots")
        if os.path.isdir(screenshots_dir):
            for fname in os.listdir(screenshots_dir):
                if os.path.splitext(fname)[1].lower() in {".png", ".jpg", ".jpeg", ".webp"}:
                    return True
        for fname in os.listdir(path):
            if re.fullmatch(r"step_\d{4}\.json", fname):
                stem = fname[:-5]
                for ext in (".png", ".jpg", ".jpeg"):
                    if os.path.isfile(os.path.join(path, stem + ext)):
                        return True
        return False

    @classmethod
    def is_meituan_sessions_root(cls, path: str) -> bool:
        if not os.path.isdir(path):
            return False
        for entry in os.listdir(path):
            if cls.is_meituan_session(os.path.join(path, entry)):
                return True
        return False

    @staticmethod
    def is_webenv_rewritten_trace(path: str) -> bool:
        if not os.path.isdir(path):
            return False
        trajectory_path = os.path.join(path, "trajectory.json")
        screenshots_dir = os.path.join(path, "screenshots")
        return os.path.isfile(trajectory_path) and os.path.isdir(screenshots_dir)

    @classmethod
    def is_webenv_rewritten_root(cls, path: str) -> bool:
        if not os.path.isdir(path) or not os.path.isfile(os.path.join(path, "index.json")):
            return False
        try:
            with open(os.path.join(path, "index.json"), "r", encoding="utf-8") as f:
                root_index = json.load(f)
        except Exception:
            return False
        trace_entries = root_index.get("traces", [])
        if not isinstance(trace_entries, list) or not trace_entries:
            return False
        for entry in trace_entries:
            if not isinstance(entry, dict):
                continue
            entry_path = str(entry.get("path", "") or entry.get("id", "") or "").strip()
            if entry_path and cls.is_webenv_rewritten_trace(os.path.join(path, entry_path)):
                return True
        return False


    def load_trace(self, trace_dir: str, task_metadata: Dict = None) -> TraceData:
        result_path = os.path.join(trace_dir, "result.json")
        with open(result_path, 'r', encoding='utf-8') as f:
            result = json.load(f)

        trace_id = result.get("task_id", os.path.basename(trace_dir))
        task = result.get("task", "")
        action_history = result.get("action_history", [])
        thoughts = result.get("thoughts", [])


        website_url = ""
        level = ""
        if task_metadata:
            website_url = task_metadata.get("website", "")
            level = task_metadata.get("level", "")


        trajectory_dir = os.path.join(trace_dir, "trajectory")
        steps = []

        for i, action in enumerate(action_history):
            screenshot_path = os.path.join(trajectory_dir, f"{i}_full_screenshot.png")
            thought = thoughts[i] if i < len(thoughts) else ""

            if not os.path.exists(screenshot_path):
                continue

            steps.append(TraceStep(
                screenshot_path=screenshot_path,
                action=action,
                thought=thought,
                step_index=i
            ))


        final_screenshot = os.path.join(trajectory_dir, f"{len(action_history)}_full_screenshot.png")
        if os.path.exists(final_screenshot):
            steps.append(TraceStep(
                screenshot_path=final_screenshot,
                action="finished()",
                thought="",
                step_index=len(action_history)
            ))

        return TraceData(
            trace_id=trace_id,
            task=task,
            website_url=website_url,
            level=level,
            steps=steps
        )


    def load_meituan_session(self, trace_dir: str) -> Optional[TraceData]:
        if not self.is_meituan_session(trace_dir):
            return None

        step_files = [
            fname for fname in os.listdir(trace_dir)
            if re.fullmatch(r"step_\d{4}\.json", fname)
        ]
        step_files.sort(key=self._meituan_step_file_index)

        steps: List[TraceStep] = []
        task = ""
        for fname in step_files:
            step_path = os.path.join(trace_dir, fname)
            with open(step_path, 'r', encoding='utf-8') as f:
                step_entry = json.load(f)

            if self._should_skip_meituan_step(step_entry):
                continue

            if not task:
                task = str(step_entry.get("instruction", "") or "")

            screenshot_path = self._resolve_meituan_step_screenshot(trace_dir, fname, step_entry)
            if not screenshot_path or not os.path.exists(screenshot_path):
                continue

            step_index = step_entry.get("step_index")
            if not isinstance(step_index, int):
                step_index = self._meituan_step_file_index(fname)

            raw_action = self._format_meituan_action(step_entry.get("action", ""))
            prediction_thought, prediction_action = self._parse_prediction_tags(
                step_entry.get("prediction", "")
            )

            steps.append(TraceStep(
                screenshot_path=screenshot_path,
                action=prediction_action or raw_action,
                thought=prediction_thought or str(
                    step_entry.get("thought", "")
                    or step_entry.get("reasoning_content", "")
                    or step_entry.get("prediction", "")
                    or ""
                ),
                step_index=step_index,
                raw_action=raw_action,
            ))

        if not steps:
            return None

        if not task:
            task = self._load_meituan_task_from_traj(trace_dir)

        return TraceData(
            trace_id=os.path.basename(os.path.normpath(trace_dir)),
            task=task,
            website_url="",
            level="",
            steps=steps,
        )

    def load_webenv_rewritten(self, resource_dir: str, trace_filter: List[str] = None) -> List[TraceData]:
        root_index_path = os.path.join(resource_dir, "index.json")
        with open(root_index_path, "r", encoding="utf-8") as f:
            root_index = json.load(f)
        website_url = str(root_index.get("app_name", "") or root_index.get("url", "") or "")
        traces: List[TraceData] = []
        for entry in root_index.get("traces", []) or []:
            if not isinstance(entry, dict):
                continue
            trace_id = str(entry.get("id", "") or "").strip()
            if trace_filter and trace_id not in trace_filter:
                continue
            entry_path = str(entry.get("path", "") or trace_id).strip()
            trace_dir = os.path.join(resource_dir, entry_path)
            trace = self.load_webenv_rewritten_trace(trace_dir, trace_id=trace_id, website_url=website_url)
            if trace:
                traces.append(trace)
        return traces

    def load_webenv_rewritten_trace(
        self,
        trace_dir: str,
        trace_id: str = "",
        website_url: str = "",
    ) -> Optional[TraceData]:
        if not self.is_webenv_rewritten_trace(trace_dir):
            return None
        trajectory_path = os.path.join(trace_dir, "trajectory.json")
        with open(trajectory_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        trace_id = trace_id or str(payload.get("task_id", "") or os.path.basename(os.path.normpath(trace_dir)))
        task = str(payload.get("instruction", "") or "")
        steps: List[TraceStep] = []
        skipped_steps: List[Dict[str, Any]] = []
        step_dispositions: List[Dict[str, Any]] = []
        terminal_observations: List[Dict[str, Any]] = []
        for index, step_entry in enumerate(payload.get("trajectory", []) or []):
            if not isinstance(step_entry, dict):
                continue
            step_index = step_entry.get("step_index")
            if not isinstance(step_index, int):
                step_index = index
            raw_action = self._format_webenv_action(step_entry)
            raw_action_payload = self._webenv_raw_action_payload(step_entry)
            screenshot_ref = str(step_entry.get("screenshot", "") or step_entry.get("image_file", "") or "").strip()
            screenshot_path = self._resolve_webenv_screenshot(trace_dir, screenshot_ref, index)
            disposition = step_entry.get("step_disposition")
            conflicts = validate_step_authority_consistency(
                step_entry,
                trace_id=trace_id,
                step_index=step_index,
            )
            if conflicts:
                raise ValueError("; ".join(conflicts))
            if isinstance(disposition, dict):
                disp_record = dict(disposition)
                disp_record.setdefault("trace_id", trace_id)
                disp_record.setdefault("step_index", step_index)
                disp_record.setdefault("screenshot_path", screenshot_path or "")
                step_dispositions.append(disp_record)
            skip_reason = self._webenv_skip_reason(step_entry)
            effective_step = self._webenv_effective_step(step_entry)
            observation_disposition = str(
                step_entry.get("observation_disposition")
                or effective_step.get("observation_disposition")
                or (disposition.get("observation_disposition") if isinstance(disposition, dict) else "")
                or ""
            ).strip().lower()
            is_terminal_observation = self._webenv_action_family(step_entry) == "finish" and (
                observation_disposition == "retain"
                or skip_reason == "terminal_observation"
            )
            if is_terminal_observation:
                terminal_observations.append({
                    "trace_id": trace_id,
                    "step_index": step_index,
                    "obs_key": f"{trace_id}:{step_index}",
                    "step_role": "terminal_observation",
                    "action_disposition": "exclude",
                    "observation_disposition": "retain",
                    "semantic_effect": "terminal_evidence",
                    "terminal_evidence": True,
                    "screenshot_path": screenshot_path or "",
                    "status": str(step_entry.get("status") or ""),
                    "summary": str(step_entry.get("summary") or ""),
                    "detail": str(step_entry.get("detail") or ""),
                    "answer": str(step_entry.get("answer") or ""),
                    "raw_action": raw_action,
                    "raw_action_payload": raw_action_payload,
                })
                continue
            if skip_reason:
                skipped_steps.append({
                    "trace_id": trace_id,
                    "step_index": step_index,
                    "skip_reason": skip_reason,
                    "reason": self._webenv_skip_detail(step_entry),
                    "source": self._webenv_skip_source(step_entry),
                    "raw_action": raw_action,
                    "raw_action_payload": raw_action_payload,
                    "screenshot_path": screenshot_path or "",
                })
                continue
            if not screenshot_path or not os.path.exists(screenshot_path):
                continue
            prediction_thought, prediction_action = self._parse_prediction_tags(step_entry.get("prediction", ""))
            steps.append(TraceStep(
                screenshot_path=screenshot_path,
                action=prediction_action or raw_action,
                thought=prediction_thought or str(step_entry.get("thought", "") or ""),
                step_index=step_index,
                raw_action=raw_action,
                raw_action_payload=raw_action_payload,
            ))
        if not steps:
            return None
        return TraceData(
            trace_id=trace_id,
            task=task,
            website_url=website_url,
            level="",
            steps=steps,
            skipped_steps=skipped_steps,
            step_dispositions=step_dispositions,
            terminal_observations=terminal_observations,
            terminal_contract=(
                dict(payload.get("terminal_contract"))
                if isinstance(payload.get("terminal_contract"), dict)
                else {}
            ),
        )

    @staticmethod
    def _resolve_webenv_screenshot(trace_dir: str, screenshot_ref: str, step_index: int) -> Optional[str]:
        if screenshot_ref:
            candidate = os.path.join(trace_dir, screenshot_ref)
            if os.path.exists(candidate):
                return candidate
            candidate = os.path.join(trace_dir, "screenshots", os.path.basename(screenshot_ref))
            if os.path.exists(candidate):
                return candidate
        screenshots_dir = os.path.join(trace_dir, "screenshots")
        for stem in (f"action_{step_index:04d}", f"step_{step_index:04d}"):
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                candidate = os.path.join(screenshots_dir, stem + ext)
                if os.path.exists(candidate):
                    return candidate
        return None

    @staticmethod
    def _format_webenv_action(step_entry: Dict[str, Any]) -> str:
        action = step_entry.get("action", "")
        if isinstance(action, dict):
            return TraceLoader._format_meituan_action(action)
        action_type = str(action or step_entry.get("action_type", "") or step_entry.get("type", "") or "").strip()
        parts = [action_type] if action_type else []
        if step_entry.get("text") is not None:
            parts.append(str(step_entry.get("text")))
        coordinate = step_entry.get("coordinate")
        if isinstance(coordinate, list) and len(coordinate) >= 2:
            parts.append(f"({coordinate[0]}, {coordinate[1]})")
        for key in ("x", "y", "start_x", "start_y", "end_x", "end_y"):
            if step_entry.get(key) is not None:
                parts.append(f"{key}={step_entry.get(key)}")
        return " ".join(parts) if parts else json.dumps(step_entry, ensure_ascii=False)

    @staticmethod
    def _webenv_raw_action_payload(step_entry: Dict[str, Any]) -> Dict[str, Any]:
        keys = (
            "action",
            "action_type",
            "type",
            "text",
            "coordinate",
            "start_coordinate",
            "end_coordinate",
            "x",
            "y",
            "start_x",
            "start_y",
            "end_x",
            "end_y",
            "duration",
            "status",
            "summary",
            "detail",
            "answer",
        )
        return {
            key: step_entry.get(key)
            for key in keys
            if step_entry.get(key) not in (None, "", [], {})
        }

    @staticmethod
    def _webenv_action_family(step_entry: Dict[str, Any]) -> str:
        action = step_entry.get("action")
        if isinstance(action, dict):
            action = action.get("type") or action.get("action") or action.get("name")
        text = str(action or step_entry.get("action_type") or step_entry.get("type") or "").strip().lower()
        if text in {"take_over", "takeover"}:
            return "take_over"
        if text in {"terminate", "finished", "finish", "done"}:
            return "finish"
        return text

    @staticmethod
    def _webenv_effective_step(step_entry: Dict[str, Any]) -> Dict[str, Any]:
        value = step_entry.get("effective_step")
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _webenv_skip_reason(step_entry: Dict[str, Any]) -> str:
        effective = TraceLoader._webenv_effective_step(step_entry)
        verdict = str(effective.get("verdict") or "").strip().lower()
        if verdict in {"skip", "drop", "manual", "manual_required", "not_applicable"}:
            return str(effective.get("skip_reason") or verdict)
        if step_entry.get("drop") is True:
            return "dropped_by_preprocess"
        if TraceLoader._webenv_action_family(step_entry) == "take_over":
            return "manual_takeover_noop"
        return ""

    @staticmethod
    def _webenv_skip_detail(step_entry: Dict[str, Any]) -> str:
        effective = TraceLoader._webenv_effective_step(step_entry)
        return str(
            effective.get("reason")
            or step_entry.get("drop_reason")
            or step_entry.get("text")
            or step_entry.get("detail")
            or ""
        )

    @staticmethod
    def _webenv_skip_source(step_entry: Dict[str, Any]) -> str:
        effective = TraceLoader._webenv_effective_step(step_entry)
        return str(effective.get("source") or "webenv_trace_loader")

    @staticmethod
    def _should_skip_meituan_step(step_entry: Dict[str, Any]) -> bool:
        annotation = step_entry.get("trace_path_annotation")
        if not isinstance(annotation, dict):
            return False
        return bool(annotation.get("should_skip"))

    @staticmethod
    def _meituan_step_file_index(fname: str) -> int:
        match = re.search(r"step_(\d{4})", fname)
        return int(match.group(1)) if match else 0

    @staticmethod
    def _resolve_meituan_step_screenshot(
        trace_dir: str,
        step_fname: str,
        step_entry: Dict[str, Any],
    ) -> Optional[str]:
        step_index = step_entry.get("step_index")
        if not isinstance(step_index, int):
            step_index = TraceLoader._meituan_step_file_index(step_fname)

        for key in ("image_file", "source_image"):
            image_file = step_entry.get(key, "")
            if image_file:
                candidate = os.path.join(trace_dir, os.path.basename(str(image_file)))
                if os.path.exists(candidate):
                    return candidate

        image_path = step_entry.get("image_path", "")
        if image_path:
            candidate = os.path.join(trace_dir, os.path.basename(str(image_path)))
            if os.path.exists(candidate):
                return candidate

        raw_candidate = TraceLoader._resolve_meituan_raw_screenshot(trace_dir, step_index)
        if raw_candidate:
            return raw_candidate

        stem = step_fname[:-5]
        for ext in (".png", ".jpg", ".jpeg"):
            candidate = os.path.join(trace_dir, stem + ext)
            if os.path.exists(candidate):
                return candidate
        return None

    @staticmethod
    def _resolve_meituan_raw_screenshot(trace_dir: str, step_index: int) -> Optional[str]:
        screenshots_dir = os.path.join(trace_dir, "screenshots")
        if not os.path.isdir(screenshots_dir):
            return None
        trace_id = os.path.basename(os.path.normpath(trace_dir))
        raw_offset = TraceLoader._infer_meituan_raw_screenshot_index_offset(trace_dir)
        stems = [
            f"{trace_id}-0-{step_index + raw_offset}",
            f"action_{step_index:04d}",
            f"step_{step_index:04d}",
            f"step{step_index:02d}",
        ]
        for stem in stems:
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                candidate = os.path.join(screenshots_dir, stem + ext)
                if os.path.exists(candidate):
                    return candidate
        return None

    @staticmethod
    @lru_cache(maxsize=2048)
    def _infer_meituan_raw_screenshot_index_offset(trace_dir: str) -> int:
        screenshots_dir = os.path.join(trace_dir, "screenshots")
        if not os.path.isdir(screenshots_dir):
            return 1
        trace_id = os.path.basename(os.path.normpath(trace_dir))

        def digest(path: str) -> str:
            try:
                with open(path, "rb") as handle:
                    return hashlib.sha256(handle.read()).hexdigest()
            except Exception:
                return ""

        zero_matches = 0
        one_matches = 0
        checked = 0
        for fname in sorted(os.listdir(trace_dir)):
            if not re.fullmatch(r"step_\d{4}\.json", fname):
                continue
            step_index = TraceLoader._meituan_step_file_index(fname)
            step_image = ""
            for ext in (".png", ".jpg", ".jpeg", ".webp"):
                candidate = os.path.join(trace_dir, f"step_{step_index:04d}{ext}")
                if os.path.isfile(candidate):
                    step_image = candidate
                    break
            if not step_image:
                continue
            step_hash = digest(step_image)
            if not step_hash:
                continue
            for offset, counter_name in ((0, "zero"), (1, "one")):
                for ext in (".png", ".jpg", ".jpeg", ".webp"):
                    raw = os.path.join(screenshots_dir, f"{trace_id}-0-{step_index + offset}{ext}")
                    if os.path.isfile(raw) and digest(raw) == step_hash:
                        if counter_name == "zero":
                            zero_matches += 1
                        else:
                            one_matches += 1
                        break
            checked += 1
            if checked >= 8 and (zero_matches or one_matches):
                break
        if zero_matches or one_matches:
            return 0 if zero_matches > one_matches else 1

        for ext in (".png", ".jpg", ".jpeg", ".webp"):
            if os.path.isfile(os.path.join(screenshots_dir, f"{trace_id}-0-0{ext}")):
                return 0
        return 1

    @staticmethod
    def _format_meituan_action(action: Any) -> str:
        if isinstance(action, str):
            return action
        if not isinstance(action, dict):
            return str(action) if action is not None else ""

        action_type = str(action.get("action_type", "") or action.get("type", "") or "").strip()
        if action_type in {"open_app", "open"} and action.get("app_name"):
            return f"{action_type} {action['app_name']}"
        if action_type in {"type", "input"} and action.get("text") is not None:
            return f"{action_type} {action['text']}"
        if action_type in {"click", "tap"}:
            x = action.get("x")
            y = action.get("y")
            if x is not None and y is not None:
                return f"{action_type} ({x}, {y})"
        if action_type in {"swipe", "drag"}:
            sx = action.get("start_x")
            sy = action.get("start_y")
            ex = action.get("end_x")
            ey = action.get("end_y")
            if None not in (sx, sy, ex, ey):
                return f"{action_type} ({sx}, {sy}) -> ({ex}, {ey})"
        if action_type in {"wait", "sleep"}:
            duration = action.get("duration")
            return f"{action_type} {duration}" if duration is not None else action_type

        parts = [action_type] if action_type else []
        for key in ("text", "app_name", "x", "y", "start_x", "start_y", "end_x", "end_y"):
            value = action.get(key)
            if value is not None:
                parts.append(f"{key}={value}")
        return " ".join(parts) if parts else json.dumps(action, ensure_ascii=False)

    @staticmethod
    def _parse_prediction_tags(prediction: Any) -> tuple:
        if not isinstance(prediction, str) or not prediction.strip():
            return "", ""

        def extract(tag: str) -> str:
            match = re.search(
                r"<{tag}>\s*(.*?)\s*</{tag}>".format(tag=re.escape(tag)),
                prediction,
                flags=re.IGNORECASE | re.DOTALL,
            )
            if not match:
                return ""
            return " ".join(match.group(1).strip().split())

        return extract("thinking"), extract("action")

    @staticmethod
    def _load_meituan_task_from_traj(trace_dir: str) -> str:
        traj_path = os.path.join(trace_dir, "traj.json")
        if not os.path.isfile(traj_path):
            return ""
        try:
            with open(traj_path, 'r', encoding='utf-8') as f:
                traj = json.load(f)
        except Exception:
            return ""
        if not isinstance(traj, dict):
            return ""
        for value in traj.values():
            if not isinstance(value, dict):
                continue
            steps = value.get("traj", [])
            if isinstance(steps, list):
                for step in steps:
                    if isinstance(step, dict) and step.get("task_goal"):
                        return str(step["task_goal"])
        return ""


    def load_mt_resource(self, resource_dir: str,
                         trace_filter: List[str] = None) -> List[TraceData]:
        root_index_path = os.path.join(resource_dir, "index.json")
        with open(root_index_path, 'r', encoding='utf-8') as f:
            root_index = json.load(f)

        website_url = root_index.get("url", "")
        trace_entries = root_index.get("traces", [])

        traces: List[TraceData] = []
        for entry in trace_entries:
            trace_id = entry["id"]
            if trace_filter and trace_id not in trace_filter:
                continue

            trace_dir = self._resolve_mt_trace_dir(resource_dir, entry)
            if not trace_dir:
                continue
            task_desc = entry.get("description_en") or entry.get("description", "")

            trace = self._load_mt_resource_trace(trace_dir, trace_id, task_desc, website_url)
            if trace:
                traces.append(trace)

        return traces

    def _load_mt_resource_trace(self, trace_dir: str, trace_id: str,
                                task: str, website_url: str) -> Optional[TraceData]:
        index_path = os.path.join(trace_dir, "index.json")
        if not os.path.isfile(index_path):
            return None

        with open(index_path, 'r', encoding='utf-8') as f:
            trace_index = json.load(f)

        step_entries = trace_index.get("steps", [])
        steps: List[TraceStep] = []

        for step_entry in step_entries:
            step_num = step_entry.get("step", len(steps))
            action = step_entry.get("action", "")
            thought = step_entry.get("thought", "") or step_entry.get("reasoning_content", "") or step_entry.get("prediction", "") or ""


            source_image = step_entry.get("source_image", "")
            if source_image:
                screenshot_path = os.path.join(trace_dir, source_image)
            else:
                screenshot_path = self._find_step_screenshot(trace_dir, step_num)

            if not screenshot_path or not os.path.exists(screenshot_path):
                continue

            steps.append(TraceStep(
                screenshot_path=screenshot_path,
                action=action,
                thought=thought,
                step_index=step_num - 1,
            ))

        if not steps:
            return None

        return TraceData(
            trace_id=trace_id,
            task=task,
            website_url=website_url,
            level="",
            steps=steps
        )

    @staticmethod
    def _find_step_screenshot(trace_dir: str, step_num: int) -> Optional[str]:
        prefix = f"step{step_num:02d}_"
        for fname in os.listdir(trace_dir):
            if fname.startswith(prefix) and fname.lower().endswith(('.jpg', '.jpeg', '.png')):
                return os.path.join(trace_dir, fname)
        return None


    def load_traces_for_website(self, trace_dirs: List[str],
                                 task_metadata_path: str = None,
                                 website_filter: str = None) -> List[TraceData]:
        if len(trace_dirs) == 1 and self.is_webenv_rewritten_root(trace_dirs[0]):
            traces = self.load_webenv_rewritten(trace_dirs[0])
            if website_filter:
                traces = [t for t in traces if website_filter in t.website_url]
            return traces
        if len(trace_dirs) == 1 and self.is_meituan_sessions_root(trace_dirs[0]):
            trace_dirs = self.discover_trace_dirs(trace_dirs[0])
        elif len(trace_dirs) == 1 and self.is_mt_resource(trace_dirs[0]):
            traces = self.load_mt_resource(trace_dirs[0])
            if website_filter:
                traces = [t for t in traces if website_filter in t.website_url]
            return traces


        mind2web_dirs: List[str] = []
        traces: List[TraceData] = []
        for d in trace_dirs:
            if self.is_webenv_rewritten_root(d):
                traces.extend(self.load_webenv_rewritten(d))
            elif self.is_webenv_rewritten_trace(d):
                trace = self.load_webenv_rewritten_trace(d)
                if trace:
                    traces.append(trace)
            elif self.is_mt_resource(d):
                traces.extend(self.load_mt_resource(d))
            elif self.is_meituan_session(d):
                trace = self.load_meituan_session(d)
                if trace:
                    traces.append(trace)
            else:
                mind2web_dirs.append(d)


        metadata_index = {}
        if mind2web_dirs and task_metadata_path and os.path.exists(task_metadata_path):
            with open(task_metadata_path, 'r', encoding='utf-8') as f:
                all_metadata = json.load(f)
            for item in all_metadata:
                metadata_index[item["task_id"]] = item

        for trace_dir in mind2web_dirs:
            if not os.path.isdir(trace_dir):
                continue

            result_path = os.path.join(trace_dir, "result.json")
            if not os.path.exists(result_path):
                continue


            with open(result_path, 'r', encoding='utf-8') as f:
                result = json.load(f)
            task_id = result.get("task_id", os.path.basename(trace_dir))
            task_metadata = metadata_index.get(task_id, {})


            if website_filter:
                website = task_metadata.get("website", "")
                if website_filter not in website:
                    continue

            trace = self.load_trace(trace_dir, task_metadata)
            traces.append(trace)


        if website_filter:
            traces = [t for t in traces if website_filter in t.website_url]

        return traces

    def discover_trace_dirs(self, base_dir: str) -> List[str]:
        if self.is_webenv_rewritten_root(base_dir):
            return [base_dir]
        if self.is_webenv_rewritten_trace(base_dir):
            return [base_dir]
        if self.is_meituan_session(base_dir):
            return [base_dir]
        if self.is_meituan_sessions_root(base_dir):
            return [
                os.path.join(base_dir, entry)
                for entry in sorted(os.listdir(base_dir))
                if self.is_meituan_session(os.path.join(base_dir, entry))
            ]

        if self.is_mt_resource(base_dir):
            return [base_dir]

        trace_dirs = []
        for entry in sorted(os.listdir(base_dir)):
            full_path = os.path.join(base_dir, entry)
            if not os.path.isdir(full_path):
                continue
            if (
                os.path.exists(os.path.join(full_path, "result.json"))
                or self.is_meituan_session(full_path)
                or self.is_webenv_rewritten_trace(full_path)
            ):
                trace_dirs.append(full_path)
        return trace_dirs
