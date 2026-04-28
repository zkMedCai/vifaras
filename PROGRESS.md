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

---

## [2.1] tier 0 anonymous onboarding (email + passkey) — 2026-04-27

### Cosa è stato fatto
- **Migration `e25338f5705c_add_tier_and_relax_nullifier.py`**: 2 alter come da brief del founder.
  - `ADD users.tier INTEGER NOT NULL DEFAULT 0` (`server_default=text("0")` → backfill automatico delle 3 righe seed esistenti a tier=0).
  - `ALTER users.nullifier_hash DROP NOT NULL`.
  - Niente partial-unique index: l'index esistente `ix_users_nullifier_hash` è un Postgres unique INDEX di colonna, NULLs sono trattati come distinti per default → multipli NULL OK senza modifiche. Verificato sul migration originale 5ef3a914c6e6.
  - Docstring esplicito che ricapitola brief §2.5, semantica del backfill, e perché non si tocca l'unique index.
- **`schema.py`** edit minimale: aggiunto `User.tier`, `nullifier_hash` ora `nullable=True`. Tutti gli altri campi invariati. Docstring del modello aggiornato per ricapitolare i tre tier.
- **`pyproject.toml`**: aggiunto `pyjwt>=2.8`. Aggiunto `asyncio_default_fixture_loop_scope = "session"` + `asyncio_default_test_loop_scope = "session"` per evitare cross-loop pool errors sull'async engine (vedi DQ-10).
- **`app/core/security.py`**: tre famiglie di JWT con `kind` claim distinto e TTL diversi:
  - `create_access_token(user_id, tier)` — 15m, payload `{sub, tier, kind="access", iat, exp}`.
  - `create_refresh_token(user_id)` — 30d, payload `{sub, kind="refresh", jti, iat, exp}`.
  - `create_challenge_token(challenge, user_id, email, purpose)` — 5m, payload `{challenge_b64, user_id, email, purpose, kind="challenge", iat, exp}`. Stateless seam tra `/begin` e `/complete` — niente Redis, niente race conditions.
  - `decode_*_token` controlla il `kind` e (per il challenge) il `purpose`.
- **`app/services/auth_service.py`** — async pure, `select()`, niente `query()`. Quattro funzioni:
  - `begin_registration(db, email)`: check email duplicata via `select(User).where(notification_email==email)`, genera `user_id` (uuid4), chiama `webauthn.generate_registration_options(user_verification=PREFERRED)`, ritorna `(options_json_dict, challenge_token)`.
  - `complete_registration(db, credential, challenge_token)`: decodifica challenge token, chiama `verify_registration_response(require_user_verification=False)`, persiste `User(tier=0, nullifier_hash=None, attributes_*=placeholders DQ-8, passkey_credential_id=b64url(verified.credential_id), passkey_pubkey=b64url(verified.credential_public_key), passkey_sign_count=verified.sign_count)`, ritorna `(user_id, access, refresh)`.
  - `begin_login(db, email)`: trova user, genera auth options con `allow_credentials=[user.passkey_credential_id]`, `user_verification=PREFERRED`, ritorna `(options, challenge_token)`.
  - `complete_login(db, credential, challenge_token)`: decodifica token, trova user, `verify_authentication_response(credential_public_key=..., credential_current_sign_count=...)`, aggiorna `passkey_sign_count = verified.new_sign_count` e `last_active_at`, ritorna nuovi access+refresh.
  - Errori: `EmailAlreadyRegistered` (409), `UserNotFound` (404), `InvalidCredential` (401), `InvalidChallengeToken` (401). Tutti subclass di `AuthError`.
- **`app/api/auth.py`** — `APIRouter(prefix="/api/auth")` con 4 endpoint POST. Pydantic v2 schemas (`RegisterBeginRequest`, `RegisterCompleteRequest`, `LoginBeginRequest`, `LoginCompleteRequest`, `BeginResponse`, `TokenResponse`). Mapper `_to_http(AuthError) → HTTPException(http_status, {code, message})`. `Depends(get_db)` → AsyncSession. Wired in `main.py` via `app.include_router(auth_routes.router)`.
- **`backend/tests/conftest.py`** — aggiunte 2 fixture:
  - `async_db_session`: AsyncSession con `engine.connect()` + `transaction.begin()` + `join_transaction_mode="create_savepoint"`. Rollback al teardown.
  - `http_client`: `httpx.AsyncClient(transport=ASGITransport(app=app))`. Override `app.dependency_overrides[get_db]` per yieldare la stessa `async_db_session` → reads dopo l'API call vedono ciò che l'API ha scritto, e il rollback teardown li wipa via.
- **`backend/tests/test_auth.py`** — 2 test:
  - `test_register_tier_0_returns_jwt_and_persists_anonymous_user`: monkeypatch `verify_registration_response` → fake `VerifiedRegistration`. POST `/begin` → POST `/complete`. Assert: User row con `tier==0`, **`nullifier_hash IS None`** (assertion critica del founder), `attributes_proven=={}`. JWT access decoda con `sub=user_id, tier=0, kind=access`. Refresh decoda con `kind=refresh, jti` non vuoto.
  - `test_register_rejects_duplicate_email`: registra bob, poi prova un secondo `/begin` con stessa email → 409 con `detail.code == email_already_registered`.

### Decisioni prese non esplicite nel brief
- **DQ-8 — Placeholder per `attributes_*` a tier=0** invece di estendere la migration a 5 alter. Founder aveva listato 2 alter; ho mantenuto quella scope. `_tier_0_attribute_placeholders(now)` è l'unico posto dove i placeholder sono generati — pivot facile a migration estesa se in futuro si decide diversamente.
- **DQ-9 — Email uniqueness app-level**, non DB-level. Check ridondante in `begin_registration` + `complete_registration`. Per V0 accettabile; partial-unique index suggerito in 2.2 o 7.x.
- **DQ-10 — Loop scope session-wide nei test**. Cross-test pool stale connections risolte settando `asyncio_default_fixture_loop_scope=session` + `asyncio_default_test_loop_scope=session`. Niente parallelism a livello di loop (problematico se in futuro pytest-xdist).
- **WebAuthn**: `user_verification='preferred'` (default lib + raccomandazione founder), `require_user_verification=False` sul verify (matcha "preferred", non "required"). Permette Touch ID consumer senza forzare biometric.
- **Challenge stateless via JWT** invece di Redis/in-memory. Pattern: il challenge token è un JWT firmato con `kind=challenge, purpose=register|login` che il client rimanda al `/complete`. 5min TTL. Niente race condition, niente cleanup, multi-worker safe per quando arriveremo a CI/prod.
- **`user_id` generato a `/register/begin`** (uuid4) e propagato via challenge token. Quando `/complete` crea il User, l'`id` è già noto e usato per il `sub` del JWT. La WebAuthn library accetta `user_id: bytes` come user handle — usiamo l'UUID stringa encoded in UTF-8.
- **`b64url` encoding senza padding** per `passkey_credential_id` e `passkey_pubkey` storati come Text. Decodifica con padding restore per `verify_authentication_response(credential_public_key=...)`.
- **EmailStr non usato**: Pydantic v2 `EmailStr` richiede `email-validator` extra. Per V0 uso `str` e validazione semantica delegata al WebAuthn / DB. Se in 2.x si vuole strict validation, basta `pip install email-validator` + flip a `EmailStr`.
- **Test approach**: monkeypatch `verify_registration_response` al boundary invece di costruire un fake authenticator (CBOR/COSE). py-webauthn 2.7.1 non espone helper sintetici e ricostruirne uno è scope creep. Documentato in test docstring.

### Test scritti / coverage
- 4 test totali nel test suite ora: 2 mandate_verifier + 2 auth.
- `pytest -v` → `4 passed in 6.00s` (~5s container+migration setup, ~1s i 4 test).
- Coverage non misurata (target 80% in 2.6+; 2.1 chiedeva solo "test happy path + nullifier IS NULL").

### Blocker / dubbi
- **WebAuthn login flow non testato** (solo registration). 2.1 chiedeva esplicitamente "Test happy path + nullifier IS NULL" — login completo richiederebbe un fake `verify_authentication_response` su una sessione registrata. Lo aggiungo se vuoi, oppure quando 2.6 farà coverage completa MandateVerifier che rifletteremo anche su auth.
- **JWT secret di default in `.env.example`** = "change-me-in-dev-and-always-rotate-in-prod". Per V0 dev OK. Per prod va rotato — promemoria pre-launch (7.4).
- **Refresh token JTI non persistito**: V0 non ha revocation list. Refresh token rubato resta valido fino a `exp` (30d). Per V0 acceptabile; in V1 si aggiunge una `refresh_tokens` table o una blocklist per JTI revocati.

### Prossima task
2.2 Tier-based gating middleware (`require_tier(min_tier)` dependency, 402 Tier Upgrade Required, test su endpoint dummy con tutti e tre i tier). **Attendo via libera.**

---

## [2.2] tier-based gating + login test + auth fixtures — 2026-04-27

### Cosa è stato fatto

**Pre-cleanup richiesto in review** (pulizia prima del lavoro 2.2):
- `_tier_0_attribute_placeholders` docstring estesa: spiega esplicitamente che i valori sono **sentinel, non significato** (DQ-8 aggiornata di conseguenza). Bias futuro: chi legge `attributes_proven={}` su utente tier=0 capisce subito che è "nessuna proof" e non "user provò set vuoto".
- `_normalize_email(email) = email.strip().lower()` aggiunto come prima istruzione di `begin_registration` e `begin_login` di `auth_service`. `User@gmail.com` e `user@gmail.com` collassano alla stessa identità prima del lookup.
- `email-validator>=2.1` aggiunto a deps. `RegisterBeginRequest.email` e `LoginBeginRequest.email` ora `EmailStr` (Pydantic v2). Validazione RFC 5322 al boundary, 422 automatico su input malformato.
  - Side-effect: `email-validator` rifiuta TLD reserved `.test/.invalid/.local` per default. Test e seed migrati da `@example.test` → `@example.com`. Documentato.
- DQ-8 e DQ-9 di `DESIGN_QUESTIONS.md` aggiornate (sentinel docstring esplicitato, normalizzazione email + threshold 7.4 in DQ-9).
- Brief §7.4 esteso con 4 item nuovi:
  - Email uniqueness DB-level (partial unique index su `lower(notification_email)`), trigger ~1k+ utenti.
  - Refresh token revocation list (DB table o Redis blocklist), trigger ~500 utenti.
  - JWT_SECRET rotation strategy (rolling key + `kid` claim), 2-3h di lavoro.
  - Verify lowercase email normalization è applicata ovunque (defensive grep).

**Implementazione 2.2:**
- `backend/app/core/security.py` — aggiunto `CurrentUser` dataclass (frozen, solo `user_id` + `tier` — niente DB hit), e `require_tier(min_tier: int)` factory. Comportamento:
  - Authorization header missing → 401 `missing_token`.
  - Header malformato → 401 `invalid_authorization_header`.
  - Token expired → 401 `token_expired` (ramo `jwt.ExpiredSignatureError`).
  - Token invalido (kind mismatch, signature non valida, claims missing) → 401 `invalid_token`.
  - Tier insufficiente → **402** `tier_upgrade_required` con `{required_tier, current_tier, next_step}`.
  - `_NEXT_STEP_BY_TIER` static map: 0→`/api/identity/verify-self` (con copy mobile italiana brief §2.5), 1→`/api/mandates/draft`. Niente entry per tier=2 (è il tetto).
  - 401 vs 402 sono failure class diverse: invalid/expired ≠ insufficient (esplicito da founder).
- `backend/app/api/_test_endpoints.py` — `APIRouter(prefix="/api/_test")` con 3 GET (`/tier0`, `/tier1`, `/tier2`) ognuno guardato da `Depends(require_tier(N))`. Registrazione condizionale a `settings.app_env == "dev"` — in prod il router è vuoto e gli endpoint non esistono. Wired in `main.py`.
- `backend/tests/conftest.py` — aggiunta fixture `authenticated_client` (factory pattern). Mint diretto di JWT via `create_access_token(user_id, tier)`, set su `http_client.headers["Authorization"]`. Niente registrazione/DB user — il gating decoda solo il JWT, non legge il DB. Cleanup: header rimosso al teardown della fixture.
- `backend/tests/test_auth.py` — 2 test aggiunti:
  - `test_login_flow_returns_valid_jwt` (recovery del lavoro non testato di 2.1): register → login → assert JWT con `kind=access, tier=0, sub=user_id_registered`.
  - `test_email_normalization_lowercase_strip`: registra "  Dario@Example.COM" → user salvato come "dario@example.com" → secondo register con "dario@example.com" → 409 (gli equivalenti collassano).
- `backend/tests/test_tier_gating.py` (nuovo) — 10 test:
  - tier=0 user passa `/tier0` (200), bloccato su `/tier1` e `/tier2` (402 con next_step `/api/identity/verify-self`).
  - tier=1 user passa `/tier0` e `/tier1` (200), bloccato su `/tier2` (402 con next_step `/api/mandates/draft`).
  - tier=2 user passa tutti e tre (200).
  - 4 failure modes su token: missing → 401 `missing_token`; malformed header → 401 `invalid_authorization_header`; garbage JWT → 401 `invalid_token`; **refresh token usato come access → 401 `invalid_token`** (verifica esplicita del kind discriminator).

### Decisioni prese non esplicite nel brief / aggiornamenti
- **EmailStr rifiuta `.test/.invalid/.local`**: scoperto runtime, non spec esplicita. Migrazione test+seed a `@example.com` (RFC 2606 reserved-for-docs ma valid syntax). Trade-off accettato: il dev seed ora usa email "real-looking" che però non hanno destinazione SMTP reale. Non blocker — V0 non manda email reali.
- **`authenticated_client(tier=N)` mint diretto via JWT** invece di register-via-API + DB tier-patch. Il gating decoda solo il JWT (no DB read), quindi si può saltare l'overhead di registrazione. Test gating sono ~10ms invece di ~200ms ciascuno. Per test che richiedono User row reale (login coverage) si fa register inline.
- **Cross-kind reuse test (`test_refresh_token_used_as_access_returns_401`)**: aggiunto come verifica esplicita che il `kind` claim discriminator funzioni. Founder ha ribadito nella review che è un guard importante — meglio un test specifico che fallisce subito se qualcuno per errore svedasse il claim.
- **`/api/_test/tier{0,1,2}` esiste solo in env=dev**: registrazione condizionale al module level. In test (`app_env="dev"` di default) le route ci sono. In prod (env=prod) le route non vengono registrate, niente discovery via OpenAPI. Pattern leggero, niente flag complicato.

### Test scritti / coverage
- 16 test totali ora: 4 auth (register+login+normalization+duplicate), 2 mandate_verifier, 10 tier gating.
- `pytest -v` → `16 passed in 6.11s` (~5s container+migration setup, ~1s i 16 test).
- Coverage non misurata; target 80% in 2.6+.

### Blocker / dubbi
- **Rate-limiting su `/login/begin`**: nessuno V0. Un attaccante può enumerare email valide misurando 200 vs 404 (`UserNotFound` ritorna 404 non 401). Trade-off: leakage email-existence per UX (user typo → "email not found" sensato). Aggiungo a brief 7.1 (Rate limiting & abuse) come item esplicito.
- **`refresh` endpoint non implementato**: 2.1 ha emesso refresh token ma non c'è `/api/auth/refresh` per swappare refresh→nuovo access. Brief 2.1 chiede solo "JWT session 15 min + refresh token", interpretato come "emit refresh, consumption later". Potrebbe servire in 2.5 (step-up) o quando il client mobile inizia a fare richieste reali. Da chiarire al primo bisogno.

### Prossima task
2.3 Tier 1 — Identity upgrade via Self Protocol. **Attendo brief esteso (Self provider decision, mock structure, atomic 0→1 transition) prima di partire.**

---

## [2.3] tier 1 identity upgrade via self protocol — 2026-04-28

### Cosa è stato fatto

**Config + env (DQ-13).**
- `core/config.py`: `self_verifier_url` default a `https://api.self.xyz/v1/verify` (era localhost stub); aggiunti `self_verifier_scope="marketplace-it-v0"`, `self_verifier_timeout_seconds=10.0`, `kms_keys_dir=".secrets/agent_keys"`.
- `.env.example`: sezione Self Protocol allargata con scope + timeout, sezione nuova KMS.
- `.gitignore`: aggiunta `.secrets/` (mai committare le privkey degli agent).

**`services/kms_service.py` (DQ-13).** Stub V0 file-based.
- `generate_agent_keypair()` → ed25519 via `cryptography`. Persiste `{alg, key_id, private_key_b64, public_key_b64}` in `.secrets/agent_keys/<uuid>.json`. Ritorna `(pubkey_b64_url, "file:<path>")`.
- `sign(kms_ref, message)` placeholder per 5.x.
- `load_pubkey(b64)` per la verifica firme future.
- `KMSError` distinct da network/HTTP (→ 500 + rollback identity-side).
- Path traversal guard sul `key_id`.
- I/O sync dentro async: file scriviture piccole + V0 = 100 utenti, blocco loop trascurabile. V1 con KMS reale è off-process comunque.

**`services/audit_service.py` (DQ-14).** Audit identity-events via structlog.
- `log_tier_upgrade(user_id, from_tier, to_tier, nullifier_hash, agent_id)` async, **mai raise**. Try/except interno + warn fallback.
- Doc esplicita: la tabella `AuditLog` (mandate_id NOT NULL) è riservata alle azioni agente sotto mandate (5.x); il tier upgrade pre-mandate va sul canale structlog (`audit.tier_upgrade`).

**`services/identity_service.py` (DQ-11, 15).** Cuore del task.
- `SelfProofPayload` (Pydantic v2) — input dal mobile (`proof`, `publicSignals`).
- `VerifiedIdentity` (frozen dataclass) — output server-validato di `verify_self_proof`.
- `Tier1UpgradeResult` — output di `upgrade_user_to_tier_1`.
- Errori tipizzati: `SelfVerifierUnavailable` (500), `SelfVerificationFailed` (422), `NullifierCollision` (409), `InvalidTierTransition` (409), `UserNotFound` (404). Ogni classe ha `code` + `http_status`; il code di `SelfVerificationFailed` è `self.<error_code_lowercase>` (es. `self.proof_invalid`, `self.isadult_required`).
- `_post_to_self_verifier(payload)` — async httpx POST con timeout da settings, raise_for_status. **Solo seam mockabile** dei test.
- `verify_self_proof(proof, public_signals, user_identifier)` — costruisce request canonica (proof, publicSignals, scope, userIdentifier, disclosureRequirements: `{minimumAge:18, issuingState:["IT"], documentValidity:true}`), chiama il seam, mappa errori httpx→`SelfVerifierUnavailable`. Su risposta valida applica **invariants server-side**: `verified=true`, scope echo match, userIdentifier echo match, `attributes.isAdult is True`, `issuingState=="IT"`, `documentValid is True`, `documentExpiry > now`. Belt-and-suspenders.
- `upgrade_user_to_tier_1(db, user_id, proof)` — sequenza atomica:
  1. `verify_self_proof` (no DB)
  2. `SELECT user FOR UPDATE` (lock anti double-click)
  3. Idempotency: `tier ≥ 1` → ritorna `already_upgraded=True` con agent esistente
  4. Tier guard: `tier != 0` → `InvalidTierTransition`
  5. Nullifier collision → `NullifierCollision`
  6. KMS keygen (prima delle mutazioni: KMSError lascia tx vuota)
  7. Mutate user (tier=1, nullifier_hash, attributes_proven, attributes_verified_at, attributes_expires_at)
  8. INSERT agent (`status='pending_mandate'`)
  9. COMMIT
  10. Audit (post-commit, fire-and-forget)

**`api/identity.py`.** `POST /api/identity/verify-self`.
- Auth: `Depends(require_tier(0))` — tutti gli utenti registrati.
- Pydantic `VerifySelfRequest` con `populate_by_name=True` (accetta sia `public_signals` snake che `publicSignals` camel da Self).
- `VerifySelfResponse` con `next_step.action="configure_mandate"` + `endpoint="/api/mandates/draft"` (copy mobile italiana).
- Mapper `_to_http(IdentityError)` come in 2.1. `NullifierCollision` ha next_step custom (`login_with_existing_account`). `KMSError` mappa a 500 `kms_error`.
- Wired in `main.py` con `app.include_router(identity_routes.router)`.

**`tests/conftest.py` (DQ-12).** Fixture refactor (rivelato bug `with_for_update()` + sessione condivisa + savepoint mode in 2.3).
- Nuovo `_async_db_connection`: apre connection + outer tx + rollback teardown.
- `async_db_session` ora binda alla connection del fixture sopra (per le assert di test).
- `http_client` apre una sessione fresca **per ogni request HTTP** (mirror del `get_db` produzione). Tutte le sessioni condividono la connection → outer tx → scritture visibili. Le `assert` post-API leggono via `async_db_session` (stessa tx).
- Mock `self_verifier_mock` esteso: auto-patcha `_post_to_self_verifier`, `set_response`/`set_error`/`reset`/`calls`, presets `valid_italian_adult_proof`, `expired_document_proof`, `non_italian_proof`, `minor_proof`, `invalid_proof`, `nullifier_reuse_proof`, e `TimeoutException` come shortcut a `httpx.TimeoutException`.

**`tests/test_identity.py`.** 7 test, tutti verdi.
1. `test_tier_0_can_upgrade_to_tier_1_happy_path` — registra tier 0, verify-self con `valid_italian_adult_proof`, asserts: 200, tier=1, agent_id, agent_pubkey, nullifier_hash. DB: `User.tier=1`, `attributes_proven` con isAdult/issuingState/documentValid; `Agent` con `status="pending_mandate"`, `pubkey == response.agent_pubkey`, `privkey_kms_ref` startswith `file:`. Verifica anche payload sent al verifier (scope + userIdentifier + disclosureRequirements).
2. `test_upgrade_fails_with_invalid_proof` — `invalid_proof()` → 422 `self.proof_invalid`. User stays tier=0, no agent.
3. `test_upgrade_fails_with_minor_user` — `minor_proof()` → 422 `self.isadult_required`. User stays tier=0.
4. `test_upgrade_idempotent_for_already_tier_1` — due call sequenziali; seconda ritorna 200 `already_upgraded=true` con stesso agent_id; DB ha esattamente 1 agent.
5. `test_nullifier_collision_returns_409` — A upgrade con nullifier X; B upgrade con stesso nullifier → 409 `nullifier_collision` con next_step `login_with_existing_account`. B resta tier=0.
6. `test_verifier_timeout_returns_500` — `set_error(TimeoutException)` → 500 `verifier_unavailable`. User stays tier=0.
7. `test_atomic_rollback_on_agent_creation_failure` — proof valida + monkeypatch `kms_service.generate_agent_keypair` raise → 500 `kms_error`. **User row untouched** (tier=0, nullifier_hash=None), no agent. Conferma atomicità.

### Decisioni prese non esplicite nel brief
- **DQ-11** (sequenza atomica) — sequenza esatta dettata dal founder, replicata. Niente `db.begin_nested()` esplicito (autobegin gestisce). Document expiry server-side aggiunto come belt-and-suspenders.
- **DQ-12** (fixture refactor) — bug scoperto in implementazione: `with_for_update()` + sessione condivisa tra request + savepoint mode = `MissingGreenlet` su seconda request. Soluzione: sessione fresca per request. Refactor minimale, no api change, tutti i test 2.1/2.2 ancora verdi.
- **DQ-13** (KMS V0) — file locali in `.secrets/agent_keys/`, ed25519. Founder ha lasciato la decisione a me, ho scelto quella più chiara per dev.
- **DQ-14** (audit channel) — structlog per identity events (no mandate_id), tabella AuditLog riservata a 5.x.
- **DQ-15** (camelCase persistito) — `attributes_proven` mantiene il formato Self verbatim, niente translation layer.
- **`_post_to_self_verifier` come unico seam mock** — confermato dal founder. Tutte le altre funzioni (verify_self_proof, upgrade_user_to_tier_1) sono testate end-to-end senza mock interno.
- **Validazione cross-kind (scope echo, userIdentifier echo)** — server-side anche se Self dovrebbe garantirla. Costo: 2 if extra, valore: protezione da Self misconfigurato/replay.
- **`InvalidTierTransition` distinto da `NullifierCollision`** — entrambi 409 ma codici diversi. Il primo è "stato corrotto" (non dovrebbe succedere), il secondo è "collisione legittima".

### Test scritti / coverage
- 23 test totali ora: 4 auth + 7 identity + 2 mandate_verifier + 10 tier_gating.
- `pytest -v` → `23 passed in 6.19s` (~5s container+migration setup, ~1s i 23 test).
- Coverage non misurata; target 80% in 2.6+ (mandate_verifier completo).

### Blocker / dubbi
- **`SELF_VERIFIER_URL` default**: ho messo `https://api.self.xyz/v1/verify` come da brief, ma è "placeholder finché non confermo l'URL preciso". Quando verifichi col team Self, basta aggiornare `.env.example` e il default in `config.py`.
- **Refresh token re-emit non incluso in 2.3**: dopo l'upgrade a tier 1, il client ha ancora il vecchio JWT con `tier=0`. La prossima chiamata gated `require_tier(1)` fallirebbe finché il client non rilogga (o usa refresh). Brief 2.3 non lo richiede esplicitamente, ma è UX da chiarire: opzione A = client logout-login dopo verify-self; opzione B = response include access_token aggiornato; opzione C = endpoint `/api/auth/refresh` esplicito (manca, suggerito in 2.5 o quando serve). Da decidere prima del frontend integration.
- **Test atomic rollback (test 7)**: il test conferma che user.tier resta 0 quando KMS fallisce. Ma il test sfrutta che KMS è chiamato **prima** delle mutazioni, quindi non c'è niente da rollback davvero. Per testare un rollback **dopo** mutazioni servirebbe simulare un fallimento al `db.commit()` o al flush dell'agent — overhead non giustificato in 2.3. Il path è coperto a livello di code review (try/except sul commit nel servizio).
- **`webauthn` ancora monkeypatcha verify_registration_response** in `_register_tier_0` helper di test 2.3, copia-incollato da test 2.1. Quando 2.6 farà coverage completa, conviene estrarre helper unico in `tests/factories.py`.
- **Locks held by outer transaction in test**: con `with_for_update()` + outer tx in test, il row lock di una request viene rilasciato solo al rollback finale del fixture (non al "commit" del servizio = savepoint release). Non è un problema per i 7 test attuali (sequenziali) ma diventerebbe rilevante se in futuro test parallelizzassero. V0 single-process è OK.

### Prossima task
2.4 Tier 2 — Mandate signing. **Attendo brief esteso del founder.**

> **Promemoria del founder per 2.4**: "mentre lavori, prepara mentalmente il payload del mandate (struttura JSON canonicalizzato, fields, scope). A 2.4 dovrai costruire `mandate_service.create_mandate_payload()` che genera il JSON da firmare. Il payload deve includere riferimento al `nullifier_hash` (per audit) e all'`agent_id` (per binding). Quando arrivi lì, ti scrivo il brief 2.4 esteso."

---

## [2.4] mandate creation + signing + tier 2 upgrade — 2026-04-28

### Cosa è stato fatto

**Pre-cleanup richiesto in review 2.3:**
- `api/identity.py` — la response di `POST /api/identity/verify-self` ora include `access_token` (JWT con `tier=1`) + `token_type="bearer"`. Il client può sostituire l'access token vecchio (tier=0) senza re-login. Refresh token resta lo stesso.
- `tests/test_identity.py` — `test_tier_0_can_upgrade_to_tier_1_happy_path` aggiornato con assert su `access_token` decodato → `tier=1, kind=access, sub=user_id`.
- `DESIGN_QUESTIONS.md` — aggiunto DQ-16 (limite copertura test rollback post-flush) ed esteso DQ-15 con nota consumer per V1.

**`pyproject.toml`**: aggiunto `jcs>=0.2` (JSON Canonicalization Scheme RFC 8785, ~50 LOC, no transitive deps).

**Migration `5765c48f21ea_add_mandate_drafts_table`**: tabella `mandate_drafts` per draft pendenti.
- Colonne: `id (UUID)`, `user_id FK→users`, `agent_id FK→agents`, `canonical_payload (BYTEA)`, `challenge (BYTEA)`, `expires_at`, `consumed (Boolean default false)`, `created_at`.
- Indice `ix_mandate_drafts_user_expires` su `(user_id, expires_at)` per cleanup futuro di draft scaduti.
- Server default `false` su `consumed` per coerenza con default Python-side.
- Migration rifiutato il "drift" autogenerate su `users.tier server_default=null` — non correlato a 2.4.

**`schema.py`**: aggiunto `MandateDraft(Base)` (legacy style coerente con DQ-1: file-internal consistency). Aggiunto `LargeBinary` agli import.

**`core/platform_limits.py`**: hard caps + V0 fixed vocabulary (DQ-18).
- Hard caps: max_price_per_deal=€1000, max_total_volume_per_mandate=€5000, max_total_volume_per_day=€1000, max_deals_per_day=10, max_active_intents=20, max_concurrent_negotiations=10, max_mandate_duration_days=90.
- Default V0: max_price_per_deal=€100, max_total_volume_per_mandate=€500, max_total_volume_per_day=€200, max_deals_per_day=3, max_active_intents=10, max_concurrent_negotiations=5, default_duration=30 giorni.
- `GEO_SCOPE_V0=("IT",)`, `HARD_FORBIDDEN_CATEGORIES` (7 voci).
- `V0_DEFAULT_ALLOWED_ACTIONS` (9 azioni allineate con `tool_layer.py` §5).
- `V0_DEFAULT_FORBIDDEN_ACTIONS` = `(modify_reservation_price, delete_account)`.
- `V0_DEFAULT_STEP_UP_REQUIRED_FOR` = `[accept_offer above €100, create_intent above €150, modify_reservation_price always]`.
- `REVOCATION_POLICY_V0`, `V0_DEFAULT_OPERATING_HOURS = "24/7"`, `MANDATE_SPEC_VERSION = "1.0"`.

**`core/canonicalization.py`**: wrapper su `jcs`.
- `canonicalize(payload_dict) → bytes` (deterministic UTF-8 lex-sorted).
- `digest(canonical_bytes) → bytes` (SHA-256).

**`services/mandate_service.py`**: cuore del task.
- Errori tipizzati: `UserNotFound (404)`, `AgentNotOwned (404)`, `AgentInWrongState (409)`, `LimitsExceedPlatformCap (422)`, `InvalidGeoScope (422)`, `InvalidExpiryWindow (422)`, `InvalidTierTransition (409)`, `DraftNotFound (404)`, `DraftExpired (410)`, `DraftAlreadyConsumed (409)`, `WebAuthnVerificationFailed (422)`. Tutti subclass di `MandateError`.
- Pydantic input: `DraftLimitsInput` (subset modificabile dall'utente), `DraftConstraintsInput`, `WebAuthnAssertionPayload` (verbatim al verify).
- Dataclass output: `DraftCreated`, `MandateSubmitResult`.
- `create_draft(db, user_id, agent_id, user_limits, user_constraints, expires_in_days)`:
  - Valida user (no tier ≥2), agent (owned, status=pending_mandate), limits ≤ caps, geo ⊆ V0, expiry ≤ 90gg.
  - Genera `mandate_id` (uuid), `challenge` (32 random bytes), payload completo (DQ-18 fixed vocab + user customizations).
  - Canonicalizza → bytes.
  - Insert MandateDraft con TTL 5 minuti.
  - Build payload_summary in italiano (helper `_format_italian_date`).
  - Ritorna `DraftCreated` con challenge in b64url.
- `submit_signed_mandate(db, user_id, draft_id, assertion)`:
  - SELECT draft FOR UPDATE (race-safe vs double-submit; fixture session-per-request gestisce la cosa).
  - Reject se consumed/expired.
  - Reload user + agent (defensive).
  - `verify_authentication_response` con challenge=draft.challenge, pubkey=user.passkey_pubkey.
  - Bump passkey sign_count, last_active_at.
  - Parse canonical_payload bytes → dict per ricreare Mandate row.
  - Insert Mandate (signature blob, canonical_payload come UTF-8 string per DQ-20).
  - Activate agent (pending_mandate → active).
  - Tier upgrade 1 → 2.
  - Mark draft consumed.
  - Commit (try/except con rollback).
  - Audit log `mandate_signed` (post-commit, fire-and-forget via `audit_service`).
  - Mint nuovo access_token con tier=2.

**`services/audit_service.py`**: aggiunto `log_mandate_signed(user_id, mandate_id, agent_id)` parallelo a `log_tier_upgrade`. Stesso pattern: structlog, mai raise.

**`api/mandates.py`**: due endpoint, `Depends(require_tier(1))` su entrambi.
- `POST /api/mandates/draft` — accetta `DraftRequest` (agent_id + opzionali limits/constraints/expires_in_days), ritorna `DraftResponse` (draft_id, payload, payload_summary, challenge b64url, expires_at_utc).
- `POST /api/mandates/submit` — accetta `SubmitRequest` (draft_id + webauthn_assertion), ritorna `SubmitResponse` (mandate_id, agent_id, agent_status, expires_at, new_access_token, token_type, next_step).
- Mapper `_to_http(MandateError) → HTTPException(http_status, {code, message})`.
- Wired in `main.py`.

**`tests/test_mandates.py`** — 12 test, tutti verdi al primo run:
1. `test_draft_creation_with_default_limits` — assert payload V0 default + summary italiano ("€100", "Italia", date) + draft row con canonical_payload e challenge persisted.
2. `test_draft_rejects_limits_above_platform_caps` — `max_price_per_deal_eur=2000` → 422 `limits_exceed_platform_cap`.
3. `test_draft_rejects_invalid_geo_scope` — geo=`["FR"]` → 422 `invalid_geo_scope`.
4. `test_submit_with_valid_signature_activates_agent` — happy path: mandate created, agent.status="active", user.tier=2, passkey_sign_count bumped, draft.consumed=True.
5. `test_submit_with_invalid_signature_fails` — webauthn raise → 422 `webauthn_verification_failed`. Niente state change (mandates vuoto, agent pending_mandate, user tier=1, draft non consumed).
6. `test_submit_with_expired_draft_fails` — draft.expires_at backdated → 410 `draft_expired`.
7. `test_submit_with_consumed_draft_fails` — primo submit ok, secondo replay → 409 `draft_already_consumed` (il draft check fires prima del tier check).
8. `test_submit_idempotent_for_already_tier_2` — user direttamente al tier=2 + draft inserito a mano → 409 `invalid_tier_transition` (DQ-17 enforcement).
9. `test_submit_returns_new_access_token_with_tier_2` — JWT in response decoda con `tier=2, kind=access, sub=user_id`.
10. `test_canonicalization_deterministic` — `canonicalize` due volte stesso input → byte-identical. Verificato lex-sort delle keys (`a:1,b:2,c:[3,1,2],...`).
11. `test_webauthn_replay_protection` — sign_count rejection simulata via monkeypatch raise → 422. State unchanged.
12. `test_audit_log_records_mandate_signed` — spy su `audit_service.log_mandate_signed` → chiamato 1 volta con `(user_id, agent_id, mandate_id)` post-commit.

### Decisioni prese non esplicite nel brief / approfondimenti
- **DQ-17** (one mandate per agent in V0) — enforcement in `create_draft` AND `submit_signed_mandate`. 2.5 (revocation) riapre la pipeline.
- **DQ-18** (V0 fixed vocab) — actions/forbidden/step_up/categories tutti hard-coded server-side. Riduce surface area errori dal client.
- **DQ-19** (UUID plain) — niente prefissi `mnd_` nel DB. Display layer V1+ può aggiungerli senza migration.
- **DQ-20** (canonical_payload come Text) — Text per Mandate (compat scaffold), BYTEA per MandateDraft (bytes esatti firmati). Round-trip UTF-8 è bit-identico.
- **WebAuthn challenge = draft.challenge raw bytes** — i 32 random bytes sono ANCHE inclusi nel payload come hex (`payload.challenge`). Il signing flow attesta "user authenticated CON challenge X"; il binding al payload è garantito server-side dalla relazione 1:1 draft.challenge ↔ draft.canonical_payload.
- **payload_summary generato server-side**, non client-side. Schermata 6 di MANDATE_UX_FLOW (mobile) renderizza `human_readable` + `key_fields` direttamente. Date in italiano via lookup table `_ITALIAN_MONTHS` (niente locale dependency).
- **Sequenza submit con SELECT FOR UPDATE**: il fixture refactor di 2.3 (sessione fresca per request HTTP, DQ-12) rende FOR UPDATE compatibile col test. Senza quel refactor, 2.4 avrebbe richiesto rework analogo.
- **`Mandate.signature` JSONB schema**: `{algorithm: "webauthn", credential_id, raw_id, response: {authenticatorData, clientDataJSON, signature, userHandle}}`. Permette ad un auditor di rifare la verifica leggendo solo dal DB.
- **`access_token` rinnovato anche post-mandate** — coerente col pattern di 2.3 (post-tier-upgrade refresh). Refresh token NON rinnovato (TTL 30gg, tier-agnostic).

### Test scritti / coverage
- 35 test totali ora: 4 auth + 7 identity + 2 mandate_verifier + **12 mandates** + 10 tier_gating.
- `pytest -v` → `35 passed in 6.41s` (~5s container+migration setup, ~1.5s i 35 test).
- Coverage non misurata; target 100% di `mandate_verifier.py` in 2.6.

### Blocker / dubbi
- **WebAuthn assertion shape**: `WebAuthnAssertionPayload` accetta verbatim ciò che il client mobile invia. py-webauthn fa il parsing. Se il mobile manda field extra non testati, potrebbero rompere alla `verify_authentication_response`. Da validare con la prima integration mobile reale.
- **`signature` blob storage size**: ogni mandate ha ~5KB di blob WebAuthn (auth_data + client_data + signature in b64). 100 utenti = 500KB, niente. 100K utenti = 500MB, ancora gestibile. V1 valutare compression se cresce.
- **Replay protection real**: il test 11 simula via monkeypatch. Per testare la replay vera servirebbe un fake authenticator (CBOR) — fuori scope V0 (libreria `webauthn` non ne ha helper). py-webauthn library affidabile su questo punto.
- **Concorrenza tra create_draft + cleanup** (futuro): un cron job che pulisce draft scaduti potrebbe race con un submit di un draft "ai limiti del TTL". Non blocker V0 (cleanup non implementato; al primo bisogno usare `WHERE expires_at < NOW() AND consumed = FALSE`). Se serve transazionalità, `WHERE id NOT IN (SELECT id FROM mandate_drafts WHERE consumed = TRUE FOR UPDATE)` lock-aware.
- **Italian months hardcoded**: `_ITALIAN_MONTHS` tuple in `mandate_service.py`. Quando V1 espanderà a EU, servirà una mappa per locale. `babel` library è la mossa standard ma overkill per V0 IT-only.
- **`InvalidExpiryWindow` non testato esplicitamente**: il test 2 copre solo `LimitsExceedPlatformCap`. Caso `expires_in_days=200` o `=0` non ha test dedicato — coperto solo da Pydantic schema validation (`Field(ge=1, le=90)` su `expires_in_days` in `DraftRequest`). Pydantic ritorna 422 prima che arrivi al service. OK per V0.

### Prossima task
2.5 Mandate revocation & step-up. **Attendo brief esteso del founder.**

> **Promemoria del founder per 2.5**: "richiederà 2.5: endpoint POST /api/mandates/{id}/revoke con stessa logica WebAuthn signing; concept di pending step-up actions (table, push notification, /api/step-up/{action_id}/sign endpoint); endpoint /api/auth/refresh per rinnovare access token. Niente di tutto questo va costruito in 2.4."
