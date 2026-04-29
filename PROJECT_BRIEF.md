# PROJECT BRIEF — Agent Marketplace V0

> Documento unico di handoff per Claude Code.
> Leggi tutto prima di scrivere qualsiasi riga di codice.
> Le decisioni di design sono **fissate**: non riaprirle, costruisci sopra di esse.

**Versione**: 1.3
**Data**: aprile 2026
**Stato**: FASE 2 completa, FASE 4 prossima

---

## 0. Cos'è il prodotto

Marketplace **mobile + web** in cui agenti AI autonomi negoziano l'acquisto/vendita di oggetti per conto di umani identificati tramite **zero-knowledge proof** della loro carta d'identità o passaporto.

**Pattern di riferimento**: Project Deal di Anthropic (aprile 2026), commercializzato e con identità ZK invece che dipendenti interni.

**Nome del prodotto**: TBD (placeholder: `MARKETPLACE`).

---

## 1. Stack tecnico (fissato)

| Layer | Tecnologia |
|-------|-----------|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0, Pydantic v2 |
| Database | Postgres 15+ con estensione `pgvector` |
| LLM agente | Claude Sonnet 4.5 via Anthropic SDK (`claude-sonnet-4-5`) |
| Embedding | OpenAI `text-embedding-3-small` (1536 dim) |
| Identity | Self Protocol (zk-passport via NFC) — wrap HTTP del verifier ufficiale |
| Auth utente | WebAuthn passkeys (libreria `webauthn` Python) |
| Web frontend | Next.js 14, TypeScript, Tailwind, shadcn/ui (V0 primary) |
| Mobile | React Native (V0.5+, companion per NFC e step-up) |
| Hosting V0 | Fly.io o Railway |
| Logging | structlog (JSON output) |
| Testing | pytest, testcontainers Postgres (NO SQLite) |
| Scheduler V0 | apscheduler in-process con asyncio |
| Migrations | Alembic |

**No**: Celery/Redis, Kubernetes, microservizi, GraphQL, ORM custom. Tutto monolite Python pulito in V0.

---

## 2. Architettura ad alto livello

Quattro layer indipendenti:

1. **Identity Layer** — Self ZK-proof + WebAuthn passkey + mandate signing
2. **Marketplace Layer** — Intent BUY/SELL/TRADE unificati con matching semantico
3. **Negotiation Layer** — Agenti Claude con tool use, mini-asta, hard cap round
4. **Audit & Compliance Layer** — Log immutabile pseudonimo, contatori limiti, step-up

**Single source of truth**: Postgres. Gli agenti **non** tengono memoria conversazionale tra tick — ricaricano sempre state via tool.

---

## 2.5 Sequenza di onboarding posticipato (CRITICA)

Friction di identity verification spostata al momento di engagement utente. Tre tier con upgrade progressivi.

### I tre tier

**Tier 0 — Anonymous (zero friction)**
- Solo email + passkey (Face ID / Touch ID)
- Può: creare intent BUY/SELL, browse marketplace, ricevere notifiche di match potenziali
- Non può: avviare negoziazioni, accettare offerte, chiudere deal
- Quando lo crea: al primo accesso

**Tier 1 — Identified (friction media, momento hot)**
- Aggiunge: ZK proof via Self Protocol (NFC su carta ID o passaporto)
- Trigger: utente ha almeno 1 match potenziale e vuole avviare negoziazione
- Genera automaticamente: keypair agente con status `pending_mandate`
- Può: tutto Tier 0 + avviare negoziazioni, ricevere offerte

**Tier 2 — Mandated (friction finale, momento decisivo)**
- Aggiunge: firma del primo mandate dell'agente con WebAuthn passkey
- Trigger: prima del primo deal da chiudere
- Attiva agente: `pending_mandate` → `active`
- Può: tutto + accettare offerte, chiudere deal, autorizzare deal sopra soglia

### Conseguenze sul codice

1. **Schema `users`** ha campo `tier` (0/1/2) monotonicamente crescente
2. Endpoint API hanno gating per tier minimo richiesto
3. Quando una request fallisce per tier insufficiente: **HTTP 402 Tier Upgrade Required** con payload del prossimo step
4. La mobile/web app gestisce il 402 mostrando il flow di upgrade in-context

### Tier-credenziale, agent-operatività (DQ-26)

`user.tier` non degrada mai. Post-revoca = tier=2 + agent revoked. Tier rappresenta lo stato di onboarding completato (verifica fatta), non operatività corrente. Solo gli agent vanno e vengono. V1 implementerà multi-agent re-creation senza ri-verifica Self.

---

## 2.7 MCP architectural principle

Il tool layer (`backend/app/agents/tool_layer.py`) è progettato come **protocollo MCP-compatible**.

Ogni tool che l'agente può chiamare è definito con JSON schema standard MCP. Il tool layer è esposto su tre transport possibili:

1. **Diretto in-process** — usato dal nostro AgentOrchestrator interno (V0 default)
2. **REST API** — usato da app mobile/web (V0 primary)
3. **MCP server** — esposto pubblicamente per client MCP esterni (V2+, opt-in)

Il marketplace è la "stazione": infrastruttura standard dove agenti (nostri o di terzi) operano sotto regole codificate. Posizionamento interoperabile con ecosistema agentic (Claude Desktop, Cursor, ChatGPT app), senza richiedere agli utenti consumer di sapere cos'è MCP.

**Per V0**: nessun MCP server pubblico. Solo disciplina architetturale: tool definitions con JSON schema standard, ToolHandler agnostico al transport.

---

## 2.8 Provider account linking via OAuth (V1.5+)

A V1.5 introduciamo "Collega Claude" come prima feature di provider linking, OAuth-based.

| Provider | Subscription consumer | OAuth 3rd-party | Status piano |
|----------|----------------------|-----------------|--------------|
| Anthropic Claude Pro/Max | ✅ esiste | ✅ supportato | V1.5 primary |
| OpenAI ChatGPT Plus | ✅ esiste | ⚠️ in evoluzione | V2 |
| Google Gemini Pro | ✅ esiste | ❌ vietato 3rd-party | V2+ con API-key fallback |

**Free tier V0/V1**: 5 negoziazioni/mese sui nostri crediti Anthropic.
**OAuth tier V1.5+**: utente collega Claude/GPT, illimitato sui suoi crediti.
**Take rate sui deal**: 5-8% blended in tutti i casi. Revenue principale.

Per V0/V1 nessun OAuth: orchestrator gira esclusivamente sui nostri crediti Anthropic.

---

## 2.9 BUY / SELL / TRADE come Intent side

Lo schema `Intent.side` è un Enum a tre valori: `'buy' | 'sell' | 'trade'`.

**V0 implementa solo `buy` / `sell`.** `trade` è schema-ready ma non operativo.

Razionale: cambiare schema in produzione è doloroso, anticipare TRADE costa 30 minuti di lavoro extra in V0, risparmia settimane in V1.

**V1+** implementerà TRADE bilaterale con subjective value theory (vedi `BARTER_DESIGN.md`).

---

## 3. Decisioni di design fissate (NON riaprire)

### Identità
- Provider unico V0: **Self Protocol** via NFC + ZK
- Selective disclosure: chiediamo SOLO `is_adult`, `country`, `document_valid`
- **Mai** memorizzare nome, CF, data nascita, foto
- Identità interna = `nullifier_hash` opaco (popolato solo a tier ≥ 1)
- Email **obbligatoria** a tier 0 per notifiche e recovery, mai come identificatore di marketplace
- Passkey **obbligatoria** a tier 0 per accesso device-based
- Recovery passkey persa: re-scan Self con stesso documento (richiede tier ≥ 1)
- Identity verification è posticipata: vedi sezione 2.5

### Mandate (autorizzazione agente)
- **Whitelist scope**, mai blacklist
- Firmato dalla passkey dell'utente con WebAuthn (RFC 8785 / JCS canonicalization)
- **Step-up obbligatorio** sopra soglie (default €100/deal)
- Auto-revoke per inattività 30 giorni
- **One mandate per agent at a time** (V0). Modifiche richiedono revoca + nuovo mandate
- Hard cap di piattaforma:
  - max €1000/deal
  - max €5000/mese per mandate
  - max 10 deal/giorno
  - max 90 giorni durata mandate
  - geo_scope V0: `["IT"]`
  - categories_forbidden hard: `["adult", "weapons", "alcohol", "drugs", "nft_crypto", "pharmaceuticals", "tobacco"]`

### Marketplace
- Tutto è `Intent` con side BUY/SELL/TRADE
- Ogni Intent ha `reservation_price` (limite) e `ideal_price` (target)
- Matching semantico via embedding (1536 dim, OpenAI `text-embedding-3-small`)
- **Trasparenza zero sui prezzi** (Opzione X): agenti vedono solo i match, non i prezzi altrui
- Multi-match: **mini-asta in parallelo**, accetta migliore
- Hard cap negoziazione: **6 round**, al 5° round forced "best and final"
- Comportamento sotto-ideale: persegui ideal, rispetta floor, chiedi all'utente se floor irraggiungibile dopo metà tempo

### Deal & Pagamento V0
- **V0 NON gestisce denaro**. Sistema crediti / gift card style.
- **Step-up signature da entrambe le parti** per confermare deal (passkey)
- Chat post-deal pseudonimizzata, E2E encrypted, mai PII
- Logistica: delegata agli umani via chat post-deal
- V1.5+: Trustee Service Cardmarket-style (vedi `TRADE_WINDOW_FLOW.md`)

### Compliance
- AI Act: **limited risk by design** (oversight via step-up, audit completo)
- GDPR: by design via ZK (no PII = nessuna erasure complicata)
- eIDAS 2.0 friendly (Self Protocol allineato)

---

## 4. Edge case status

### ✅ Risolti by design
- **EC1 GDPR right-to-erasure** → no PII, niente da cancellare
- **EC2 utente inattivo** → auto-revoke mandate 30gg
- **EC4 passkey persa** → recovery via Self re-scan

### ⚠️ Mitigati con accettazione del trade-off
- **EC3 ban truffatore** → ban del nullifier; può tentare con secondo documento
  (mitigato da pattern detection cross-nullifier in V1)

### ✅ Risolti in implementazione (FASE 2)
- **EC6 agente in loop counter-offer** — hard cap 6 round implementato in `negotiation_service` (FASE 5)

### 🔲 Da risolvere in implementazione futura
- **EC5 race condition due deal simultanei sullo stesso intent**
  → optimistic locking + idempotency_key sulla creazione deal (FASE 5)
- **EC7 Self.xyz / Aztec down**
  → cache della proof verificata, verifica solo a onboarding e step-up
  → graceful degradation, marketplace continua a girare

---

## 5. Componenti già scritti (NON riscrivere senza motivo esplicito)

### Scaffold originale (battle-tested via FASE 1-2)
- `backend/app/models/schema.py` — schema completo SQLAlchemy
- `backend/app/services/mandate_verifier.py` — gate autorizzazione (100% coverage)
- `backend/app/agents/tool_layer.py` — tool definitions + handler dispatch
- `backend/app/agents/orchestrator.py` — loop di Claude con tool use

### Componenti FASE 1-2 implementati
- `backend/app/core/{config,db,security,canonicalization,platform_limits}.py`
- `backend/app/services/{auth_service,identity_service,mandate_service,mandate_revocation_service,step_up_service,kms_service,audit_service,passkey_service,notification_service}.py`
- `backend/app/api/{auth,identity,mandates,step_up,_test_gating}.py`
- `backend/migrations/` — 3 migration applicate
- `backend/tests/` — 94 test verdi, factories module
- `scripts/seed_dev.py`

---

## 6. TODO ordinato

### ✅ FASE 1 — Foundations (completa)

- ✅ 1.1 Setup progetto e infrastruttura base
- ✅ 1.2 Migrazioni database
- ✅ 1.3 Test infrastructure

### ✅ FASE 2 — Identity & Auth (completa)

- ✅ 2.1 Tier 0 anonymous onboarding
- ✅ 2.2 Tier-based gating + login test + auth fixtures
- ✅ 2.3 Tier 1 identity upgrade via Self Protocol
- ✅ 2.4 Tier 2 mandate signing
- ✅ 2.5 Mandate revocation + step-up + auth refresh
- ✅ 2.6 MandateVerifier 100% coverage

> Nota: la vecchia Fase 3 ("Mandate management") è stata fusa nella Fase 2 dato che mandate è parte integrante del flow di tier upgrade.

### ✅ FASE 4 — Marketplace core (completa)

#### ✅ 4.1 Intent service
- `services/intent_service.py`
- `create_intent()` (richiede **tier ≥ 0**) genera embedding via OpenAI **sincrono in-line**
- `update_intent()` modifica reservation/ideal price (richiede step-up se sale, quindi tier ≥ 2)
- `cancel_intent()` chiude intent attivo
- `get_user_intents()` lista intent per utente
- API endpoints: `POST /api/intents`, `GET /api/intents`, `PATCH /api/intents/{id}`, `DELETE /api/intents/{id}`
- Utenti tier=0 ricevono notifiche di match potenziali ma NON possono avviare negoziazioni
- V0 implementa solo `side='buy'` e `side='sell'`. `side='trade'` accetta lo schema ma rifiuta operativamente con `NotImplementedError` (esplicito, documenta che è feature V1+)

#### ✅ 4.2 Embedding service
- `services/embedding_service.py`
- Wrapper su OpenAI `text-embedding-3-small` (1536 dim)
- Cache LRU in-memory per stringhe già viste
- Batch processing per multipli intent simultanei
- Retry con backoff su rate limit
- **Test deterministic**: hash-based fake embedding (vedi `seed_dev.py` pattern)

#### ✅ 4.3 Match service
- `services/match_service.py`
- `find_matches(intent_id)`:
  1. Query intent della parte opposta in stessa categoria, attivi
  2. Cosine similarity sui `description_embedding` (top N)
  3. Filtra dove price_overlap esiste (BUY cap >= SELL floor)
  4. Score combinato: 0.7 * similarity + 0.3 * price proximity
- Persiste nuovi match (UniqueConstraint evita duplicati)
- Job apscheduler che gira ogni 60s e refresh match per intent attivi
- API endpoint: `GET /api/intents/{id}/matches`
- **Vector index HNSW cosine** creato in migration separata pre-4.3 (vedi DQ-3)

### 🔲 FASE 5 — Negoziazione

#### 🔲 5.1 Negotiation service
- `services/negotiation_service.py`
- `start_or_continue(match_id, agent_id, price_cents, message)` (richiede **tier ≥ 1**)
- `add_counter_offer()` (richiede **tier ≥ 1**)
- `accept_offer()` (richiede **tier ≥ 2**)
- `reject_offer()` (richiede **tier ≥ 1**)
- Logica best-and-final automatica al 5° round

#### 🔲 5.2 Mini-asta logic
- N>1 match → offerte parallele
- EC5 race condition: optimistic locking via `SELECT ... FOR UPDATE`

#### 🔲 5.3 Deal service
- `services/deal_service.py`
- `create_pending_deal()` con `idempotency_key`
- Step-up signatures buyer + seller
- Chat E2E pseudonimizzata

### 🔲 FASE 6 — Agent runtime

#### 🔲 6.1 Notification service esteso
- V0: console log + `/api/dev/notifications`
- V1+: APNs / FCM reale

#### 🔲 6.2 Agent state & inbox services
- `services/agent_state_service.py.get_full_state(agent_id)`
- `services/inbox_service.py`

#### 🔲 6.3 Scheduler agent ticks
- apscheduler in-process
- Rate limiting per cost explosion Claude API
- Integration test orchestrator + step-up resume cycle (vedi IDEAS_BACKLOG)

### 🔲 FASE 7 — Hardening & ship

- 7.1 Rate limiting & abuse
- 7.2 Observability
- 7.3 Cost monitoring
- 7.4 Pre-launch checklist (refresh token rotation, JWT secret rotation, email DB-level partial unique)

### 🔲 FASE 8 — TRADE bilaterale (V1)

- TRADE↔TRADE matching
- TRADE↔SELL/BUY mixed
- Subjective value theory: wishlist con priorità + urgency
- Multi-dimensional negotiation (cash adjustment + items)
- Vedi `BARTER_DESIGN.md`

### 🔲 FASE 9 — Trustee Service & escrow (V1.5)

- Integrazione Stripe Connect Express
- 4 corrieri italiani certificati con tracking API (Poste, BRT, GLS, InPost)
- Trustee flow: cash escrow + delivery confirmation
- Trade Window: dual-tracking + dual-confirmation
- Tier reputazione seller
- Postal claim handoff workflow
- Vedi `TRADE_WINDOW_FLOW.md`

### 🔲 FASE 10 — Web frontend V0

- Next.js 14 con App Router
- 8-10 schermate principali (vedi `MANDATE_UX_FLOW.md` per Tier 2)
- WebAuthn cross-device
- Handoff QR code → mobile per Tier 1 NFC

### 🔲 FASE 11 — Mobile companion (V0.5)

- React Native iOS+Android
- Self SDK NFC integration
- Step-up biometric flow
- Push notifications

---

## 7. Principi di codice

- **Type hints ovunque** (Python 3.12+ syntax: `list[str]`, `X | None`)
- **SQLAlchemy 2.0 style** queries (no legacy `query()`, usa `select()`)
- **Pydantic v2** per request/response models
- **Async** dove sensato (endpoint FastAPI, chiamate LLM)
- **Test pytest** per ogni service (almeno happy path + 1 edge case)
- **Niente `print`**, usa `structlog`
- **Niente segreti hardcoded**, tutto via env var con Pydantic settings
- **Money: SEMPRE in cents** (BigInteger), MAI float
- **Datetime: SEMPRE UTC**
- **Niente import circolari** — services possono importare da models, mai viceversa
- **Idempotency keys** su tutte le operazioni che creano Deal o transazioni
- **Audit log su ogni azione di stato** che impatta utente/mandato

### Eccezione scaffold legacy (DQ-1)

Gli scaffold originali (`schema.py`, `mandate_verifier.py`, `tool_layer.py`, `orchestrator.py`) usano stile legacy (`datetime.utcnow()`, `declarative_base()`, `.query()`). Filterwarnings configurati in `pyproject.toml`. Mantenere coerenza interna di ogni file, non globale: scaffold legacy stay, codice nuovo è async/select/Mapped style.

---

## 8. Testing approach

- **Test infrastructure**: testcontainers Postgres (NO SQLite). Lo schema è Postgres-pure (JSONB, UUID, pgvector).
- **Session-scoped lazy container**: si avvia solo quando un test richiede `db_session`. `pytest -m "not db"` istantaneo.
- **Function-scoped session**: con `join_transaction_mode="create_savepoint"`, savepoint per ogni test, rollback a teardown.
- **Mock Anthropic API**: `anthropic_mock` factory con response canned. Inject via `AgentOrchestrator(db, anthropic_client=fake)`.
- **Mock Self Protocol**: `self_verifier_mock` patcha `app.services.identity_service._post_to_self_verifier`. Preset fixtures per scenari comuni.
- **Mock WebAuthn**: helper di sintesi credenziali per test signing flow.
- **Test puramente computazionali**: niente DB, niente container, marker default.
- **Marker `@pytest.mark.db`**: separa fast unit tests da slow integration.

Coverage target: 80%+ sui service, **100% su `mandate_verifier.py`** (sicurezza-critica).

---

## 9. Domande aperte per il founder

Vedi `DESIGN_QUESTIONS.md` per la lista completa. Sintesi delle domande non bloccanti che attendono input founder:

- **SELF_VERIFIER_URL produzione**: confermare URL canonica con Self Labs (DQ-?)
- **Token refresh rotation**: rinviato a 7.4 (DQ-25)
- **Tier=2 dopo revoca**: chiarito DQ-26 (tier non degrada, multi-agent V1)

---

## 10. Workflow operativo

Quando completi una task del TODO:

1. **Aggiorna lo stato** (`🔲` → `✅`)
2. **Commit** con format `[fase.task] descrizione`
3. **Aggiorna `PROGRESS.md`** con: cosa fatto, decisioni fuori brief, test scritti, blocker
4. **Aggiorna `DESIGN_QUESTIONS.md`** se nuove DQ emergono
5. **Fermati** e attendi via libera prima della task successiva

**Una task alla volta. Test obbligatori. Non riaprire decisioni di design.**

---

## Changelog

**v1.3** (post-FASE 2) — Aggiunte sezioni 2.7 (MCP architectural principle), 2.8 (OAuth provider linking V1.5+), 2.9 (Intent.side enum BUY/SELL/TRADE). Aggiunto stack web Next.js (V0). Nuove fasi 8-11 nella roadmap (TRADE V1, Trustee V1.5, Web V0, Mobile V0.5). Stato FASE 1-2 marcato completo. DQ-26 tier-credenziale agent-operatività formalizzato. Documenti satellite referenziati: MANDATE_UX_FLOW.md, BARTER_DESIGN.md, TRADE_WINDOW_FLOW.md, IDEAS_BACKLOG.md.

**v1.2** — Stack web-first + mobile-companion architecture introdotta.

**v1.1** — Sequenza onboarding posticipato (sezione 2.5). Tre tier (0/1/2). Rinumerazione Fase 2 (Identity & Auth fuse con Mandate management).

**v1.0** — Versione iniziale con onboarding monolitico upfront.
