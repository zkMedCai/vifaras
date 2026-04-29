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

---

## [2.5] mandate revocation + step-up + auth refresh — 2026-04-28

### Cosa è stato fatto

**Bugfix scaffold (DQ-21).** `mandate_verifier.py` — `StepUpRequired` era `@dataclass`, ma il codice fa `raise StepUpRequired(...)`. Bug latente fino a 2.5 (test 1.3 non esercitavano il path step-up). Fix: ora eredita da `Exception` con `__init__` esplicito che assegna `action`, `params`, `reason`. Cambio minimo, scaffold preservato.

**Stub vuoti scaffold (DQ-23).** Creati stub minimi (1-3 righe ciascuno) per `services/{intent,match,negotiation,deal}_service.py`. Importabili da `tool_layer.py` senza crash; verranno sostituiti in FASE 4-5.

**Migration `52b8a8ddb144`**: due tabelle nuove.
- `mandate_revocation_drafts`: id, user_id FK, mandate_id FK, canonical_payload BYTEA, challenge BYTEA, expires_at, consumed (server_default false), created_at. Index `ix_revocation_drafts_user_expires`.
- `step_up_requests`: id, agent_id FK, mandate_id FK, user_id FK, action, action_params JSONB, reason, challenge BYTEA, canonical_payload BYTEA, status (server_default 'pending'), expires_at, resolved_at, signature JSONB nullable, created_at. Partial index `ix_step_up_pending_user` su `(user_id, status) WHERE status='pending'`.
- Migration pulita degli alter spuri di drift autogenerate.

**`schema.py`**: aggiunti `MandateRevocationDraft` e `StepUpRequest` modelli (legacy style coerente con file-internal consistency, DQ-1).

**`services/notification_service.py` (V0 stub).** Sync interface `push_step_up_request(db, agent_id, action, params, reason, step_up_id=None)` e `push_question(db, agent_id, question, context)`. Niente raise. structlog su stdout. APNs/FCM è V1+.

**`services/step_up_service.py`.** Async API + sync helper.
- `create_pending_request_sync(db, agent_id, mandate_id, user_id, nullifier_hash, action, action_params, reason)` — usato da `tool_layer._queue_step_up`. Genera challenge 32 byte + canonicalizza payload (`step_up_approval` action). TTL 600s. Ritorna step_up_id.
- `get_pending_for_user(db, user_id)` async — non-expired, status=pending, ordered by created_at.
- `get_for_signing(db, user_id, step_up_id)` async — ritorna payload dict + challenge b64url + expires_at.
- `sign(db, user_id, step_up_id, assertion)` async — `SELECT FOR UPDATE`, verify_authentication_response, status='approved' + signature blob persistita. Bumps user.passkey_sign_count.
- `reject(db, user_id, step_up_id)` async — status='rejected'.
- `mark_expired(db)` async — sweep cleanup per cron 7.x.
- Errori tipizzati: `StepUpNotFound (404)`, `StepUpAlreadyResolved (409)`, `StepUpExpired (410)`, `StepUpVerificationFailed (422)`.

**`services/mandate_revocation_service.py`.** Async, draft + submit.
- `create_revocation_draft(db, user_id, mandate_id, reason)` — valida reason ∈ V0 list (`user_requested`, `suspicious_activity`, `lost_device`), genera challenge + canonicalizza `revoke_mandate` payload. Idempotente: se `mandate.revoked_at != None` ritorna `already_revoked=True` con campi vuoti.
- `submit_revocation(db, user_id, mandate_id, draft_id, assertion)` — verify WebAuthn, marca `mandate.revoked_at` + `revocation_reason`, `agent.status='revoked'`, **cascade** delle cancellazioni:
  - Negotiations attive di intents dell'agente → status=`cancelled_revoked` (17 char per stare nel `String(20)` schema, DQ-24).
  - Deals pending_buyer/pending_seller del user → status=`cancelled_revoked`. **Confirmed deals invariati** (test 5).
  - Active intents dell'agente → status=`paused`.
  - Pending mandate_drafts del user/agent → consumed=True.
  - Pending step_up_requests del mandate → status=`expired`.
  - Audit log via structlog `audit.mandate_revoked` con counts.
- Idempotente al submit anche con draft fresco: se `mandate.revoked_at != None`, marca draft consumed e ritorna `already_revoked=True` con `cancellations` zero.

**`services/auth_service.py`** esteso.
- `refresh_access_token(db, refresh_token)` async → `(access_token, ttl_seconds)`. Decode refresh JWT, load user (DB), check user.status=='active', mint nuovo access con **tier corrente da DB** (non dal payload del JWT — fix critico per utenti promossi mid-session, test 14).
- Errori nuovi: `InvalidRefreshToken (401)`, `UserNotActive (403)`.
- Refresh token NON rinnovato (V0, DQ-25). Rotation rinviata a 7.4.

**`agents/tool_layer.py` extension.** `_queue_step_up` ora carica il mandate attivo dell'agente, prende user.nullifier_hash, chiama `step_up_service.create_pending_request_sync` per inserire la row, poi chiama `notification_service.push_step_up_request` (esistente). Ritorna `step_up_id` che `execute()` include nella response a Claude. Estensione di ~25 righe, scaffold preservato per il resto.

**Endpoints.**
- `POST /api/mandates/{id}/revoke/draft` (require_tier(2)) — `RevokeDraftResponse` con payload + challenge + already_revoked flag.
- `POST /api/mandates/{id}/revoke/submit` (require_tier(2)) — `RevokeSubmitResponse` con cancellation counts.
- `GET /api/step-up/pending` (require_tier(2)) — list di pending con agent_id/action/reason.
- `GET /api/step-up/{id}/draft` (require_tier(2)) — payload + challenge.
- `POST /api/step-up/{id}/sign` (require_tier(2)) — verify + approve.
- `POST /api/step-up/{id}/reject` (require_tier(2)) — explicit cancel.
- `POST /api/auth/refresh` (no auth, refresh nel body) — new access_token + ttl.
- Tutti wired in `main.py`.

**Test factories** (`backend/tests/factories.py`). Nuovo file. Helper riutilizzabili tra test sync/async:
- `default_user_kwargs(tier, email)` — User row a tier ≥ 1 con tutti i campi.
- `build_mandate_payload_dict(...)` — payload mandate per fixture, accetta `step_up_rules` custom.
- `setup_active_mandate_sync(db_session, ...)` — User+Agent+Mandate completi via Session sync.
- `setup_active_mandate_async(db, ...)` — equivalente async.
- `fake_assertion_payload()` — assertion WebAuthn finta.

**`tests/test_revocation.py`** (5 test):
1. `test_revoke_mandate_with_valid_signature_succeeds` — happy path: draft + submit, mandate.revoked_at set, agent.status='revoked'.
2. `test_revoke_with_invalid_signature_fails` — webauthn raise → 422 `revocation_verification_failed`. Mandate untouched.
3. `test_revoke_already_revoked_is_idempotent` — secondo /draft → 200 con `already_revoked=true`, draft_id null. revoked_at preservato.
4. `test_revoke_cancels_active_negotiations_and_pending_deals` — setup buyer+seller agents, intents, match, negotiation active, deal pending + deal confirmed. Revoke. Counts: 1/1/1. DB-side: nego status='cancelled_revoked', pending deal 'cancelled_revoked', buy intent 'paused'.
5. `test_revoke_does_not_affect_confirmed_deals` — confirmed deal resta `confirmed` post-revoke.

**`tests/test_step_up.py`** (6 test):
6. `test_step_up_request_created_when_action_above_threshold` — sync via `ToolHandler`, send_offer €100 con threshold €50 → step_up_request inserted con status='pending', mandate_id, action_params['price_cents']=10_000.
7. `test_step_up_sign_with_valid_signature_approves` — pending row inserted manually, /sign endpoint, status='approved', signature blob persistita.
8. `test_step_up_sign_with_invalid_signature_fails` — webauthn raise → 422 `step_up_verification_failed`. Status untouched.
9. `test_step_up_reject_marks_as_rejected_and_cancels_action` — /reject → status='rejected', resolved_at set.
10. `test_step_up_expired_after_ttl` — expired pending + fresh pending; `mark_expired` sweep returns 1, expired row → status='expired', fresh row untouched.
11. `test_step_up_resume_action_with_approved_signature` — sync MandateVerifier: senza signature in params → raises StepUpRequired; con `step_up_signature` truthy → mandate ritornato.

**`tests/test_auth.py`** + 4 refresh test:
12. `test_refresh_returns_new_access_token` — register → refresh → JWT decoda con tier=0, kind=access. ttl=900s.
13. `test_refresh_with_invalid_token_fails` — garbage token → 401 `invalid_refresh_token`.
14. `test_refresh_returns_current_tier_not_token_tier` — register tier=0, mutate user.tier=1 in DB, refresh → access JWT con tier=1.
15. `test_refresh_for_banned_user_fails` — user.status='banned' → 403 `user_not_active`.

### Decisioni prese non esplicite nel brief
- **DQ-21** — bugfix `StepUpRequired` Exception. Latent bug, motivato.
- **DQ-22** — V0 accetta più pending step-up per (agent, action). Hard-enforce solo se vediamo abuse.
- **DQ-23** — stub vuoti per intent/match/negotiation/deal_service. Path-neutral.
- **DQ-24** — `cancelled_revoked` (17 char) invece di `cancelled_due_to_revocation` (28 char) per stare nel `String(20)` scaffold.
- **DQ-25** — refresh token NO rotation in V0. Aggiunto a 7.4 hardening.
- **Cascade revoca esecuzione**: scan delle negotiations attive con per-row check sulla relazione match→intent→agent. Per V0 100 utenti il full scan è cheap; in 7.x con più volume si può aggiungere `agent_id` denormalizzato su Negotiation per query diretta.
- **Test pattern `populate_existing=True`**: i test che leggono dal DB DOPO una API call (che usa una sessione separata) devono forzare il refresh dell'identity map del test. `populate_existing=True` come execution_option è pulito.
- **`step_up_service.create_pending_request_sync`** prende `nullifier_hash` come parametro esplicito (non lo carica dal DB) — il caller (`tool_layer`) ha già il User loaded. Riduce query.
- **API revoke chiamabile solo a tier=2**: l'utente deve essere completamente "mandated" per poter revocare. Coerente: tier 0/1 non hanno mandate da revocare.
- **Step-up endpoints richiedono tier=2**: solo utenti mandated possono avere agent attivo che genera step-up.

### Test scritti / coverage
- 50 test totali ora: 8 auth (4 nuovi refresh) + 7 identity + 2 mandate_verifier + 12 mandates + 5 revocation + 6 step_up + 10 tier_gating.
- `pytest -v` → `50 passed in 6.60s` (~5s container+migration setup, ~1.5s i 50 test).
- Coverage non misurata; target 100% di `mandate_verifier.py` in 2.6.

### Blocker / dubbi
- **Notification service è solo log**: nessuna integrazione con APNs/FCM. Il client mobile dovrà fare polling su `/api/step-up/pending`. Documentato nel docstring del service. V1 cambio drop-in di `push_step_up_request`.
- **Step-up resume sincrona NON testata end-to-end**: il test 11 verifica che MandateVerifier accetti la signature. Il flow completo (orchestrator riprova action al prossimo tick, tool_layer carica step_up_request approved, attacca signature) richiede agent runtime FASE 6. Per ora coperta a livello di componenti separati.
- **Refresh non rotato (DQ-25)**: refresh stolen → 30 giorni di danno potenziale. Acceptable V0, hardening prima del launch reale.
- **Tier=2 per /step-up endpoints**: se un utente revoca il mandate (tier 2 → ?), questo ricade in ambito 2.5+. Per ora il founder spec dice "revoca è irreversibile per V0 — l'utente deve ricreare il mandate, ricominciando dal tier 1 di fatto". Ma `user.tier` resta 2 nel DB? Da chiarire: post-revoke l'utente è tier=2 ma agent è revoked → endpoint /step-up/* non ha senso (no pending step-up generabili senza agent attivo). Implicit handling: pending list è vuota, ma chiamare /sign su step-up vecchi (pre-revoca) è OK (sono in stato 'expired' dopo cascade).
- **Validazione `revocation_draft` mandate_id consistency**: l'URL ha `mandate_id`, il body ha `revocation_draft_id`. Il service verifica che `draft.mandate_id == url.mandate_id` → se no, `RevocationDraftNotFound`. Test esplicito non c'è (caso edge); il codice path è coperto da revisione.
- **Refresh token con user_id in JWT ma user assente in DB**: il refresh fallisce con `invalid_refresh_token` (404→401 mapping interno). Test esplicito non c'è ma il code path è coperto.

### Prossima task
2.6 Test completo MandateVerifier (coverage 100%). **Attendo via libera.**

> **Promemoria del founder per 2.6**: "dopo 2.5 hai il MandateVerifier che gestisce davvero step-up via step_up_service. 2.6 sarà coverage completo — ogni branch, ogni edge case, ogni interazione con i servizi esterni. Già ora il MandateVerifier ha test smoke (1.3), 2.6 lo porta a 100% coverage. Quando arrivi lì, ti scrivo brief breve."

---

## [2.6] mandate_verifier full coverage + factories module — 2026-04-28

### Cosa è stato fatto

**Bugfix scaffold log_failed (DQ-27).** `mandate_verifier.log_failed` provava a inserire `AuditLog(user_id=None, mandate_id=None, ...)` per il caso `NoActiveMandate`. Schema dichiara entrambi NOT NULL → INSERT crashava. Latente fino a 2.6 (path non esercitato prima). Fix: best-effort lookup del mandate via `agent_id`; se trovato scrive AuditLog completo, altrimenti emette `audit.action_denied_no_mandate` su structlog. Coerente con DQ-14 split.

**Documentazione DQ-26.** `user.tier` non degrada mai post-revoke. Tier=2 + agent revoked = "credenziale verificata, niente operatività". V0 lascia l'utente in stato dormiente; V1 implementerà multi-agent re-creation (no re-verify Self).

**`backend/tests/factories.py` esteso (founder pattern).** Factory granulari sync per `mandate_verifier` tests:
- `make_user_sync(db, *, tier=2, status, email, label)` → User row.
- `make_agent_sync(db, *, user, status, label)` → Agent row.
- `make_mandate_sync(db, *, user, agent, scope_overrides, limits_overrides, step_up_overrides, constraints_overrides, expires_in_days, revoked, expired, issued_offset_days, spent_today_eur, spent_total_eur, deals_count, last_reset_date)` → Mandate con surgical override su ogni campo. Default V0 ovunque.

Le factory async esistenti (`setup_active_mandate_async`, ecc.) restano per i test 2.4/2.5.

**`backend/tests/test_mandate_verifier.py` riscritto da 2 smoke a 46 test:**

Group 1 — `_get_active_mandate` (4 test):
- `test_no_active_mandate_raises`
- `test_expired_mandate_raises`
- `test_revoked_mandate_excluded_raises_no_active_mandate` (test della SEMANTICA: revoked filtered out → NoActiveMandate; il post-check `MandateRevoked` è dead branch unreachable, marcato `# pragma: no cover` con docstring esplicativo)
- `test_returns_most_recent_active_mandate` (3 mandate: revoked vecchio, attivo medio, attivo nuovo → seleziona il newest active)

Group 2 — `_check_scope` (3 test):
- `test_action_in_forbidden_raises`
- `test_action_not_in_allowed_raises`
- `test_action_in_allowed_passes`

Group 3 — `_check_constraints` (6 test):
- `test_geo_scope_match_passes` / `_mismatch_raises` / `_no_location_in_params_passes`
- `test_category_forbidden_raises` / `_allowed_wildcard_passes` / `_not_in_explicit_allowlist_raises`

Group 4 — `_check_limits` (8 test):
- `test_per_deal_cap_exceeded_raises` (refactored from existing)
- `test_per_deal_cap_at_boundary_passes` (price == cap is permitted)
- `test_daily_volume_cap_exceeded_raises`
- `test_daily_volume_increments_correctly`
- `test_mandate_total_cap_exceeded_raises`
- `test_deals_count_cap_per_day_exceeded`
- `test_no_price_in_params_skips_price_checks`
- `test_action_not_price_relevant_skips_volume_checks` (volume check fires only on accept_offer/create_deal)

Group 5 — `_check_step_up` (6 test, +1 vs founder spec):
- `test_step_up_required_when_above_threshold`
- `test_step_up_passes_when_signature_present`
- `test_step_up_always_required`
- `test_step_up_below_threshold_passes`
- `test_step_up_no_threshold_no_always_passes`
- `test_step_up_rule_for_different_action_is_skipped` — extra test per coprire la `continue` line nel for loop (rule per accept_offer, autorizzo send_offer → loop continua, no raise)

Group 6 — `_reset_daily_counters_if_needed` (2 test):
- `test_counters_reset_on_new_day` (last_reset_date ieri → counters azzerati, last_reset_date aggiornata a today)
- `test_counters_not_reset_same_day` (last_reset_date stesso giorno → counters preservati)

Group 7 — `record_usage` (3 test):
- `test_record_usage_increments_counters_on_success` (accept_offer → spent_today/spent_total/deals_count incrementati)
- `test_record_usage_does_not_increment_on_failure` (success=False → counters invariati)
- `test_record_usage_writes_audit_log` (verifica row con user_id, mandate_id, params, result)

Group 8 — `log_failed` (2 test, post-DQ-27):
- `test_log_failed_with_active_mandate_writes_audit_log` (mandate trovato → AuditLog row con error_code, success=False)
- `test_log_failed_without_mandate_does_not_crash` (no mandate → niente AuditLog row, niente exception. Caso ex-bug)

Group 9 — Helpers (parametrize, 2 funzioni × cases):
- `test_extract_price_eur` parametrize 6 casi (price_cents, price_eur con int e str, vuoto, campo irrelevant)
- `test_extract_country` parametrize 6 casi (Roma IT, Milan IT, paris fr → uppercase, no comma, trailing comma, 3-letter ITA)

**Coverage gate.** `pytest --cov=app.services.mandate_verifier --cov-report=term-missing --cov-fail-under=100` → **100% coverage** (138 statements). Da preservare in CI a 7.x.

### Decisioni prese non esplicite nel brief
- **DQ-26** — post-revoke user.tier resta 2. Decisione architetturale: tier = credenziale, agent = operatività.
- **DQ-27** — log_failed bug fix (analogo a DQ-21). Best-effort + fallback structlog.
- **`# pragma: no cover` su line 141** — il post-filter `MandateRevoked` check è dead branch (la query già esclude i revoked). Non è dead code rimuovibile però — è defensive guardrail in caso di future modifica della query. Marcato esplicitamente per il coverage gate, docstring on the line spiega.
- **+1 test sopra spec** (`test_step_up_rule_for_different_action_is_skipped`) — necessario per coprire la `continue` del for loop nel `_check_step_up`. Sposta la suite da 35 a 36 test (37 con i refactored).
- **Boundary test `_per_deal_cap_at_boundary_passes`** — price == cap permesso (stretti `>`, non `>=`). Non era esplicitato nel brief ma è importante per chiarire la semantica.
- **`test_action_not_price_relevant_skips_volume_checks`** — chiarisce che il volume cap fira solo su `accept_offer`/`create_deal`. send_offer con prezzo > daily cap passa (offers in flight non contano).
- **Parametrize per helpers**: 6 casi `_extract_price_eur` + 6 casi `_extract_country` invece di 4+5 spec'ati. Coperti più edge case (price_cents=0, "Roma," trailing comma, "Roma, ITA" 3-letter).

### Test scritti / coverage
- **94 test totali** ora: 8 auth + 7 identity + **46 mandate_verifier** + 12 mandates + 5 revocation + 6 step_up + 10 tier_gating.
- `pytest -v` → `94 passed in 6.99s` (~5s container+migration setup, ~2s i 94 test).
- **Coverage `mandate_verifier.py`: 100%** (138/138 statements, 1 line con `# pragma: no cover` per dead defensive branch).

### Blocker / dubbi
- **`# pragma: no cover` come standard?** Per V0 lo uso solo dove giustificato (defensive code unreachable in pratica). Va valutato come policy a 7.x: alcune codebase preferiscono no-pragma + accept <100%, altre preferiscono pragma esplicito. Per ora pragma esplicito con docstring perché lascia traccia chiara nel codice.
- **Non ho aggiunto coverage gate a `pyproject.toml` ancora**: il brief 2.6 dice "preservare in CI quando arriveremo lì". Per ora è eseguibile manualmente via `--cov-fail-under=100`. Quando configuriamo CI in 7.2 lo blindo.
- **Il `_extract_country` con location vuota o con solo separatore**: testato i casi ragionevoli; edge cases come "  ,  " (whitespace only) non testati ma il codice gestisce graceful (parts=[",",""] → last="", len 0 → None).
- **Mandate factory con limit `max_total_volume_eur_per_mandate=500` (default)**: alcuni test che testano i limiti devono ESPLICITAMENTE override per non sbattere su questo cap accidentalmente. Tutti i test che lo richiedono lo settano a 1_000+. Documented inline.
- **AuditLog `user_id`/`mandate_id` NOT NULL post-fix `log_failed`**: la tabella ora è veramente "agent action under mandate". Identity-lifecycle events (revoke, tier upgrade, log_failed senza mandate) usano structlog. DQ-14 + DQ-27 cementano questa separazione.

### Cosa significa "FASE 2 completa"
Con 2.6 chiusa, la **FASE 2 (Identity & Auth) è completata al 100%**:
- ✅ 2.1 Tier 0 anonymous onboarding (passkey + email)
- ✅ 2.2 Tier-based gating middleware
- ✅ 2.3 Tier 1 identity upgrade via Self Protocol
- ✅ 2.4 Tier 2 mandate signing (WebAuthn)
- ✅ 2.5 Mandate revocation + step-up + auth refresh
- ✅ 2.6 MandateVerifier 100% coverage

94 test verdi totali. 27 DQ documentate. 5 commit `[2.x]` ordinati. Lo schema reggerà 100 utenti V0 senza tweak.

### Prossima task
**4.1 Intent service (FASE 4 — Marketplace core)**. Cambio di mood: da identity/security a business logic + matching semantico. **Attendo brief esteso del founder** per partire bene su 4.1.

> **Promemoria del founder pre-FASE 4**: "Fase 4 (Marketplace core) è tutto un altro lavoro: business logic, matching semantico, embedding, scoring algorithms. Più creativo, più 'let's see how it performs', più calibration su feedback reali. Ti scriverò un brief denso per 4.1 (Intent service) quando ci arriviamo, perché è il primo pezzo dove il prodotto inizia davvero a esistere."

---

## [chore] v1.3 stocktaking — pre-FASE 4 housekeeping (2026-04-29)

### Cosa fatto
- `PROJECT_BRIEF.md` v1.1 → **v1.3**:
  - §2.7 MCP architectural principle (tool layer come MCP-compatible, V2+ public server)
  - §2.8 OAuth provider linking V1.5+ (Anthropic primary, ChatGPT/Gemini deferred)
  - §2.9 BUY/SELL/TRADE come Intent.side enum (V0 schema-ready, TRADE V1+)
  - Stack web Next.js 14 + mobile React Native companion
  - FASE 1-2 marcate ✅ complete; rinumerate fasi 8 (TRADE V1), 9 (Trustee V1.5), 10 (Web V0), 11 (Mobile V0.5)
  - DQ-26 tier-credenziale / agent-operatività formalizzato in §2.5
  - Spec di prodotto referenziate
- **Nuovi documenti satellite** (V1+ specs, non implementare in V0):
  - `IDEAS_BACKLOG.md` v1.0 — backlog accumulato per categoria (provider linking, MCP, sicurezza, performance, testing, multi-agent, TRADE, Trustee, frontend, compliance, GTM)
  - `MANDATE_UX_FLOW.md` v1.0 — 7 schermate Tier 2 onboarding + stati errore + backend mapping (deliverable FASE 10/11)
  - `BARTER_DESIGN.md` v1.0 — TRADE bilaterale V1+ (subjective value theory, Pareto matching, multi-dim negotiation, Opzione β, regolatorio IT) (deliverable FASE 8)
  - `TRADE_WINDOW_FLOW.md` v1.0 — Trustee Service V1.5 (Stripe Connect Express, 4 corrieri IT, dual-tracking, dispute resolution, postal claim handoff) (deliverable FASE 9)

### Decisioni cementate da questo chore
- **Intent.side = enum a 3 valori** (`buy` | `sell` | `trade`). V0 implementa solo buy/sell. `trade` accetta lo schema ma il service rifiuta operativamente con `NotImplementedError`. Razionale: 30 min in V0 risparmiano settimane in V1.
- **Tool layer MCP-compatible** anche se nessun MCP server pubblico in V0. Disciplina: tool definitions con JSON schema standard, ToolHandler agnostico al transport.
- **OAuth provider linking deferred a V1.5**: V0/V1 girano sui nostri crediti Anthropic, free tier 5 negoziazioni/mese. Take rate sui deal 5-8% blended.
- **Trustee Service obbligatorio (non opt-in) in V1.5**: pattern Cardmarket, default-on. Differenzia da Vinted (opt-in, piena di truffe).
- **Tutti i deliverable V1+ sono ora documentati** ma fuori scope V0. Nessun debt in agenda per FASE 4-7.

### Cosa NON cambia
- Stack tecnico backend (Python/FastAPI/Postgres/SQLAlchemy 2.0/Pydantic v2)
- Decisioni di design §3 (identity, mandate, marketplace, deal, compliance)
- Test architecture (testcontainers Postgres only)
- Workflow operativo (una task alla volta, commit `[fase.task]`, PROGRESS, stop)
- Tutti gli scaffold §5 e i 9 service implementati in FASE 1-2

### Test
N/A — chore documentale. `pytest -v` post-chore: **94 passed** invariato.

### Prossima task
**4.1 Intent service** — pronto a partire. Founder ha promesso brief esteso pre-4.1; attendo via libera.


---

## [4.1] Intent service + crud endpoints (2026-04-29)

### Cosa fatto
- **Migration `8df1d6891fd9`** — `intents.agent_id`, `audit_log.agent_id`, `audit_log.mandate_id` → nullable. `intents.side` widened String(4) → String(5) per future-compat con `'trade'` (§2.9).
- **`core/categories.py`** — 22 categorie V0 chiuse + helpers `is_allowed`/`is_forbidden`. `HARD_FORBIDDEN_CATEGORIES` resta in `platform_limits.py` (decisione platform), categorie V0 sono vocabulary.
- **`services/embedding_service.py`** — async `generate_embedding(text)`. Backend env-switched: `EMBEDDING_BACKEND=openai` (default, AsyncOpenAI inline) o `=fake` (deterministic SHA-256-seeded vector L2-normalized). LRU cache 1000 entries. Failure mode: `EmbeddingServiceUnavailable` → 503 `Retry-After: 30` lato API.
- **`services/intent_service.py`** — CRUD completo async:
  - `create_intent` (tier≥0): valida side/title/description/category/prices/duration/location/currency. Cap intent attivi per tier (0:5, 1:10, 2:mandate). Embedding inline. Audit row.
  - `list_user_intents` con filtri status/side + paginazione clamped [1,100].
  - `get_intent_for_user` (404 sia per non-found sia per non-owned, no info leak).
  - `update_intent` con field-level gating: title/desc/soft_pref → tier≥0; price → tier≥2 + active-negotiation guard (409); category/side mai modificabili (422).
  - `cancel_intent` con cascade su Negotiation→cancelled e Match→expired. Idempotent.
- **`api/intents.py`** — 4 endpoint REST: POST/GET/PATCH/DELETE su `/api/intents{,/{id}}`. Pydantic v2 request/response. `IntentError → HTTPException` mapping (pattern da mandates.py). `TooManyActiveIntents` aggiunge `next_step.action='upgrade_tier'` al payload 402.
- **`audit_service.py`** — nuovo `log_intent_event(db, *, user_id, action, params, ...)` per AuditLog table con `agent_id`/`mandate_id` nullable. Doctrine docstring aggiornata (DQ-14 evolved): table = marketplace actions, structlog = identity-lifecycle.
- **`tool_layer._create_intent`** → `NotImplementedError` con riferimento a DQ-28. Sync-async mismatch tra scaffold legacy e nuovo service rinviato a FASE 5/6.
- **`main.py`** — router `/api/intents` registrato.

### Decisioni prese non esplicite nel brief
- **DQ-28** — tool_layer._create_intent rinviato a FASE 5/6. V0 intent CRUD è solo via API.
- **DQ-29** — step-up biometrico su PATCH price update rinviato a V0.5. V0 gate solo per `tier=2` (la passkey è già stata firmata recentemente con il mandate). Aggiunto a IDEAS_BACKLOG.
- **DQ-30** — caps tier-based asimmetrici: tier 0 → 5, tier 1 → 10, tier 2 → mandate. Tier 0 deliberatamente più stretto del default mandate (10) per anti-abuse di utenti non verificati.
- **`intents.side` widening migration** — schema String(4) → String(5) anche se 'trade' è rifiutato a service-level. Mantenere il column troppo stretto per il valore sarebbe un foot-gun futuro, widening costa zero.
- **Audit doctrine evolved** — DQ-14 originale diceva "AuditLog table = agent under mandate, structlog = identity-lifecycle". Post-4.1: "AuditLog table = marketplace actions (qualsiasi entry/cambio su Intent/Negotiation/Deal anche tier 0), structlog = identity-lifecycle (tier upgrade, mandate signed)". `agent_id`/`mandate_id` nullable rendono possibile la prima parte. Aggiornato docstring di audit_service.py.
- **Cache embedding LRU in-memory** — 1000 entries. V0 single-process, sufficient. V1 → Redis. Già in IDEAS_BACKLOG § Performance.
- **Embedding text format** — `f"{title}\n{description}"` (semplice). Brief consigliava di sperimentare con `f"{category}: {title}\n..."`. Ho lasciato il format semplice per ora — la categoria è già un filtro pre-similarity nel match service (4.3), prependerla nell'embedding sarebbe ridondante e potenzialmente noise. Posso cambiarlo se 4.3 mostra match cross-category subottimali.
- **`update_intent` ricalcola `expires_at` da NOW** quando `duration_days` cambia (semantica "renew"), invece di sommare a `created_at`. Più intuitivo per l'utente che vuole "estendere di altri 14 giorni".
- **`IntentInActiveNegotiation` (409)** — guard sul price update richiede join `Match.id == Negotiation.match_id` filtrato sull'intent. Non userà `with_for_update()` sulla tabella Negotiation (ridondante: il lock sull'Intent row + l'idempotency_key futuro su Deal coprono la race).

### Test scritti / coverage
- **25 nuovi test** in `tests/test_intents.py`:
  - 8 create (BUY/SELL happy path, embedding, trade rejection, price-relationship × 2, category × 2)
  - 3 tier limits (0:5, 1:10, 2:mandate)
  - 3 list (filter, paginate, ownership isolation)
  - 5 update (title OK at tier 0, price 402 at tier 0, price OK at tier 2, 409 in active negotiation, category 422)
  - 3 cancel (mark cancelled, cascade neg+match, idempotent)
  - 3 embedding integration (calls service, 503 on failure, cache hit)
- **Suite totale**: `pytest` → **119 passed in ~7s** (94 pre-4.1 + 25 nuovi). Nessuna regressione.
- Autouse fixture `_force_fake_embedding` setta `EMBEDDING_BACKEND=fake` per ogni test del modulo, cache pulita pre/post.

### Blocker / dubbi
- **Pydantic deprecation** — `self.model_fields` in `UpdateIntentInput._at_least_one_field` (instance access deprecato V2.11). Cambiato a `type(self).model_fields`. Nessun warning post-fix.
- **AsyncSession identity-map staleness** — il test `cancels_active_negotiations` inizialmente leggeva `Negotiation`/`Match` mutati dalla session API via `select()`, ma `expire_on_commit=False` lasciava lo snapshot pre-cancel nell'identity map. Risolto con `await async_db_session.refresh(obj)` (non `expire_all()` — quello fa lazy-load fuori greenlet → MissingGreenlet).
- **`agent_id` nullable cascade** — `mandate_revocation_service` cascade query `Intent.agent_id == agent_id` continua a funzionare correttamente con NULL (Postgres: NULL ≠ qualsiasi valore non-NULL → skip). Tier-0 intents sono per design immuni a revocation cascade. Verificato che non ci siano altre cascade rotte (deal cascade è 5.x quindi out-of-scope).
- **OpenAI sync-inline costo** — text-embedding-3-small ~$0.000004/intent, ~150ms latency. A V0 traffic (10K intent/giorno target) trascurabile. Se passiamo 100K/giorno consideriamo batch inline (ma è 4.2 estensione).
- **`_create_intent_directly` test helper** — bypassa l'API per il setup dei test tier-limit/list. Comodo ma rischia di drift dallo schema reale. Mitigazione: usato solo per scenari dove la creazione API è già coperta da altri test (1, 2, 3); il setup helper produce intent identici al path API + embedding deterministico.

### Cosa significa "4.1 completa"
Marketplace ha il suo primo concrete deliverable. Tier=0 user può:
1. POST /api/intents → embedding generato, intent persisto, status=active
2. GET /api/intents → lista propri intent
3. PATCH /api/intents/{id} → modifiche non-sensibili (title, desc)
4. DELETE /api/intents/{id} → cancel + cascade

Tier=2 (con mandate) anche modifica prezzi. Trade è schema-ready. 119 test verdi. Pronto per 4.2 (embedding service estension) e 4.3 (match service).

### Prossima task
**4.2 Embedding service**. Estende lo stub minimale di 4.1: retry con backoff su rate limit, batch processing (V1+ come bulk import), cache LRU formale. Brief minimale già nel BRIEF §6. Attendo via libera (e eventualmente brief denso analogo a 4.1 se vuoi).


---

## [4.2] embedding service hardening: retry, lru cache, batch ready (2026-04-29)

### Cosa fatto
- **`pyproject.toml`** — aggiunte dep `tenacity>=8.2` + `cachetools>=5.3`. Installate nel venv.
- **`core/config.py`** — nuovi knobs: `embedding_backend`, `embedding_cache_size` (1000), `embedding_cache_ttl_seconds` (86400 = 24h), `embedding_max_retries` (3), `embedding_retry_min_wait_seconds` (2.0), `embedding_retry_max_wait_seconds` (10.0), `enable_dev_endpoints` (False).
- **`services/embedding_service.py` rewritten** dalla forma 4.1 (1 funzione + dict cache) a:
  - `EmbeddingBackend` Enum (`openai` / `fake`).
  - `EmbeddingCache` wrapper su `cachetools.TTLCache`. LRU eviction a `max_size`, TTL hygiene. `__contains__`, `__len__`, `stats()` con hit/miss/hit_rate/size/max_size/ttl.
  - `EmbeddingService` class che possiede backend + cache + retry policy + telemetry counter (openai_calls, openai_errors per-type, cost_estimate_usd accumulato).
  - `generate(text)` cache-first con backend dispatch.
  - `generate_batch(texts)` con cache partial-hit + 1 sola chiamata OpenAI per i missing (input array, order preserved).
  - Tenacity `AsyncRetrying`: 3 attempts, exponential backoff 2s/4s/10s capped, retry SOLO su `(APITimeoutError, APIConnectionError, RateLimitError, InternalServerError)`. 4xx (BadRequestError, AuthenticationError) fail immediato. Retry exausto → `EmbeddingServiceUnavailable`.
  - `estimate_cost(text_length)` — $0.02/1M tokens × ~4 chars/token, per telemetry.
  - **Singleton lazy** via `get_embedding_service()`. Backend resolved da `os.environ.get("EMBEDDING_BACKEND", settings.embedding_backend)` al primo call. Test seam: `_reset_singleton_for_tests()`.
  - **Module-level shims preservati** (`generate_embedding`, `generate_embeddings_batch`, `build_embedding_text`, `_clear_cache_for_tests`, `_fake_embedding`, `EmbeddingServiceUnavailable`, `EMBEDDING_DIM`). 4.1 callers non hanno bisogno di churn.
  - OpenAI client **lazy-construct** dentro `_get_openai_client()`. Tests con backend=fake non importano openai mai.
- **`audit_service.py`** — vocabolario `IntentActions` / `MatchActions` / `NegotiationActions` / `DealActions` con codici lowercase verb-noun (`create_intent`, `accept_offer`, `create_deal`, ...). Aligned con `platform_limits.V0_DEFAULT_ALLOWED_ACTIONS` e con `mandate_verifier.record_usage` (che usa il tool action name come `AuditLog.action`). Una sola query `WHERE action='accept_offer'` cattura sia path agent-driven (FASE 5+) sia path user-driven (4.1 → 5.x).
- **`intent_service.py`** — string literals "create_intent"/"update_intent"/"cancel_intent" sostituite con `audit_service.IntentActions.*`.
- **`api/_dev_endpoints.py`** — `GET /api/_dev/embedding-stats`. Route registrato unconditionally, gated per-request da `settings.enable_dev_endpoints` (404 se off). Permette ai test di flippare il flag a runtime senza rebuild dell'app.
- **`main.py`** — router `_dev_endpoints` registrato.
- **`IDEAS_BACKLOG.md`** — entry "Step-up biometrico su PATCH price update (V0.5)" aggiunta sotto Sicurezza/Auth con threshold concreto: ">100 utenti attivi tier=2".
- **`tests/test_intents.py`** — autouse fixture cambiata da `_clear_cache_for_tests()` a `_reset_singleton_for_tests()` (force re-resolve backend env). Test #25 aggiornato a usare la nuova cache API (`get_embedding_service().cache`, `_hash_text(text)` come key, `cache.stats()`).

### Decisioni prese non esplicite nel brief
- **Vocabolario action codes — verb-noun, NON event past-tense**. Il brief 4.2 proponeva UPPER_CASE event-style (`MATCH_CREATED`, `NEGOTIATION_STARTED`). Ho seguito la convenzione esistente di 4.1 e dello schema comment (`create_intent`, `send_offer`, ...) — match con `mandate_verifier` (logga il tool name) + con `platform_limits` (allowed_actions list). Una sola convenzione = una sola query per analytics. Decisione documentata nel docstring di audit_service.
- **Action codes nidificati come classes con `Final[str]` constants** invece di Enum o flat constants. Pattern leggibile (`audit_service.IntentActions.CREATE`), tipizzato (mypy può fare drift detection), nessun import-cycle risk.
- **Cache truthiness gotcha**: ho aggiunto `__len__` a `EmbeddingCache` (`len(cache)` dovuto). Questo rende empty cache falsy. `_make_service(cache=cache)` test helper inizialmente usava `cache or EmbeddingCache(...)` → empty cache veniva sostituita. Fixato a `cache if cache is not None else ...`. Documentato inline.
- **OpenAI exception lazy-import**: `_retryable_openai_exceptions()` importa `openai` solo quando il retry decorator deve risolvere i tipi. Test con backend=fake non caricano openai mai. Stessa filosofia del 4.1 lazy `from openai import AsyncOpenAI`.
- **Singleton telemetry resettable solo via `_reset_singleton_for_tests()`** — non ho aggiunto un reset_stats() pubblico. Il singleton vive per tutto il process; resettare i counter mid-vita confonderebbe le dashboard. Tests che hanno bisogno di stats puliti rebuildano il singleton.
- **Retry waits in test = 0**: `retry_min_wait=0.0, retry_max_wait=0.0` nel test fixture. `wait_exponential(min=0, max=0)` genera attese 0. Test 9 (3 retry) finisce in <100ms.
- **TTL test usa `asyncio.sleep(1.05)` con TTL=1**. Ho considerato injecting un timer custom (cachetools lo supporta) ma ho preferito sleep reale per keep test code minimal — 1.05s aggiunge poco al tempo totale della suite (8.5s).
- **Endpoint dev gated per-request**, non al register-time, così tests possono flippare `settings.enable_dev_endpoints` a runtime via monkeypatch senza ricostruire l'app FastAPI.
- **+1 test "smoke" oltre i 15 della spec** — `test_dev_embedding_stats_endpoint_gated`. Coverage del routing + del gate. Cheap (uses http_client già esistente), niente disparità tra "endpoint c'è e funziona" e "endpoint solo definito".

### Test scritti / coverage
- **16 nuovi test** in `tests/test_embedding.py`:
  - 4 backend (deterministic, uncorrelated, unit-norm, openai mock 1536-dim)
  - 4 cache (hit-avoids-call, miss-stores, LRU eviction, TTL expiration)
  - 3 retry (5xx eventually succeeds, 4xx no-retry, retries exhausted)
  - 3 batch (all inputs, partial cache hit, empty input)
  - 1 cost (proportional to text length)
  - 1 smoke (dev stats endpoint 404→200 gating)
- **Suite totale**: `pytest` → **135 passed in ~9s** (94 pre-4.1 + 25 4.1 + 16 4.2). Nessuna regressione.
- `_FakeOpenAIClient` test helper con queue di response/exception. Riusato da retry tests, batch tests, basic openai mock test.
- OpenAI exception construction usa `httpx.Request`+`Response` mock per soddisfare i required args di `InternalServerError(message, response, body)` e `BadRequestError(message, response, body)`.

### Blocker / dubbi
- **Cost tracking accumulato per-singleton**: i contatori (`openai_calls`, `cost_estimate_usd`) si resettano solo se rebuiltdi il singleton. Per la dashboard di 7.3 (cost monitoring) serve persistere su DB con bucket (per-day, per-hour) — questa è work di 7.3, non 4.2. Per ora il singleton è una telemetry minima per dev-only diagnostic.
- **`enable_dev_endpoints` default False** — producesse il pattern dove un setup di prod malconfigurato potrebbe accidentalmente esporlo. Mitigazione futura: `if app_env == "production" and enable_dev_endpoints: raise ConfigError` in main.py startup. Non urgente per V0 (deploy ancora locale + dev). Aggiunto a IDEAS_BACKLOG implicit (sotto Compliance/Security).
- **`generate_batch` ha 1 caller in V0?** Nessuno. È preparazione per V1+ bulk import da CSV. I test la coprono per evitare regression. Decisione di brief.
- **Cache hit rate atteso V0**: ~5-10% perché ogni intent è semanticamente unico (title + description user-generated). Il valore reale della cache è in test (deterministic) e in update flows che non cambiano title/description. A V1 con bulk import → hit rate ben più alto.
- **Tenacity `AsyncRetrying` vs decorator-based retry**: ho scelto `AsyncRetrying` runtime API perché il retry config viene da settings (per-instance), non da costanti. Decorator-based richiederebbe wrapping più contorto.
- **Non ho rimosso il vecchio test `test_create_intent_uses_cache_for_identical_text` poke su `_cache`** — l'ho aggiornato all'API nuova. Il test verifica ora la stessa proprietà ma via `cache.stats()` e `key in cache`. Più leggibile, meno coupled all'internal state.

### Cosa significa "4.2 completa"
Embedding service è production-ready: retry su OpenAI transient, cache LRU+TTL con telemetry, batch API zero-day per V1 bulk import, cost estimation, dev endpoint per diagnostic, vocabolario audit pre-emptive per FASE 4.3+ pronto. 135 test verdi. **Pronto per 4.3 (Match service)**: la dipendenza chiave (deterministic fake embedding + unit-norm cosine-friendly) è in posto e testata.

### Prossima task
**4.3 Match service** — il primo punto dove il marketplace prende vita. Userà `description_embedding` per cosine similarity tra intent BUY/SELL della stessa categoria con price overlap. Vector index HNSW va creato come migration separata pre-4.3 (vedi IDEAS_BACKLOG § Performance + DQ-3). Attendo via libera + brief denso analogo a 4.1/4.2.


---

## [4.3] match service + hnsw index + scoring (2026-04-29)

### Cosa fatto
- **Migration `e42f1c9ed0a1`** — HNSW index su `intents.description_embedding` (`vector_cosine_ops`, m=16, ef_construction=64). Match score breakdown: aggiunte `price_proximity_score`, `combined_score`. Indici composti filtered su `(buy_intent_id, combined_score DESC) WHERE status='discovered'` + simmetrico sell-side per il hot path "top-N discovered matches per intent".
- **`services/match_service.py`** completo:
  - Pure functions: `compute_price_proximity` (zone-center model, [0,1] clamped), `combine_scores` (0.7 sim + 0.3 price, weights as `Final[float]` constants).
  - `find_matches_for_intent`: 3-layer filter (categoria + side + freshness via SQL → HNSW cosine_distance ranking + oversampling 3x → application-side price-overlap filter + scoring → top-N upsert + audit).
  - `_upsert_match` policy: net-new audit `create_match`, score drift ≥ 0.05 audit `update_match_score`, sub-threshold drift silente (idempotent re-discovery).
  - Read API: `list_matches_for_intent` (owner-only, 404 if non-owner), `get_match_for_user` (owner OR 403).
  - Lifecycle: `mark_match_negotiating` (discovered→negotiating, idempotent, raise on terminal state), `expire_matches_for_intent` (cascade da intent cancel).
- **`services/match_scheduler.py`** — `AsyncIOScheduler` periodico (default 5 min) che ri-scansiona intent con < 3 match. Bound al lifespan FastAPI. Gated da `settings.enable_match_scheduler` (default True; tests non triggerano lifespan via httpx ASGITransport).
- **`api/matches.py`** — 2 endpoint:
  - `GET /api/intents/{id}/matches` (tier ≥ 0, owner-only): list view privacy-aware. Counterparty espone `reservation_price_eur` ma NON `ideal_price_eur` (DQ-31).
  - `GET /api/matches/{id}` (tier ≥ 2): detail view full prices per agent negotiation.
- **`intent_service`** hooks:
  - `create_intent` triggera `find_matches_for_intent` post-commit (best-effort, MatchError swallowed).
  - `update_intent` regenera embedding se title/desc cambia (DQ-32 — fix bug latente di 4.1) + re-triggera matcher se embedding o prezzo cambiano.
  - `cancel_intent` delegata a `match_service.expire_matches_for_intent` (deduplicazione).
- **`audit_service.MatchActions`** — aggiunto `SCORE_UPDATED = "update_match_score"` (oltre a CREATE/EXPIRE già pre-emptive in 4.2).
- **`config.py`** — knobs scheduler: `enable_match_scheduler`, `match_scheduler_interval_minutes` (5), `match_scheduler_batch_size` (50), `match_scheduler_min_matches` (3).
- **`main.py`** — router `matches` registrato. Lifespan start/stop scheduler.
- **`IDEAS_BACKLOG.md`** — entry "Match scheduler → Redis-backed (V0.5+)" + "Match list privacy: nascondere anche reservation_price (V1+)".
- **`DESIGN_QUESTIONS.md`** — DQ-31 (privacy compromise), DQ-32 (embedding regen su update), DQ-33 (scheduler in-process apscheduler).

### Decisioni prese non esplicite nel brief
- **Privacy compromise: reservation visibile, ideal nascosto** (DQ-31). L'utente ha bisogno di vedere "a che prezzo è il match" per capire — Opzione X estrema renderebbe il list view incomprensibile. Reservation è già implicitly leaked dal price overlap (deciding to match implies cap >= floor); ideal è la vera info strategica privata. Compromesso non puro ma difendibile.
- **Embedding regeneration su title/description update** (DQ-32). 4.1 update_intent permetteva modifiche a title/desc senza regenerare l'embedding — bug latente che si manifesta con 4.3 attivo (matcher rankerebbe contro testo stale). Fix: regen sync inline, stesso failure contract di create_intent.
- **Score audit threshold 0.05** — `_AUDIT_SCORE_DELTA` evita audit flood su idempotent re-discovery (stesso embedding, stessi prezzi, score deterministico → no audit). Soglia bassa abbastanza da catturare drift significativo, alta abbastanza da silenziare il noise. Hardcoded constant; tunable se 7.x mostra che è troppo loquace.
- **Owner-only su list view → 404 non 403** quando non-owner. Non leak di esistenza intent altrui. La detail view (tier 2) usa 403 — lì il caller ha già provato ownership di un intent, e 404 sarebbe misleading.
- **Match cascade su intent cancel via match_service.expire_matches_for_intent** (refactor del cancel_intent diretto UPDATE). DRY + audit consistency: la cascade lifecycle vive next to match logic.
- **Detail view fa get + 2x get separati invece di una join eager-load** — più round-trip ma più leggibile. Performance trascurabile a V0 traffic; ottimizzabile in 7.x se metric mostrano problema.
- **Default match limit 20**, max 50. Brief proponeva 20 default. UI probabilmente paginerà a 5-10 — limit alto serve all'agent negotiation in 5.x che ha bisogno di vedere il pool completo per multi-match auctioning (5.2).
- **Oversampling 3x** del HNSW pre-filter: prendiamo top-(limit*3) candidati semantici prima del price filter. Empirically clear il filter per la varianza di V0 spreads. Constant `_OVERSAMPLING_MULTIPLIER`; tunable se vediamo top-N starvation.
- **Test mock vs real pgvector**: tutti i test discovery usano fake embedding deterministic, ma il cosine distance operator gira su pgvector reale (testcontainer). Test 28 esplicita la verifica che cosine ranking funziona end-to-end.
- **`find_matches_for_intent` empty-list su intent missing/inactive** — non raise. Permette "fire-and-forget" call da intent_service hooks. Solo `side='trade'` raise (programmatic error).
- **InvalidMatchTransition (409)** distinto da MatchError generico per `mark_match_negotiating` su terminal state. 5.x error path quando programmatic bug invierà transition errata.

### Test scritti / coverage
- **31 nuovi test** in `tests/test_match.py`:
  - 6 score functions (pure math, no DB)
  - 8 discovery (overlap/no-overlap, self-exclusion, inactive/expired, opposite-side/same-category, trade rejection)
  - 5 persistence (unique constraint, upsert, idempotent, status, score breakdown)
  - 4 lifecycle (cancel cascade, mark_negotiating, invalid transition, scheduler tick)
  - 4 API (owner list, non-owner 404, min_score filter, detail tier-gating)
  - 3 integration (real pgvector cosine ranking, create-intent hook, update-intent re-trigger)
  - 1 privacy (DQ-31 verification)
- **Suite totale**: `pytest` → **166 passed in ~9s** (94 + 25 + 16 + 31). Nessuna regressione.
- Test 23 (scheduler tick) usa monkeypatch su `AsyncSessionLocal` per bind allo stesso connection del test, così le rows seedate sono visibili al tick.

### Blocker / dubbi
- **Schema `Intent.agent_id` ancora dichiarato `nullable=False` nel docstring di alcuni service** — la migration 4.1 (8df1d6891fd9) lo ha rilassato a nullable, e il match service correttamente non assume agent_id. Nessun bug, solo nota.
- **HNSW index recall**: pgvector default `ef_search=40`. Con 100K+ vectors, recall potrebbe scendere. Trigger di tuning: V1 quando dataset grande. Già in IDEAS_BACKLOG.
- **`_upsert_match` non usa `INSERT ... ON CONFLICT`** — split SELECT + INSERT/UPDATE per audit clarity (distinguish create vs score-update). Volumes ≤20 per call, niente preoccupazioni perf.
- **Match scheduler sotto carico**: 50 intent per tick × ogni 5 min = 600/h. A 10K active intents starved, bastano 17h per coprire tutto. Accettabile per V0; trigger di re-design se backlog grows.
- **Test 23 monkeypatching `AsyncSessionLocal`** è invasivo. Funziona per il test ma se in 4.4+ qualcuno aggiungesse logic che dipende dal sessionmaker globale, potrebbe rompersi silenziosamente. Mitigation: documented inline + only used in this single integration test.
- **No test esplicito che HNSW index è actually used** (EXPLAIN check). Test 28 verifica end-to-end correctness, ma non plan optimization. EXPLAIN-based test è 7.x level rigour, sopra 4.3 scope.

### Cosa significa "FASE 4 completa"
Il marketplace è vivo. Tier 0+ utenti possono creare intent, ricevere match automatici (sync alla creazione + scheduled refresh), modificare/cancellare intent con cascade pulita. Embedding deterministic per test, OpenAI in prod con retry. Vector search HNSW. 166 test verdi.

**Pronto per FASE 5 (Negoziazione)** — prima fase dove agenti AI iniziano a negoziare automaticamente. 5.1 Negotiation service, 5.2 mini-asta, 5.3 Deal service.

### Prossima task
**5.1 Negotiation service**. `start_or_continue`, `add_counter_offer`, `accept_offer`, `reject_offer`. Hard cap 6 round (EC6). Tier ≥ 1 per start/counter, tier ≥ 2 per accept (step-up sopra threshold). Match status transition discovered → negotiating → agreed/rejected. Attendo via libera + brief denso analogo.


---

## [5.1] negotiation service primitives + state machine (2026-04-29)

### Cosa fatto
- **`services/negotiation_service.py`** completo:
  - 4 primitive async: `start_or_continue` (tier ≥ 1), `accept_offer` (tier ≥ 2), `reject_offer` (tier ≥ 1), `get_negotiation_state` (tier ≥ 1, party-only).
  - `list_negotiations_for_user` per il list endpoint.
  - `cancel_negotiations_for_intent` cascade hook (chiamato da intent_service.cancel_intent).
  - Pessimistic locking via `select(...).with_for_update()` su Match + Negotiation. Combinato con `UniqueConstraint(match_id)`, le start concorrenti serializzano sul Match lock.
  - JSONB `state` con `turns[]`, `is_final_round`, `final_status`, `agreed_price_cents`. Reassignment esplicito su mutation per il tracking SQLAlchemy.
  - Hard cap `MAX_ROUNDS=6` (V0). `is_final_round=True` quando rounds_used == max-1 — flag letto dal futuro orchestrator (FASE 6) per adattare il prompt "best and final".
  - Match transition: `discovered → negotiating` su prima offerta, `→ agreed` su accept, `→ rejected` su reject.
  - Truncation silenziosa del message a 500 char (no validation). Decisione UX-friendly per mobile clients.
- **Errori tipizzati**: `MatchNotFoundForNegotiation` (404), `InvalidMatchState` (409), `AgentNotOwned` (403), `AgentNotInUsableState` (409), `AgentNotPartyToMatch` (403), `NegotiationNotFound` (404), `NegotiationNotActive` (409), `NegotiationNotForUser` (403), `MaxRoundsReached` (409), `NoOfferToAccept` (409), `CannotActOnOwnOffer` (409), `InvalidPrice` (422).
- **Agent state policy**:
  - `start_or_continue` / `reject` accettano agent in `('active', 'pending_mandate')` — tier 1 con agent pending può iniziare/rifiutare.
  - `accept_offer` richiede agent `'active'` — accept porta verso deal closure (5.3) che richiede mandate firmato.
- **`api/negotiations.py`** — 5 endpoint REST: POST start, POST accept, POST reject, GET id, GET list.
- **`audit_service.NegotiationActions`** — aggiunte costanti `EXPIRE` e `CANCEL` (oltre alle 7 pre-emptive di 4.2). Audit emesso su ogni primitiva via `log_intent_event` (nome storico, helper generic per qualsiasi marketplace event).
- **`intent_service.cancel_intent`** refactor: cascade alle negoziazioni delegata a `negotiation_service.cancel_negotiations_for_intent`. Rimosso direct `update(Negotiation)` + import inutile di `update`. Cleaner per 5.x.
- **`main.py`** — router `negotiations` registrato.

### Decisioni prese non esplicite nel brief
- **Action codes verb-noun preservati**, NON past-tense. Il brief 5.1 proponeva `OFFER_SENT`/`COUNTER_OFFER_SENT`/`OFFER_ACCEPTED`/etc. — sarebbe stata regressione vs convenzione 4.2 approvata. Mantenuto verb-noun matching mandate_verifier + platform_limits + tool_layer naming. Una sola query `WHERE action='accept_offer'` cattura sia user-initiated che future agent-initiated occurrences.
- **`AgentNotOwned` (403) vs `AgentNotPartyToMatch` (403)** — due errori distinti. Owned = agent.user_id != caller (auth boundary, info hiding non importa). Party = caller possiede uno degli intent del match (authorization per match-specific action). Entrambi 403 ma codici distinti per UI clarity.
- **`get_negotiation_state` ritorna 403 (non 404) per non-party**. Razionale: il caller ha già il negotiation_id (legittimamente o meno). 403 dice "esiste, non tuo"; 404 implicherebbe "non esiste" misleadingly. Coerente con pattern di get_match_for_user in 4.3.
- **`accept_offer` richiede agent.status=='active' (no pending)**, mentre start/reject accettano pending. Razionale: accept porta verso 5.3 deal creation che richiede mandate attivo. Tier=2 da JWT è condizione necessaria ma non sufficiente — agent.status è authoritative (DQ-26: post-revoke tier=2 ma agent='revoked'; non si deve poter accettare).
- **Truncation message vs validation**: brief diceva truncate, l'ho seguito. Validation friendlier per V0 mobile (input field può sovra-mandare, server normalizza). Validation più strict si può aggiungere in V0.5 se vediamo abuse.
- **`AcceptResult.next_step="create_deal_in_5_3"` come placeholder string** — handoff esplicito per 5.3. Quando 5.3 implementerà Deal creation, sostituirà con `{"path": "/api/deals", ...}` o equivalente. Per ora il caller della response sa che il deal è da creare nel prossimo step.
- **Concurrency test (#20) non true-concurrent** — pytest async + `join_transaction_mode='create_savepoint'` rende il vero concurrent test problematico (le 2 sessioni condividono la stessa connection wrapper, no real DB-level concurrency). Test verifica l'invariante via Match lock: la seconda call vede Negotiation esistente e appende. La race su INSERT (fallback su UniqueConstraint) è coperta logicamente dal codice + UniqueConstraint declarato; vero stress test di concurrency è infrastruttura V0.5.
- **`negotiation_service.cancel_negotiations_for_intent` non emette audit per-row**. Cascade è event aggregato catturato dal parent `cancel_intent` audit (params.intent_id + result.negotiations_cancelled count). Per-negotiation audit su cancel cascade è feature di V0.5 quando audit volume sarà calibrato.
- **`audit_service.log_intent_event` riusato per negoziazioni** invece di nuovo `log_negotiation_event`. La function è già generic (action+params+result+optional agent_id/mandate_id); il nome `log_intent_event` è 4.1 historical baggage. Renaming diff sarebbe rumoroso (5+ callers); decisione: preservare il nome, documentare nel docstring. 7.x cleanup task per il rename.
- **`AcceptRequest` / `RejectRequest` body con solo `agent_id`** — minimal. Reject opzionalmente `reason`. Niente price re-confirmation (l'API accetta semanticamente l'ultimo turn, no override). Test 11/16 coprono "can't accept own offer".

### Test scritti / coverage
- **28 nuovi test** in `tests/test_negotiation.py`:
  - 8 start/continue (creates new, appends, inactive match, non-party agent, already agreed, rounds increment, is_final_round flag, max_rounds exceeded)
  - 5 accept (marks agreed, requires tier 2 via API, rejects own offer, no offers yet, response carries next_step)
  - 3 reject (marks rejected, requires tier 1 via API, rejects own offer)
  - 3 state/list (party reads history, non-party 403, list only own)
  - 3 concurrency/cascade (concurrent start invariant, intent cancel cascade, agent_not_owned)
  - 2 validation (negative price, message truncation)
  - 4 API surface (POST happy path, start tier-gated, list endpoint, get endpoint)
- **Suite totale**: `pytest` → **194 passed in ~10s** (94 + 25 + 16 + 31 + 28). Nessuna regressione.
- Test factory `_seed_setup` produce un negotiation context complete (seller tier-2 + buyer tier-1/2 + 2 intents + 1 match) in 1 chiamata. Riusato in tutti i 28 test.
- `_seed_tier_1_user_with_agent` helper inline (factories.py non l'aveva). Da promuovere a factories.py se 5.x lo riusa.

### Blocker / dubbi
- **Concurrency test (#20)** verifica invariante Match-lock + UniqueConstraint ma non vera DB-level concurrency. È noto + accettato.
- **`audit_service.log_intent_event` naming** ora è ufficialmente fuorviante (cattura intent, match, negotiation events). Rename a `log_marketplace_event` rinviato a 7.x cleanup — ora 5+ callers, ridurrebbe diff cleanliness.
- **Step-up signature su accept** non implementato in V0. Brief dice rinviato a 5.3 (Deal service). In 5.3 quando Deal viene creato, step-up signature di entrambe le parti sarà richiesto.
- **Match status `expired` non gestito in start_or_continue**. Solo `discovered` e `negotiating` consentiti. Match expired da intent expiry rifiuta start. Test 3 copre `cancelled`; expired ha lo stesso path.
- **Negotiation expiry V0**: nessun auto-expire di negoziazioni inattive >72h. Decisione brief: V0.5+. Match scheduler in 4.3 non tocca negotiations.
- **Helper `_seed_tier_1_user_with_agent` non in factories.py** — duplicato logically con default_user_kwargs(tier=1) + Agent insert. Da promuovere a factories.setup_tier_1_user_async se 5.x/6.x lo riusano.

### Cosa significa "5.1 completa"
Il primo livello di interazione strutturata tra agenti esiste. Da 5.1 in avanti, due utenti tier 1+ possono scambiarsi offerte/contro-offerte su un match, con cap 6 round, lock pessimistico, cascade lifecycle automatica. Tier 2+ può accettare → match.status='agreed', handoff a 5.3 per Deal creation.

194 test verdi. Pronto per **5.2 Mini-asta** (concurrent matches sullo stesso intent: quando una controparte accetta, le altre vengono cancellate; optimistic locking + intent.status race).

### Prossima task
**5.2 Mini-asta logic**. Optimistic locking via `SELECT ... FOR UPDATE` su Intent quando un accept arriva. Cancellazione delle altre negotiations attive sullo stesso intent. EC5 race condition handling. Attendo via libera + brief denso analogo.


---

## [5.2] mini-auction concurrency + intent matched lifecycle (2026-04-29)

### Cosa fatto
- **`negotiation_service.accept_offer` esteso con mini-auction safety**:
  - Lock entrambi gli intent `FOR UPDATE` in **sorted-ID order** (deadlock-prevention cross-transaction).
  - Verifica entrambi `status='active'`; se uno è `'matched'` → `IntentAlreadyMatched` (409). La race è risolta atomicamente sotto lock.
  - Promote entrambi gli intent a `'matched'` (held by deal flow di 5.3).
  - Cascade `_cancel_competing_negotiations`: cancella negoziazioni `active` su match che toccano uno dei due intent (esclusa la corrente). Per-row mutation con `state["cancellation_reason"] = OTHER_MATCH_ACCEPTED` + audit.
  - Cascade `_expire_competing_matches`: marca `expired` i match `discovered`/`negotiating` su quegli intent (escluso il corrente). Audit con reason.
- **`CancelReason` constants** in negotiation_service: `OTHER_MATCH_ACCEPTED`, `INTENT_CANCELLED_BY_USER`, `INTENT_EXPIRED`, `AGENT_REVOKED`. V0 usa solo il primo; gli altri sono pre-emptive per 5.3+ (deal cancel, intent expire sweep, mandate revocation).
- **`IntentAlreadyMatched` (409)** error in negotiation_service. Cross-service usage: anche `intent_service.cancel_intent` lo solleva quando l'utente prova a cancellare un intent già `matched`.
- **`intent_service.cancel_intent`** rifiuta intent `'matched'` con `IntentAlreadyMatched`. `cancelled` resta idempotent. Documenta: "matched intent è held by deal flow; cancel via deal endpoint instead" (5.3).
- **`api/intents.py`** error mapping esteso a `(IntentError, NegotiationError)` tuple per il DELETE handler.
- **`audit_service`** invariato: i constants `NegotiationActions.CANCEL` / `MatchActions.EXPIRE` già esistevano. Il cascade emette audit con `params={"reason": "other_match_accepted"}`.
- **`IDEAS_BACKLOG.md`** aggiornato con due note founder: (1) Rename `log_intent_event → log_marketplace_event` in 7.x. (2) True concurrency stress test pgbench/Locust per V0.5 pre-launch.

### Decisioni prese non esplicite nel brief
- **`IntentAlreadyMatched` definito in `negotiation_service` + cross-imported da `intent_service.cancel_intent`**. Razionale: la transition `active → matched` è scritta atomicamente da `accept_offer`; la classe vive accanto al codice che fa la promotion. `intent_service` la importa quando refusa il cancel. `api/intents.py` cattura entrambe `(IntentError, NegotiationError)` per mapping HTTP. Alternativa scartata: definirla in entrambi services con multiple inheritance — over-engineering.
- **Per-row mutation invece di bulk `UPDATE`** in `_cancel_competing_negotiations` e `_expire_competing_matches`. Costa N round-trip per N matches/negotiations (max ~5 in V0 per intent), ma permette: (a) stamp del `cancellation_reason` nello state JSONB, (b) audit per-row con context. Bulk update farebbe perdere entrambi.
- **Audit cascade emesso col `user_id` dell'accepting party** (l'utente che ha vinto la mini-auction). Razionale: l'audit "cancel_negotiation reason=other_match_accepted" ha responsibility chiaro — è quello che ha causato la cascade. Per `expire_match`, idem.
- **Test 11 (sequential second accept) accetta tuple `(NegotiationNotActive, IntentAlreadyMatched)`** come outcomes validi. Razionale: l'ordine in cui la cascade tocca le rows competing dipende da query plan; la seconda accept potrebbe vedere la sua negotiation già `cancelled` (NegotiationNotActive) PRIMA di arrivare al check intent. Entrambi sono semanticamente corretti — la cascade ha già reso impossibile l'accept. Documentato inline.
- **Sorted-ID lock** invece di lock di un solo intent. Brief proponeva entrambi sotto FOR UPDATE. Sorting prevents deadlock cross-transaction se due accept di intent diversi (ma overlapping su one shared intent) racing. Test 13 verifica funziona indipendentemente dall'ordine lessicografico di buy_intent.id vs sell_intent.id.
- **`expired` match status non più transitable** dopo accept (filter `status.in_(("discovered","negotiating"))` in expire). Test 8 verifica che match già `rejected` resta `rejected`. Coerente con match_service.expire_matches_for_intent (4.3) che ha lo stesso filtro.
- **Test helper `_seed_multi_match` inline in test_mini_auction.py** invece di promuovere a `factories.py`. È il 3° caso di duplicazione (test_match.py + test_negotiation.py + test_mini_auction.py); il founder aveva detto "promotion quando 3+ moduli lo riusano". Tuttavia il helper in ogni file è leggermente diverso (multi-buyer setup qui, single pair lì). Promotion a factories richiederebbe parametrizzazione + risk di rompere test esistenti. Preferito: keep inline per 5.x, fare cleanup unico in 7.x quando lo shape sarà chiaro. Documentato in IDEAS_BACKLOG (test factories consolidation V0.5+).
- **`update_intent` su intent matched fallisce con `IntentNotEditable` (409)**, non con `IntentAlreadyMatched`. Razionale: update_intent gating su `status != "active"` era già presente da 4.1 — rifiuta ANY non-active state (cancelled, matched, expired, ecc.). Test 18 verifica il comportamento. La distinzione `IntentNotEditable` vs `IntentAlreadyMatched` è semantica (former: generic "non-active", latter: specific "matched, blocking your action"); per update il primo è sufficiente.

### Test scritti / coverage
- **18 nuovi test** in `tests/test_mini_auction.py`:
  - 4 single-match regression (both intents matched, vanilla case, unrelated unaffected, lifecycle progression)
  - 6 cascade (cancels competing negotiations, expires competing matches, ignores already-cancelled, ignores terminal-status, only relevant intents, cancellation_reason persisted)
  - 4 race conditions (sequential second accept, disjoint pairs, lock-order invariant, audit with reason)
  - 4 intent state (cannot cancel matched, find_matches skips matched, atomic transition, update rejected on matched)
- **Suite totale**: `pytest` → **212 passed in ~10s** (94 + 25 + 16 + 31 + 28 + 18). Nessuna regressione.
- Test factory `_seed_multi_match(num_buyers=N)` produce 1 seller + N buyers + N matches in 1 chiamata. Riusato in 14 test.
- Test 14 verifica audit row in DB con SQL direct query (`SELECT FROM audit_log WHERE action='expire_match' AND params->>'match_id'=...`). Pattern utile per future audit-coverage tests.

### Blocker / dubbi
- **Test 11 outcome non-deterministic** tra `NegotiationNotActive` e `IntentAlreadyMatched` (entrambi accettati). Vero stress test rivelerebbe che ratio ottenibili in produzione. Non un blocker — entrambi semanticamente corretti.
- **Locking granularity Intent vs Mandate**: ho lockato Intent per V0. Per V1+ multi-agent (DQ-26), un user con N agent attivi potrebbe avere N intent, e una mandate revocation cascade dovrebbe lockare tutto. V0 single-agent: Intent lock è sufficiente. Da rivedere in V1 quando multi-agent attivo.
- **Cascade audit volume**: con 1 accept, ~N audit rows per-row su negotiations + matches expired. Se in V1 ogni intent ha 20 match attivi, accept genera ~40 audit rows. Tunable threshold (analogo a `_AUDIT_SCORE_DELTA` di 4.3) potrebbe ridurre. Per ora: V0 traffic ridotto, full audit OK.
- **`api/intents.py` _to_http accetta union** `IntentError | NegotiationError`. Il pattern funziona ma non è scalabile: in 5.3 aggiungeremo DealError, ecc. Refactoring futuro: introdurre un `MarketplaceError` base class che tutti i service errors estendono. 7.x cleanup task.
- **Test 18 espone bug latente di 4.1?** No — `IntentNotEditable` su matched è il comportamento corretto. Ma il messaggio "intent is in status 'matched', not editable" è ambiguo per il client (suggerisce "puoi editarlo dopo"). UX clarity: in V0.5 quando aggiungeremo Italian copy, distinguere "matched" da "expired" nei messaggi error.

### Cosa significa "5.2 completa"
Mini-auction sicura: il marketplace gestisce N negoziazioni concorrenti sullo stesso intent senza creare deal duplicati. Una sola può chiudere, le altre vengono cancelled atomicamente. Intent in stato `matched` è bloccato (no update, no cancel) finché 5.3 Deal flow non lo rilascia.

212 test verdi. Pronto per **5.3 Deal service** — il punto di chiusura del loop: idempotency key, step-up signature da entrambe le parti, chat E2E pseudonimizzata.

### Prossima task
**5.3 Deal service**. `create_pending_deal` con idempotency_key consumando il next_step di accept_offer. Step-up signatures buyer + seller (passkey-firmate). Match status: `agreed → completed`. Intent status: `matched → closed`. Rollback path: se step-up fallisce, ripristina intent `active`. Attendo via libera + brief denso analogo.


---

## [5.3] deal service + dual signing + e2e chat transport (2026-04-29)

### Cosa fatto
- **Migration `83695fb4e8a6`** — schema reconciliation:
  - Deal: `final_price_cents` → `agreed_price_cents` (rename); `+currency`, `+expires_at` (server_default NOW+24h), `+cancelled_at`, `+cancellation_reason`; status default `pending_buyer` → `pending_signatures`.
  - DealMessage: `encrypted_content` Text → BYTEA (drop+re-add, no live rows); `+nonce BYTEA`; `created_at` → `sent_at`.
  - **Nuova table `deal_signature_drafts`**: shape mandate_drafts-like + discriminator `kind` (`sign` | `cancel`) + `role` (`buyer` | `seller`). Index su (deal_id, user_id, expires_at).
- **`schema.py`** updated to match. Python-side default `expires_at = NOW + 24h` per ORM-inserted Deal rows. New `DealSignatureDraft` model.
- **`audit_service.DealActions`** esteso con `SIGN`, `EXPIRE`, `SEND_MESSAGE` (oltre alle 7 esistenti pre-emptive).
- **`services/deal_service.py`** completo:
  - `create_pending_deal` idempotente via natural key `deal_v1_{negotiation_id}_{agreed_price_cents}`. NO commit (caller in `accept_offer` owns transaction boundary).
  - `request_sign_draft` / `request_cancel_draft` shared via `_create_signature_draft(kind=...)`. Genera challenge + canonical JCS payload, persiste draft con TTL 5min.
  - `submit_signature`: WebAuthn verify → mark party signed → if both signed, transition `pending_signatures → confirmed`. Per-role audit + separate CONFIRM audit when both lands.
  - `submit_cancel`: WebAuthn verify → cancel + `_rollback_deal_state` (intents matched→active, chosen match agreed→discovered).
  - `expire_deal`: scheduler-driven, idempotent, same rollback as cancel with `cancellation_reason='deal_expired'`.
  - 11 typed errors (`DealError` hierarchy) + `DealMessageError` extension.
- **`services/deal_message_service.py`** — chat E2E transport-only:
  - `send_message` / `list_messages`. Server treats `encrypted_content` + `nonce` as opaque bytes (real crypto FASE 11 mobile).
  - V0 caps: 100 messages/deal, 4KB/message. Status check: only `confirmed` deals open chat.
- **`negotiation_service.accept_offer` esteso**:
  - Step 8 nuovo: dopo cascade, chiama `deal_service.create_pending_deal` nello stesso transaction. Atomic accept-and-deal.
  - `AcceptResult` ora carry `deal_id` + `deal_expires_at`. `next_step` cambiato da `"create_deal_in_5_3"` (placeholder) a `"sign_deal_with_passkey"`.
  - Audit `accept_offer` arricchito con `result.deal_id`.
- **`api/deals.py`** — 8 endpoint REST tutti `tier ≥ 2`: GET list/detail, POST sign draft/submit, POST cancel draft/submit, POST/GET messages. Base64 encoding per blob binari nei messages.
- **`match_scheduler`** esteso: nuovo job `expire_pending_deals` (interval 10min) che chiama `deal_service.expire_deal` per ogni Deal `pending_signatures` past `expires_at`. Coesiste con `refresh_low_match_intents` esistente.
- **`mandate_revocation_service`** updated: cascade su deals pending include sia `pending_signatures` (post-5.3) che legacy `pending_buyer`/`pending_seller` per safety. Test esistenti aggiornati (column rename `final_price_cents→agreed_price_cents`).
- **`main.py`** — router `deals` registrato.
- **Test esistenti aggiornati**: `test_revocation.py` (column rename + status string), `test_negotiation.py` (next_step assertion).

### Decisioni prese non esplicite nel brief
- **Schema state default `'pending_signatures'` (singolo stato)** invece di `pending_buyer → pending_seller` ordering. Razionale: i due signature sono semanticamente simmetrici; non c'è "buyer signs first" ordering. Single-state semplifica il codice e i test. Status string capped at 20 chars (schema constraint). Cancel/expire logic also simpler: un solo `pending_signatures` da catturare invece di due valori OR'd.
- **Idempotency key naturale** `deal_v1_{negotiation_id}_{agreed_price_cents}` invece di UUID random. Una negotiation può accettare al massimo una volta (transitional rule); price è il valore agreed. Stessa coppia → stessa Deal row → idempotency by construction. UUID random richiederebbe extra state per detectare duplicates.
- **`expires_at` Python-side default in schema.py** (`lambda: datetime.utcnow() + timedelta(hours=24)`) oltre al server_default in migration. Razionale: ORM-instantiated Deal rows (test seed via Deal(...)) bypass server_default; senza Python default, INSERT fallisce su nullable=False. Server_default rimane per raw SQL inserts. Belt + suspenders.
- **`_rollback_deal_state` re-loka FOR UPDATE** sugli Intent + Match in cancel/expire path. Già lockati da accept_offer in 5.2 ma il txn è committed; rollback è una nuova txn. Defensive lock is cheap insurance.
- **Cancel rollback ripristina solo il chosen match** a `discovered`. I match competing (expired by mini-auction in 5.2) NON vengono ripristinati. Razionale: match scheduler (4.3) li rediscoverà automaticamente quando intent torna `active` con < 3 match. Evita complicazioni di tracking "which matches were affected by the mini-auction".
- **Single `_create_signature_draft` shared per sign + cancel** con `kind` parameter, invece di 2 funzioni dedicate. DRY: validazione status + WebAuthn challenge + payload structure sono identiche; differisce solo `action` field nel canonical payload (`deal_sign` vs `deal_cancel`).
- **Chat transport-only per V0**: encrypted_content / nonce sono opaque blobs. Server NON valida il formato crypto. Documentato che real key exchange è FASE 11.
- **`MAX_MESSAGES_PER_DEAL=100`, `MAX_MESSAGE_BYTES=4096`** hard-coded constants in `deal_message_service`. Da settings se mai V0.5 vuole tunable, ma V0 non vediamo motivo. Caps sono anti-spam + anti-storage-bloat baseline.
- **`cancellation_reason='user_cancelled'` vs `'deal_expired'`** — due valori canonical V0. V1+ può estendere (`buyer_cancelled`, `seller_cancelled`, `dispute_initiated`, ...). Stringe magic ma constants in deal_service module.
- **Audit cascade su `expire_pending_deals` scheduler** emette EXPIRE per-deal. Volume basso (< deals_pending_per_tick), accettabile.
- **`create_pending_deal` non auto-create chat thread**. Chat è gated da `status == 'confirmed'`; thread emerge implicitly al confirm. Niente DealChat row separata — sarebbe overhead per V0.
- **Tutti gli endpoint deals require_tier(2)**. Anche GET list. Razionale: a tier 0/1 non hai mai un deal (accept richiede mandate active = tier 2). Tier check è ridondante ma defensive.
- **`api/deals.py` error mapping cattura solo `DealError`** (non `IntentError` / `NegotiationError` come `cancel_intent`). Razionale: deal endpoints non chiamano intent_service / negotiation_service direttamente; solo deal_service.

### Test scritti / coverage
- **32 nuovi test** in `tests/test_deal.py`:
  - 5 creation (correct fields, idempotent, links, expires_at 24h, status pending)
  - 5 sign draft (buyer ok, seller ok, non-party 403, already-signed 409, non-pending 409)
  - 8 sign submit (valid signature, first doesn't confirm, both confirm, invalid sig 422, expired draft 410, consumed 409, replay flag, audit per-role + confirm)
  - 4 cancel (valid sig, intent rollback, match reset, post-confirm rejected)
  - 3 expire (scheduler tick, intent rollback, partially-signed)
  - 4 chat (send to confirmed ok, pending fails, non-party 403, size cap)
  - 3 concurrency (two-sig serialize, double-cancel rejected, expired-during-sign 410)
- **Suite totale**: `pytest` → **244 passed in ~12s** (94 + 25 + 16 + 31 + 28 + 18 + 32). Nessuna regressione.
- WebAuthn mock pattern: `webauthn_ok` fixture patcha `app.services.deal_service.verify_authentication_response` a SimpleNamespace(new_sign_count=1). Failure case via `_patch_webauthn_raise(monkeypatch, msg)`.
- Test seed `_seed_pending_deal(db)` produce in 1 chiamata: 2 utenti tier-2 + 2 intent + 1 match + 1 negotiation + 1 deal pending. Riusato in 30 test.

### Blocker / dubbi
- **Test esistenti rotti dalla rename `final_price_cents → agreed_price_cents`**: `test_revocation.py` aveva 3 hardcoded references. Sed-fixed inline. Plus `mandate_revocation_service` cascade query updated per supportare entrambi i status string (legacy + new).
- **WebAuthn signing semantica diversa da mandate signing**: mandate è "I authorize this agent". Deal è "I confirm this contract". Stesso transport (passkey assertion + challenge), ma il canonical payload distingue (`action: 'deal_sign'` vs `action: 'mandate_signed'`). Replay across contexts è impossibile per construction (challenge bytes uniche per draft).
- **Scheduler test pattern**: V0 single-process, scheduler driven by FastAPI lifespan. Test 23 (deal expiration) chiama `deal_service.expire_deal` direttamente, bypassa il timer apscheduler. La job-glue logic in `match_scheduler.expire_pending_deals` non è testata direttamente (analogo a 4.3 `test_refresh_low_match_intents`).
- **Cancel rollback non re-triggera match scheduler**: dopo cancel, intents tornano active e chosen match torna discovered. Il match scheduler li rediscoverà al next tick (ogni 5min). Per V0 latency accettabile; V0.5 può aggiungere trigger immediato `find_matches_for_intent` dentro rollback.
- **`api/deals.py` POST messages risponde 201**: status_code=201 per creazione, ma list endpoint risponde 200. Inconsistency minor accettata (REST conventions).
- **`base64` encoding for messages**: client manda b64-encoded encrypted_content + nonce, server decode + persist as bytes. Round-trip via b64 nelle response. V0.5+ potrebbe esporre bytes diretti via multipart/form-data se il throughput lo richiede.

### Cosa significa "5.3 completa" e "FASE 5 chiusa"
Loop end-to-end del marketplace funziona:

1. Tier 0+ utenti creano intent (4.1) → embedding inline (4.2) → match discovery (4.3)
2. Tier 1+ negoziazione turn-based (5.1) con cap 6 round
3. Tier 2 accept atomico → mini-auction cancella concorrenti (5.2)
4. Tier 2 dual-WebAuthn signing → deal confirmed (5.3)
5. Chat E2E pseudonimizzata transport-ready (5.3)
6. Auto-expiration 24h con rollback (5.3)

244 test verdi. Pronto per **FASE 6 (Agent runtime)** — l'orchestrator Claude prende il sopravvento e gli umani diventano "step-up signers" + chat partecipanti invece di operatori manuali.

### Prossima task
**FASE 6 — Agent Runtime**. 6.1 notification service esteso, 6.2 agent_state_service + inbox_service, 6.3 scheduler agent ticks. Il momento dove l'AI agent diventa active player. Modernizzazione tool_layer per async (DQ-28). Attendo via libera + brief denso analogo per 6.1.
