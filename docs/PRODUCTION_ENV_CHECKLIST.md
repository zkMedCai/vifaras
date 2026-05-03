# Production Env Checklist

FASE 10.2 locked V0 to platform-managed AI. Vifaras uses official API
accounts; users do not connect Claude Pro/Max or ChatGPT Plus/Pro.

This checklist is static first, then smoke tests. None of these commands
should print secret values.

## Static Config Check

Run before starting a production-like backend:

```bash
uv run python scripts/check_launch_config.py --profile production --require-scheduler
```

For an Anthropic-only rehearsal with fake embeddings:

```bash
uv run python scripts/check_launch_config.py --profile production --allow-fake-embeddings
```

`--allow-fake-embeddings` is not a marketplace production launch posture.
It is only for validating agent runtime before OpenAI embeddings are funded.

## Required Secrets

- `APP_ENV=production`
- `JWT_SECRET_CURRENT`: strong non-default value, at least 32 chars
- `JWT_SECRET_PREVIOUS`: empty unless in a rotation window
- `KMS_MASTER_KEY`: `openssl rand -base64 32`
- `ANTHROPIC_API_KEY`: Vifaras-owned Anthropic API key
- `ANTHROPIC_MODEL=claude-sonnet-4-5`
- `OPENAI_API_KEY`: required when `EMBEDDING_BACKEND=openai`

Never use consumer subscription credentials or browser sessions as provider
secrets.

## Public Surface

- `WEBAUTHN_RP_ID` must be the production domain, not localhost.
- `WEBAUTHN_ORIGIN` must be the HTTPS production frontend origin.
- `CORS_ALLOWED_ORIGINS` must include only HTTPS production origins.
- `ENABLE_RATE_LIMITING=true`.
- `ENABLE_DEV_ENDPOINTS=false`.

## AI Cost Guardrails

- `MAX_DAILY_LLM_COST_USD`: global daily hard cap.
- `DAILY_USER_COST_CAP_USD`: per-user daily soft cap.
- `AGENT_TICK_COST_CAP_USD`: per-tick circuit breaker.
- `ENABLE_AGENT_SCHEDULER=true` for autonomous launch.

Recommended V0 bootstrap:

```env
MAX_DAILY_LLM_COST_USD=50.0
DAILY_USER_COST_CAP_USD=0.50
AGENT_TICK_COST_CAP_USD=0.10
```

## Founder Diagnostics

For local/staging checks only, temporarily enable dev endpoints and inspect
the AI operations snapshot:

```bash
curl -sS http://127.0.0.1:8000/api/_dev/ai/status
```

The payload reports provider configured booleans, model names, scheduler
settings and daily cost caps. It must not print secret values.

Keep `ENABLE_DEV_ENDPOINTS=false` on any public production surface.

## Smoke Tests

After static config passes:

```bash
uv run python scripts/smoke_anthropic.py
uv run python scripts/smoke_agent_runtime.py --timeout-seconds 45
curl -sS https://<backend-host>/api/health/ready
```

Expected autonomous readiness:

```json
{"status":"ready","checks":{"database":"healthy","scheduler":"healthy"}}
```

If scheduler is intentionally disabled for a rehearsal, readiness may report
`"scheduler":"disabled"`; do not treat that as autonomous production launch.
