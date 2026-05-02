# IDEAS_BACKLOG.md

> Idee, miglioramenti, e debt tecnico identificato ma non bloccante per V0.
> Non aggiungere al PROJECT_BRIEF se non c'è conferma del founder.
> Quando V0 sarà completo, rivisitare questa lista per planning V1.

---

## Categoria: Auth tokens hardening (V0.5+)

### Refresh token reuse detection: chain-only invalidation (V0.5+ multi-device)

**Trigger**: V0.5+ alpha esterno con user multi-device reali.

**Background**: V0 [7.4.2] simplification: reuse detection invalida ALL active/consumed tokens for user. False positive su multi-device legitimate (un compromise su mobile revoca anche desktop session).

**Action V0.5+**:
- PostgreSQL recursive CTE per walk `parent_id` chain a root + descendants
- Invalidate solo tokens nella chain compromise, niente collateral damage su sessioni altre device
- Test esplicito multi-device scenario (insert 2 chain root distinti per stesso user, verify reuse su una non tocca l'altra)

**Effort**: 1-2 ore (CTE + test isolation per device).

---

### Concurrent refresh load test (V0.5+ pre-launch)

**Trigger**: pre-launch alpha con load testing infrastructure.

**Background**: V0 [7.4.2] usa `SELECT FOR UPDATE` row lock per atomicity rotation. Pattern PostgreSQL standard, empirically affidabile, ma test concurrent in pytest = friction sproporzionata (race reproduction non-deterministica per design).

**Action V0.5+**:
- Setup k6/locust scenario "user calls /api/auth/refresh con stesso token in parallel N times"
- Verify: solo 1 success, N-1 fails con lock conflict (PG returns specific error code o serialization failure)
- Verify: niente orphan token state in DB post-test
- Verify: il vincitore ha rotation completa (old=consumed, new=active)

**Effort**: 1-2 ore (harness setup + scenario + assertions).

---

### Settings field naming convention audit (V0.5+ pre-launch)

**Trigger**: pre-launch alpha o emergence di un altro bug field naming inconsistency.

**Background**: pydantic-settings convention `field_name` → env var `FIELD_NAME` (uppercase). Discrepancies introducono bug silenti — env var ignored, default value triggered, hard-fail al lifespan o silent misbehavior.

Catched 2 volte in FASE 7.4:
- `[7.4.1]`: `kms_master_key_b64` field cercava `KMS_MASTER_KEY_B64` ma docs/error message dicevano `KMS_MASTER_KEY`. Emerged solo durante boot verify [7.4.1.3], hotfix in `[7.4.1.fix]`.
- `[7.4.2]`: `jwt_refresh_ttl_days` setting nome legacy post-format change (refresh non più JWT). Rinominato durante refactor.

**Action V0.5+**:
- Audit `core/config.py` per field naming convention compliance + env var docs alignment
- Pre-commit hook `python -c "from app.core.config import Settings; Settings()"` per intercettare regressioni che breakano boot
- Documentation block in config.py docstring sul mapping convention

**Effort**: 30 min audit + 30 min hook setup.

---

### JWT `kid` header for explicit key ID (V0.5+ refinement)

**Trigger**: V0.5+ scaling con multiple key candidates simultaneously (es. multiple regions, multiple services, N-secret rotation history).

**Background**: V0 [7.4.3] usa "try current then previous" pattern senza `kid`. Worst case 2 attempts decode (~50μs total). Funziona per overlap window 2-secret. Non scala a N-secret né a key catalogs cross-region.

**Action V0.5+**:
- Aggiungere `kid` claim nell'header JWT al sign time (`{"kid": "v3"}`)
- Decode lookup secret by `kid` invece di trial-and-error loop
- Settings da single pair a key catalog: `jwt_secrets: dict[str, str]` mappato `kid → secret`
- Backward compat: token senza `kid` cadono sul fallback loop esistente per X giorni

**Effort**: 2-3 ore (header injection + lookup logic + settings refactor + test).

---

### JWT secret rotation automation (V0.5+ deploy)

**Trigger**: V0.5+ deploy production, esp. multi-replica.

**Background**: V0 [7.4.3] manual rotation via env var update + restart, documentato in `docs/JWT_ROTATION_PROCEDURE.md`. Acceptable single-instance dev/alpha, friction in production multi-replica (atomic env var update cross-replica è non-trivial).

**Action V0.5+**:
- DB-backed secret storage (analogous a KMS pattern V0 [7.4.1])
- Scheduled rotation cron (es. weekly auto-rotate, configurable)
- Auto-retire `previous` post-window (no manual cleanup founder)
- Audit trail: `SecurityActions.JWT_SECRET_ROTATED` constant + log entry
- Multi-replica safe: secret rotation state in DB, ogni replica reads at decode time

**Effort**: 3-4 ore (DB schema + scheduler + audit + multi-replica test).

---

### JWT signing via KMS asymmetric (V1+ enterprise)

**Trigger**: V1+ se compliance richiede HSM-backed signing keys o federation cross-service.

**Background**: V0 HMAC-SHA256 con shared secret in env. Symmetric crypto significa che anyone con verify capability ha anche sign capability — unsuitable per scenarios federated/HSM. V1+ pre-enterprise potrebbe richiedere asymmetric (RS256/ES256) con private key in cloud KMS (AWS KMS Sign API o equivalent).

**Action V1+**:
- Switch HS256 → RS256 (o ES256 per smaller signature)
- Sign via KMS provider Sign API (AWS KMS supporta RSA/ECDSA signing senza esporre private key)
- Public key esposto via JWKS endpoint per verification third-party
- Caching layer JWKS per performance (TTL configurabile)
- Frontend impact: cambia algorithm in JWT header, niente altro client-side

**Effort**: 1 settimana (significant refactor + auth flow change + JWKS endpoint + frontend coordination + test integration end-to-end).

---

### Conftest settings caching: testcontainer effective vs LOCAL DB shadow (V0.5+ refactor)

**Trigger**: scoperto durante diagnosi `[7.4.1.fix]`. Non bloccante per V0 ma anti-pattern test isolation.

**Background**: `_pg_container` fixture setta `POSTGRES_*` env vars MA `app.core.config.settings` è già instanziato durante test collection (test files importano `app.services.*` → chain a `app.core.config`). Risultato: `alembic upgrade head` durante `_pg_container` runa contro DB **locale** (settings cached con localhost values), non testcontainer effective. Tests funzionano per via di transactional rollback su outer transaction, ma è anti-pattern (test pollute local DB).

**Action V0.5+**:
- Set `POSTGRES_*` env vars BEFORE conftest imports any `app.*` module
- Alternative: `pytest_plugins` mechanism per controllo init order
- Verify post-refactor che testcontainer è effective (e.g. assertion che inserted row visibile in testcontainer non in local DB)
- Test count invariate

**Effort**: 1-2 ore (refactor conftest + verify isolation).

---

## Categoria: KMS hardening (V0.5+)

### KMS provider AWS / Vault / GCP swap

**Trigger**: deploy production con multi-replica scaling.

**Background**: V0 `LocalDBProvider` con envelope encryption AES-256-GCM. Master key in env var = OK dev su single host, ma single point of failure per production. Cloud KMS preserves separation of concerns: encryption key never touches application memory (Encrypt/Decrypt API).

**Action V0.5+**:
- Implement `AWSKMSProvider` con boto3 + KMS Encrypt/Decrypt API (alternativa: `VaultProvider`, `GCPKMSProvider`)
- Master key vive in cloud KMS, application never sees it
- Migration backend keys da Local-encrypted a cloud-KMS-encrypted (re-encrypt loop su `kms_agent_keys` table)
- Setting `kms_provider: str = "local" | "aws" | "vault" | "gcp"` con factory dispatch in `app.services.kms.__init__.get_kms()`

**Effort**: 4-6 ore (provider impl + migration script + integration test).

---

### Agent keypair cleanup orphan post-deletion

**Trigger**: V0.5+ implementation di user/agent deletion logic (V0 niente Agent.delete() path).

**Background**: V0 niente FK cascade da Agent a `kms_agent_keys` (deliberate — `kms_ref` opaque). Se Agent viene cancellato, KMSAgentKey resta orphan in DB. Niente immediate impact (storage trascurabile) ma è debt accumulato.

**Action V0.5+**:
- Pre-delete hook: `await kms.revoke(agent.privkey_kms_ref)` prima di `db.delete(agent)`
- `KMSProvider.revoke(db, kms_ref)` aggiunto all'interface
- `LocalDBProvider.revoke()`: hard delete row OR status='revoked' (compliance signal)
- Audit trail: `SecurityActions.KMS_REVOKE` constant + log entry
- Bonus: scheduled job che identifica orphan rows (`kms_agent_keys` senza Agent.privkey_kms_ref matching) per cleanup retroattivo

**Effort**: 30-60 min.

---

### KMS access audit logging

**Trigger**: pre-launch alpha esterno o GDPR compliance review.

**Background**: V0 niente audit per chi accede a quale privkey quando. Per compliance (es. user demanda chi ha firmato cosa), serve trail. `sign()` è ancora placeholder (zero callsite V0), ma quando FASE 5+ A2A messaging lo cabla audit diventa requirement.

**Action V0.5+**:
- `SecurityActions.KMS_SIGN` + `KMS_GENERATE` constants
- Audit log entry su ogni `sign()` call: actor_user_id, kms_ref, timestamp, message_hash (sha256 del payload, no plaintext)
- Audit log entry su `generate_agent_keypair`: user_id, kms_ref creato
- Query audit per user_id → discovery legale "what did this user sign and when"

**Effort**: 1 ora (audit hooks + test).

---

### Granular `KMSError` hierarchy

**Trigger**: caller code che vuole discrimine error type per recovery logic.

**Background**: V0 single `KMSError` OK — caller (`identity_service`) tratta tutto come 500 + transaction rollback. V0.5+ se introduciamo retry logic, fallback path, alerting per categoria error → granular hierarchy facilita.

**Action V0.5+**:
- `KMSMasterKeyError` (lifespan validation, missing/wrong-size master key — startup-only)
- `KMSDecryptError` (auth tag mismatch, key rotation senza re-encrypt — alerting trigger)
- `KMSKeyNotFoundError` (`db.get` returns None — possibly orphan or DB drift)
- `KMSRefError` (malformed `kms_ref` parsing — caller bug signal)
- Tutti subclass di `KMSError` (backward compat: existing `except KMSError` continua a funzionare)

**Effort**: 30 min (refactor exception classes + test update).

---

### `load_master_key` cache for hot signing path

**Trigger**: `sign()` diventa hot path (es. A2A messaging frequente, V0.5+).

**Background**: V0 `load_master_key()` re-decoda + valida settings ogni call. KMS ops V0 sono rare (~1/tier upgrade), zero performance impact. V0.5+ se `sign()` cabla A2A messaging che fanno multipli sign per tick, base64 decode per call diventa overhead misurable.

**Action V0.5+**:
- Module-level `_cached_master_key: bytes | None = None`
- Test fixture invalidation: `clear_master_key_cache()` helper chiamato in `fresh_master_key` fixture teardown
- Threading consideration: GIL protegge module-global assignment, niente lock necessario

**Effort**: 30 min (cache + invalidation + test fixture update).

---

## Categoria: Provider linking (V1.5+)

### OAuth "Collega Claude"
- A V1.5: bottone "Collega Claude" nell'app web/mobile
- OAuth flow → Anthropic, salviamo access_token cifrato
- Orchestrator usa quel token invece del nostro account
- Free tier limitato sui nostri crediti per chi non collega
- Riferimento dettagli: PROJECT_BRIEF §2.8

### OAuth ChatGPT (V2)
- Quando OpenAI maturera l'OAuth flow per terzi
- Stesso pattern di Claude

### API-key fallback Gemini (V2+)
- Google vieta OAuth third-party per Gemini consumer
- Possiamo guidare l'utente a generare API key da AI Studio
- Friction maggiore ma fattibile per power user

---

## Categoria: MCP server pubblico (V2+)

- Esposizione del tool layer come MCP server stand-alone
- Costo dev: 1-2 settimane (lavoro precedentemente fatto si trasferisce)
- Audience: power user con Claude Desktop / Cursor / ChatGPT app
- Storytelling: "primo marketplace consumer A2A-compatible"
- Riferimento: PROJECT_BRIEF §2.7

---

## Categoria: Frontend coordination

### Refresh token rotation update (V0.5+ post-[7.4.2] backend)

**Trigger**: backend `[7.4.2]` pushato (commit on main). Prossimo lavoro frontend FASE 10.1.x.

**Background**: V0 frontend refresh flow legge solo `access_token` da `/api/auth/refresh` response. Post-[7.4.2], endpoint ritorna `{access_token, refresh_token, expires_in_seconds, token_type}` — il refresh è ruotato a ogni use. Se il frontend continua a riusare il vecchio refresh token, il secondo refresh hit triggererà reuse-detection → chain revoked → user forced to re-login.

**Action**:
- Update `RefreshResponse` TypeScript type in frontend a `{access_token, refresh_token, expires_in_seconds, token_type}`
- Update auth store action: persiste new `refresh_token` post-rotation (sostituire stored value, non riusare il vecchio)
- Verify flow E2E: login → refresh → check stored refresh ≠ initial → next refresh use new (NON il vecchio, che ora è 'consumed')
- Bonus: handle 401 con `code: refresh_token_reuse` → forced logout UX (chain revoked, sessione invalidata)

**Effort**: 30 min (1 type + 1 store action + 1 test E2E + 1 error handler).

---

## Categoria: Sicurezza / Auth

### ~~Refresh token rotation (V1+)~~ — DONE in [7.4.2]
- ✅ Implementato in `[7.4.2]`: opaque random token + DB-backed `refresh_tokens` table + rotation on consume + reuse detection con chain invalidation + audit + Prometheus counter.
- Risolto DQ-25.

### Email DB-level partial unique index (7.x)
- Convertire email uniqueness da app-level a DB-level
- `CREATE UNIQUE INDEX ix_users_email_unique ON users (lower(email))`
- Risolve race condition teorica
- Riferimento: DQ-9

### ~~JWT secret rotation strategy (7.4)~~ — DONE in [7.4.3]
- ✅ Implementato in `[7.4.3]`: dual-secret (`current` + `previous`) overlap window pattern + Prometheus counter monitoring + 5-step founder procedure in `docs/JWT_ROTATION_PROCEDURE.md`.
- `kid` claim deferred a V0.5+ (entry "JWT kid header for explicit key ID" in Auth tokens hardening).
- Pre-launch checklist embedded in operational doc.

### WebAuthn config pre-launch (7.4)
- Trigger: pre-launch alpha (deploy pubblico).
- Action:
  - `WEBAUTHN_ORIGIN=https://app.vifaras.com` (o dominio finale)
  - `WEBAUTHN_RP_ID=app.vifaras.com` (rp.id match exact, niente wildcard / subdomain)
  - `WEBAUTHN_RP_NAME=Vifaras` (allinea al rebrand commit dedicato)
  - Verify frontend deploy sullo stesso dominio esatto del rp.id
  - Test signup/login e2e in staging prima di production
- Riferimento: [7.0.1] hotfix default `webauthn_origin` localhost:8000 → :3000.

### Refresh token as Bearer header (V0.5+ enhancement)

**Trigger**: pre-launch alpha quando l'API surface si stabilizza, prima del lock breaking change.

**Background**: V0 `/api/auth/refresh` rate limit è IP-keyed (non per-user) perché refresh_token sta nel body, e slowapi `key_func` è sync — consumare lo stream rompe FastAPI body parsing. 30/min IP è generoso ma non ottimale (un attaccante con refresh token validi multipli da una NAT condivide il bucket con utenti legitimate).

**Action V0.5+**:
- Move `refresh_token` from body to `Authorization: Bearer <refresh>` header
- Update key_func a `user_key` (decodifica refresh token from header invece che access token)
- Apply `rate_limit_auth_refresh` per-user invece di per-IP

**Breaking change**: frontend deve aggiornare logica refresh per inviare token come header invece che body. Coordinare con frontend release.

**Effort**: ~1 ora backend + 30 min frontend + test integration.

**Riferimento**: [7.1.2] deviation documentata nel docstring `auth.py:refresh` endpoint.

### KMS reale (V1+)
- Da file-based ed25519 (V0) a AWS KMS / GCP KMS
- ed25519 supportato nativamente da AWS KMS dal 2025
- Niente migration pesante
- Riferimento: DQ-13

### Privacy docs + LLM data egress (FASE 7.x)
- 6.3 inserisce `get_full_state(agent_id)` come input al prompt Claude. Primo punto in cui dati cliente fluiscono out-of-platform a un provider LLM (Anthropic).
- Da fare in 7.x pre-launch:
  - (a) Inventario campi del state che vanno a Anthropic (mandate, intent, match, negotiations, deals, inbox).
  - (b) Documentare retention Anthropic per API call payloads (verificare ToS attuali).
  - (c) Update privacy policy con menzione esplicita LLM provider + processing legal basis.
  - (d) Valutare field stripping pre-prompt: e.g. nullifier_hash truncation è già fatta (12-char in 6.2), description truncation a 300 char, ma ci sono altri campi che possiamo omettere (es. intent IDs interni → hash).
- Riferimento: 6.2 + 6.3 brief chiusura FASE 6.

### STEP_UP_REQUIRED notification — wire al modernization (FASE 6.3)
- 6.1 ha la notification path preparata in `step_up_service.create_pending_request_sync` ma il callsite (`tool_layer.ToolHandler._queue_step_up`) è scaffold sync, dead-code in V0.
- Quando 6.3 modernizzerà `tool_layer` ad async (DQ-28), il path async di `create_pending_request` emetterà la notifica STEP_UP_REQUIRED automaticamente.
- Niente da fare ora — solo memoria che il loop si chiude in 6.3.
- Riferimento: DQ-28 + brief 6.1 §"STEP_UP_REQUIRED non emesso in V0".

### Pattern @limiter.limit lambda lazy-resolve (preserve)
- Tutti i `@limiter.limit()` decorator devono usare lambda (`@limiter.limit(lambda: settings.rate_limit_X)`), mai stringa diretta.
- Motivazione: stringa è captured at decoration time → monkeypatch nei test non propaga + settings env override a runtime non funzionerebbe.
- Lambda lazy-resolve risolve entrambi.
- Caught in 7.0 mentre debuggavo i rate-limit test (404 invece di 429 perché slowapi catturava il default valore al import-time).
- Riferimento: 7.0 PROGRESS decisioni.

### Step-up biometrico su PATCH price update (V0.5)
- V0 (4.1): tier=2 da solo è sufficient gate per modifiche a `reservation_price_eur` / `ideal_price_eur`. Razionale: passkey già firmata recentemente con il mandate (max 30gg), `intent.status != active` blocca update durante negoziazione, niente settlement = niente impatto monetario diretto.
- Attack surface noto: device sbloccato + JWT valido = attacker può modificare floor/cap senza biometria fresh.
- Mitigazione naturale V0: mandate scade a 30gg, refresh richiede nuovo step-up.
- **Trigger di promozione**: implementare il pattern draft+submit (analogo a mandate revocation) per il PATCH price update **quando >100 utenti attivi tier=2**.
- Riferimento: DQ-29

---

## Categoria: Schema / Migrations

### Schema reconciliation policy (V0.5+ defensive)

**Status**: [7.4.0] one-time reconciliation **COMPLETED** (2026-05-02). 23 drift risolti, future autogenerate produce migration vuote.

**Trigger periodico**: ogni 5 migration nuove o pre-major release.

**Background**: V0 [7.4.0] è snapshot one-time. Drift può ri-accumulare se future migration custom aggiunge DDL non riflesso in `schema.py` (es. raw `op.execute("CREATE INDEX ...")` senza corresponding `__table_args__` update).

**Action periodica**:
1. `uv run alembic revision --autogenerate -m "DRIFT_CHECK" --rev-id zzz_check`
2. Inspect output: `cat backend/migrations/versions/zzz_check_*.py`
3. Atteso: `def upgrade(): pass` + `def downgrade(): pass`
4. Se drift presente: immediate reconciliation con same pattern di [7.4.0] (categorize accidentale/intentional, reflect in model o document)
5. Cleanup: `rm backend/migrations/versions/zzz_check_*.py`

**Tooling possible V0.5+**: pre-commit hook che runna dry-run check su CI, fail se output non è `pass`. Catches drift al PR-time invece di accumular durante development cycles.

**Effort recurring**: 30 min - 2h per cycle, dipende drift accumulato (idealmente zero se policy è seguita).

---

## Categoria: Performance / Optimization

### Vector index HNSW pre-4.3
- Migration separata prima di abilitare 4.3 Match service
- Operatore: `vector_cosine_ops`
- Parametri di partenza: `m=16, ef_construction=64`
- Riferimento: DQ-3

### Denormalize agent_id su Negotiation
- Cascade revoke fa scan via match→intent→agent
- A 100 utenti V0 cheap, a 100K serve denormalizzazione
- Da fare in 7.x quando metriche traffico reali

### Match scheduler → Redis-backed (V0.5+)
- V0: in-process apscheduler `AsyncIOScheduler` per il refresh dei match-starved intents.
- V0.5+: quando deployi su 2+ worker, N processi ticka in parallelo → race su UPSERT match (mitigato da unique constraint ma audit log diventa rumoroso).
- Migration target: Redis SETNX TTL leader-election lock + Celery beat o arq.
- Trigger: prima del deploy su >1 worker.
- Riferimento: DQ-33

### Deal cancel: ripristinare match competing expired (V0.5+)
- Quando un deal viene cancellato/expired, il chosen match torna `discovered`. I match competing che 5.2 mini-auction aveva expired NON vengono ripristinati — match scheduler li rediscoverà al next tick (5 min latency).
- Trigger di promozione: se utenti report "ho perso match dopo cancel deal", aggiungere logica in `_rollback_deal_state` che identifica i match competing tramite query audit log (`MatchActions.EXPIRE` con `params.reason='other_match_accepted'` linked al deal) e li ripristina a `discovered`.
- Costo: tracking + query audit + selective restore. Forse 30-50 LOC.
- Beneficio: zero latenza UX, no "match flicker".

### Match list privacy: nascondere anche reservation_price (V1+)
- V0: compromesso "show reservation, hide ideal" (DQ-31).
- Trigger di revisione: se in V1 vediamo gaming pattern (seller "vedo cap buyer = sparo prezzo alto"), nascondere anche reservation. Mostrare solo `combined_score` + `counterparty.title/category`.
- Costo UX: utente non capisce a che prezzo è il match. Da bilanciare con metriche reali post-launch.
- Riferimento: DQ-31

### Compression mandate signature blob
- ~5KB per mandate, insignificante a 100 utenti
- A 100K-1M mandate diventa 0.5-5GB
- Valutare compression (zstd) in V1+

### Embedding batch async per bulk import (V1+)
- V0: embedding sincrono in-line
- V1: se supportiamo bulk import di intent (CSV upload), batch async via background worker
- Trigger: feature "import inserzioni esistenti da Vinted"

---

## Categoria: Testing & QA

### Integration test orchestrator + step-up resume cycle
- FASE 6 deliverable
- End-to-end: orchestrator chiama tool con step-up trigger → step_up_request creato → utente firma → orchestrator riprende azione con signature
- Coverage gap noto in FASE 2

### WebAuthn assertion shape verification
- Test con assertion contenente campi extra non documentati (`prf`, `largeBlobKey`, `clientExtensionResults`)
- Costo: 5 minuti
- Paga 2 ore di debug all'integrazione mobile
- Da fare prima di FASE 11

### Rename audit_service.log_intent_event → log_marketplace_event
- Function già generic: action+params+result+optional agent_id/mandate_id. Il nome è 4.1 historical baggage; ora cattura intent + match + negotiation events e in 5.3+ catturerà deal events.
- Quando: 7.x cleanup, dopo che 5.2/5.3/6.x avranno popolato i callers (≥10 sites). Prima di V0 launch.
- Diff piccolo (string rename); minimal risk.

### Test coverage debt — service layer gaps (7.4 pre-launch)
- 7.0 CI ha settato threshold 80% (real: 83.78%). Gap noti per ripagare prima dell'alpha launch:
  - `services/mandate_revocation_service.py`: 48% — il revoke flow ha branches non coperti.
  - `services/match_scheduler.py`: 39% — scheduler lifecycle helpers + tick functions.
  - `services/kms_service.py`: 62% — error paths (KMSError variants).
  - `services/auth_service.py`: 62% — register/login flow paths.
  - `services/mandate_service.py`: 65% — submit_signed_mandate edge cases.
  - `api/_dev_endpoints.py`: 56% — gated endpoints (incluso `/scheduler/status` di 6.3.c).
  - `api/deals.py`: 71% — sub-endpoints (sign / cancel / message).
  - `api/step_up.py`: 79% — completion paths.
- Strategy: ri-tightenare CI threshold progressivamente (80 → 83 → 85 → 90) man mano che si chiudono i gap.
- Trigger: 7.4 pre-launch, quando alpha utenti vengono on-boarded e ogni regression è critica.

### True concurrency stress test su negotiation + match service
- V0 ha test "lock-check invariant" che verifica logica ma non vera DB-level concurrency (pytest async + savepoint rendono impossibile real contention).
- Quando: V0.5 pre-launch, con setup pgbench / Locust dedicato.
- Verifica: race accept simultanei, deadlock cross-transaction, throughput sotto N concurrent users.

### Atomic rollback test post-flush
- DQ-16: copertura solo via code review attualmente
- Simulare fallimento durante db.commit() richiede infrastructure di test invasiva
- Rivisitare se mai un bug reale lo richiede

---

## Categoria: Multi-agent V1

### Multi-agent re-creation post-revoca
- DQ-26: tier non degrada, agent va e viene
- Flow: utente tier=2 con agent revoked → endpoint per creare nuovo agent + nuovo mandate
- Niente ri-verifica Self richiesta
- Trigger: prima feature post-V0 launch

### Multi-agent specializzati per categoria
- "Agente compra GPU max €500"
- "Agente vende libri"
- Schema già many-to-one supporta
- UX nuova: dashboard multi-agent con filter per intent

### Plan Pro €19/mese (V1)
- Bundle che sblocca multi-agent + features
- Notifiche Telegram/WhatsApp
- Analytics dashboard
- Priority matching
- Step-up via WhatsApp

---

## Categoria: TRADE / Baratto (V1+)

Vedi `BARTER_DESIGN.md` per dettaglio completo.

### Schema trade-ready V0
- `Intent.side` enum a 3 valori (buy/sell/trade)
- V0 implementa solo buy/sell, trade rifiutato con NotImplementedError
- Schema in posto per V1

### TRADE bilaterale (V1, FASE 8)
- TRADE↔TRADE, TRADE↔SELL/BUY mixed
- Wishlist con priority
- Subjective value theory
- Pareto matching

### Catene multi-hop di baratto (V2)
- Graph cycle detection su embedding semantici
- Storytelling: "il marketplace dove l'AI fa girare gli oggetti senza denaro"

---

## Categoria: Trustee Service (V1.5+)

Vedi `TRADE_WINDOW_FLOW.md` per dettaglio completo.

### Stripe Connect Express integration
- Fase 9
- Marketplace facilitator pattern, Stripe custode legale fondi
- Fee piattaforma 5% del valore deal

### 4 corrieri italiani certificati
- Poste Italiane, BRT, GLS, InPost
- Tracking API integration

### Manual dispute resolution V1.5
- Founder-led, 2-3% dei deal expected
- Tempo medio target: 5 giorni lavorativi

### AI-assisted dispute (V2)
- Vision AI per controllo foto
- Pattern detection cross-nullifier
- Manual review solo per casi ambigui

---

## Categoria: Notification / Push

### APNs / FCM reali (V1)
- V0: console log + polling endpoint
- V1: push notification native iOS+Android
- Trigger: launch mobile app FASE 11

### WebSocket per dashboard real-time
- Step-up requests in real-time invece di polling
- "Match trovato" notifications live
- Trigger: post-PMF V0

---

## Categoria: Observability / Probes (V0.5+)

### Scheduler-level span detach context
- **Trigger**: deploy V0.5+ con OTLP backend reale (Jaeger/Tempo) + need per visualization scheduler→tick relationship.
- **Background**: V0 [7.2.3] ha `agent.tick` come root-span per ogni dispatched tick. `agent_scheduler.discover_and_dispatch_ticks` crea ticks via `asyncio.create_task` fire-and-forget — uno span scheduler-level si chiuderebbe prima dei child tick spans, producendo orphan visualization in tracing UI.
- **Action**: usa OTel `context.attach()` + `detach()` pattern per propagare context al child task asincrono, mantenendo parent-child relationship valida cross-async. Test esplicito che valida nesting span structure.
- **Effort**: 1-2 ore.

### TZ-naive datetime audit
- **Trigger**: pre-launch alpha o pre-deploy multi-region.
- **Background**: durante [7.2.5] discovery scoperto bug `datetime.utcnow().timestamp()` su sistema non-UTC. Naive datetime + `.timestamp()` interpretation è local-TZ-dependent, source di bug latenti. WSL2 UTC+2 ha catturato il caso; deploy su host UTC sarebbe passato silenzioso.
- **Action**: audit codebase per pattern `datetime.utcnow().timestamp()` o `datetime.now().timestamp()` senza tzinfo. Sostituisci con `time.time()` (Unix epoch) o `datetime.now(timezone.utc).timestamp()` (explicit UTC).
  ```bash
  grep -rn "utcnow().timestamp()\|now().timestamp()" backend/app/ --include="*.py"
  ```
- **Effort**: 30 min audit + 15 min test esplicito su sistema TZ ≠ UTC.

### Multi-replica scheduler heartbeat
- **Trigger**: deploy multi-replica backend (V0.5+ horizontal scaling).
- **Background**: V0 readiness check legge `SCHEDULER_LAST_TICK_TIMESTAMP` Prometheus gauge in-memory. Single-process funziona, multi-replica non condivide stato gauge tra istanze.
- **Action**:
  - Persist scheduler last tick a DB column (`system_state` table) o Redis key.
  - Update `_read_scheduler_last_tick_epoch()` helper in `app/api/health.py` a leggere DB/Redis invece di gauge.
  - Boundary già isolato in [7.2.5]: rewrite solo del helper, niente touch a `readiness()`.
- **Effort**: 1-2 ore (migration + helper rewrite + test).

### /metrics endpoint protection
- **Trigger**: pre-launch alpha esterno (deploy pubblico).
- **Background**: V0 `/metrics` è unauthenticated (dev/internal scrape). Pre-launch decidi:
  - Option A: protect via JWT (richiede Prometheus scraper auth)
  - Option B: protect via IP allowlist (Prometheus on private VPC)
  - Option C: separate internal port (8001) con `/metrics`, pubblico solo 8000 con `/api/*` + `/health`
- **Default V0.5+**: Option C (port separation). Standard k8s/Fly.io pattern.
- **Effort**: 1-2 ore.

### Prometheus user_id label cardinality
- **Trigger**: utenti > 1000.
- **Background**: V0 [7.3.4] metrics `vifaras_cost_usd_total{user_id, model}` e `vifaras_cost_user_daily_usd{user_id}` hanno `user_id` come label. Storage Prometheus = O(unique_user_ids), problematic >10K user.
- **Action V0.5+**:
  - Aggregate metric senza `user_id` label (cross-user total)
  - Per-user data via DB query separato (admin endpoint)
  - Top-N user reporting via Prometheus histogram bucket
- **Alternative**: separate "system metrics" (low-cardinality) vs "business metrics" (high-cardinality, opt-in scrape).
- **Effort**: 1-2 ore.

### get_today_cost_usd query optimization (V0.5+ scaling)
- **Trigger**: > 1000 daily user OR observed latency > 50ms su scheduler kill-switch check.
- **Background**: V0 [7.3.2] usa `SUM(total_cost_usd) WHERE date = today`. O(N rows). Index `(user_id, date)` accelera per-user but non aggregate query.
- **Action V0.5+**:
  - Cached aggregate table `daily_cost_global (date PK, total_usd)` updated on each upsert
  - OR materialized view refresh on insert
  - OR Prometheus gauge-based check (no DB hit, but less reliable cross-replica)
- **Effort**: 1-2 ore (depends on path chosen).

### Dynamic Anthropic pricing fetch
- **Trigger**: V0.5+ multi-model usage o pricing change Anthropic non catturato in audit trimestrale.
- **Background**: V0 [7.3.2] pricing hardcoded in `app/services/anthropic_pricing.py`. Audit trimestrale founder responsibility, drift potenziale tra audit cycles.
- **Action V0.5+**:
  - ENV var override: `ANTHROPIC_PRICING_OVERRIDE_JSON` permetti config-driven update senza code change
  - OR fetch periodico da `docs.anthropic.com` pricing endpoint (se exists API)
  - Caching 24h, fallback hardcoded se fetch fallisce
- **Effort**: 1-2 ore.

### Hard cap kill switch refinement
- **Trigger**: pre-launch alpha esterno o post-incident review.
- **Background**: V0 [7.3.3] hard cap globale (`max_daily_llm_cost_usd=$50`) è simple `>= cap: return`. Niente alerting, niente graceful degradation.
- **Action V0.5+**:
  - Webhook/email alert quando cap raggiunto al 80%
  - Graceful degradation: pause new tick dispatch, complete in-flight ticks
  - Auto-reset notification al UTC midnight rollover
  - Post-incident dashboard per investigation
- **Effort**: 2-3 ore.

### Admin endpoint pattern
- **Trigger**: V0.5+ alpha esterno quando emergerà necessità di:
  - Review per-user cost data
  - Manage tier upgrade requests
  - Trigger manual scheduler operations
  - View audit log entries
- **Action V0.5+**:
  - Define admin tier (probabilmente `tier=99` hardcoded, o ENV var `ADMIN_USER_IDS`)
  - Create `app/api/admin.py` router con `dependencies=[Depends(require_tier(99))]`
  - Endpoints: `/api/admin/costs/today`, `/api/admin/users/{id}`, `/api/admin/audit/recent`
  - Auth path: same JWT, additional tier check
- **Effort**: 2-3 ore (admin pattern + 3-5 endpoint + test).

---

## Categoria: Pricing / Billing (V1+)

### Stripe / billing integration (post-alpha monetization)
- **Trigger**: post-alpha se cost cap hits frequenti = signal di willingness to pay.
- **Background**: V0 [7.3.3] niente fatturazione, soft cap è hard skip a `$0.50/day`. Per V1 pricing tier: free $0.10/day, paid $1.00/day, premium $5.00/day.
- **Action V1+**:
  - Stripe customer creation post-signup
  - Subscription plan enforcement (tied to existing `users.tier`)
  - Usage-based billing oltre cap (e.g., $0.10 / 1K tokens overage)
  - Webhook handler per subscription lifecycle (`subscription.created`, `invoice.paid`, etc.)
  - Frontend pricing page + checkout flow
- **Effort**: 1-2 settimane (plan + integration + frontend pricing page + test).

---

## Categoria: Frontend

### Web app Next.js V0 (FASE 10)
- 8-10 schermate principali
- Onboarding Tier 0 / 1 / 2
- Dashboard intent
- Marketplace browse
- Negoziazioni in corso
- Deal history

### Mobile companion (FASE 11)
- React Native iOS+Android
- NFC Self integration
- Step-up biometric
- Deep link handoff da web

### QR code handoff web → mobile (V0.5)
- Per Tier 1 NFC: utente browse da web, scansiona QR, completa NFC su mobile
- Auto-resume sessione web post-verifica

### Per-user costs UI (V0.5+ post-launch alpha)
- **Trigger**: alpha esterno quando user vuole self-service visibility su spend.
- **Background**: V0 [7.3.x] cost data accessibile via dev endpoint (`enable_dev_endpoints` gated) + Prometheus only. Frontend non ha visibility user-side.
- **Action V0.5+**:
  - Backend: `/api/users/me/costs/today` endpoint (auth required, JWT user_id key). Reads `cost_tracking_service.get_user_cost_today`.
  - Frontend: dashboard widget "Today's usage: $0.X / $0.50 cap"
  - Notifica visiva quando user raggiunge 80% cap (warning state)
- **Effort**: 2-3 ore (backend + frontend + test).

### OpenAPI deep documentation pass (FASE 7.4, pre-launch)
- 7.0 ha fatto pass minimale: 4 critical POST endpoint hanno `summary` + `description`. Restano ~75 endpoint (matches, negotiations, deals, notifications, step_up, mandate revocation, dev, test) senza description completa.
- Da fare in 7.4 pre-launch:
  - (a) Pass su ogni endpoint per `summary` + `description` (markdown OK).
  - (b) `responses={...}` con status codes documentati per ogni endpoint (404, 422, 409, 429).
  - (c) Esempi nei Pydantic models via `model_config = ConfigDict(json_schema_extra={"example": ...})`.
  - (d) Verificare che ogni endpoint abbia `response_model` esplicito (la maggior parte ce l'ha già).
- **Trigger**: pre-launch alpha, quando `/openapi.json` verrà esposto a developer/partner esterni o quando il frontend richiede tipi più rigorosi per response examples.
- Costo stimato: 3-4 giorni di lavoro per ~75 endpoint × ~5 fields. Zero feature value pre-frontend, alto value pre-public-launch.
- Riferimento: 7.0 PROGRESS decisioni "OpenAPI pass minimale, non deep".

---

## Categoria: Compliance / Legal

### SELF_VERIFIER_URL produzione
- Confermare URL canonica con Self Labs
- Setup account Self + scope identifier "marketplace-it-v0"
- Riferimento: TODO 2.3 placeholder

### Privacy policy + ToS custom (7.4)
- Non template generici
- Specifici per marketplace agent-mediato
- Coperti AI Act, GDPR, eIDAS, Consumer Rights Directive

### Cookie banner / consent management
- Anche se ZK-by-design riduce PII, serve comunque per email marketing, analytics
- Considerare Plausible o Fathom (privacy-friendly analytics) invece di GA4

### Costituzione SRL italiana (7.4)
- Per accesso benefit startup innovative (sgravi fiscali R&D, semplificazioni)
- Domicilio operativo Italia

---

## Categoria: Marketing / GTM

### Build in public X account
- Post 3-4 volte/settimana sul tema "sto costruendo X"
- Crea audience prima del lancio
- Target: 500-2000 follower al lancio

### Reddit r/Vinted, r/italyinformatica
- Posts organici (non spam)
- Pattern: "ho costruito X per risolvere Y, cerco beta tester"

### TikTok / Reels
- Demo "agente che negozia mentre dormi"
- Format vincente per nicchia flipper

### Hacker News al lancio
- Post tecnico "ZK identity + AI agent commerce"
- Target: front page → 5000 visite + 200 signup

### Press italiana tech
- Wired, Il Post, Repubblica Tech
- Pitch: "primo marketplace agent-native EU"

---

## Categoria: Misc / Founder

### Co-founder GTM/marketing
- Da solo non scala su acquisition
- Search target: ex-founder marketplace consumer EU, growth marketing track record
- Trigger: post FASE 4-5, pre-launch alpha

### MSI laptop come dev/staging server
- Setup: Ubuntu 24.04 server, headless, Tailscale
- Use case: CI runner self-hosted, Self verifier self-hosted (V1.5)
- NOT production: produzione su Fly.io / Railway

### Documenta DESIGN_QUESTIONS.md mensilmente
- Review tutte le DQ deferred, vedere se ancora rilevanti
- Promote a "decided" o droppare
- Ogni 4-6 settimane

---

## Note per il founder

Quando rivisiti questa lista (suggerito: ogni inizio fase, e dopo V0 launch):

1. Promuovi item rilevanti a TODO del PROJECT_BRIEF
2. Drop item ormai irrilevanti (markets cambiano)
3. Aggiungi nuove idee accumulate
4. Tieni il file ordinato per categoria

**Non aggiungere niente di tutto questo al PROJECT_BRIEF senza decisione esplicita.** Il backlog è il backlog, il brief è il piano operativo.
