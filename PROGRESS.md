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

---

## [1.2] Alembic + first migration + dev seed — 2026-04-27

### Cosa è stato fatto
- `alembic init backend/migrations` poi customizzazione:
  - `alembic.ini`: `sqlalchemy.url` lasciato vuoto (settato programmaticamente da `env.py`).
  - `backend/migrations/env.py`: importa `Settings` da `app.core.config` e `Base` da `app.models.schema`. Override di `sqlalchemy.url` con `settings.database_url_sync`. Import di `pgvector.sqlalchemy` perché altrimenti il tipo `Vector` rende come `NullType` nell'autogenerate. `compare_type=True` e `compare_server_default=True` su entrambe le modalità (offline/online).
- Prima migration: `5ef3a914c6e6_initial_schema.py`. Tutte le 8 tabelle (users, agents, mandates, intents, matches, negotiations, deals, deal_messages, audit_log) + 5 indici + unique constraints (`uq_match`, `deals.idempotency_key`, `users.nullifier_hash`).
- **Patch manuali al file generato** (autogenerate non li produce):
  1. Aggiunto `import pgvector.sqlalchemy` in cima — l'autogen referenzia `pgvector.sqlalchemy.vector.VECTOR(dim=1536)` ma non genera l'import.
  2. Aggiunto `op.execute("CREATE EXTENSION IF NOT EXISTS vector")` come prima istruzione di `upgrade()`. Senza questo, `CREATE TABLE intents` fallirebbe sulla colonna `description_embedding`.
- Migration applicata (`alembic upgrade head`) su Postgres locale: estensione `vector 0.8.2` registrata, `description_embedding | vector(1536)` corretta, FK + indici allineati con lo schema.
- `scripts/seed_dev.py`:
  - 3 utenti (`alice`, `bob`, `carol`) con `nullifier_hash` deterministico SHA-256 placeholder, `attributes_proven={"adult":true,"country":"IT","valid":true}`.
  - 1 agent per utente (`status=active`, no mandate ancora — il mandate arriva in 2.4).
  - 5 intent: `alice BUY laptop` ↔ `bob SELL laptop` (electronics, prezzi che si overlappa, dovrebbe matchare); `carol SELL camera` (vintage_photo, no controparte); `alice BUY bike` (bikes, no controparte); `bob SELL monitor` (electronics, no controparte).
  - 1 Match `alice-bob-laptop` creato manualmente con `similarity_score=0.87` placeholder e `price_overlap=True`.
- Idempotenza: ID via `uuid.uuid5(SEED_NS, label)` deterministici; ogni upsert fa `Session.get(model, id)` e short-circuita. Run 2× consecutivo è no-op (verificato).
- Embedding finti: `random.Random(int(sha256(text)))` → 1536 float in [-1, 1]. Nessuna chiamata OpenAI (no API key richiesta in dev).

### Decisioni prese non esplicite nel brief
- **Migration dir a `backend/migrations/`**, `alembic.ini` a root del repo. Path `script_location = %(here)s/backend/migrations`. Pattern Alembic standard quando si vuole il config in root e i file di migration vicini al codice.
- **No vector index in 1.2** (HNSW/IVFFlat su `description_embedding`). Lo schema non lo dichiara; per V0 con ~100 utenti il sequential scan è OK. Da pianificare in una migration separata prima di 4.3 (match service). Quando aggiunto: `CREATE INDEX ON intents USING hnsw (description_embedding vector_cosine_ops)` (cosine perché brief §3 Marketplace).
- **Downgrade non droppa l'extension `vector`**: l'extension è per-database e potrebbe essere usata da altri schema. `DROP EXTENSION` in downgrade è rischioso. Lascio l'extension installata anche dopo downgrade — extension creation è idempotente all'upgrade successivo.
- **Seed namespace UUID v4 visivamente "fake"**: `00000000-0000-4000-8000-00d0e15eedde`. Distingue subito le righe seed da quelle di prod future durante debug.
- **Upsert via `Session.get()` invece di `INSERT ... ON CONFLICT DO NOTHING`**: due query per row vs una, ma più leggibile e matcha il pattern dello scaffold `mandate_verifier.py`. Per seed dev (5 intent, 3 user) la differenza di perf è 0.
- **Seed in italiano per descrizioni intent**: matcha la realtà del marketplace (V0 geo_scope = `["IT"]`). I test futuri sui match semantici troveranno descrizioni in lingua coerente.

### Test scritti / coverage
Nessun test pytest in 1.2 (test infra arriva in 1.3). Smoke manuale completo:
- `alembic current` pre-migration → contesto OK, no revision applied.
- `alembic upgrade head` → migration applicata, `alembic_version` popolata con `5ef3a914c6e6`.
- `psql \dt` → 10 relations (8 schema + alembic_version + (vector è una extension, non rel)).
- `psql \dx vector` → `vector | 0.8.2` presente.
- `psql \d intents` → `description_embedding | vector(1536)` corretto.
- `python scripts/seed_dev.py` × 2 → entrambe stampano `seed complete: 3 users, 3 agents, 5 intents, 1 matches`.
- Query DB di sanity: 3/3/5/1, e join sul match mostra `MacBook Pro 14 M3 16GB ↔ MacBook Pro 14 M3 16/512 — usato 6 mesi`.

### Blocker / dubbi
- **Vector index pianificato, non creato** (vedi decisione sopra). Decidere prima di 4.3 se HNSW o IVFFlat e con quale ops (`vector_cosine_ops` per cosine sim).
- **Alembic offline mode** non testato (uso solo online). Per generare SQL deploy in futuro va validato. Overkill per ora.
- **uuid v4 verifica**: il namespace seed `00000000-0000-4000-8000-00d0e15eedde` è formalmente un UUID v4 valido (versione bit `4`, variant `8`). Ma `uuid.uuid5(NS, label)` ignora la versione del NS e produce sempre v5. Niente problema funzionale.

### Prossima task
1.3 Test infrastructure (fixture pytest per DB di test, mock Anthropic, mock Self verifier, smoke test MandateVerifier). **Attendo via libera.**

---

## [1.3] Test infrastructure + mandate_verifier smoke tests — 2026-04-27

### Cosa è stato fatto
- **`pyproject.toml`**: aggiunto `testcontainers[postgres]>=4.0` ai dev deps. Registrato il marker `db` in `[tool.pytest.ini_options].markers`. Aggiunto `filterwarnings` per silenziare i `datetime.utcnow()` deprecation e i warning su `Query is being used as a legacy interface` — entrambi vengono dallo scaffold §5 che, per direttiva del founder, non riscriviamo.
- **`backend/tests/conftest.py`**:
  - `_pg_container` fixture session-scoped **lazy**: parte solo se un test richiede `db_session`. Booota `pgvector/pgvector:pg16`, setta `POSTGRES_*` env vars **prima** di importare `app.core.db` (engine creati con la URL del container, non con default localhost), runna `alembic upgrade head` una volta. Stop al session teardown.
  - `db_session` fixture function-scoped: `connection.begin()` esterna + `Session(bind=connection, join_transaction_mode="create_savepoint")`. Rollback al teardown → zero state leak tra test.
  - `anthropic_mock` factory fixture: `FakeAnthropicClient` con coda di `SimpleNamespace` canned, mimica `client.messages.create()` con `.content`/`.stop_reason`. Helper `text_block`, `tool_use_block`, `_make_message` esposti come attributi della factory. Pair col costruttore `AgentOrchestrator(db, anthropic_client=fake)` (l'orchestrator già accetta client iniettato — niente monkeypatch necessario).
  - `self_verifier_mock` placeholder fixture: stand-in per il futuro `app.services.identity_service` (task 2.3). Espone `set_response()`, `calls`, `fake_post()` shape `httpx.Response`. Quando 2.3 atterra, il monkeypatch verrà fatto sul callsite reale.
- **`backend/tests/test_mandate_verifier.py`**:
  - `test_mandate_verifier_happy_path`: User+Agent+Mandate creati da factory inline, mandate.scope.allowed_actions include `send_offer`, max_price_per_deal_eur=€100. `verifier.authorize(agent.id, "send_offer", {"price_cents": 5000})` → ritorna il mandate (id confronto).
  - `test_mandate_verifier_limit_exceeded`: stesso setup ma `max_price_per_deal_eur=€200`, chiamata con `price_cents=200_000` (€2000). Si aspetta `LimitExceeded`. Verifica anche che `record_usage()` NON sia stato chiamato sul rejection path: `mandate.spent_today_eur` invariato + nessuna riga `audit_log` con `success=True` per quell'`agent_id`.
  - Entrambi marcati `@pytest.mark.db`. Helper di factory inline al file (non in conftest) — solo 2 test in 1.3, generalizzazione prematura.
- **`DESIGN_QUESTIONS.md` creato a root del repo**: 7 voci che documentano le decisioni del founder fin qui. DQ-1 (legacy ↔ §7), DQ-2 (testcontainers > SQLite), DQ-3 (vector index → 4.3), DQ-4 (/ready → 7.x), DQ-5 (hard limits in services, non config), DQ-6 (no DROP EXTENSION), DQ-7 (doppio engine sync+async).

### Decisioni prese non esplicite nel brief
- **Container lazy invece di `pytest_configure`**: la prima implementazione bootava il container in `pytest_configure`, il che faceva pagare ~6s anche a `pytest -m "not db"`. Refactorato in fixture `_pg_container` session-scoped + `db_session` che la richiede. Ora `pytest -m "not db"` colleziona istantaneamente senza container. Costo: dipendenza esplicita da `_pg_container` su `db_session` (non un problema, la fixture chain pytest la risolve normalmente).
- **Helpers di factory inline a `test_mandate_verifier.py`** invece di `backend/tests/factories.py`. Per 2 test l'estrazione sarebbe overhead. Quando 2.6 (test completo MandateVerifier) o 4.x avranno bisogno di factories riusate da più moduli, le promuovo allora.
- **`filterwarnings` via pyproject** per le deprecation dello scaffold: il founder ha esplicitamente suggerito questa strada. Documentato in DQ-1.
- **Eccezione SQLite-style** documentata in conftest e DQ-2: per test puramente computazionali (es. `_extract_country`, `_extract_price_eur`), niente fixture/container, plain `def test_xxx(): ...`. Da usare quando arriverà il caso (probabilmente 2.6).
- **`session.commit()` test-side ≡ savepoint release** grazie a `join_transaction_mode="create_savepoint"`. Significa che il `MandateVerifier._reset_daily_counters_if_needed` può chiamare `db.commit()` senza compromettere l'isolation del test — l'outer transaction rolla back comunque a teardown.

### Test scritti / coverage
- `backend/tests/test_mandate_verifier.py`: 2 test, entrambi PASS.
- `pytest -v` → `2 passed in 6.05s` (di cui ~5s container boot + alembic upgrade, ~50ms i due test).
- `pytest -m "not db" --collect-only` → 0 collected, container non bootato.
- `pytest -m db --collect-only` → 2 collected.
- Coverage non misurata in 1.3 (target 80% sui service / 100% su mandate_verifier è obiettivo di 2.6, non di 1.3 che chiede solo "1 smoke test").

### Blocker / dubbi
- **`testcontainers` 4.x in CI**: localmente Docker è disponibile come daemon. In CI servirà un runner con Docker-in-Docker o socket Docker mounted. Non blocker per V0 dev locale, da affrontare quando si imposta CI in 7.x.
- **First-run lentezza**: ~5s di boot container + alembic. Su 100+ test (futuri) il costo è amortizzato. Su pochi test è percepibile ma accettabile. Se diventa fastidioso, opzioni: (1) container always-on shared con dev (Postgres su 5432), (2) setup Postgres esterno via env var override.
- **`db_session.refresh(mandate)` post-LimitExceeded**: nel verifier `_check_limits` non c'è side effect prima del raise (per il caso per-deal cap; daily/total cap leggono `mandate.spent_today_eur` ma non lo modificano). Test verifica che lo state non sia stato sporcato. OK.

### Prossima task
2.1 Tier 0 — Anonymous onboarding (email + WebAuthn passkey). **Attendo via libera.**

> **Promemoria del founder per 2.1**: la libreria Python `webauthn` ha helper per generare credentials sintetiche per i test (registrazione/verifica WebAuthn). Vale la pena guardare la docs della libreria *prima* di partire — il mocking sarà più pulito che rollare CBOR sintetici a mano.
