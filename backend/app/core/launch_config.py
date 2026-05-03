"""Launch configuration sanity checks.

These checks are intentionally static: no DB connection and no provider
network calls. They catch dangerous env drift before a deploy boots the
FastAPI lifespan or starts spending Anthropic tokens.
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlparse

from app.core.config import Settings, settings
from app.services import anthropic_pricing

Severity = Literal["error", "warning"]

_PRODUCTION_ENVS = {"prod", "production"}
_DEFAULT_JWT_SECRET = "change-me-in-dev-and-always-rotate-in-prod"
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1"}


@dataclass(frozen=True)
class LaunchConfigIssue:
    severity: Severity
    code: str
    message: str


def validate_launch_config(
    cfg: Settings = settings,
    *,
    profile: str | None = None,
    require_scheduler: bool = False,
    allow_fake_embeddings: bool = False,
) -> list[LaunchConfigIssue]:
    """Return launch config issues without exposing secret values.

    `profile` lets CI validate production rules even when the local
    `APP_ENV` remains `dev`. `allow_fake_embeddings` is only for an
    intentional Anthropic-only rehearsal; production marketplace launch
    should use OpenAI embeddings.
    """
    target_env = (profile or cfg.app_env).strip().lower()
    is_prod = target_env in _PRODUCTION_ENVS
    issues: list[LaunchConfigIssue] = []

    _check_common_ai_caps(cfg, issues, is_prod=is_prod)

    if not is_prod:
        if _is_default_secret(cfg.jwt_secret_current):
            _warn(
                issues,
                "jwt_secret_default_dev",
                "JWT_SECRET_CURRENT is still the dev default; rotate before production.",
            )
        return issues

    _check_prod_identity_and_auth(cfg, issues)
    _check_prod_provider_config(
        cfg,
        issues,
        allow_fake_embeddings=allow_fake_embeddings,
    )
    _check_prod_http_surface(cfg, issues)
    _check_prod_schedulers(
        cfg,
        issues,
        require_scheduler=require_scheduler,
    )
    return issues


def has_errors(issues: list[LaunchConfigIssue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


def issue_counts(issues: list[LaunchConfigIssue]) -> dict[str, int]:
    return {
        "errors": sum(1 for issue in issues if issue.severity == "error"),
        "warnings": sum(1 for issue in issues if issue.severity == "warning"),
    }


def _check_common_ai_caps(
    cfg: Settings,
    issues: list[LaunchConfigIssue],
    *,
    is_prod: bool,
) -> None:
    severity: Severity = "error" if is_prod else "warning"
    if cfg.anthropic_model not in anthropic_pricing.known_models():
        _add(
            issues,
            severity,
            "anthropic_model_pricing_unknown",
            (
                "ANTHROPIC_MODEL is not in the local pricing table; cost caps "
                "would fall back to conservative default pricing."
            ),
        )

    matching_backend = cfg.matching_backend.strip().lower()
    if matching_backend not in {"embedding", "anthropic"}:
        _add(
            issues,
            severity,
            "matching_backend_unknown",
            "MATCHING_BACKEND must be either embedding or anthropic.",
        )

    if cfg.max_daily_llm_cost_usd <= 0:
        _add(
            issues,
            severity,
            "max_daily_llm_cost_invalid",
            "MAX_DAILY_LLM_COST_USD must be greater than 0.",
        )
    if cfg.daily_user_cost_cap_usd <= 0:
        _add(
            issues,
            severity,
            "daily_user_cost_cap_invalid",
            "DAILY_USER_COST_CAP_USD must be greater than 0.",
        )
    if cfg.agent_tick_cost_cap_usd < 0:
        _add(
            issues,
            severity,
            "agent_tick_cost_cap_negative",
            "AGENT_TICK_COST_CAP_USD cannot be negative.",
        )
    if cfg.agent_tick_cost_cap_usd == 0:
        _add(
            issues,
            "warning",
            "agent_tick_cost_cap_disabled",
            "AGENT_TICK_COST_CAP_USD=0 disables the per-tick circuit breaker.",
        )
    if (
        cfg.agent_tick_cost_cap_usd > 0
        and cfg.daily_user_cost_cap_usd > 0
        and cfg.agent_tick_cost_cap_usd > cfg.daily_user_cost_cap_usd
    ):
        _add(
            issues,
            "warning",
            "agent_tick_cap_above_user_cap",
            (
                "AGENT_TICK_COST_CAP_USD is above DAILY_USER_COST_CAP_USD; a "
                "single tick can exhaust or exceed the user's daily allowance."
            ),
        )


def _check_prod_identity_and_auth(
    cfg: Settings, issues: list[LaunchConfigIssue]
) -> None:
    if _is_default_secret(cfg.jwt_secret_current) or len(cfg.jwt_secret_current) < 32:
        _error(
            issues,
            "jwt_secret_current_weak",
            (
                "JWT_SECRET_CURRENT must be a non-default secret with at least "
                "32 characters."
            ),
        )
    if cfg.jwt_secret_previous:
        if cfg.jwt_secret_previous == cfg.jwt_secret_current:
            _error(
                issues,
                "jwt_secret_previous_same_as_current",
                "JWT_SECRET_PREVIOUS must not equal JWT_SECRET_CURRENT.",
            )
        if _is_default_secret(cfg.jwt_secret_previous) or len(cfg.jwt_secret_previous) < 32:
            _error(
                issues,
                "jwt_secret_previous_weak",
                (
                    "JWT_SECRET_PREVIOUS is set but weak/default. Leave empty "
                    "outside a rotation window, or set the previous strong secret."
                ),
            )

    _validate_kms_master_key(cfg.kms_master_key, issues)

    if cfg.webauthn_rp_id in _LOCAL_HOSTS:
        _error(
            issues,
            "webauthn_rp_id_localhost",
            "WEBAUTHN_RP_ID must be the production relying-party domain.",
        )
    origin = urlparse(cfg.webauthn_origin)
    if origin.scheme != "https":
        _error(
            issues,
            "webauthn_origin_not_https",
            "WEBAUTHN_ORIGIN must use https in production.",
        )
    if (origin.hostname or "") in _LOCAL_HOSTS:
        _error(
            issues,
            "webauthn_origin_localhost",
            "WEBAUTHN_ORIGIN must not point at localhost in production.",
        )


def _check_prod_provider_config(
    cfg: Settings,
    issues: list[LaunchConfigIssue],
    *,
    allow_fake_embeddings: bool,
) -> None:
    if not cfg.anthropic_api_key:
        _error(
            issues,
            "anthropic_api_key_missing",
            "ANTHROPIC_API_KEY is required for platform-managed agent runtime.",
        )

    matching_backend = cfg.matching_backend.strip().lower()
    if matching_backend == "anthropic":
        return

    backend = cfg.embedding_backend.strip().lower()
    if backend == "openai" and not cfg.openai_api_key:
        _error(
            issues,
            "openai_api_key_missing",
            "OPENAI_API_KEY is required when EMBEDDING_BACKEND=openai.",
        )
    elif backend == "fake":
        if allow_fake_embeddings:
            _warn(
                issues,
                "fake_embeddings_allowed_rehearsal",
                "EMBEDDING_BACKEND=fake is allowed only for an Anthropic-only rehearsal.",
            )
        else:
            _error(
                issues,
                "fake_embeddings_in_production",
                "EMBEDDING_BACKEND=fake is not acceptable for production marketplace launch.",
            )
    elif backend != "openai":
        _error(
            issues,
            "embedding_backend_unknown",
            "EMBEDDING_BACKEND must be either openai or fake.",
        )


def _check_prod_http_surface(cfg: Settings, issues: list[LaunchConfigIssue]) -> None:
    if cfg.enable_dev_endpoints:
        _error(
            issues,
            "dev_endpoints_enabled",
            "ENABLE_DEV_ENDPOINTS must be false in production.",
        )
    if not cfg.enable_rate_limiting:
        _error(
            issues,
            "rate_limiting_disabled",
            "ENABLE_RATE_LIMITING must be true in production.",
        )
    if not cfg.cors_allowed_origins:
        _error(
            issues,
            "cors_origins_empty",
            "CORS_ALLOWED_ORIGINS must include the production frontend origin.",
        )
    for origin in cfg.cors_allowed_origins:
        parsed = urlparse(origin)
        if origin == "*" or parsed.hostname in _LOCAL_HOSTS:
            _error(
                issues,
                "cors_origin_not_production",
                "CORS_ALLOWED_ORIGINS must not include wildcard or localhost origins.",
            )
        if parsed.scheme != "https":
            _error(
                issues,
                "cors_origin_not_https",
                "CORS_ALLOWED_ORIGINS must use https origins in production.",
            )


def _check_prod_schedulers(
    cfg: Settings,
    issues: list[LaunchConfigIssue],
    *,
    require_scheduler: bool,
) -> None:
    if require_scheduler and not cfg.enable_agent_scheduler:
        _error(
            issues,
            "agent_scheduler_required_but_disabled",
            "ENABLE_AGENT_SCHEDULER must be true for autonomous production launch.",
        )
    elif not cfg.enable_agent_scheduler:
        _warn(
            issues,
            "agent_scheduler_disabled",
            (
                "ENABLE_AGENT_SCHEDULER is false. This is acceptable only for "
                "manual smoke/rehearsal, not autonomous launch."
            ),
        )


def _validate_kms_master_key(raw_b64: str, issues: list[LaunchConfigIssue]) -> None:
    if not raw_b64:
        _error(
            issues,
            "kms_master_key_missing",
            "KMS_MASTER_KEY is required. Generate with: openssl rand -base64 32",
        )
        return
    try:
        decoded = base64.b64decode(raw_b64, validate=True)
    except (binascii.Error, ValueError):
        _error(
            issues,
            "kms_master_key_invalid_base64",
            "KMS_MASTER_KEY must be valid base64.",
        )
        return
    if len(decoded) != 32:
        _error(
            issues,
            "kms_master_key_wrong_size",
            "KMS_MASTER_KEY must decode to exactly 32 bytes.",
        )


def _is_default_secret(value: str) -> bool:
    return not value or value == _DEFAULT_JWT_SECRET


def _add(
    issues: list[LaunchConfigIssue],
    severity: Severity,
    code: str,
    message: str,
) -> None:
    issues.append(LaunchConfigIssue(severity=severity, code=code, message=message))


def _error(issues: list[LaunchConfigIssue], code: str, message: str) -> None:
    _add(issues, "error", code, message)


def _warn(issues: list[LaunchConfigIssue], code: str, message: str) -> None:
    _add(issues, "warning", code, message)
