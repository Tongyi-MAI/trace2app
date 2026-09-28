
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional


@dataclass
class InstrumentationValueSpec:
    kind: str = "legacy"
    expr: str = ""
    fields: List[Dict[str, Any]] = field(default_factory=list)
    items: List[Dict[str, Any]] = field(default_factory=list)
    expr_fallback: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'InstrumentationValueSpec':
        return cls(**data)


@dataclass
class InstrumentationVariable:
    variable_name: str
    variable_type: str
    set_in_function: str
    set_condition: str
    value_to_set: str
    reason: str
    value_spec: Optional[InstrumentationValueSpec] = None

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'InstrumentationVariable':
        value_spec = data.get("value_spec")
        return cls(
            variable_name=data["variable_name"],
            variable_type=data["variable_type"],
            set_in_function=data["set_in_function"],
            set_condition=data["set_condition"],
            value_to_set=data.get("value_to_set", ""),
            reason=data["reason"],
            value_spec=InstrumentationValueSpec.from_dict(value_spec) if isinstance(value_spec, dict) else None,
        )


@dataclass
class InstrumentationSignal:
    signal_name: str
    signal_role: str
    stage: str
    variable_type: str
    set_in_function: str
    set_condition: str
    value_to_set: str = ""
    reason: str = ""
    value_spec: Optional[InstrumentationValueSpec] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'InstrumentationSignal':
        value_spec = data.get("value_spec")
        return cls(
            signal_name=data["signal_name"],
            signal_role=data["signal_role"],
            stage=data.get("stage", ""),
            variable_type=data["variable_type"],
            set_in_function=data["set_in_function"],
            set_condition=data["set_condition"],
            value_to_set=data.get("value_to_set", ""),
            reason=data.get("reason", ""),
            value_spec=InstrumentationValueSpec.from_dict(value_spec) if isinstance(value_spec, dict) else None,
        )


@dataclass
class InstrumentationRequirement:
    task_id: str
    task_name: str
    task_description: str
    needs_instrumentation: bool
    existing_variables: List[str] = field(default_factory=list)
    required_variables: List[InstrumentationVariable] = field(default_factory=list)
    progress_signals: List[InstrumentationSignal] = field(default_factory=list)
    success_signal: Optional[InstrumentationSignal] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_name": self.task_name,
            "task_description": self.task_description,
            "needs_instrumentation": self.needs_instrumentation,
            "existing_variables": self.existing_variables,
            "required_variables": [v.to_dict() for v in self.required_variables],
            "progress_signals": [s.to_dict() for s in self.progress_signals],
            "success_signal": self.success_signal.to_dict() if self.success_signal else None,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'InstrumentationRequirement':
        return cls(
            task_id=data["task_id"],
            task_name=data["task_name"],
            task_description=data["task_description"],
            needs_instrumentation=data["needs_instrumentation"],
            existing_variables=data.get("existing_variables", []),
            required_variables=[
                InstrumentationVariable.from_dict(v)
                for v in data.get("required_variables", [])
            ],
            progress_signals=[
                InstrumentationSignal.from_dict(v)
                for v in data.get("progress_signals", [])
            ],
            success_signal=InstrumentationSignal.from_dict(data["success_signal"]) if isinstance(data.get("success_signal"), dict) else None,
        )


@dataclass
class InstrumentationPlan:
    requirements: List[InstrumentationRequirement] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requirements": [r.to_dict() for r in self.requirements]
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'InstrumentationPlan':
        return cls(
            requirements=[
                InstrumentationRequirement.from_dict(r)
                for r in data.get("requirements", [])
            ]
        )

    def get_variables_for_task(self, task_id: str) -> List[str]:
        for req in self.requirements:
            if req.task_id == task_id:
                names = [v.variable_name for v in req.required_variables]
                names.extend([s.signal_name for s in req.progress_signals])
                if req.success_signal:
                    names.append(req.success_signal.signal_name)
                return names
        return []

    def get_all_variables(self) -> List[str]:
        all_vars = []
        for req in self.requirements:
            all_vars.extend([v.variable_name for v in req.required_variables])
            all_vars.extend([s.signal_name for s in req.progress_signals])
            if req.success_signal:
                all_vars.append(req.success_signal.signal_name)
        return all_vars

    def has_instrumentation_needs(self) -> bool:
        return any(req.needs_instrumentation for req in self.requirements)


@dataclass
class ValidationResult:
    success: bool
    original_tests_passed: bool
    instrumentation_tests_passed: bool
    total_tests: int = 0
    passed_tests: int = 0
    failed_tests: int = 0
    iterations_used: int = 0
    message: str = ""
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class EvaluatorValidationResult:
    success: bool
    total_evaluators: int = 0
    validated_evaluators: int = 0
    failed_evaluators: int = 0
    iterations_used: int = 0
    message: str = ""
    errors: List[str] = field(default_factory=list)
    failed_evaluator_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
