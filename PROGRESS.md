# PROGRESS — Marketplace V0

Note per ogni task completata. Riferimento: `PROJECT_BRIEF.md`.

---

## [1.1] Project skeleton + healthcheck — 2026-04-27

### Cosa è stato fatto
- `pyproject.toml` PEP 621 con tutte le dipendenze del brief §1.1 + extras `dev` (pytest, pytest-asyncio, pytest-cov, ruff). Build backend hatchling, `packages = ["backend/app"]` → l'app è installata come pacchetto top-level nel venv.
- Driver DB doppio: `asyncpg` per i nuovi service async, `psycopg[binary]` per la `Session` sync usata da `MandateVerifier` (scaffold §5) e da Alembic in 1.2.
- `requires-python = ">=3.11,<3.13"` come da nota del founder (no 3.13).
- `.env.example` documentato per gruppo (App / Postgres / JWT / WebAuthn / Anthropic / OpenAI / Self).
- `docker-compose.yml` con `pgvector/pgvector:pg16`, volume `marketplace_pgdata`, healthcheck `pg_isready`, porta 5432; niente campo `version:` (deprecato in compose v2).
- `backend/app/core/config.py` — `Settings` Pydantic v2, `cached_property` per `database_url_async` / `database_url_sync` derivati dai param Postgres.
- `backend/app/core/db.py` — engine async + sync sullo stesso DB; factory `AsyncSessionLocal`, `SyncSessionLocal`; dependency `get_db()` async + `get_sync_db()` sync.
- `backend/app/core/logging.py` — structlog → JSON su stdout, ISO UTC, `EventRenamer("msg")`, livello da `LOG_LEVEL`.
- `backend/app/main.py` — FastAPI con `lifespan` che configura logging + chiude l'engine in shutdown; `GET /health` esegue `SELECT 1` via async engine, ritorna `status: ok|degraded` e `db: ok|down`.
- `__init__.py` vuoti in `app`, `core`, `models`, `services`, `agents`, `api`, `tests` per chiarezza di import e supporto build hatch.

### Decisioni prese non esplicite nel brief
- **Doppio engine async + sync** sullo stesso DB. Motivo: `mandate_verifier.py` (§5) usa `sqlalchemy.orm.Session`. Il nuovo codice userà l'async; i test verifier (1.3) e Alembic (1.2) useranno il sync. Niente da rifattorizzare nello scaffold.
- **Build via hatchling con `packages = ["backend/app"]`**: `from app.core.config import settings` funziona da qualsiasi CWD nel venv senza tweak di `PYTHONPATH`. Pytest configurato con `pythonpath = ["backend"]` come fallback per la discovery dei test fuori wheel.
- **`/health` ritorna sempre 200**, anche con DB down (campo `status: degraded`). Per readiness probe stretto si farà `/health/ready` separato in 7.x (osservabilità). Brief 1.1 chiede solo "healthcheck", interpretato come liveness.
- **Autori `pyproject.toml`**: nome derivato dall'email `teodorodomenico96@gmail.com` → "Teodoro Domenico". Idem per le commit signature, inline via `-c user.name=...` (nessuna modifica al `~/.gitconfig` globale). Se vuoi un nome diverso lo cambio dal commit successivo.
- **Hard limits piattaforma del brief §3 NON ancora in `config.py`**: saranno aggiunti in 2.4 (mandate signing) dove vengono enforced. Per ora niente config morta.

### Test scritti / coverage
Nessun test pytest in 1.1 (l'infra test arriva in 1.3 per brief). Smoke test manuale:
- `uv run python -c "from app.main import app"` → import puliti, route `/health` registrata.
- `docker-compose up -d` → `pgvector/pgvector:pg16` healthy in ~6s.
- `uvicorn app.main:app` → boot ok, log JSON: `{"env": "dev", "name": "marketplace", "level": "info", "timestamp": "2026-04-27T...", "msg": "app.startup"}`.
- `curl http://127.0.0.1:8765/health` → `200 {"status":"ok","service":"marketplace","env":"dev","db":"ok"}`.

### Blocker / dubbi
- **`docker compose` v2 plugin non installato**, solo `docker-compose` (v1.29.2). YAML già senza `version:` (sintassi v2-compatibile) e v1 lo legge senza problemi. Se vogliamo compose v2 nativo, install plugin in 7.x.
- **Scaffold `schema.py` usa `datetime.utcnow()` (deprecato 3.12+)**, `declarative_base()` (legacy 2.0-compat) e `.query()` legacy. Brief §7 dice "no legacy query()". Per istruzioni del founder non lo riscrivo: tema da decidere prima di FASE 4 quando i nuovi service useranno comunque async + `select()`. Lascio nota in `DESIGN_QUESTIONS.md` alla prima creazione (1.3 o 2.x).
- **Postgres lasciato up** dopo lo smoke. Per fermarlo: `docker-compose down`. Per resettare DB (perde dati): `docker-compose down -v`.

### Prossima task
1.2 Migrazioni database (Alembic init + prima migration da `models/schema.py` + `scripts/seed_dev.py`). **Attendo via libera.**
