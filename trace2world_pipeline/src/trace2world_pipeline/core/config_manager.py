
import os
import json
from typing import Dict, Any, Optional, List
from dataclasses import dataclass, asdict, field


@dataclass
class Trace2WorldConfig:


    output_dir: str = "src/test/generated"


    log_level: str = "INFO"


    max_fix_iterations: int = 3
    temperature: float = 0.7


    task_count_range: str = "3-5"
    custom_task_names: List[str] = field(default_factory=list)
    website_type: str = "shopping_website"


    image_mode: str = "Real"




    deployment: str = "gpt-5.2-2025-12-11"
    endpoints: List[str] = field(default_factory=lambda: [
        os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    ])
    load_balance_strategy: str = "round_robin"
    max_retries: int = 5
    retry_delay: float = 1.0


    vision_max_width: int = 1280
    vision_max_height: int = 720
    vision_jpeg_quality: int = 85


    max_concurrent: int = 64
    initial_concurrent: Optional[int] = None
    model_concurrency: Dict[str, int] = field(default_factory=dict)
    model_initial_concurrency: Dict[str, int] = field(default_factory=dict)


    skip_tdd_validation: bool = False


    custom: Dict[str, Any] = field(default_factory=dict)


    trace_input_dir: str = ""
    task_metadata_path: str = ""
    site_model_path: str = ""
    hybrid_site_model: bool = True
    site_model_parallel_enabled: bool = True
    site_model_consistency_mode: str = "deterministic"
    site_model_frame_quality_refinement_enabled: bool = False
    site_model_random_seed: int = 42
    site_model_unknown_url_decision_threshold: float = 0.75
    site_model_use_url_signals: bool = False
    site_model_global_reasoning_enabled: bool = True
    site_model_global_reasoning_overflow_strategy: str = "chunk_reduce"
    site_model_global_reasoning_token_budget: int = 60000
    site_model_page_name_soft_limit: int = 256
    site_model_global_reasoning_logic_flow: str = "compact_transition_resolver"
    site_model_global_reasoning_input_path: str = ""
    global_reasoning_page_assignment_audit_auto_accept: bool = False
    global_reasoning_page_assignment_audit_pair_budget: int = 256
    component_route_outedge_hard_gate_enabled: bool = True
    action_slot_route_hard_gate_enabled: bool = True
    site_model_schema_boundary_slice_token_budget: int = 12000
    site_model_schema_boundary_slice_candidate_budget: int = 24
    site_model_skip_full_extraction_enabled: bool = False
    site_model_task_interaction_contract_logic_flow: str = "l0_schema_registry"
    page_environment_planning_mode: str = "unified"


    environment_profile: Dict[str, Any] = field(default_factory=dict)


    pagegen_logic_flow: str = "l0_fidelity"
    pagegen_standard_max_prompt_screenshots: int = 16
    pagegen_cascade_round1_image_limit: int = 8
    pagegen_cascade_max_images_per_round: int = 8
    pagegen_cascade_patching_enabled: bool = True
    pagegen_cascade_use_unique_steps_enabled: bool = True
    pagegen_cascade_patch_max_tokens: int = 8000
    pagegen_cascade_prompt_mode: str = "current_pipeline"
    pagegen_adaptive_cascade_enabled: bool = True
    pagegen_adaptive_cascade_min_prompt_screenshots: int = 12
    pagegen_adaptive_cascade_min_resources: int = 48
    pagegen_resource_semantic_labeling_enabled: bool = True
    pagegen_scoped_content_enabled: bool = True
    pagegen_l0_prompt_resource_extraction_enabled: bool = True
    page_execution_obligation_action_surface_runtime_enabled: bool = True
    stage_model_routing: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Trace2WorldConfig':

        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered_data = {k: v for k, v in data.items() if k in valid_fields}

        return cls(**filtered_data)


class Trace2WorldConfigManager:


    DEFAULT_CONFIG_DIR = "config/tdd"
    DEFAULT_CONFIG_FILE = "default_config.json"

    def __init__(self, config_path: Optional[str] = None):
        self.config_path = config_path
        self.config = Trace2WorldConfig()
        self.config_loaded = False
        self._raw_config: Dict[str, Any] = {}


        if config_path:
            self.load_config(config_path)
        else:
            self.load_defaults()

    def load_config(self, config_path: str):
        if not os.path.exists(config_path):
            raise FileNotFoundError(f"Configuration file not found: {config_path}")

        with open(config_path, 'r', encoding='utf-8') as f:
            config_data = json.load(f)
        self._reject_removed_config_keys(config_data)


        self._raw_config = config_data


        merged_config = self._merge_with_defaults(config_data)


        self.config = Trace2WorldConfig.from_dict(merged_config)
        self.config_path = config_path
        self.config_loaded = True

    def load_defaults(self):

        default_path = os.path.join(self.DEFAULT_CONFIG_DIR, self.DEFAULT_CONFIG_FILE)
        if os.path.exists(default_path):
            self.load_config(default_path)
        else:

            self.config = Trace2WorldConfig()
            self._raw_config = {}
            self.config_loaded = True

    def _merge_with_defaults(self, config_data: Dict[str, Any]) -> Dict[str, Any]:

        default_config = Trace2WorldConfig().to_dict()


        merged = default_config.copy()
        for key, value in config_data.items():
            if isinstance(value, dict) and key in merged and isinstance(merged[key], dict):

                merged[key] = {**merged[key], **value}
            else:
                merged[key] = value

        global_max_concurrent = int(merged.get("max_concurrent") or Trace2WorldConfig.max_concurrent)
        for key in (
        ):
            if key not in config_data:
                merged[key] = global_max_concurrent

        if "model_concurrency" not in config_data:
            merged["model_concurrency"] = {}
        if "model_initial_concurrency" not in config_data:
            merged["model_initial_concurrency"] = {}

        return merged

    @staticmethod
    def _reject_removed_config_keys(config_data: Dict[str, Any]):
        removed = sorted(
            key
            for key in (
                "site_model_global_reasoning_mode",
                "site_model_global_reasoning_constraint_flow_enabled",
                "page_environment_artifact_validation_hard_gate",
                "page_execution_obligation_repair_enabled",
                "page_execution_obligation_repair_max_failures",
                "page_execution_obligation_repair_max_pages",
                "page_execution_obligation_repair_max_rounds",
                "action_host_materializer_enabled",
                "action_effect_materialization_enabled",
                "component_dom_materializer_enabled",
                "route_materializer_enabled",
                "pagegen_dom_contract_hard_gate_enabled",
                "pagegen_runtime_dom_contract_hard_gate_enabled",
                "trace_coverage_closure_hard_gate_enabled",
                "action_host_manifest_hard_gate_enabled",
                "action_effect_dom_materialization_hard_gate_enabled",
            )
            if key in config_data
        )
        if removed:
            raise ValueError(
                "Removed config key(s) are not supported: "
                + ", ".join(removed)
                + ". PageGen DOM/action-host issues must be fixed in the PageGen source or visible control."
            )

    def get(self, key: str, default: Any = None) -> Any:

        if key in self._raw_config:
            return self._raw_config[key]


        keys = key.split('.')
        value = self.config.to_dict()

        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default

        return value

    def get_component_config(self, component_name: str) -> Dict[str, Any]:

        stage_configs = self._raw_config.get("stage_configs", {})
        if component_name in stage_configs:
            stage_config = stage_configs[component_name]
            return {
                "model": stage_config.get("model") or self.get("deployment"),
                "reasoning_effort": stage_config.get("reasoning_effort") or "medium",
                "max_concurrent": stage_config.get("max_concurrent"),
            }


        return {
            "model": self.get(f"{component_name}_model") or self.get("deployment"),
            "reasoning_effort": self.get(f"{component_name}_reasoning_effort") or "medium",
            "max_concurrent": self.get(f"{component_name}_max_concurrent"),
        }

    def get_stage_model_routing(self) -> Dict[str, str]:
        routing = self.get("stage_model_routing", None)
        if routing is None:
            routing = self.get("phase_model_routing", {})
        if not isinstance(routing, dict):
            return {}
        return {
            str(stage): str(model)
            for stage, model in routing.items()
            if str(stage).strip() and str(model).strip()
        }

    def set(self, key: str, value: Any):
        if key in {
            "site_model_global_reasoning_mode",
            "site_model_global_reasoning_constraint_flow_enabled",
        }:
            raise ValueError(
                f"{key} was removed; use "
                "site_model_global_reasoning_logic_flow only."
            )

        self._raw_config[key] = value


        if hasattr(self.config, key):
            setattr(self.config, key, value)
        else:

            self.config.custom[key] = value

    def update(self, updates: Dict[str, Any]):
        for key, value in updates.items():
            self.set(key, value)

    def validate(self) -> Dict[str, Any]:
        errors = []
        warnings = []


        if not self.config.output_dir:
            errors.append("output_dir is required")


        valid_log_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if self.config.log_level.upper() not in valid_log_levels:
            errors.append(f"Invalid log_level: {self.config.log_level}")


        if self.config.max_fix_iterations < 1:
            warnings.append("max_fix_iterations should be at least 1")

        if self.config.max_fix_iterations > 10:
            warnings.append("max_fix_iterations > 10 may cause long execution times")


        if not self.config.endpoints:
            errors.append("At least one API endpoint is required")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings
        }

    def save(self, filepath: Optional[str] = None):
        save_path = filepath or self.config_path
        if not save_path:
            save_path = os.path.join(self.DEFAULT_CONFIG_DIR, "config.json")


        os.makedirs(os.path.dirname(save_path), exist_ok=True)


        with open(save_path, 'w', encoding='utf-8') as f:
            json.dump(self.config.to_dict(), f, indent=2, ensure_ascii=False)

    def export_for_llm(self) -> Dict[str, Any]:
        return {
            "deployment": self.config.deployment,
            "temperature": self.config.temperature,
            "endpoints": self.config.endpoints,
            "load_balance_strategy": self.config.load_balance_strategy,
            "max_retries": self.config.max_retries,
            "retry_delay": self.config.retry_delay
        }

    def export_for_test_execution(self) -> Dict[str, Any]:
        return {
            "max_fix_iterations": self.config.max_fix_iterations
        }

    def get_summary(self) -> str:
        lines = [
            "=== TDD Configuration ===",
            f"Output Directory: {self.config.output_dir}",
            f"Log Level: {self.config.log_level}",
            f"Max Fix Iterations: {self.config.max_fix_iterations}",
            f"API Model: {self.config.deployment}",
            f"Endpoints: {len(self.config.endpoints)} configured",
            f"Task Count Range: {self.config.task_count_range}",
            f"Hybrid AppSpec: {self.config.hybrid_site_model}",
            f"AppSpec Parallel: {self.config.site_model_parallel_enabled}",
            f"Website Type: {self.config.website_type}",
        ]

        if self.config.custom:
            lines.append(f"Custom Settings: {len(self.config.custom)} configured")

        if self.config_path:
            lines.append(f"Config File: {self.config_path}")

        lines.append("=" * 25)

        return "\n".join(lines)

    def print_summary(self):
        print(self.get_summary())

    @classmethod
    def create_default_config_file(cls, filepath: str):
        default_config = Trace2WorldConfig()

        os.makedirs(os.path.dirname(filepath), exist_ok=True)

        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(default_config.to_dict(), f, indent=2, ensure_ascii=False)

        print(f"Default configuration saved to: {filepath}")


def resolve_page_environment_planning_mode(config_manager: Any) -> str:
    mode = str(config_manager.get("page_environment_planning_mode", "") or "").strip()
    if mode in {"unified", "legacy_stages"}:
        return mode
    old_enabled = config_manager.get("enrich_phase_3_cross_page_decisions_enabled", None)
    if old_enabled is False:
        return "legacy_stages"
    return "unified"
