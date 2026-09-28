
from dataclasses import dataclass, asdict, fields as dc_fields, field
from typing import Dict, Any, List, Optional


class Trace2WorldBase:

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        known = {f.name for f in dc_fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class EntityField(Trace2WorldBase):
    name: str = ""
    type: str = ""
    required: bool = False
    primary_key: bool = False
    foreign_key: Optional[str] = None
    description: str = ""
    observed_examples: List[Any] = field(default_factory=list)


@dataclass
class Entity(Trace2WorldBase):
    name: str = ""
    storage_key: str = ""
    fields: List[EntityField] = field(default_factory=list)
    data_pre_generation_num: str = "none"
    description: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        known = {f.name for f in dc_fields(cls)}
        d = {k: v for k, v in data.items() if k in known}
        d["fields"] = [
            EntityField.from_dict(f) if isinstance(f, dict) else f
            for f in d.get("fields", [])
        ]
        return cls(**d)


@dataclass
class EntityRelationship(Trace2WorldBase):
    from_entity: str = ""
    to_entity: str = ""
    type: str = ""
    field_name: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "from": self.from_entity,
            "to": self.to_entity,
            "type": self.type,
            "field": self.field_name,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        return cls(
            from_entity=data.get("from", ""),
            to_entity=data.get("to", ""),
            type=data.get("type", ""),
            field_name=data.get("field", ""),
        )


@dataclass
class EntityReference(Trace2WorldBase):
    entity: str = ""
    displayField: str = ""
    valueField: str = ""


@dataclass
class InterfaceParam(Trace2WorldBase):
    name: str = ""
    type: str = ""
    required: bool = True
    default: Any = None
    description: str = ""
    entityReference: Optional[EntityReference] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        known = {f.name for f in dc_fields(cls)}
        d = {k: v for k, v in data.items() if k in known}
        if isinstance(d.get("entityReference"), dict):
            d["entityReference"] = EntityReference.from_dict(d["entityReference"])
        return cls(**d)


@dataclass
class InterfaceReturn(Trace2WorldBase):
    type: str = "void"
    properties: Dict[str, Any] = field(default_factory=dict)
    items: Any = None


@dataclass
class Interface(Trace2WorldBase):
    name: str = ""
    description: str = ""
    parameters: List[InterfaceParam] = field(default_factory=list)
    returns: Optional[InterfaceReturn] = None
    relatedTasks: List[str] = field(default_factory=list)
    sideEffects: List[str] = field(default_factory=list)
    synthetic_rationale: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        known = {f.name for f in dc_fields(cls)}
        d = {k: v for k, v in data.items() if k in known}
        d["parameters"] = [
            InterfaceParam.from_dict(p) if isinstance(p, dict) else p
            for p in d.get("parameters", [])
        ]
        if isinstance(d.get("returns"), dict):
            d["returns"] = InterfaceReturn.from_dict(d["returns"])
        elif "returns" not in d:
            d["returns"] = InterfaceReturn(type="void")
        return cls(**d)


@dataclass
class WrappedInterface(Trace2WorldBase):
    name: str = ""
    description: str = ""
    parameters: List[InterfaceParam] = field(default_factory=list)
    returns: Optional[InterfaceReturn] = None
    sideEffects: List[str] = field(default_factory=list)
    relatedTasks: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        known = {f.name for f in dc_fields(cls)}
        d = {k: v for k, v in data.items() if k in known}
        d["parameters"] = [
            InterfaceParam.from_dict(p) if isinstance(p, dict) else p
            for p in d.get("parameters", [])
        ]
        if isinstance(d.get("returns"), dict):
            d["returns"] = InterfaceReturn.from_dict(d["returns"])
        elif "returns" not in d:
            d["returns"] = InterfaceReturn(type="void")
        return cls(**d)


@dataclass
class StateDataModel(Trace2WorldBase):
    name: str = ""
    fields: List[EntityField] = field(default_factory=list)
    dynamic: bool = True
    description: str = ""

    @classmethod
    def from_dict(cls, data: Dict[str, Any]):
        known = {f.name for f in dc_fields(cls)}
        d = {k: v for k, v in data.items() if k in known}
        d["fields"] = [
            EntityField.from_dict(f) if isinstance(f, dict) else f
            for f in d.get("fields", [])
        ]
        return cls(**d)


@dataclass
class ImplementationMapping(Trace2WorldBase):
    wrapped_function: str = ""
    original_function: str = ""
    parameter_mapping: Dict[str, str] = field(default_factory=dict)
    state_dependencies: List[str] = field(default_factory=list)


@dataclass
class HelperFunction(Trace2WorldBase):
    name: str = ""
    description: str = ""
    returns: str = ""
    visibility: str = "private"


@dataclass
class Trace2WorldTask(Trace2WorldBase):
    id: str = ""
    name: str = ""
    description: str = ""
    steps: List[str] = field(default_factory=list)


@dataclass
class Trace2WorldEvaluator(Trace2WorldBase):
    task_id: str = ""
    name: str = ""
    description: str = ""
    localStorage_variables: List[str] = field(default_factory=list)
    evaluation_logic: str = ""
