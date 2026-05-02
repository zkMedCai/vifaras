"""Custom Prometheus metrics for Vifaras backend (FASE 7.2).

Naming convention: ``vifaras_<domain>_<entity>_<action>_<unit>``

- Prefix ``vifaras_`` prevents collision with auto-instrumented metrics
  (HTTP server / SQLAlchemy / httpx) and with default Prometheus metrics.
- Suffix follows Prometheus standards: ``_total`` (counter),
  ``_seconds`` (histogram), ``_timestamp`` (gauge).

V0 instrumentation domains:
  - **auth**: signup / login completions
  - **security**: rate-limit hits, moderation rejections
  - **business**: intents created, matches discovered, deals signed/cancelled
  - **agent**: per-tick duration, Anthropic API call outcome
  - **scheduler**: tick total, last successful tick timestamp

V0.5+ expansion (deferred): per-tier metrics, Anthropic API cost per
user, database query duration, Self verifier latency.
"""
from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

SIGNUP_COMPLETED_TOTAL = Counter(
    "vifaras_signup_completed_total",
    "Total successful WebAuthn signup completions.",
)

LOGIN_COMPLETED_TOTAL = Counter(
    "vifaras_login_completed_total",
    "Total successful WebAuthn login completions.",
)

# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

RATE_LIMIT_HITS_TOTAL = Counter(
    "vifaras_rate_limit_hits_total",
    "Total rate-limit hits (HTTP 429 responses).",
    ["endpoint"],
)

MODERATION_REJECTIONS_TOTAL = Counter(
    "vifaras_moderation_rejections_total",
    "Total content-moderation rejections (HTTP 422 responses).",
    ["field", "code"],
)

# ---------------------------------------------------------------------------
# Business
# ---------------------------------------------------------------------------

INTENTS_CREATED_TOTAL = Counter(
    "vifaras_intents_created_total",
    "Total intents created.",
    ["category", "side"],
)

MATCHES_DISCOVERED_TOTAL = Counter(
    "vifaras_matches_discovered_total",
    "Total net-new matches discovered (excludes score-only updates).",
)

DEALS_SIGNED_TOTAL = Counter(
    "vifaras_deals_signed_total",
    "Total deals dual-signed (both buyer and seller).",
)

DEALS_CANCELED_TOTAL = Counter(
    "vifaras_deals_canceled_total",
    "Total deals cancelled by user (post-deal cancellation window).",
)

# ---------------------------------------------------------------------------
# Agent runtime
# ---------------------------------------------------------------------------

AGENT_TICK_DURATION_SECONDS = Histogram(
    "vifaras_agent_tick_duration_seconds",
    "Agent tick wall-clock execution time.",
    buckets=[0.1, 0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0],
)

AGENT_API_CALLS_TOTAL = Counter(
    "vifaras_agent_api_calls_total",
    "Total Anthropic API calls made by the agent orchestrator.",
    ["status"],
)

# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

SCHEDULER_TICK_TOTAL = Counter(
    "vifaras_scheduler_tick_total",
    "Total scheduler discovery cycles.",
    ["status"],
)

SCHEDULER_LAST_TICK_TIMESTAMP = Gauge(
    "vifaras_scheduler_last_tick_timestamp",
    "Unix timestamp of the last successful scheduler discovery cycle.",
)

# ---------------------------------------------------------------------------
# Cost monitoring (7.3.4)
#
# Caveat label cardinality: `user_id` as a label is high-cardinality
# (one entry per user × one per model). Acceptable at V0 alpha (<100
# users) but problematic > 10K users — see IDEAS_BACKLOG entry
# "Prometheus user_id label cardinality (V0.5+ pre-launch)".
#
# Caveat gauge in-memory: COST_USER_DAILY_USD is process-local and
# does NOT persist across restart. Source of truth remains the
# `daily_cost_tracking` table; the gauge is observability only. On
# restart the gauge resets to 0 until the next upsert refreshes it.
# ---------------------------------------------------------------------------

COST_USD_TOTAL = Counter(
    "vifaras_cost_usd_total",
    "Total Anthropic API cost in USD, per user × model. Cumulative since "
    "process start (Prometheus counter semantics).",
    ["user_id", "model"],
)

COST_USER_DAILY_USD = Gauge(
    "vifaras_cost_user_daily_usd",
    "Per-user cost USD for the current UTC day. Resets at midnight UTC "
    "(implicit: the daily_cost_tracking row keys on UTC date).",
    ["user_id"],
)

USER_COST_CAP_HITS_TOTAL = Counter(
    "vifaras_user_cost_cap_hits_total",
    "Total times a user tick was skipped due to the daily soft cap.",
)
