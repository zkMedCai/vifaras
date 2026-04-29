# DESIGN QUESTIONS — Marketplace V0

Tracker delle decisioni di design che si sono presentate durante l'implementazione e di quelle che il founder ha già risolto. Pensato per la rilettura del repo a +4 mesi: non per riaprire decisioni, ma per ricordare *perché* il codice è fatto così.

Convenzione:
- **DECIDED** = il founder ha chiuso la decisione, niente da fare.
- **OPEN** = decisione ancora aperta, richiederà discussione.
- **DEFERRED** = decisione consapevolmente rimandata a una task specifica.

---

## DQ-1 — Tensione scaffold §5 ↔ principi §7 (DECIDED)

**Contesto.** La sezione §5 del brief consegna 4 file scaffold (`models/schema.py`, `services/mandate_verifier.py`, `agents/tool_layer.py`, `agents/orchestrator.py`) con direttiva "leggi e usa, non riscrivere senza motivo esplicito". I principi §7 dicono però:
- "SQLAlchemy 2.0 style queries (no legacy `query()`, usa `select()`)"
- "Datetime sempre UTC, naive evitati (usare `datetime.utcnow()` o `timezone.utc`)" — `datetime.utcnow()` è deprecato 3.12+
- Async dove sensato

Lo scaffold usa il pattern legacy: `declarative_base()`, `Session`, `.query()`, `datetime.utcnow()`.

**Decisione del founder (2026-04-27).** Gli scaffold restano **legacy as-is**. NON riscrivere. Il nuovo codice (services, API, agent runtime) segue §7 pieno: async + `select()` + `Mapped[]` + Pydantic v2.

Motivazione del founder: "la loro logica è già testata mentalmente e funziona; riscriverli ora è solo overhead di stile senza guadagno funzionale". Mantenere coerenza interna di ogni file, non coerenza globale. Pattern brutto ma pragmatico, debt accettato per V0, eventualmente unificato in V1.

**Conseguenze pratiche.**
- `MandateVerifier` resta sync con `.query()`. Servizi async che dovranno chiamarlo manterranno una **sync session pool a parte** (via `run_in_executor`, `anyio.to_thread`, o una dependency `get_sync_db()`).
- I nuovi modelli (se mai ce ne saranno) usano la 2.0 `DeclarativeBase` class style. **Due `Base` classes coesistenti nello stesso schema sono accettabili** in V0.
- Warning di deprecation `datetime.utcnow()` filtrate via `pyproject.toml [tool.pytest.ini_options].filterwarnings` per non sporcare l'output dei test.

---

## DQ-2 — Test architecture: testcontainers, non SQLite (DECIDED)

**Contesto.** Brief §8 dice "Unit test: per service, DB SQLite in-memory". Ma:
- Lo schema è Postgres-pure: `JSONB`, `UUID`, `pgvector.Vector(1536)`.
- I service futuri useranno feature Postgres-specifiche: cosine similarity, JSONB queries su `mandate.scope`, `SELECT FOR UPDATE` per optimistic locking dei deal (EC5).
- TypeDecorator dual-dialect richiederebbe di toccare lo scaffold (vietato da DQ-1) o aggiungere uno strato di astrazione "solo per i test" (debt).

**Decisione del founder (2026-04-27).** Il brief è in tensione con sé stesso su questo punto. **Override**: tutti i test che toccano il DB girano su `pgvector/pgvector:pg16` via testcontainers. Niente SQLite, niente TypeDecorator di compatibilità.

Motivazione: 
1. Schema Postgres-only, lo scaffold non si tocca.
2. Test su SQLite testerebbero un prodotto diverso (i bug più subdoli — JSON vs JSONB — si nascondono lì).
3. testcontainers nel 2026 sono veloci: ~2-3 secondi a container, immagini cached, session-scoped fixture amortizza per tutta la run.

**Pattern di fixture (in `backend/tests/conftest.py`).**
- Session-scoped lazy: `_pg_container` parte solo se un test richiede `db_session` (direttamente o transitivamente). `pytest -m "not db"` non boota nulla.
- Function-scoped: `db_session` apre transaction esterna + Session con `join_transaction_mode="create_savepoint"`. Rollback al teardown — niente cleanup, niente stato condiviso tra test.
- Marker `@pytest.mark.db` su ogni test che tocca il DB. `pytest -m "not db"` per fast-track.

**Eccezione SQLite-style.** Test puramente computazionali (parsing string, aritmetica Decimal, canonicalizzazione JSON) niente fixture, niente container. Plain `def test_xxx(): ...`.

---

## DQ-3 — Vector index su `intents.description_embedding` (DEFERRED → 4.3)

**Contesto.** Lo schema dichiara la colonna `description_embedding Vector(1536)` ma **nessun indice vettoriale**. Per V0 con ~100 utenti il sequential scan è OK — probabilmente più veloce dell'indice su corpus piccoli.

**Decisione del founder (2026-04-27).** Pianificato HNSW con `vector_cosine_ops` da creare in una migration separata **prima di task 4.3 (Match service)**, quando si sa la dimensione del corpus e i parametri ottimali. A 5K-10K vettori HNSW è il default sano (m=16, ef_construction=64).

**Migration nota di promemoria** (da scrivere in 4.x):
```sql
CREATE INDEX intents_description_embedding_hnsw_cosine_idx
  ON intents USING hnsw (description_embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
```

Cosine perché brief §3 Marketplace parla di "Cosine similarity sui description_embedding".

---

## DQ-4 — `/health` vs `/ready` (DEFERRED → 7.x)

**Contesto.** Brief 1.1 chiede "healthcheck endpoint", senza specificare se liveness o readiness.

**Decisione del founder (2026-04-27).** `/health` resta **liveness** (sempre 200 con campo `db: ok|down`). Un endpoint separato `/ready` che ritorna **503** quando il DB è down sarà aggiunto in **task 7.x** (Hardening & ship), pattern K8s-style anche se non siamo su K8s.

---

## DQ-5 — Posizionamento dei platform hard limits del brief §3 (DEFERRED → 2.4 e 4.1)

**Contesto.** Brief §3 elenca limiti hard-coded di piattaforma (max €1000/deal, €5000/mese per mandate, 10 deal/giorno, geo IT, categorie proibite). Sembrano configurazione globale.

**Decisione del founder (2026-04-27).** **NON in `core/config.py`**. Vivono dove sono enforced:
- Cap sui deal/volume → `services/mandate_service.py` (task 2.4) quando l'utente firma un mandate, validando che i suoi limiti non superino i platform caps.
- Limiti su intent attivi → `services/intent_service.py` (task 4.1).

Motivazione: "Config morta è debt, non resilienza."

---

## DQ-6 — Drop dell'estensione `vector` su downgrade Alembic (DECIDED)

**Contesto.** La migration 1.2 fa `CREATE EXTENSION IF NOT EXISTS vector`. Logica simmetrica suggerirebbe `DROP EXTENSION` su downgrade.

**Decisione del founder (2026-04-27).** Il downgrade **NON droppa l'extension**. L'extension è per-database e potrebbe essere usata da altri schema. `CREATE EXTENSION IF NOT EXISTS` è idempotente all'upgrade successivo, quindi nessun problema operativo. `DROP EXTENSION` rischierebbe di rompere altri schema condivisi → trade-off non vale.

---

## DQ-7 — Driver Postgres: doppio sync + async (DECIDED)

**Contesto.** Lo scaffold §5 (`mandate_verifier.py`, `tool_layer.py`, `orchestrator.py`) usa `sqlalchemy.orm.Session` sync. Brief §7 vuole "async dove sensato (endpoint FastAPI, chiamate LLM)". Alembic è natively sync (async è possibile ma rumoroso).

**Decisione del founder (2026-04-27).** `app/core/db.py` espone **due engine sullo stesso DB**:
- `engine` async (asyncpg) → endpoint FastAPI, nuovi service async.
- `sync_engine` (psycopg) → scaffold, Alembic, test.

La coesistenza è intenzionale, non disordine. Documentata in docstring del modulo. Pattern unificato eventualmente in V1 se diventa fastidioso.

---

## DQ-8 — Placeholder valori per `attributes_*` a tier=0 (DECIDED)

**Contesto.** La migration di task 2.1 (e25338f5705c) doveva permettere lo storage di utenti tier=0. Le 2 alter approvate dal founder (`ADD tier`, `DROP NOT NULL` su `nullifier_hash`) coprono `tier` e `nullifier_hash`. Ma `users.attributes_proven` / `attributes_verified_at` / `attributes_expires_at` restano `NOT NULL` per design dello scaffold §5 — e a tier=0 il Self proof non c'è ancora, quindi semanticamente questi campi non hanno valore "vero".

Due strade:
- (A) Estendere la migration a 5 alter (DROP NOT NULL anche su quei 3) → schema più "onesto" ma scope creep oltre la lista esplicita del founder.
- (B) Mantenere lo schema invariato e mettere placeholder nei campi a tier=0 → 2 alter come da piano.

**Decisione.** Strada (B). A tier=0 il `auth_service` popola:
- `attributes_proven = {}` (dict vuoto)
- `attributes_verified_at = NOW`
- `attributes_expires_at = NOW + 1 day`

Questi valori vengono **sovrascritti** in 2.3 quando arriva la verifica Self del proof. Sono placeholder, non semantica reale — non vanno mai letti come prova di qualcosa a tier=0.

**Motivazione.**
- Match con la lista esplicita del founder (2 alter).
- Schema-purity meno importante della scope discipline in V0.
- Il rischio è che qualcuno legga `attributes_verified_at` di un utente tier=0 e pensi "Self verificato" — mitigato dal docstring sul model + il check di tier= 0 prima di leggere.

**Helper centrale.** `app/services/auth_service.py::_tier_0_attribute_placeholders(now)` è l'unico posto dove vengono generati. Il suo docstring spiega esplicitamente che i valori sono **sentinel, non significato** (es. `attributes_proven={}` non significa "user provò un set vuoto", significa "nessuna proof ancora"). Se in futuro si vuole switchare alla strada (A), questo è il singolo punto di rimozione.

---

## DQ-9 — Email uniqueness app-level, non DB-level (DEFERRED → 2.2 o 7.x)

**Contesto.** A tier=0, l'email è l'identificatore di login (visto che non c'è ancora `nullifier_hash`). Lo schema `users.notification_email` è `nullable=True` senza unique constraint. Il `auth_service.begin_registration` fa una check `select` prima di inserire — race condition possibile tra 2 begin/complete simultanei sulla stessa email.

**Decisione (2026-04-27).** Per V0 accettiamo la race app-level. Mitigation:
- Check ridondante anche in `complete_registration` (l'ultima difesa app-level).
- `_normalize_email(email) = email.strip().lower()` chiamato come prima istruzione di ogni public function `auth_service` che riceve email — così `User@gmail.com` e `user@gmail.com` collassano alla stessa identità prima del lookup.
- Tracciato come item di brief §7.4 (pre-launch checklist). Threshold: ~1k+ utenti, dove la race condition diventa statisticamente probabile. Migration prevista: `CREATE UNIQUE INDEX ix_users_email_unique ON users (lower(notification_email)) WHERE notification_email IS NOT NULL` (partial unique, lower-case). NULLs multipli OK (Postgres NULL ≠ NULL).

---

## DQ-10 — Test event loop scope = "session" (DECIDED)

**Contesto.** I test async di 2.1 (httpx AsyncClient + ASGITransport) hanno cominciato a fallire sul secondo test consecutivo con `RuntimeError: Event loop is closed` durante teardown della connection. Causa: l'async engine di `app.core.db` ha un connection pool che persiste per la lifetime del modulo; le connessioni asyncpg si legano al loop in cui sono create; pytest-asyncio default dà un loop fresco per ogni test → connessioni pool create in test 1 sono morte quando test 2 prova a usarle.

**Decisione.** `pyproject.toml [tool.pytest.ini_options]` setta:
```toml
asyncio_default_fixture_loop_scope = "session"
asyncio_default_test_loop_scope = "session"
```

Tutti i test async in una test run condividono lo stesso event loop. Il pool dell'engine è coerente.

**Trade-off.** Niente parallelismo a livello di loop (pytest-xdist richiederebbe loop separati per worker — affrontabile in 7.x se il numero di test cresce). V0 single-process è OK.

**Alternativa scartata.** `NullPool` sull'async engine in fase test: avrebbe richiesto override di `app.core.db` solo per test, fragile. Loop-scope=session è una linea di config.

---

## DQ-11 — Tier 0→1 atomic upgrade: ordering & rollback shape (DECIDED)

**Contesto.** Task 2.3 chiede che `upgrade_user_to_tier_1` sia atomica: o tutto (Self verified + nullifier + attributi + tier=1 + agent keypair) o niente. Sequenza dettata dal founder, replicata in `identity_service.upgrade_user_to_tier_1`:

1. Verifica Self proof **fuori** da qualunque transazione DB (HTTP call lenta dentro tx tiene il pool occupato).
2. `SELECT user FOR UPDATE` per evitare race condition di doppio click sull'app.
3. Idempotency: se `tier ≥ 1` torna `already_upgraded=true` con l'agent esistente.
4. Tier guard: se `tier != 0` raise (stato corrotto).
5. Nullifier collision check (other user → 409).
6. KMS keygen — fatto **dopo** i check ma **prima** delle mutazioni: una KMSError lascia la transazione vuota, rollback è no-op.
7. Mutazioni user (tier, nullifier, attributes, timestamps).
8. INSERT agent con `status='pending_mandate'`.
9. COMMIT.
10. Audit (post-commit, fire-and-forget — vedi DQ-14).

**Perché niente `db.begin_nested()` esplicito**: l'AsyncSession in autobegin (default) apre una transazione al primo execute; l'esplicito `nested` complica il path test (savepoint dentro savepoint dentro outer rollback) senza vantaggi. La transazione singola con commit finale è chiara.

**Document expiry server-side**: anche quando Self risponde `verified=true`, ricontrolliamo `attributes.documentExpiry > now`. Belt-and-suspenders contro un Self che restituisse "verified" su un documento scaduto. Stesso pattern per `isAdult`, `issuingState=="IT"`, `documentValid=true`, scope/userIdentifier echo.

---

## DQ-12 — Test fixture: connection condivisa, sessione fresca per request (DECIDED)

**Contesto.** Task 2.3 ha rivelato un bug nel pattern del fixture `http_client` di 2.1/2.2: con `with_for_update()` nel servizio + sessione condivisa tra più request HTTP nello stesso test, la **seconda** request fallisce con `sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called` durante la creazione del savepoint. La race condition è interna a SQLAlchemy 2.0 async: dopo un `db.commit()` in `join_transaction_mode="create_savepoint"`, il prossimo `execute()` deve auto-aprire un nuovo savepoint, ma l'auto-begin non si trova nel greenlet di `await_only` quando arriva via `with_for_update()`.

**Decisione.** Refactor di `conftest.py`:
- Nuovo fixture `_async_db_connection` (function-scoped): apre `engine.connect()` + `connection.begin()`, yielda la connection, rollback al teardown.
- `async_db_session` ora binda alla connection del fixture sopra (non l'engine). Una sessione, riservata alle assert di test.
- `http_client` apre una **sessione fresca per ogni request HTTP**, sempre bindata alla stessa connection. Mirror del pattern produzione (`get_db` → `AsyncSessionLocal()` per request).

Le scritture restano dentro l'outer transaction → visibili a `async_db_session`. Il rollback al teardown wipa tutto. `with_for_update()` funziona perché ogni request ha autobegin pulito.

**Trade-off.** Più fixture, leggermente più verboso. Ma allinea i test al ciclo di vita reale della session in produzione, eliminando una classe di "funziona in test ma rompe in prod" e viceversa.

---

## DQ-13 — KMS stub V0: file locali ed25519 (DECIDED)

**Contesto.** `Agent.privkey_kms_ref` è `Text NOT NULL` per design — la privkey **non** sta in DB. Per V0 serve un produttore di keypair che persista la privkey altrove e ritorni una reference opaca.

**Decisione del founder (2026-04-28).** "Tu decidi, lo formalizzeremo a V1 con KMS reale." Scelto: file-based.

- `services/kms_service.py` genera ed25519 keypair via `cryptography`. La privkey raw (32B) viene serializzata come b64 in un JSON `{alg, key_id, private_key_b64, public_key_b64}` salvato in `.secrets/agent_keys/<uuid>.json`.
- `kms_ref` ritornato è `file:<path>` — opaco al chiamante (un futuro `arn:aws:kms:...` userebbe lo stesso campo).
- Path traversal guard: refuse se `key_id` contiene `/` o `..`.
- `.secrets/` aggiunta a `.gitignore`.
- KMS_KEYS_DIR configurabile via env var (default `.secrets/agent_keys`).

**Migrazione V1.** Sostituire `kms_service.generate_agent_keypair()` con chiamata AWS/GCP KMS. `kms_ref` diventa `arn:aws:kms:...` o equivalente. Niente data migration (gli ed25519 esistenti restano leggibili dal file system fino a rotazione).

**Sync I/O dentro async** (file write): per V0 file piccoli + 100 utenti il blocco del loop è negligibile. V1 con KMS reale è off-process via HTTP comunque, niente da rifare.

---

## DQ-14 — Audit channel split: structlog vs AuditLog table (DECIDED)

**Contesto.** Schema `AuditLog` ha `mandate_id NOT NULL`. Ha senso: l'AuditLog è specifico per **azioni dell'agente** sotto un mandate attivo (FASE 5+). Ma il tier upgrade succede **prima** che esista un mandate — non c'è mandate_id da scrivere.

**Decisione.** Due canali di audit coesistenti:
1. **Tabella `AuditLog`** — riservata alle azioni agente con `mandate_id`. Verrà popolata dal tool_layer in 5.x.
2. **structlog event `audit.*`** — per eventi identity/lifecycle pre-mandate (tier upgrade, mandate revoke futuro, login se serve). JSON su stdout, namespace `audit.*` filtrabile da log aggregator.

`audit_service.log_tier_upgrade(...)` usa il canale (2). Mai raise: try/except interno + warn fallback. Audit secondario rispetto al commit dell'upgrade.

Suggerimento del founder esplicito (2026-04-28): "silently log-and-continue per V0".

---

## DQ-15 — Persistenza attributes_proven con keys camelCase di Self (DECIDED)

**Contesto.** Self ritorna `{isAdult, issuingState, documentValid, documentExpiry}`. Nello scaffold lo schema parla di `{adult, country, valid}`. Conversione `isAdult→adult`, `issuingState→country`, `documentValid→valid` sembra naturale.

**Decisione.** Persistere il blob **come arriva da Self** (camelCase), niente translation layer. Motivazione:
1. Il JSONB è opaco per il DB; il consumer (servizi futuri di gating) leggerà esattamente quello che Self ha emesso → meno mapping da debuggare.
2. Se Self aggiunge nuovi attributi, il blob li accoglie senza schema change.
3. La docstring dello schema (`{"adult": true, "country": "IT", "valid": true}`) è un esempio non un contratto — DESIGN_QUESTIONS supera quei commenti dove c'è conflitto.

**Conseguenza pratica.** Servizi futuri che leggono `attributes_proven` devono usare `attributes_proven.get("isAdult")` etc. Test 2.3 verifica le keys camelCase persistite. Documentato in commento dello schema in 4.x se necessario.

**Nota di consumer (founder, 2026-04-28).** Inconsistenza con il resto del codebase Python che è snake_case. Per V1: valutare normalizzazione a snake_case nel service layer **prima** di persistere, oppure helper di translation `attributes_proven_to_snake()` per i consumer. Per V0 (UI mobile + audit log) accettata l'inconsistenza in cambio di "blob fedele alla source of truth".

---

## DQ-16 — Atomic rollback test coverage limitato a pre-flush failure (DECIDED)

**Contesto.** Task 2.3 test 7 (`test_atomic_rollback_on_agent_creation_failure`) verifica che se KMS fallisce durante l'upgrade, lo user resti `tier=0` e nessun agent venga creato. Ma il test sfrutta che KMS è chiamato **prima** delle mutazioni — il path "fallimento al `db.commit()` o al flush dell'agent" non è testato.

**Decisione del founder (2026-04-28).** Limitazione consapevole. Il path post-flush è coperto solo via code review (try/except sul commit nel servizio + propagazione standard SQLAlchemy del rollback). Costo di simulare un fallimento al commit (es. constraint violation crafted, connection drop midway, transient DB error) supera il beneficio per V0.

**Riapri quando**: un bug reale in produzione scopre un caso post-flush non rollback-ato. A quel punto: aggiungere fixture che inietta un `flush`/`commit` failure e estendere il test.

---

## DQ-17 — Un mandate attivo per agente alla volta in V0 (DECIDED)

**Contesto.** Schema permette N mandate per agent (FK `mandates.agent_id → agents.id`, no unique). Brief §3 dice "Auto-revoke per inattività 30 giorni" ma non spec'a "mandate concorrenti". Domanda: cosa succede se un utente tier=2 prova a firmare un secondo mandate?

**Decisione del founder (2026-04-28).** "Un mandate alla volta in V0. Se l'utente vuole modificarlo, prima revoca il vecchio (richiederà 2.5), poi crea nuovo. Niente rolling mandate o overlap."

**Enforcement V0**:
- `create_draft` rifiuta se `user.tier >= 2` → `InvalidTierTransition` (409). L'utente è già "fully mandated".
- `submit_signed_mandate` rifiuta se `user.tier >= 2` → `InvalidTierTransition` (409). Defense post-draft anche se la pipe ha più punti d'ingresso in futuro.
- L'agente passa `pending_mandate → active` una sola volta. 2.5 (revocation) lo riporterà a `pending_mandate` o `revoked` per riaprire la pipeline.

**Conseguenza.** Schema NOT NULL su `signature` / `canonical_payload` / `expires_at` resta naturalmente OK. Niente rolling/version-2 mandate finché 2.5 non riapre la finestra.

**V1+ pivot path.** Aggiungere `mandates.is_active` Boolean partial-unique constraint:  
`CREATE UNIQUE INDEX uq_one_active_mandate_per_agent ON mandates(agent_id) WHERE revoked_at IS NULL`. Permette overlap solo se policy lo richiede.

---

## DQ-18 — V0 fixed mandate vocabulary (DECIDED)

**Contesto.** Brief task 2.4 lascia all'utente solo limits + geo + expiry. Le altre parti del payload (`scope.allowed_actions`, `scope.forbidden_actions`, `step_up_required_for`, `categories_forbidden`, `revocation`, `operating_hours`) sono **fissate dal sistema** in V0.

**Decisione.** Tutti i fixed values vivono in `core/platform_limits.py`:
- `V0_DEFAULT_ALLOWED_ACTIONS` — 9 azioni: create_intent, search_intents, send_offer, send_counter_offer, accept_offer, reject_offer, send_message, read_inbox, check_state.
- `V0_DEFAULT_FORBIDDEN_ACTIONS` — modify_reservation_price, delete_account.
- `V0_DEFAULT_STEP_UP_REQUIRED_FOR` — accept_offer above €100, create_intent above €150, modify_reservation_price always.
- `HARD_FORBIDDEN_CATEGORIES` — adult, weapons, alcohol, drugs, nft_crypto, pharmaceuticals, tobacco.
- `REVOCATION_POLICY_V0` — revocable_anytime + auto_revoke 30gg + suspicious_pattern.
- `MANDATE_SPEC_VERSION = "1.0"`.

**Perché fissi.** Riduce surface area di errore: il client mobile non può mandare valori "creativi" che il backend deve validare con tassonomia. Ogni nuova azione richiede code change → review esplicito.

**V1+ evoluzione.** Quando aggiungiamo TRADE/baratto: nuovi action codes (`create_trade_intent`, `propose_swap`) → bump `MANDATE_SPEC_VERSION` a "1.1" + extension del set. La canonicalizzazione si auto-aggiorna (JCS è stable).

---

## DQ-19 — UUID plain (no prefix) per ID di marketplace (DECIDED)

**Contesto.** Brief 2.4 esempi response usano `mnd_01HXYZ...`, `usr_...`, `agt_...` — prefissi tipo Stripe. Lo schema usa `UUID(as_uuid=False)` plain.

**Decisione.** V0 mantiene UUID plain ovunque. Niente prefissi. Motivazione:
- Schema esistente (1.2) usa già UUID plain — re-prefissare richiede migration + changes ovunque.
- I prefissi sono cosmetici per debug (`mnd_` vs `agt_` distingue), non funzionali.
- V1 può aggiungere prefissi via display layer (API serializer) senza toccare il DB — pivot non costoso.

**Conseguenza.** Test 2.4 e response API ritornano UUID plain. La docstring del response model lo nota dove utile. I prefissi negli esempi del brief sono esemplificativi, non normativi.

---

## DQ-20 — Mandate.canonical_payload come Text (UTF-8) invece di BYTEA (DECIDED)

**Contesto.** `mandate_drafts.canonical_payload` è BYTEA (i bytes esatti che la passkey firma). `mandates.canonical_payload` è Text dallo scaffold §5.

**Decisione.** Mantenere Text per `mandates.canonical_payload`, salvare il decode UTF-8 dei bytes JCS (che sono SEMPRE valid UTF-8 per spec RFC 8785). Per verifiche future (audit, replay), re-encodare a bytes con `.encode("utf-8")` — round-trip è bit-identico.

**Perché non riscrivere Text → BYTEA.** Lo scaffold §5 (DQ-1) non si tocca. Modificare ora richiede migration + change downstream (mandate_verifier che legge canonical_payload). Costo>beneficio per V0.

**V1**: in unificazione legacy/modern, valutare BYTEA + storage come bytes nativi. Per ora il cast UTF-8 è esplicito e documentato.

---

## DQ-21 — Bug nello scaffold StepUpRequired (DECIDED + FIX MINIMO)

**Contesto.** `mandate_verifier.py` (§5 scaffold) definiva `StepUpRequired` come `@dataclass`. Il codice circostante però fa `raise StepUpRequired(...)` e `except StepUpRequired as step:` — entrambi richiedono che la classe erediti da `BaseException` / `Exception`. Un `raise` di un `@dataclass` puro fallisce con `TypeError: exceptions must derive from BaseException`.

Il bug era **latente** fino a 2.5: i test 1.3 (mandate_verifier smoke) non esercitavano il path step-up, quindi non si era visto.

**Fix (2026-04-28).** Cambiato `@dataclass class StepUpRequired:` in `class StepUpRequired(Exception)` con `__init__` esplicito che assegna `action`, `params`, `reason` come attributi e chiama `super().__init__(reason)`. Compat con il dataclass-pattern originale: i tre attributi sono ancora accessibili.

**Perché motivato il fix dello scaffold (overrid DQ-1).** DQ-1 dice "non riscrivere senza motivo esplicito". Un `raise` che crasha è motivo esplicito. Il cambiamento è di una riga + un `__init__` di 4 righe — minima invasività, massimo guadagno (ora 2.5 ha tool_layer integration funzionante).

---

## DQ-22 — Step-up: una pending request per (agent, action) (DECIDED, V0 SOFT)

**Contesto.** Niente DB constraint impedisce a un agente di avere più step_up_requests pending per la stessa action contemporaneamente (es. due `accept_offer` sopra threshold mentre il primo è ancora pending). Il client mobile dovrebbe deduplicare; ma se non lo fa?

**Decisione.** V0 accetta la possibilità — la lista in `GET /api/step-up/pending` mostra tutte le pending. Il client può presentarle separatamente o consolidare. Quando l'agente ri-tenta sul prossimo tick, prende la più recente approved e usa quella.

**Trigger di hard-enforcement.** Se in produzione vediamo abuse (agente che genera N step-up sulla stessa action in cascata), aggiungiamo:
- Partial unique index: `CREATE UNIQUE INDEX uq_one_pending_step_up_per_action ON step_up_requests(agent_id, action) WHERE status = 'pending'`
- O check applicativo prima di INSERT in `create_pending_request_sync`.

V0 documenta la limitazione ma non hard-enforce.

---

## DQ-23 — Stub vuoti per intent/match/negotiation/deal_service (DECIDED)

**Contesto.** Lo scaffold §5 `tool_layer.py` importa al module top:
```python
from app.services import (
    intent_service, match_service, negotiation_service, deal_service
)
```

Questi servizi sono pianificati per FASE 4-5. A 2.5 non esistono ancora — l'import fallisce, e qualunque test che importa `tool_layer` (es. test 6 di step-up) non parte.

**Decisione (2026-04-28).** Creati stub vuoti — file `services/{intent,match,negotiation,deal}_service.py` con solo docstring "placeholder until brief task X.Y, real implementation lands in FASE Z". Importabili, ma se una funzione viene mai chiamata fallisce con `AttributeError`. Path-neutral: quando i servizi reali atterrano, sostituiscono lo stub completamente.

**Perché non un import lazy in tool_layer.** Modifying tool_layer per fare lazy import (es. dentro ogni handler) è "rewrite" del scaffold (DQ-1 violation). Stub vuoti sono additivi — niente tocca lo scaffold.

---

## DQ-24 — Revocation cascade status string `cancelled_revoked` (DECIDED)

**Contesto.** Il founder spec'a `cancelled_due_to_revocation` (28 char) come status string per negotiations e deals al revoke. Lo schema scaffold ha `status = Column(String(20))` su entrambi: 28 > 20 → IntegrityError.

**Decisione.** Uso `cancelled_revoked` (17 char) per stare nel limite. Semantica preservata. Conviene **non** ALTER lo schema (rewrite scaffold proibito) per estendere il varchar — la stringa più corta è equivalente.

**V1+ pivot.** Quando si tocca lo schema in modo strutturato (es. unify legacy/modern in 1.x V1), valutare `String(50)` su tutti i campi `status` per più head-room. Per ora `cancelled_revoked` è universale tra negotiations e deals revocati.

---

## DQ-25 — Refresh token rotation rinviata (V0 SOFT, V1 HARDENING)

**Contesto.** `POST /api/auth/refresh` (2.5) ritorna un nuovo access_token ma **lascia il refresh_token invariato**. Best practice security è rotation: ad ogni refresh emetti un nuovo refresh_token, invalida il vecchio (one-time-use). Se un attaccante usa due volte lo stesso refresh, la "rotation reuse detection" invalida la famiglia di token.

**Decisione (founder, 2026-04-28).** V0 senza rotation. Il refresh_token resta valido fino a `exp` (30gg). Una sessione rubata fa danno per max 30 giorni.

**Trigger di hardening (brief §7.4).** Aggiunto come pre-launch item:
- Tabella `refresh_tokens` con `jti`, `user_id`, `revoked_at`, `replaced_by_jti`. 
- Su ogni refresh: insert nuovo token, mark vecchio `revoked_at`. Se reuso un token già revoked → disable user, alert.

Stima 4-6 ore di lavoro a 7.4. Threshold ~500 utenti registrati (founder l'aveva già messo nella checklist 7.4 a 2.2).

---

## DQ-26 — Post-revoke user state: tier resta 2, agent revoked (DECIDED)

**Contesto.** Dopo `POST /api/mandates/{id}/revoke/submit`, il mandate è `revoked`, l'agent è `status='revoked'`, ma `user.tier` resta a 2. È coerente?

**Decisione del founder (2026-04-28).** Sì, **`user.tier` non degrada mai**. Il tier rappresenta lo stato di onboarding completato:
- Tier 0 = email + passkey verificate
- Tier 1 = identità ZK verificata via Self
- Tier 2 = ha firmato almeno un mandate (operatività piena dimostrata)

L'agent rappresenta l'**operatività corrente**. Tier è "credenziale", agent è "stato attivo".

**Conseguenza pratica.**
- Utente tier=2 + agent revoked → resta tier=2, l'identità è verificata. Non deve ri-verificare CIE.
- V1 implementerà flow di "ri-creazione agent + nuovo mandate" senza re-attraversare 2.3.
- V0 = stato dormiente: tier=2 ma niente agent attivo. UI mostra "il tuo agente non è attivo, configurane uno nuovo" (richiede V1).

**V0 limbo accettato.** Per V0 tutti i 100 utenti previsti completeranno il primo mandate; i revocati saranno casi rari (lost device, suspicious activity) e accettano lo stato dormiente fino a V1. Documentato esplicitamente per evitare panico in caso di support ticket "ho revocato e ora non posso fare niente".

---

## DQ-27 — Bug nello scaffold log_failed (DECIDED + FIX)

**Contesto.** `mandate_verifier.log_failed` (§5 scaffold) tenta di scrivere `AuditLog(user_id=None, mandate_id=None, ...)` per il caso `NoActiveMandate`. Lo schema `AuditLog` ha `user_id`, `agent_id`, `mandate_id` tutti `NOT NULL` → INSERT fallisce.

Bug latente fino a 2.6 (test 1.3 e 2.x non esercitavano il path `log_failed` con NoActiveMandate). Stessa categoria di DQ-21.

**Fix (2026-04-28).** `log_failed` ora fa **best-effort lookup del mandate attivo via `agent_id`**:
- Se trova un mandate (caso `ActionNotAllowed`, `LimitExceeded`, `ConstraintViolation`, ecc.): scrive AuditLog completo con `mandate_id`/`user_id` reali.
- Se non trova (caso `NoActiveMandate`): emette evento structlog `audit.action_denied_no_mandate` con `agent_id`, `action`, `error_code`, `message`. Niente DB write.

Coerente con DQ-14 (split AuditLog table per agent actions con mandate, structlog per identity-lifecycle senza mandate).

**Test.**
- `test_log_failed_with_active_mandate_writes_audit_log` — verifica row con error_code, success=False.
- `test_log_failed_without_mandate_does_not_crash` — chiamata su agent senza mandate non solleva, structlog event captured (monkey-patch).

Scaffold cambiato di ~10 righe (best-effort query + branch). Motivato (bug fix, raise di scaffold), come DQ-21.


## DQ-28 — tool_layer._create_intent rinviato a FASE 5/6 (DECIDED, V0 SOFT)

**Contesto (4.1).** Il brief 4.1 dice "estensione tool_layer.py per create_intent tool che ora delega a intent_service invece di stub". Ma:
- `tool_layer.py` è **scaffold legacy sync** (DQ-1): usa `Session.query()` style.
- `intent_service.create_intent` è **async**, prende `user_id` + `CreateIntentInput` (no `agent_id`).

Wirare i due richiede modernizzare tool_layer ad async, oppure aggiungere un sync-wrapper duplicato in intent_service. Il primo è lavoro di FASE 5/6 (orchestrator + agent runtime); il secondo introduce code duplication subito buttata via.

**Decisione (2026-04-29).** Per V0 4.1, **`intent_service.create_intent` è esposto solo dagli endpoint FastAPI** (`POST /api/intents`). `tool_layer._create_intent` solleva `NotImplementedError` con riferimento a questa DQ.

L'agente Claude in V0 non scrive intent autonomamente — gli intent vengono creati dall'utente via UI. Quando in FASE 5/6 implementeremo orchestrator runtime, modernizzeremo tool_layer ad async e wireremo correttamente `_create_intent` allora.

**Implicazioni per il brief.** Lo step-up rule `{"action": "create_intent", "above_eur": 150}` in `V0_DEFAULT_STEP_UP_REQUIRED_FOR` resta nel mandate ma non firea in V0 (l'agent non chiama mai create_intent). Diventerà operativo in FASE 5/6.

**Test.** Nessuno specifico per V0 — il path NotImplementedError non viene esercitato dai test della suite (i test di intent passano via API). Coverage dell'integrazione FASE 5/6.


## DQ-29 — Step-up biometrico su PATCH price update rinviato (V0 SOFT, V0.5 HARDENING)

**Contesto (4.1).** Il brief 4.1 specifica per la modifica di `reservation_price_eur`: "richiede tier=2 + step-up se sopra threshold del mandate". Razionale: cambiare il floor/cap mid-negoziazione è un'azione finanziariamente rilevante che merita conferma biometrica.

**Tensione tecnica.** L'infrastruttura step-up esistente (`step_up_service`) lavora su **azioni dell'agente bloccate dal verifier**: l'agente tenta `accept_offer`, il verifier solleva `StepUpRequired`, viene creato uno `StepUpRequest` row, l'utente firma via push. Per **azioni utente-iniziate** (PATCH /api/intents/{id}) non c'è oggi un meccanismo equivalente di challenge-binding canonicalizzato.

Implementarlo in V0 significherebbe:
- Endpoint `POST /api/intents/{id}/price-update/draft` → `IntentPriceUpdateDraft` row + challenge.
- Endpoint `POST /api/intents/{id}/price-update/submit` → verifica WebAuthn + applica.
- Tabella + migration nuova.
- ~150 LOC + test dedicati.

Out-of-scope per il brief 4.1 che dice "stub minimale embedding service estendibile in 4.2".

**Decisione (2026-04-29).** V0 **gate solo per tier**: `reservation_price_eur` / `ideal_price_eur` update richiede `tier ≥ 2`. Tier 2 implica già passkey registrata + mandate firmato, quindi c'è una conferma biometrica recente "in cassaforte". Non richiediamo una conferma fresh per ogni price update.

**Quando promuovere.** V0.5 (FASE 10/11 frontend), quando saremo in fase di hardening UX e introdurremo il pattern draft+submit canonico per azioni utente-iniziate sensibili (e.g. revocation flow è già così, possiamo riutilizzarlo). Trigger: prima di alpha pubblica. Aggiunto a `IDEAS_BACKLOG.md` § Sicurezza/Auth.

**Test.** `test_update_reservation_price_tier_2_succeeds` verifica solo il gating per tier (no step-up). Quando V0.5 lo introdurrà, aggiungere test challenge+verify.


## DQ-30 — Tier-based active intent cap diverso da mandate.max_active_intents (DECIDED)

**Contesto (4.1).** Il brief 4.1 specifica:
- tier=0: max 5 intent attivi
- tier=1: max 10 intent attivi
- tier=2: limite letto da `mandate.limits.max_active_intents`

`platform_limits.DEFAULT_MAX_ACTIVE_INTENTS = 10` e `MAX_ACTIVE_INTENTS = 20` (hard cap). Tier 0 è 5, NON 10. Volutamente più restrittivo dei tier verificati.

**Razionale.** Tier 0 sono utenti anonimi (solo email + passkey). Permettere a un utente non verificato di spammare 10-20 intent attivi è un vettore di abuso (matching cost, spam visibility). 5 è la soglia "decent demo experience" senza permettere abuso.

**Implementazione.** Costanti separate in `intent_service.py`:
- `TIER_0_MAX_ACTIVE_INTENTS = 5`
- `TIER_1_MAX_ACTIVE_INTENTS = 10`
- Tier 2 → letto da `mandate.limits.max_active_intents` (default 10, hard cap 20 in `platform_limits`)

Test `test_tier_0_max_5_active_intents`, `test_tier_1_max_10_active_intents`, `test_tier_2_uses_mandate_limits`.

**Quando rivedere.** Se vediamo abuse pattern in V0 (utenti tier=0 che riempiono di garbage), abbassare a 3. Se vediamo lamentele di "non posso provare il prodotto" perché 5 è poco, alzare a 7. A/B testabile post-launch.
