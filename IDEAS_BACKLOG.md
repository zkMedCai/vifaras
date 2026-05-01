# IDEAS_BACKLOG.md

> Idee, miglioramenti, e debt tecnico identificato ma non bloccante per V0.
> Non aggiungere al PROJECT_BRIEF se non c'è conferma del founder.
> Quando V0 sarà completo, rivisitare questa lista per planning V1.

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

## Categoria: Sicurezza / Auth

### Refresh token rotation (V1+)
- Pattern security best practice: nuovo refresh ad ogni use, blocklist su reuse
- Riferimento: DQ-25
- Trigger threshold: ~500 utenti registrati

### Email DB-level partial unique index (7.x)
- Convertire email uniqueness da app-level a DB-level
- `CREATE UNIQUE INDEX ix_users_email_unique ON users (lower(email))`
- Risolve race condition teorica
- Riferimento: DQ-9

### JWT secret rotation strategy (7.4)
- Rolling key, kid claim, graceful rotation
- Pre-launch checklist obbligatoria

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

### Schema reconciliation pass [URGENT, blocker per future autogenerate]

**Discovered**: [7.1.5 step 1] durante autogenerate per audit_log migration.

**Problem**: `alembic --autogenerate` produce spurious DROP/REWRITE su elementi DB esistenti che non sono dichiarati in model `schema.py`:
- HNSW index `ix_intents_embedding_hnsw` (CRITICAL: required for FASE 4.3 vector match)
- Partial indexes su `matches` table (`ix_matches_buy_intent_discovered_score`, `ix_matches_sell_intent_discovered_score`)
- DESC vs ASC ordering su `ix_notifications_user_*` (model says ASC, DB has DESC)
- `server_default` parametri mancanti su ~10 columns (notifications.payload, daily_cost_tracking.*, users.tier, deals.*)
- `deal_messages.sent_at` NOT NULL discrepancy

**Risk**: future autogenerate mid-flight di altra migration rischia di applicare spurious diff, rompendo features critiche (HNSW = match pipeline).

**Mitigation V0** (immediate): documento in PROGRESS.md di [7.1.5] che ogni autogenerate output va manualmente filtrato per applicare SOLO i diff intenzionali.

**Action V0.5+** (task [7.X] dedicata, ~2-4 ore):
1. Audit complete schema.py vs live DB con `alembic check` o equivalent
2. Per ogni drift, decidere: reflect in model OR document as intentional drift in comments
3. Sync `server_default` parametri
4. Add missing index declarations in model
5. Verify autogenerate produce clean diff (no spurious changes) post-reconciliation

**Trigger**: prima task che richiede nuova migration NON banale (es. nuova table, alter column complex).

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
