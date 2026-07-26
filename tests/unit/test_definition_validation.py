from __future__ import annotations

import copy
import importlib
import sys
import tomllib
from pathlib import Path
from types import MappingProxyType

import pytest


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class StubToolCatalog:
    def __init__(self) -> None:
        self._schemas = {
            ("prepare", "1.0.0"): {"path": str},
            ("publish", "2.1.0"): {"target": str, "metadata": dict},
            ("rollback", "1.0.0"): {"target": str},
        }

    def contains(self, name: str, version: str) -> bool:
        return (name, version) in self._schemas

    def validate_arguments(
        self,
        name: str,
        version: str,
        arguments: dict[str, object],
    ) -> None:
        schema = self._schemas[(name, version)]
        missing = sorted(set(schema) - set(arguments))
        unexpected = sorted(set(arguments) - set(schema))
        if missing or unexpected:
            raise ValueError(
                f"missing={missing}, unexpected={unexpected}"
            )
        for key, expected_type in schema.items():
            if not isinstance(arguments[key], expected_type):
                raise ValueError(f"{key} must be {expected_type.__name__}")

    def coerce_arguments(
        self,
        name: str,
        version: str,
        arguments: dict[str, object],
    ) -> dict[str, object]:
        self.validate_arguments(name, version, arguments)
        return arguments


def load_definitions_module():
    try:
        return importlib.import_module("changepilot.workflow.domain.definitions")
    except ModuleNotFoundError as exc:
        pytest.fail(f"missing implementation module: {exc.name}")


def base_payload() -> dict[str, object]:
    return {
        "definition_id": "workflow.release",
        "version": 7,
        "steps": [
            {
                "id": "publish",
                "tool": {"name": "publish", "version": "2.1.0"},
                "arguments": {
                    "target": "prod",
                    "metadata": {"team": "platform", "ticket": "CP-42"},
                },
                "depends_on": ["prepare"],
                "risk": "high",
                "retry": {"initial_backoff_seconds": 2.5},
                "compensation_tool": {"name": "rollback", "version": "1.0.0"},
            },
            {
                "id": "prepare",
                "tool": {"version": "1.0.0", "name": "prepare"},
                "arguments": {"path": "/srv/release"},
            },
        ],
    }


def assert_validation_error(payload: dict[str, object], text: str) -> None:
    definitions = load_definitions_module()
    with pytest.raises(definitions.DefinitionValidationError) as excinfo:
        definitions.WorkflowDefinition.from_mapping(payload, StubToolCatalog())
    assert text in str(excinfo.value)


def test_from_mapping_normalizes_steps_and_produces_stable_digest() -> None:
    definitions = load_definitions_module()

    original = base_payload()
    reordered = {
        "version": original["version"],
        "steps": [
            {
                "arguments": {"path": "/srv/release"},
                "tool": {"name": "prepare", "version": "1.0.0"},
                "id": "prepare",
            },
            {
                "risk": "high",
                "depends_on": ["prepare"],
                "compensation_tool": {"version": "1.0.0", "name": "rollback"},
                "retry": {"initial_backoff_seconds": 2.5},
                "id": "publish",
                "tool": {"version": "2.1.0", "name": "publish"},
                "arguments": {
                    "metadata": {"ticket": "CP-42", "team": "platform"},
                    "target": "prod",
                },
            },
        ],
        "definition_id": original["definition_id"],
    }

    normalized = definitions.WorkflowDefinition.from_mapping(
        original,
        StubToolCatalog(),
    )
    reordered_definition = definitions.WorkflowDefinition.from_mapping(
        reordered,
        StubToolCatalog(),
    )

    assert [step.id for step in normalized.steps] == ["prepare", "publish"]
    assert normalized.version == 7
    assert isinstance(normalized.version, int)
    assert normalized.digest == reordered_definition.digest
    assert normalized.steps[0].risk == "low"
    assert normalized.steps[0].retry.max_attempts == 3
    assert normalized.steps[0].retry.initial_backoff_seconds == 1.0


def test_project_uses_standard_backend_and_declares_planned_dependencies() -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert pyproject["build-system"]["requires"] == ["setuptools>=75"]
    assert pyproject["build-system"]["build-backend"] == "setuptools.build_meta"
    assert "backend-path" not in pyproject["build-system"]
    assert pyproject["project"]["dependencies"] == [
        "pydantic>=2.10,<3",
        "sqlalchemy>=2.0,<3",
        "alembic>=1.14,<2",
        "langgraph>=1.0,<2",
        "openai>=2.0,<3",
        "pyyaml>=6.0,<7",
        "fastapi>=0.116,<1",
        "uvicorn>=0.35,<1",
    ]
    assert pyproject["project"]["optional-dependencies"]["retrieval"] == [
        "sentence-transformers>=5.0,<6",
    ]
    assert pyproject["project"]["optional-dependencies"]["dev"] == [
        "pytest>=8.3,<9",
        "pytest-cov>=6,<7",
        "hypothesis>=6.120,<7",
    ]


def test_package_init_does_not_expose_build_backend_hooks() -> None:
    package = importlib.import_module("changepilot")

    assert not hasattr(package, "build_wheel")
    assert not hasattr(package, "build_editable")
    assert not hasattr(package, "prepare_metadata_for_build_wheel")


def test_step_definition_defaults_risk_to_low() -> None:
    definitions = load_definitions_module()

    step = definitions.StepDefinition(
        id="prepare",
        tool=definitions.ToolReference(name="prepare", version="1.0.0"),
        arguments=MappingProxyType({}),
    )

    assert step.risk == "low"


def test_from_mapping_rejects_duplicate_ids_and_dangling_dependencies() -> None:
    duplicate = base_payload()
    duplicate["steps"] = [
        duplicate["steps"][0],
        {**duplicate["steps"][1], "id": "publish"},
    ]
    assert_validation_error(duplicate, "duplicate")

    dangling = base_payload()
    dangling["steps"][0] = {
        **dangling["steps"][0],
        "depends_on": ["missing-step"],
    }
    assert_validation_error(dangling, "missing-step")


def test_from_mapping_rejects_cycles() -> None:
    payload = base_payload()
    payload["steps"] = [
        {
            **payload["steps"][0],
            "depends_on": ["prepare"],
        },
        {
            **payload["steps"][1],
            "depends_on": ["publish"],
        },
    ]

    assert_validation_error(payload, "cycle")


def test_from_mapping_rejects_unregistered_tools_and_invalid_arguments() -> None:
    missing_tool = base_payload()
    missing_tool["steps"][0] = {
        **missing_tool["steps"][0],
        "tool": {"name": "unknown", "version": "9.9.9"},
    }
    assert_validation_error(missing_tool, "unknown")

    invalid_arguments = base_payload()
    invalid_arguments["steps"][1] = {
        **invalid_arguments["steps"][1],
        "arguments": {"path": 7},
    }
    assert_validation_error(invalid_arguments, "path")


def test_from_mapping_rejects_scale_limits_and_retry_boundaries() -> None:
    too_many_steps = {
        "definition_id": "workflow.large",
        "version": 1,
        "steps": [
            {
                "id": f"step-{index:03d}",
                "tool": {"name": "prepare", "version": "1.0.0"},
                "arguments": {"path": f"/tmp/{index}"},
            }
            for index in range(101)
        ],
    }
    assert_validation_error(too_many_steps, "100")

    too_many_edges = {
        "definition_id": "workflow.dense",
        "version": 1,
        "steps": [
            {
                "id": f"step-{index:03d}",
                "tool": {"name": "prepare", "version": "1.0.0"},
                "arguments": {"path": f"/tmp/{index}"},
                "depends_on": [f"step-{dependency:03d}" for dependency in range(index)],
            }
            for index in range(46)
        ],
    }
    assert_validation_error(too_many_edges, "1000")

    too_many_attempts = base_payload()
    too_many_attempts["steps"][0] = {
        **too_many_attempts["steps"][0],
        "retry": {"max_attempts": 11},
    }
    assert_validation_error(too_many_attempts, "max_attempts")

    bool_attempts = base_payload()
    bool_attempts["steps"][0] = {
        **bool_attempts["steps"][0],
        "retry": {"max_attempts": True},
    }
    assert_validation_error(bool_attempts, "max_attempts")

    bool_backoff = base_payload()
    bool_backoff["steps"][0] = {
        **bool_backoff["steps"][0],
        "retry": {"initial_backoff_seconds": True},
    }
    assert_validation_error(bool_backoff, "initial_backoff_seconds")

    string_version = base_payload()
    string_version["version"] = "7"
    assert_validation_error(string_version, "version")


def test_from_mapping_copies_input_into_immutable_definition() -> None:
    definitions = load_definitions_module()

    payload = base_payload()
    original = copy.deepcopy(payload)

    definition = definitions.WorkflowDefinition.from_mapping(
        payload,
        StubToolCatalog(),
    )

    payload["definition_id"] = "mutated"
    payload["steps"][0]["arguments"]["metadata"]["ticket"] = "CHANGED"
    payload["steps"].append(
        {
            "id": "late-step",
            "tool": {"name": "prepare", "version": "1.0.0"},
            "arguments": {"path": "/tmp/late"},
        }
    )

    assert definition.definition_id == original["definition_id"]
    publish = next(step for step in definition.steps if step.id == "publish")
    assert publish.arguments["metadata"]["ticket"] == "CP-42"
    with pytest.raises(Exception):
        definition.steps += ()
