
import os
import json
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict, field, fields as dc_fields

from trace2world_pipeline.core.models import (
    Trace2WorldBase,
    Entity, EntityField, EntityRelationship,
    Interface, InterfaceParam, InterfaceReturn, EntityReference,
    WrappedInterface, StateDataModel, ImplementationMapping, HelperFunction,
    Trace2WorldTask, Trace2WorldEvaluator,
)
from trace2world_pipeline.core.post_processing_paths import post_processing_path
from trace2world_pipeline.core.phase_output import PhaseOutputManager


@dataclass
class DesignAnalysis:
    visual_features: Dict[str, Any]
    color_scheme: Dict[str, Any]
    layout_characteristics: Dict[str, Any]
    ui_patterns: List[Dict[str, Any]]
    typography: Dict[str, Any]
    spacing_system: Dict[str, Any]
    interaction_hints: List[str]


@dataclass
class Trace2WorldDataModels(Trace2WorldBase):
    entities: List[Entity] = field(default_factory=list)
    relationships: List[EntityRelationship] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Trace2WorldDataModels':
        return cls(
            entities=[Entity.from_dict(e) if isinstance(e, dict) else e
                      for e in data.get('entities', [])],
            relationships=[EntityRelationship.from_dict(r) if isinstance(r, dict) else r
                           for r in data.get('relationships', [])],
            metadata=data.get('metadata', {}),
        )


@dataclass
class Trace2WorldInterfaces(Trace2WorldBase):
    interfaces: List[Interface] = field(default_factory=list)
    helperFunctions: List[HelperFunction] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Trace2WorldInterfaces':
        return cls(
            interfaces=[Interface.from_dict(i) if isinstance(i, dict) else i
                        for i in data.get('interfaces', [])],
            helperFunctions=[HelperFunction.from_dict(h) if isinstance(h, dict) else h
                             for h in data.get('helperFunctions', [])],
            metadata=data.get('metadata', {}),
        )


@dataclass
class Trace2WorldWrappedInterfaces(Trace2WorldBase):
    wrapped_interfaces: List[WrappedInterface] = field(default_factory=list)
    original_interfaces: List[Interface] = field(default_factory=list)
    state_data_models: List[StateDataModel] = field(default_factory=list)
    implementation_mapping: List[ImplementationMapping] = field(default_factory=list)
    helper_functions: List[HelperFunction] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'Trace2WorldWrappedInterfaces':
        return cls(
            wrapped_interfaces=[WrappedInterface.from_dict(w) if isinstance(w, dict) else w
                                for w in data.get('wrapped_interfaces', [])],
            original_interfaces=[Interface.from_dict(i) if isinstance(i, dict) else i
                                 for i in data.get('original_interfaces', [])],
            state_data_models=[StateDataModel.from_dict(s) if isinstance(s, dict) else s
                               for s in data.get('state_data_models', [])],
            implementation_mapping=[ImplementationMapping.from_dict(m) if isinstance(m, dict) else m
                                    for m in data.get('implementation_mapping', [])],
            helper_functions=[HelperFunction.from_dict(h) if isinstance(h, dict) else h
                              for h in data.get('helper_functions', [])],
            metadata=data.get('metadata', {}),
        )


@dataclass
class Trace2WorldGenerationResult(Trace2WorldBase):
    success: bool = False
    implementation: str = ""
    tests: str = ""
    test_results: Dict[str, Any] = field(default_factory=dict)
    data_models: Dict[str, Any] = field(default_factory=dict)
    interfaces: Dict[str, Any] = field(default_factory=dict)
    wrapped_interfaces: Dict[str, Any] = field(default_factory=dict)
    tasks: List[Dict[str, Any]] = field(default_factory=list)
    architecture: Dict[str, Any] = field(default_factory=dict)
    page_designs: List[Dict[str, Any]] = field(default_factory=list)
    design_analysis: Optional[Dict[str, Any]] = None
    evaluators: List[Trace2WorldEvaluator] = field(default_factory=list)
    generation_time: float = 0.0
    iterations_used: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)


class Trace2WorldDataManager:

    def __init__(self, output_dir: Optional[str] = None):
        self.output_dir = output_dir or "src/test/generated"
        self.data_dir = str(PhaseOutputManager(self.output_dir).debug("post_processing", "data_manager"))


        self.tasks: List[Trace2WorldTask] = []
        self.data_models: Optional[Trace2WorldDataModels] = None
        self.interfaces: Optional[Trace2WorldInterfaces] = None
        self.wrapped_interfaces: Optional[Trace2WorldWrappedInterfaces] = None
        self.implementation: str = ""
        self.tests: str = ""
        self.test_results: Dict[str, Any] = {}
        self.architecture: Dict[str, Any] = {}
        self.page_designs: List[Dict[str, Any]] = []
        self.design_analysis: Optional[DesignAnalysis] = None
        self.evaluators: List[Trace2WorldEvaluator] = []
        self.generation_result: Optional[Trace2WorldGenerationResult] = None
        self.site_model = None
        self.page_framework = None


        self.metadata = {
            "created_at": datetime.now().isoformat(),
            "website_type": None,
            "version": "1.0.0"
        }


    def _setup_directories(self):
        os.makedirs(self.data_dir, exist_ok=True)


    def set_tasks(self, tasks: List[Dict[str, Any]], website_type: str = None):
        self.tasks = [Trace2WorldTask.from_dict(task) for task in tasks]
        if website_type:
            self.metadata["website_type"] = website_type
        tasks_path = post_processing_path(self.output_dir, "tasks", "tasks.json")
        tasks_path.parent.mkdir(parents=True, exist_ok=True)
        with tasks_path.open("w", encoding="utf-8") as f:
            json.dump({"tasks": tasks, "website_type": website_type}, f, indent=2, ensure_ascii=False)

    def get_tasks(self) -> List[Trace2WorldTask]:
        return self.tasks

    def get_tasks_dict(self) -> List[Dict[str, Any]]:
        return [task.to_dict() for task in self.tasks]


    def set_data_models(self, data_models: Dict[str, Any]):
        self.data_models = Trace2WorldDataModels.from_dict(data_models)
        self._save_data("entity_schema.json", data_models)

    def get_data_models(self) -> Optional[Trace2WorldDataModels]:
        return self.data_models

    def get_data_models_dict(self) -> Dict[str, Any]:
        return self.data_models.to_dict() if self.data_models else {}


    def set_interfaces(self, interfaces: Dict[str, Any]):
        self.interfaces = Trace2WorldInterfaces.from_dict(interfaces)
        self._save_data("interfaces.json", interfaces)

    def get_interfaces(self) -> Optional[Trace2WorldInterfaces]:
        return self.interfaces

    def get_interfaces_dict(self) -> Dict[str, Any]:
        return self.interfaces.to_dict() if self.interfaces else {}


    def set_wrapped_interfaces(self, wrapped_interfaces: Dict[str, Any]):
        self.wrapped_interfaces = Trace2WorldWrappedInterfaces.from_dict(wrapped_interfaces)
        self._save_data("wrapped_interfaces.json", wrapped_interfaces)

    def get_wrapped_interfaces(self) -> Optional[Trace2WorldWrappedInterfaces]:
        return self.wrapped_interfaces

    def get_wrapped_interfaces_dict(self) -> Dict[str, Any]:
        return self.wrapped_interfaces.to_dict() if self.wrapped_interfaces else {}

    def get_enhanced_data_models(self) -> Dict[str, Any]:
        base_models = self.get_data_models_dict()

        if self.wrapped_interfaces and self.wrapped_interfaces.state_data_models:

            enhanced_entities = base_models.get("entities", []).copy()

            enhanced_entities.extend([
                s.to_dict() if hasattr(s, 'to_dict') else s
                for s in self.wrapped_interfaces.state_data_models
            ])

            enhanced_models = base_models.copy()
            enhanced_models["entities"] = enhanced_entities
            enhanced_models["has_state_models"] = True
            enhanced_models["state_model_count"] = len(self.wrapped_interfaces.state_data_models)

            return enhanced_models

        return base_models


    def set_implementation(self, implementation: str):
        self.implementation = implementation
        self._save_code("business_logic.js", implementation)

    def get_implementation(self) -> str:
        return self.implementation


    def set_tests(self, tests: str):
        self.tests = tests
        self._save_code("test_flows.js", tests)

    def get_tests(self) -> str:
        return self.tests


    def set_test_results(self, test_results: Dict[str, Any]):
        self.test_results = test_results
        self._save_data("test_results.json", test_results)

    def get_test_results(self) -> Dict[str, Any]:
        return self.test_results


    def set_architecture(self, architecture: Any):

        if hasattr(architecture, '__dict__'):
            self.architecture = asdict(architecture) if hasattr(architecture, '__dataclass_fields__') else vars(architecture)
        else:
            self.architecture = architecture
        self._save_data("architecture.json", self.architecture)

    def get_architecture(self) -> Dict[str, Any]:
        return self.architecture


    def set_page_designs(self, page_designs: Any):
        if isinstance(page_designs, list):
            self.page_designs = []
            for design in page_designs:
                if hasattr(design, '__dict__'):

                    design_dict = asdict(design) if hasattr(design, '__dataclass_fields__') else vars(design)
                    self.page_designs.append(design_dict)
                else:
                    self.page_designs.append(design)
        else:
            self.page_designs = page_designs

        self._save_data("page_designs.json", self.page_designs)

    def get_page_designs(self) -> List[Dict[str, Any]]:
        return self.page_designs

    def update_page_designs_with_layouts(self, page_designs_with_layouts: List[Any]):
        self.set_page_designs(page_designs_with_layouts)
        self._save_data("page_designs_with_layouts.json", self.page_designs)

    def set_page_framework(self, framework: Any):
        if hasattr(framework, '__dataclass_fields__'):
            self.page_framework = framework
            framework_dict = asdict(framework)
        else:
            self.page_framework = framework
            framework_dict = framework if isinstance(framework, (dict, type(None))) else vars(framework)

        if framework_dict is not None:
            self._save_data("page_framework.json", framework_dict)

    def get_page_framework(self) -> Optional[Dict[str, Any]]:
        if self.page_framework is None:
            return None
        if hasattr(self.page_framework, '__dataclass_fields__'):
            return asdict(self.page_framework)
        elif isinstance(self.page_framework, dict):
            return self.page_framework
        else:
            return vars(self.page_framework)


    def set_design_analysis(self, design_analysis: Optional[DesignAnalysis]):
        self.design_analysis = design_analysis
        if design_analysis:

            analysis_dict = asdict(design_analysis) if hasattr(design_analysis, '__dataclass_fields__') else vars(design_analysis)
            self._save_data("design_analysis.json", analysis_dict)

    def get_design_analysis(self) -> Optional[DesignAnalysis]:
        return self.design_analysis


    def set_site_model(self, site_model):
        self.site_model = site_model

    def get_site_model(self):
        return self.site_model


    def create_generation_result(self, success: bool, generation_time: float,
                                iterations_used: int) -> Trace2WorldGenerationResult:
        self.generation_result = Trace2WorldGenerationResult(
            success=success,
            implementation=self.implementation,
            tests=self.tests,
            test_results=self.test_results,
            data_models=self.get_enhanced_data_models(),
            interfaces=self.get_interfaces_dict(),
            wrapped_interfaces=self.get_wrapped_interfaces_dict(),
            tasks=[task.to_dict() for task in self.tasks],
            architecture=self.architecture,
            page_designs=self.page_designs,
            design_analysis=asdict(self.design_analysis) if self.design_analysis else None,
            evaluators=self.evaluators,
            generation_time=generation_time,
            iterations_used=iterations_used,
            metadata=self.metadata
        )


        self._save_data("generation_result.json", self.generation_result.to_dict())

        return self.generation_result

    def get_generation_result(self) -> Optional[Trace2WorldGenerationResult]:
        return self.generation_result


    def _save_data(self, filename: str, data: Any):
        filepath = os.path.join(self.data_dir, filename)
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    def _save_code(self, filename: str, code: str):
        pom = PhaseOutputManager(self.output_dir)
        if filename == "business_logic.js":
            filepath = str(pom.formal("sdk_generation", "runtime", filename))
        elif filename == "test_flows.js":
            filepath = str(pom.formal("sdk_generation", "validation", "test_flows", filename))
        else:
            filepath = str(pom.debug("post_processing", "data_manager", "code", filename))
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(code)

    def _load_data(self, filename: str) -> Optional[Any]:
        filepath = os.path.join(self.data_dir, filename)
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
        return None


    def validate_data_models(self) -> Dict[str, Any]:
        if not self.data_models:
            return {"valid": False, "errors": ["No data models set"]}

        errors = []
        warnings = []


        if not self.data_models.entities:
            errors.append("No entities defined")
        else:
            for entity in self.data_models.entities:
                if not entity.name:
                    errors.append(f"Entity missing name: {entity}")
                if not entity.fields:
                    warnings.append(f"Entity {entity.name or 'unknown'} has no fields")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings
        }

    def validate_interfaces(self) -> Dict[str, Any]:
        if not self.interfaces:
            return {"valid": False, "errors": ["No interfaces set"]}

        errors = []
        warnings = []


        if not self.interfaces.interfaces:
            errors.append("No interfaces defined")
        else:
            for interface in self.interfaces.interfaces:
                if not interface.name:
                    errors.append(f"Interface missing name: {interface}")
                if not interface.parameters:
                    warnings.append(f"Interface {interface.name or 'unknown'} has no parameters")

        return {
            "valid": len(errors) == 0,
            "errors": errors,
            "warnings": warnings
        }


    def export_debug_data(self, include_code: bool = True) -> Dict[str, Any]:
        debug_data = {
            "metadata": self.metadata,
            "tasks": self.get_tasks_dict(),
            "data_models": self.get_data_models_dict(),
            "interfaces": self.get_interfaces_dict(),
            "architecture": self.architecture,
            "page_designs": self.page_designs,
            "test_results": self.test_results,
            "validations": {
                "data_models": self.validate_data_models(),
                "interfaces": self.validate_interfaces()
            }
        }

        if include_code:
            debug_data["implementation"] = self.implementation[:1000] + "..." if len(self.implementation) > 1000 else self.implementation
            debug_data["tests"] = self.tests[:1000] + "..." if len(self.tests) > 1000 else self.tests

        return debug_data

    def save_debug_data(self, filename: str = "debug_data.json"):
        debug_data = self.export_debug_data()
        self._save_data(filename, debug_data)


    def get_summary(self) -> Dict[str, Any]:

        enhanced_data = self.get_enhanced_data_models()
        base_entity_count = len(self.data_models.entities) if self.data_models else 0

        return {
            "website_type": self.metadata.get("website_type"),
            "task_count": len(self.tasks),
            "entity_count": len(enhanced_data.get("entities", [])),
            "base_entity_count": base_entity_count,
            "state_entity_count": enhanced_data.get("state_model_count", 0),
            "interface_count": len(self.interfaces.interfaces) if self.interfaces else 0,
            "wrapped_interface_count": len(self.wrapped_interfaces.wrapped_interfaces) if self.wrapped_interfaces else 0,
            "implementation_size": len(self.implementation),
            "tests_size": len(self.tests),
            "test_results": {
                "total": self.test_results.get("total", 0),
                "passed": self.test_results.get("passed", 0),
                "failed": self.test_results.get("failed", 0)
            } if self.test_results else None,
            "generation_success": self.generation_result.success if self.generation_result else None,
            "has_wrapped_interfaces": self.wrapped_interfaces is not None
        }

    def print_summary(self):
        summary = self.get_summary()
        print("\n=== TDD Data Summary ===")
        print(f"Website Type: {summary['website_type']}")
        print(f"Tasks: {summary['task_count']}")
        print(f"Total Entities: {summary['entity_count']} (Business: {summary['base_entity_count']}, State: {summary['state_entity_count']})")
        print(f"Original Interfaces: {summary['interface_count']}")
        if summary['has_wrapped_interfaces']:
            print(f"Wrapped Interfaces: {summary['wrapped_interface_count']}")
        print(f"Implementation Size: {summary['implementation_size']} chars")
        print(f"Tests Size: {summary['tests_size']} chars")
        if summary['test_results']:
            print(f"Test Results: {summary['test_results']['passed']}/{summary['test_results']['total']} passed")
        if self.page_designs:
            print(f"Page Designs: {len(self.page_designs)} pages")
        print("=" * 24)
