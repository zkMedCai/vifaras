"""Launch config sanity checker tests."""
from __future__ import annotations

import base64
import secrets
from types import SimpleNamespace

from app.core.launch_config import has_errors, validate_launch_config


def _fresh_key_b64() -> str:
    return base64.b64encode(secrets.token_bytes(32)).decode("ascii")


def _cfg(**overrides):
    data = {
        "app_env": "production",
        "jwt_secret_current": "current-secret-padded-to-at-least-32-bytes",
        "jwt_secret_previous": "",
        "kms_master_key": _fresh_key_b64(),
        "webauthn_rp_id": "vifaras.com",
        "webauthn_origin": "https://vifaras.com",
        "anthropic_api_key": "sk-ant-test",
        "anthropic_model": "claude-sonnet-4-5",
        "matching_backend": "embedding",
        "openai_api_key": "sk-openai-test",
        "embedding_backend": "openai",
        "enable_dev_endpoints": False,
        "enable_rate_limiting": True,
        "cors_allowed_origins": ["https://vifaras.com"],
        "enable_agent_scheduler": True,
        "max_daily_llm_cost_usd": 50.0,
        "daily_user_cost_cap_usd": 0.50,
        "agent_tick_cost_cap_usd": 0.10,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def _codes(issues):
    return {issue.code for issue in issues}


def test_production_hardened_config_has_no_errors() -> None:
    issues = validate_launch_config(_cfg(), profile="production", require_scheduler=True)

    assert issues == []


def test_production_rejects_default_jwt_and_missing_kms() -> None:
    issues = validate_launch_config(
        _cfg(
            jwt_secret_current="change-me-in-dev-and-always-rotate-in-prod",
            kms_master_key="",
        ),
        profile="production",
    )

    assert has_errors(issues)
    assert {"jwt_secret_current_weak", "kms_master_key_missing"}.issubset(
        _codes(issues)
    )


def test_production_rejects_missing_anthropic_key_and_unknown_model() -> None:
    issues = validate_launch_config(
        _cfg(anthropic_api_key="", anthropic_model="claude-future"),
        profile="production",
    )

    assert has_errors(issues)
    assert {"anthropic_api_key_missing", "anthropic_model_pricing_unknown"}.issubset(
        _codes(issues)
    )


def test_production_rejects_openai_backend_without_key() -> None:
    issues = validate_launch_config(
        _cfg(embedding_backend="openai", openai_api_key=""),
        profile="production",
    )

    assert has_errors(issues)
    assert "openai_api_key_missing" in _codes(issues)


def test_anthropic_matching_does_not_require_openai_key() -> None:
    issues = validate_launch_config(
        _cfg(
            matching_backend="anthropic",
            embedding_backend="openai",
            openai_api_key="",
        ),
        profile="production",
    )

    assert not has_errors(issues)
    assert "openai_api_key_missing" not in _codes(issues)


def test_production_rejects_unknown_matching_backend() -> None:
    issues = validate_launch_config(
        _cfg(matching_backend="bananas"),
        profile="production",
    )

    assert has_errors(issues)
    assert "matching_backend_unknown" in _codes(issues)


def test_fake_embeddings_can_be_allowed_for_anthropic_only_rehearsal() -> None:
    issues = validate_launch_config(
        _cfg(embedding_backend="fake", openai_api_key=""),
        profile="production",
        allow_fake_embeddings=True,
    )

    assert not has_errors(issues)
    assert "fake_embeddings_allowed_rehearsal" in _codes(issues)


def test_production_rejects_localhost_http_surface() -> None:
    issues = validate_launch_config(
        _cfg(
            webauthn_rp_id="localhost",
            webauthn_origin="http://localhost:3000",
            cors_allowed_origins=["http://localhost:3000"],
            enable_rate_limiting=False,
            enable_dev_endpoints=True,
        ),
        profile="production",
    )

    assert has_errors(issues)
    assert {
        "webauthn_rp_id_localhost",
        "webauthn_origin_not_https",
        "webauthn_origin_localhost",
        "cors_origin_not_production",
        "cors_origin_not_https",
        "rate_limiting_disabled",
        "dev_endpoints_enabled",
    }.issubset(_codes(issues))


def test_require_scheduler_turns_disabled_scheduler_into_error() -> None:
    issues = validate_launch_config(
        _cfg(enable_agent_scheduler=False),
        profile="production",
        require_scheduler=True,
    )

    assert has_errors(issues)
    assert "agent_scheduler_required_but_disabled" in _codes(issues)
