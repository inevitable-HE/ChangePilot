from __future__ import annotations

from changepilot.operations.application.service import _redact


def test_nested_sensitive_values_are_redacted_before_serialization() -> None:
    payload = {
        "authorization": "Bearer private",
        "nested": {
            "api_key": "deepseek-secret",
            "safe": "visible",
        },
        "items": [{"password": "private"}, {"token": "private"}],
    }

    redacted = _redact(payload)

    assert redacted["authorization"] == "***REDACTED***"
    assert redacted["nested"]["api_key"] == "***REDACTED***"
    assert redacted["nested"]["safe"] == "visible"
    assert redacted["items"][0]["password"] == "***REDACTED***"
    assert redacted["items"][1]["token"] == "***REDACTED***"
