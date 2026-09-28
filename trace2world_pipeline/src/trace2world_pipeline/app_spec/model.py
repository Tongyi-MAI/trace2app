
import os
import json
import threading
from collections import Counter
from typing import ClassVar, Dict, Any, List, Optional, Tuple
from dataclasses import dataclass, field, asdict


@dataclass
class GraphEdge:
    from_page_id: str
    to_page_id: str
    trigger: str
    params: Dict[str, str] = field(default_factory=dict)
    observed_in_trace: str = ""
    step_index: int = -1
    evidence_count: int = 1
    evidence_steps: List[str] = field(default_factory=list)
    confidence: float = 0.5
    from_page_name: str = ""
    to_page_name: str = ""
    from_route_path: str = ""
    to_route_path: str = ""
    from_route_pattern: str = ""
    to_route_pattern: str = ""
    from_page_key: str = ""
    to_page_key: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'GraphEdge':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class PageBehaviorSpec:
    page_id: str
    page_name: str
    route_pattern: str
    route_params: List[str] = field(default_factory=list)
    navigation_out: List[Dict[str, Any]] = field(default_factory=list)
    navigation_in: List[Dict[str, Any]] = field(default_factory=list)
    page_actions: List[Dict[str, Any]] = field(default_factory=list)
    structured_actions: Dict[str, Any] = field(default_factory=dict)
    task_continuations: List[Dict[str, Any]] = field(default_factory=list)
    interaction_policy: Dict[str, Any] = field(default_factory=dict)
    component_contracts: List[Dict[str, Any]] = field(default_factory=list)
    floating_component: Dict[str, Any] = field(default_factory=dict)
    overlay_policy: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PageBehaviorSpec':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SiteGraph:
    graph_type: str = "navigation_graph_v1"
    edges: List[GraphEdge] = field(default_factory=list)
    nodes: List[Dict[str, Any]] = field(default_factory=list)
    route_hierarchy: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "graph_type": self.graph_type,
            "edges": [e.to_dict() for e in self.edges],
            "nodes": self.nodes,
            "route_hierarchy": self.route_hierarchy
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SiteGraph':
        edges = [GraphEdge.from_dict(e) for e in data.get("edges", [])]
        return cls(
            graph_type=data.get("graph_type", "navigation_graph_v1"),
            edges=edges,
            nodes=data.get("nodes", []) if isinstance(data.get("nodes", []), list) else [],
            route_hierarchy=data.get("route_hierarchy", {}) if isinstance(data.get("route_hierarchy", {}), dict) else {}
        )


@dataclass
class PagePrototype:
    page_id: str
    page_type: str
    page_name: str
    filename: str
    page_name_full: str = ""
    description: str = ""
    primary_functions: List[str] = field(default_factory=list)
    url_pattern: str = ""
    observed_urls: List[str] = field(default_factory=list)
    components: List[Dict[str, Any]] = field(default_factory=list)
    screenshot_refs: List[str] = field(default_factory=list)
    confidence: float = 0.5
    structure_data: Dict[str, Any] = field(default_factory=dict)
    content_data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PagePrototype':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class SiteStructure:
    page_prototypes: List[PagePrototype] = field(default_factory=list)
    site_graph: SiteGraph = field(default_factory=SiteGraph)
    route_tree: Dict[str, Any] = field(default_factory=dict)
    global_navigation: Dict[str, List[Dict]] = field(default_factory=dict)
    page_behaviors: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "page_prototypes": [p.to_dict() for p in self.page_prototypes],
            "site_graph": self.site_graph.to_dict(),
            "route_tree": self.route_tree,
            "global_navigation": self.global_navigation,
            "page_behaviors": self.page_behaviors
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SiteStructure':
        prototypes = [PagePrototype.from_dict(p) for p in data.get("page_prototypes", [])]
        site_graph = SiteGraph.from_dict(data.get("site_graph", {}))
        return cls(
            page_prototypes=prototypes,
            site_graph=site_graph,
            route_tree=data.get("route_tree", {}) if isinstance(data.get("route_tree", {}), dict) else {},
            global_navigation=data.get("global_navigation", {}),
            page_behaviors=data.get("page_behaviors", {}) if isinstance(data.get("page_behaviors", {}), dict) else {}
        )


@dataclass
class SiteContent:
    entity_schemas: List[Dict[str, Any]] = field(default_factory=list)
    entity_relationships: List[Dict[str, Any]] = field(default_factory=list)
    content_samples: Dict[str, List[Dict]] = field(default_factory=dict)
    runtime_state_samples: Dict[str, List[Dict]] = field(default_factory=dict)
    content_constraints: Dict[str, Dict] = field(default_factory=dict)
    entity_component_sources: Dict[str, List[str]] = field(default_factory=dict)
    task_state_contract: Dict[str, Any] = field(default_factory=dict)
    interaction_contract: Dict[str, Any] = field(default_factory=dict)
    schema_registry: Dict[str, Any] = field(default_factory=dict)
    global_execution_schema: Dict[str, Any] = field(default_factory=dict)
    success_evidence_hints: Dict[str, Any] = field(default_factory=dict)
    schema_candidate_projection: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'SiteContent':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class FrameObservation:
    trace_id: str
    step_index: int
    screenshot_path: str


    description: str = ""


    brand_identity: Dict[str, str] = field(default_factory=dict)


    layout_structure: Dict[str, Any] = field(default_factory=dict)


    components: List[Dict[str, Any]] = field(default_factory=list)


    extracted_items: Dict[str, List[Dict]] = field(default_factory=dict)


    page_state: Dict[str, Any] = field(default_factory=dict)


    identified_resources: Dict[str, Any] = field(default_factory=dict)


    confidence: float = 0.5


    observed_url: str = ""
    url_source: str = "unknown"
    normalized_url: str = ""
    url_prefix: str = ""
    url_confidence: float = 0.0
    fallback_cluster_id: str = ""
    structure_signature: str = ""
    content_signature: str = ""
    route_hints: Dict[str, Any] = field(default_factory=dict)
    header_pattern: str = ""
    shell_section: str = ""
    body_pattern: str = ""
    sticky_action_pattern: str = ""
    identity_confidence: float = 0.0
    transition_from_previous: Optional[bool] = None
    transition_kind: str = ""
    transition_evidence: List[str] = field(default_factory=list)


    action: str = ""
    thought: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'FrameObservation':
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})


@dataclass
class AppSpec:
    version: str = "1.0"
    source_website: str = ""
    website_type: str = ""
    trace_ids: List[str] = field(default_factory=list)
    environment_profile: Dict[str, Any] = field(default_factory=dict)
    structure: SiteStructure = field(default_factory=SiteStructure)
    visual_style: Dict[str, Any] = field(default_factory=dict)
    content: SiteContent = field(default_factory=SiteContent)
    metadata: Dict[str, Any] = field(default_factory=dict)
    task_elements: Dict[str, Any] = field(default_factory=dict)
    task_interaction_contracts: Dict[str, Any] = field(default_factory=dict)
    page_signatures: Dict[str, Any] = field(default_factory=dict)
    entity_seed: Dict[str, Any] = field(default_factory=dict)
    semantic_design: Dict[str, Any] = field(default_factory=dict)
    page_execution_contracts: Dict[str, Any] = field(default_factory=dict)
    route_execution_contracts: Dict[str, Any] = field(default_factory=dict)
    input_surface_contracts: Dict[str, Any] = field(default_factory=dict)
    dynamic_surface_contracts: Dict[str, Any] = field(default_factory=dict)
    page_execution_obligations_by_page: Dict[str, Any] = field(default_factory=dict)
    materialized_observation_bindings: Dict[str, Any] = field(default_factory=dict)
    step_boundary_contracts: Dict[str, Any] = field(default_factory=dict)
    step_component_bindings: Dict[str, Any] = field(default_factory=dict)
    page_environment_specs: Dict[str, Any] = field(default_factory=dict)
    interfaces: Dict[str, Any] = field(default_factory=dict)
    interface_requirement_projection: Dict[str, Any] = field(default_factory=dict)

    instance_plan: Dict[str, Any] = field(default_factory=dict)
    cluster_assignments: Dict[str, Any] = field(default_factory=dict)
    runtime_state_observations: Dict[str, Any] = field(default_factory=dict)
    content_lifecycle_plan: Dict[str, Any] = field(default_factory=dict)
    l0_execution_pack: Dict[str, Any] = field(default_factory=dict)
    route_affordance_candidates: Dict[str, Any] = field(default_factory=dict)
    canonical_route_materialization: Dict[str, Any] = field(default_factory=dict)

    def infer_device_profile(self) -> Dict[str, Any]:
        from PIL import Image
        from .viewport_defaults import (
            MOBILE_ASPECT_RATIO,
            MOBILE_VIEWPORT_HEIGHT,
            MOBILE_VIEWPORT_WIDTH,
        )


        pixel_sizes: List[tuple] = []
        for proto in self.structure.page_prototypes:
            for ref in (proto.screenshot_refs or []):
                if os.path.isfile(ref):
                    try:
                        with Image.open(ref) as img:
                            pixel_sizes.append(img.size)
                    except Exception:
                        pass

        if not pixel_sizes:
            return {"device_class": "desktop"}


        resolution_counter = Counter(pixel_sizes)
        most_common_resolution = resolution_counter.most_common(1)[0][0]
        sc_width, sc_height = most_common_resolution
        aspect_ratio = round(sc_height / sc_width, 2) if sc_width > 0 else 1.0

        if aspect_ratio <= 1.5:
            return {"device_class": "desktop"}

        return {
            "device_class": "mobile",
            "viewport_width": MOBILE_VIEWPORT_WIDTH,
            "viewport_height": MOBILE_VIEWPORT_HEIGHT,
            "aspect_ratio": MOBILE_ASPECT_RATIO,
            "screenshot_resolution": [sc_width, sc_height],
            "screenshot_aspect_ratio": aspect_ratio,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": self.version,
            "source_website": self.source_website,
            "website_type": self.website_type,
            "trace_ids": self.trace_ids,
            "environment_profile": self.environment_profile,
            "structure": self.structure.to_dict(),
            "visual_style": self.visual_style,
            "content": self.content.to_dict(),
            "metadata": self.metadata,
            "task_elements": self.task_elements,
            "task_interaction_contracts": self.task_interaction_contracts,
            "page_signatures": self.page_signatures,
            "entity_seed": self.entity_seed,
            "semantic_design": self.semantic_design,
        }

    @staticmethod
    def _write_json(path: str, data: Any):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    @staticmethod
    def _write_json_atomic(path: str, data: Any, lock_map: Optional[Dict[str, threading.Lock]] = None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        lock = None
        if lock_map is not None:
            lock = lock_map.setdefault(path, threading.Lock())
        if lock is not None:
            lock.acquire()
        try:
            temp_path = f"{path}.tmp"
            with open(temp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(temp_path, path)
        finally:
            if lock is not None:
                lock.release()

    @staticmethod
    def _read_json(path: str, default: Any = None) -> Any:
        if not os.path.exists(path):
            return default
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    FIELD_GROUPS: ClassVar[Dict[str, List[str]]] = {
        "core_structure": [
            "site_graph", "route_tree", "global_navigation",
            "page_behaviors", "page_signatures", "page_prototypes",
            "visual_style", "content", "schema_registry",
            "task_elements", "task_interaction_contracts",
            "environment_profile",
            "meta",
        ],
        "execution_contracts": [
            "page_execution_contracts", "route_execution_contracts",
            "input_surface_contracts", "dynamic_surface_contracts",
            "page_execution_obligations_by_page",
        ],
        "observation_bindings": [
            "materialized_observation_bindings",
            "step_boundary_contracts",
            "step_component_bindings",
        ],
        "interfaces": ["interfaces", "interface_requirement_projection"],
        "entity_seed": ["entity_seed"],
        "semantic_design": ["semantic_design"],
        "intermediate": [
            "instance_plan", "cluster_assignments",
            "runtime_state_observations", "content_lifecycle_plan",
            "l0_execution_pack", "route_affordance_candidates",
            "canonical_route_materialization",
        ],
    }


    ALL_FIELDS: ClassVar[Tuple[str, ...]] = (
        "page_prototypes", "site_graph", "route_tree", "global_navigation",
        "page_behaviors", "visual_style", "content", "schema_registry",
        "environment_profile",
        "task_elements", "task_interaction_contracts", "page_signatures",
        "semantic_design", "meta",
        "page_execution_contracts", "route_execution_contracts",
        "input_surface_contracts", "dynamic_surface_contracts",
        "page_execution_obligations_by_page",
        "materialized_observation_bindings", "step_boundary_contracts", "step_component_bindings",
        "interfaces", "interface_requirement_projection",
        "instance_plan", "cluster_assignments",
        "runtime_state_observations", "content_lifecycle_plan",
        "l0_execution_pack", "route_affordance_candidates",
        "canonical_route_materialization",
    )

    def save_as_directory(
        self,
        dir_path: str,
        *,
        fields: Optional[List[str]] = None,
        preserve_semantic_design: bool = False,
    ):
        os.makedirs(dir_path, exist_ok=True)
        file_locks: Dict[str, threading.Lock] = {}
        write_set = set(fields) if fields else set(self.ALL_FIELDS)

        if "page_prototypes" in write_set:
            self._write_page_prototypes(dir_path, file_locks)

        field_payload_map: Dict[str, tuple] = {
            "site_graph": ("site_graph.json", self.structure.site_graph.to_dict()),
            "route_tree": ("route_tree.json", self.structure.route_tree),
            "global_navigation": ("global_navigation.json", self.structure.global_navigation),
            "page_behaviors": ("page_behaviors.json", self.structure.page_behaviors),
            "visual_style": ("visual_style.json", self.visual_style),
            "environment_profile": ("environment_profile.json", self.environment_profile),
            "content": ("content.json", self.content.to_dict()),
            "schema_registry": ("schema_registry.json", self.content.schema_registry or {
                "format": "l0_schema_registry", "schema_revision": 1,
                "catalog_entities": {}, "runtime_states": {}, "refs": {},
            }),
            "task_elements": ("task_elements.json", self.task_elements),
            "task_interaction_contracts": ("task_interaction_contracts.json",
                self.task_interaction_contracts or {
                    "format": "task_interaction_contracts", "schema_revision": 1,
                    "contracts": [], "contracts_by_candidate_id": {},
                }),
            "page_signatures": ("page_signatures.json", self.page_signatures or {
                "format": "page_signatures", "schema_revision": 1,
                "pages": [], "pages_by_id": {},
            }),
            "entity_seed": ("entity_seed.json", self.entity_seed or {
                "format": "entity_seed", "schema_revision": 1, "entities": {},
            }),
            "page_execution_contracts": ("page_execution_contracts.json", self.page_execution_contracts),
            "route_execution_contracts": ("route_execution_contracts.json", self.route_execution_contracts),
            "input_surface_contracts": ("input_surface_contracts.json", self.input_surface_contracts),
            "dynamic_surface_contracts": ("dynamic_surface_contracts.json", self.dynamic_surface_contracts),
            "page_execution_obligations_by_page": ("page_execution_obligations_by_page.json", self.page_execution_obligations_by_page),
            "materialized_observation_bindings": ("materialized_observation_bindings.json", self.materialized_observation_bindings),
            "step_boundary_contracts": ("step_boundary_contracts.json", self.step_boundary_contracts),
            "step_component_bindings": ("step_component_bindings.json", self.step_component_bindings),
            "interfaces": ("interfaces.json", self.interfaces),
            "interface_requirement_projection": ("interface_requirement_projection.json", self.interface_requirement_projection),
            "instance_plan": ("instance_plan.json", self.instance_plan),
            "cluster_assignments": ("cluster_assignments.json", self.cluster_assignments),
            "runtime_state_observations": ("runtime_state_observations.json", self.runtime_state_observations),
            "content_lifecycle_plan": ("content_lifecycle_plan.json", self.content_lifecycle_plan),
            "l0_execution_pack": ("l0_execution_pack.json", self.l0_execution_pack),
            "route_affordance_candidates": ("route_affordance_candidates.json", self.route_affordance_candidates),
            "canonical_route_materialization": ("canonical_route_materialization.json", self.canonical_route_materialization),
        }

        written_fields: List[str] = []
        for field_name, (filename, payload) in field_payload_map.items():
            if field_name not in write_set:
                continue
            self._write_json_atomic(os.path.join(dir_path, filename), payload, lock_map=file_locks)
            written_fields.append(field_name)

        if "semantic_design" in write_set and not preserve_semantic_design:
            self._write_json_atomic(
                os.path.join(dir_path, "semantic_design.json"),
                self.semantic_design or {
                    "format": "site_model_semantic_design", "schema_revision": 1, "views": {},
                },
                lock_map=file_locks,
            )
            written_fields.append("semantic_design")

        if "meta" in write_set:
            meta_dir = os.path.join(dir_path, "meta")
            self._write_json_atomic(
                os.path.join(meta_dir, "core.json"),
                {"version": self.version, "source_website": self.source_website,
                 "website_type": self.website_type, "trace_ids": self.trace_ids},
                lock_map=file_locks,
            )
            self._write_json_atomic(
                os.path.join(meta_dir, "metadata.json"), self.metadata, lock_map=file_locks,
            )
            written_fields.append("meta")

        if "page_prototypes" in write_set:
            written_fields.append("page_prototypes")

        self._write_manifest(dir_path, file_locks, written_fields)

    def _write_page_prototypes(self, dir_path: str, file_locks: Dict[str, threading.Lock]):
        page_proto_dir = os.path.join(dir_path, "page_prototypes")
        os.makedirs(page_proto_dir, exist_ok=True)
        expected_page_files = {
            filename
            for page in self.structure.page_prototypes
            for filename in (
                f"{page.page_id}_structure.json",
                f"{page.page_id}_content.json",
            )
        }


        for filename in os.listdir(page_proto_dir):
            if not (
                filename.endswith("_structure.json")
                or filename.endswith("_content.json")
            ):
                continue
            if filename not in expected_page_files:
                os.unlink(os.path.join(page_proto_dir, filename))
        page_index = {
            "format": "page_prototypes_split_v3",
            "count": len(self.structure.page_prototypes),
            "pages": [],
        }
        for p in self.structure.page_prototypes:
            structure_filename = f"{p.page_id}_structure.json"
            content_filename = f"{p.page_id}_content.json"
            sd_clean = {k: v for k, v in p.structure_data.items()
                        if k not in ("components", "component_relationships", "visual_layout_text")}
            structure_payload = {
                "page_id": p.page_id, "page_type": p.page_type,
                "page_name": p.page_name,
                "page_name_full": p.page_name_full or p.page_name,
                "filename": p.filename, "description": p.description,
                "components": p.components,
                "component_relationships": p.structure_data.get("component_relationships", []),
                "visual_layout_text": p.structure_data.get("visual_layout_text", ""),
                "screenshot_refs": p.screenshot_refs, "confidence": p.confidence,
                "structure_data": sd_clean,
            }
            if p.primary_functions:
                structure_payload["primary_functions"] = p.primary_functions
            if p.url_pattern:
                structure_payload["url_pattern"] = p.url_pattern
            if p.observed_urls:
                structure_payload["observed_urls"] = p.observed_urls
            page_index["pages"].append({
                "page_id": p.page_id, "structure_file": structure_filename,
                "content_file": content_filename, "page_name": p.page_name,
                "page_name_full": p.page_name_full or p.page_name,
                "structure_path": os.path.join("page_prototypes", structure_filename),
                "content_path": os.path.join("page_prototypes", content_filename),
            })
            self._write_json_atomic(
                os.path.join(page_proto_dir, structure_filename), structure_payload,
                lock_map=file_locks,
            )
            self._write_json_atomic(
                os.path.join(page_proto_dir, content_filename), p.content_data,
                lock_map=file_locks,
            )
        self._write_json_atomic(os.path.join(page_proto_dir, "index.json"), page_index, lock_map=file_locks)

    def _write_manifest(self, dir_path: str, file_locks: Dict[str, threading.Lock],
                        written_fields: List[str]):
        manifest_path = os.path.join(dir_path, "manifest.json")
        existing_manifest = self._read_json(manifest_path, default={}) or {}
        existing_write_log = existing_manifest.get("field_write_log", {}) if isinstance(existing_manifest, dict) else {}
        import time
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        for f in written_fields:
            existing_write_log[f] = {"written_at": ts}
        manifest = {
            "format": "site_model_dir_v3",
            "version": self.version,
            "source_website": self.source_website,
            "website_type": self.website_type,
            "counts": {
                "page_prototypes": len(self.structure.page_prototypes),
                "edges": len(self.structure.site_graph.edges),
                "trace_ids": len(self.trace_ids),
            },
            "paths": {
                "page_prototypes": "page_prototypes",
                "site_graph": "site_graph.json",
                "route_tree": "route_tree.json",
                "global_navigation": "global_navigation.json",
                "page_behaviors": "page_behaviors.json",
                "visual_style": "visual_style.json",
                "environment_profile": "environment_profile.json",
                "content": "content.json",
                "schema_registry": "schema_registry.json",
                "task_elements": "task_elements.json",
                "task_interaction_contracts": "task_interaction_contracts.json",
                "page_signatures": "page_signatures.json",
                "entity_seed": "entity_seed.json",
                "semantic_design": "semantic_design.json",
                "page_execution_contracts": "page_execution_contracts.json",
                "route_execution_contracts": "route_execution_contracts.json",
                "input_surface_contracts": "input_surface_contracts.json",
                "dynamic_surface_contracts": "dynamic_surface_contracts.json",
                "page_execution_obligations_by_page": "page_execution_obligations_by_page.json",
                "materialized_observation_bindings": "materialized_observation_bindings.json",
                "step_boundary_contracts": "step_boundary_contracts.json",
                "step_component_bindings": "step_component_bindings.json",
                "interfaces": "interfaces.json",
                "interface_requirement_projection": "interface_requirement_projection.json",
                "instance_plan": "instance_plan.json",
                "cluster_assignments": "cluster_assignments.json",
                "runtime_state_observations": "runtime_state_observations.json",
                "content_lifecycle_plan": "content_lifecycle_plan.json",
                "l0_execution_pack": "l0_execution_pack.json",
                "route_affordance_candidates": "route_affordance_candidates.json",
                "canonical_route_materialization": "canonical_route_materialization.json",
                "meta": "meta",
            },
            "page_prototypes_format": "split_v3",
            "field_write_log": existing_write_log,
        }
        self._write_json_atomic(manifest_path, manifest, lock_map=file_locks)

    @classmethod
    def load_from_directory(cls, dir_path: str) -> 'AppSpec':
        core = cls._read_json(os.path.join(dir_path, "meta", "core.json"), default={}) or {}
        metadata = cls._read_json(os.path.join(dir_path, "meta", "metadata.json"), default={}) or {}

        page_index = cls._read_json(os.path.join(dir_path, "page_prototypes", "index.json"), default=None)
        if not isinstance(page_index, dict):
            raise FileNotFoundError(
                f"Missing page_prototypes/index.json in AppSpec v3 directory: {dir_path}"
            )

        page_prototypes: List[PagePrototype] = []
        for item in page_index.get("pages", []):
            if not isinstance(item, dict):
                continue
            structure_file = item.get("structure_file", "")
            content_file = item.get("content_file", "")
            if not structure_file or not content_file:
                continue
            structure_data = cls._read_json(
                os.path.join(dir_path, "page_prototypes", structure_file),
                default=None
            )
            content_data = cls._read_json(
                os.path.join(dir_path, "page_prototypes", content_file),
                default={}
            ) or {}
            if not isinstance(structure_data, dict):
                continue
            structure_data["content_data"] = content_data if isinstance(content_data, dict) else {}
            page_prototypes.append(PagePrototype.from_dict(structure_data))

        structure = SiteStructure(
            page_prototypes=page_prototypes,
            site_graph=SiteGraph.from_dict(
                cls._read_json(os.path.join(dir_path, "site_graph.json"), default={"edges": []}) or {"edges": []}
            ),
            route_tree=(
                cls._read_json(os.path.join(dir_path, "route_tree.json"), default={}) or {}
            ),
            global_navigation=(
                cls._read_json(os.path.join(dir_path, "global_navigation.json"), default={}) or {}
            ),
            page_behaviors=(
                cls._read_json(os.path.join(dir_path, "page_behaviors.json"), default={}) or {}
            )
        )

        content_raw = cls._read_json(os.path.join(dir_path, "content.json"), default={}) or {}
        content = SiteContent.from_dict(content_raw) if content_raw else SiteContent()
        schema_registry = cls._read_json(os.path.join(dir_path, "schema_registry.json"), default={}) or {}
        if schema_registry and not content.schema_registry:
            content.schema_registry = schema_registry

        visual_style = cls._read_json(os.path.join(dir_path, "visual_style.json"), default={}) or {}

        task_elements = cls._read_json(os.path.join(dir_path, "task_elements.json"), default={}) or {}
        task_interaction_contracts = cls._read_json(
            os.path.join(dir_path, "task_interaction_contracts.json"),
            default={}
        ) or {}
        page_signatures = cls._read_json(os.path.join(dir_path, "page_signatures.json"), default={}) or {}
        entity_seed = cls._read_json(os.path.join(dir_path, "entity_seed.json"), default={}) or {}
        semantic_design = cls._read_json(os.path.join(dir_path, "semantic_design.json"), default={}) or {}

        def _opt(name: str) -> Dict[str, Any]:
            return cls._read_json(os.path.join(dir_path, name), default={}) or {}

        return cls(
            version=core.get("version", "1.0"),
            source_website=core.get("source_website", ""),
            website_type=core.get("website_type", ""),
            trace_ids=core.get("trace_ids", []),
            environment_profile=_opt("environment_profile.json"),
            structure=structure,
            visual_style=visual_style,
            content=content,
            metadata=metadata,
            task_elements=task_elements,
            task_interaction_contracts=task_interaction_contracts,
            page_signatures=page_signatures,
            entity_seed=entity_seed,
            semantic_design=semantic_design,
            page_execution_contracts=_opt("page_execution_contracts.json"),
            route_execution_contracts=_opt("route_execution_contracts.json"),
            input_surface_contracts=_opt("input_surface_contracts.json"),
            dynamic_surface_contracts=_opt("dynamic_surface_contracts.json"),
            page_execution_obligations_by_page=_opt("page_execution_obligations_by_page.json"),
            materialized_observation_bindings=_opt("materialized_observation_bindings.json"),
            step_boundary_contracts=_opt("step_boundary_contracts.json"),
            step_component_bindings=_opt("step_component_bindings.json"),
            page_environment_specs={},
            interfaces=_opt("interfaces.json"),
            interface_requirement_projection=_opt("interface_requirement_projection.json"),
            instance_plan=_opt("instance_plan.json"),
            cluster_assignments=_opt("cluster_assignments.json"),
            runtime_state_observations=_opt("runtime_state_observations.json"),
            content_lifecycle_plan=_opt("content_lifecycle_plan.json"),
            l0_execution_pack=_opt("l0_execution_pack.json"),
            route_affordance_candidates=_opt("route_affordance_candidates.json"),
            canonical_route_materialization=_opt("canonical_route_materialization.json"),
        )

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AppSpec':
        return cls(
            version=data.get("version", "1.0"),
            source_website=data.get("source_website", ""),
            website_type=data.get("website_type", ""),
            trace_ids=data.get("trace_ids", []),
            environment_profile=(
                data.get("environment_profile", {})
                if isinstance(data.get("environment_profile", {}), dict)
                else {}
            ),
            structure=SiteStructure.from_dict(data.get("structure", {})),
            visual_style=data.get("visual_style", {}),
            content=SiteContent.from_dict(data.get("content", {})),
            metadata=data.get("metadata", {}),
            task_elements=data.get("task_elements", {}) if isinstance(data.get("task_elements", {}), dict) else {},
            task_interaction_contracts=data.get("task_interaction_contracts", {}) if isinstance(data.get("task_interaction_contracts", {}), dict) else {},
            page_signatures=data.get("page_signatures", {}) if isinstance(data.get("page_signatures", {}), dict) else {},
            entity_seed=data.get("entity_seed", {}) if isinstance(data.get("entity_seed", {}), dict) else {},
            semantic_design=data.get("semantic_design", {}) if isinstance(data.get("semantic_design", {}), dict) else {},
        )

    def get_final_page_inputs(self) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        for proto in self.structure.page_prototypes:
            if not proto.filename:
                continue
            structure_data = proto.structure_data if isinstance(proto.structure_data, dict) else {}
            if structure_data.get("excluded_from_pagegen") is True:
                continue
            content_data = proto.content_data if isinstance(proto.content_data, dict) else {}
            prompt_screenshot_refs = list(proto.screenshot_refs or [])
            page_screenshot_refs = list(structure_data.get("resource_screenshot_refs", []) or prompt_screenshot_refs)
            prompt_ref_set = set(prompt_screenshot_refs)
            rows.append({
                "page_id": proto.page_id,
                "filename": proto.filename,
                "page_name": proto.page_name,
                "page_type": proto.page_type,
                "description": proto.description,
                "page_screenshot_refs": page_screenshot_refs,
                "prompt_screenshot_refs": prompt_screenshot_refs,
                "non_prompt_page_screenshot_refs": [
                    ref for ref in page_screenshot_refs if ref not in prompt_ref_set
                ],
                "unique_prompt_screenshot_refs": prompt_screenshot_refs,
                "extracted_items": content_data.get("extracted_items", {}),
                "entity_task_relevance": content_data.get("entity_task_relevance", {}),
                "entity_component_mapping": content_data.get("entity_component_mapping", []),
                "item_lifecycle": content_data.get("item_lifecycle", {}),
                "runtime_items": content_data.get("runtime_items", {}),
                "screenshot_render_roles": structure_data.get("screenshot_render_roles", {}),
                "screenshot_render_annotations": structure_data.get("screenshot_render_annotations", {}),
                "initial_authority_kind_by_step": structure_data.get("initial_authority_kind_by_step", {}),
            })
        return rows

    def save(self, path: str, *, fields: Optional[List[str]] = None,
             preserve_semantic_design: bool = False):
        if path.endswith(".json"):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
            return
        self.save_as_directory(path, fields=fields,
                               preserve_semantic_design=preserve_semantic_design)

    @classmethod
    def load(cls, path: str) -> 'AppSpec':
        if os.path.isdir(path):
            manifest_path = os.path.join(path, "manifest.json")
            if os.path.exists(manifest_path):
                return cls.load_from_directory(path)
            raise FileNotFoundError(f"AppSpec directory missing manifest.json: {path}")

        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return cls.from_dict(data)
