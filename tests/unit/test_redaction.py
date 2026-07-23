from __future__ import annotations

import importlib
import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def load_ports_module():
    try:
        return importlib.import_module("changepilot.workflow.ports.tools")
    except ModuleNotFoundError as exc:
        pytest.fail(f"missing ports module: {exc.name}")


def load_tooling_module():
    try:
        return importlib.import_module("changepilot.workflow.application.tooling")
    except ModuleNotFoundError as exc:
        pytest.fail(f"missing tooling module: {exc.name}")


def make_secret_ref(raw_name: str):
    ports = load_ports_module()
    return ports.SecretRef(provider="env", name=raw_name)


class Credentials(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    username: str
    token: object


class DeployPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    credentials: Credentials
    metadata: dict[str, object]
    attempts: tuple[object, ...]


def test_secret_ref_never_leaks_through_repr_or_string_conversion() -> None:
    secret = make_secret_ref("PROD_DEPLOY_TOKEN")

    assert "PROD_DEPLOY_TOKEN" not in repr(secret)
    assert "PROD_DEPLOY_TOKEN" not in str(secret)


def test_redact_handles_nested_structures_and_declared_paths_without_mutation() -> None:
    tooling = load_tooling_module()

    payload = {
        "credentials": {
            "username": "deploy-bot",
            "token": "raw-token-value",
            "nested": {"api_key": make_secret_ref("ENV_API_KEY")},
        },
        "metadata": {
            "owner": "platform",
            "secret": "top-secret-note",
        },
        "attempts": (
            {"status": "ok"},
            make_secret_ref("ENV_SHADOW_TOKEN"),
            ["keep", {"password": "raw-password"}],
        ),
    }
    original = deepcopy(payload)

    redacted = tooling.redact(
        payload,
        sensitive_paths=(
            "credentials.token",
            "metadata.secret",
            "attempts.2.1.password",
        ),
    )

    assert payload == original
    assert redacted["credentials"]["username"] == "deploy-bot"
    assert redacted["credentials"]["token"] == tooling.REDACTED
    assert redacted["credentials"]["nested"]["api_key"] == tooling.REDACTED
    assert redacted["metadata"]["secret"] == tooling.REDACTED
    assert redacted["attempts"][1] == tooling.REDACTED
    assert redacted["attempts"][2][1]["password"] == tooling.REDACTED


def test_redact_does_not_serialize_raw_exception_args_by_default() -> None:
    tooling = load_tooling_module()

    serialized = json.dumps(
        tooling.redact(RuntimeError("AWS_SECRET_ACCESS_KEY", "super-secret-value")),
        sort_keys=True,
        default=str,
    )

    assert "AWS_SECRET_ACCESS_KEY" not in serialized
    assert "super-secret-value" not in serialized
    assert tooling.REDACTED in serialized


def test_redact_supports_structured_exception_arg_paths() -> None:
    tooling = load_tooling_module()

    redacted = tooling.redact(
        {
            "error": RuntimeError(
                "deploy failed",
                {"safe": "visible-before-redaction", "secret": "super-secret-value"},
            )
        },
        sensitive_paths=("error.args.1.safe",),
    )

    assert redacted["error"]["type"] == "RuntimeError"
    assert redacted["error"]["args"][0] == tooling.REDACTED
    assert redacted["error"]["args"][1]["safe"] == tooling.REDACTED
    assert redacted["error"]["args"][1]["secret"] == tooling.REDACTED


def test_redact_fails_closed_for_secret_refs_inside_models_and_exceptions() -> None:
    tooling = load_tooling_module()

    model = DeployPayload(
        credentials=Credentials(
            username="deploy-bot",
            token=make_secret_ref("ENV_DEPLOY_KEY"),
        ),
        metadata={"owner": "platform", "secret": "should-hide"},
        attempts=(RuntimeError("tool failed", make_secret_ref("ENV_CRASH_KEY")),),
    )

    redacted = tooling.redact(
        model,
        sensitive_paths=("metadata.secret",),
    )
    serialized = json.dumps(redacted, sort_keys=True, default=str)

    assert "ENV_DEPLOY_KEY" not in serialized
    assert "ENV_CRASH_KEY" not in serialized
    assert "should-hide" not in serialized
    assert tooling.REDACTED in serialized
