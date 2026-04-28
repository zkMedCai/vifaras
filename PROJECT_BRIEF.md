# PROJECT BRIEF — Agent Marketplace V0

> Documento unico di handoff per Claude Code.
> Leggi tutto prima di scrivere qualsiasi riga di codice.
> Le decisioni di design sono **fissate**: non riaprirle, costruisci sopra di esse.

---

## 0. Cos'è il prodotto

Marketplace **mobile** in cui agenti AI autonomi negoziano l'acquisto/vendita di oggetti per conto di umani identificati tramite **zero-knowledge proof** della loro carta d'identità o passaporto.

**Pattern di riferimento**: Project Deal di Anthropic (aprile 2026), commercializzato e con identità ZK invece che dipendenti interni.

**Nome del prodotto**: TBD (placeholder: `MARKETPLACE`).

---

## 1. Stack tecnico (fissato)

| Layer | Tecnologia |
|-------|-----------|
| Backend | Python 3.11+, FastAPI, SQLAlchemy 2.0, Pydantic v2 |
| Database | Postgres 15+ con estensione `pgvector` |
| LLM agente | Claude Sonnet 4.5 via Anthropic SDK (`claude-sonnet-4-5`) |
| Embedding | OpenAI `text-embedding-3-small` (1536 dim) |
| Identity | Self Protocol (zk-passport via NFC) — wrap HTTP del verifier ufficiale |
| Auth utente | WebAuthn passkeys (libreria `webauthn` Python) |
| Mobile | React Native (V0) — sviluppo separato, fuori scope di questo brief |
| Hosting V0 | Fly.io o Railway |
| Logging | structlog (JSON output) |
| Testing | pytest, SQLite in-memory per unit, Postgres reale per integration |
| Scheduler V0 | apscheduler in-process con asyncio |
| Migrations | Alembic |

**No**: Celery/Redis, Kubernetes, microservizi, GraphQL, ORM custom. Tutto monolite Python pulito in V0.

---

## 2. Architettura ad alto livello

Quattro layer indipendenti:

1. **Identity Layer** — Self ZK-proof + WebAuthn passkey + mandate signing
2. **Marketplace Layer** — Intent BUY/SELL unificati con matching semantico
3. **Negotiation Layer** — Agenti Claude con tool use, mini-asta, hard cap round
4. **Audit & Compliance Layer** — Log immutabile pseudonimo, contatori limiti, step-up

**Single source of truth**: Postgres. Gli agenti **non** tengono memoria conversazionale tra tick — ricaricano sempre state via tool.

---

## 2.5 Sequenza di onboarding posticipato (CRITICA)

> Questa è una modifica strutturale rispetto al design originario. La friction di identity verification viene **spostata** al momento di massimo engagement utente, non chiesta upfront. Riduce drop-off di onboarding del 30-50%.

### Principio

L'utente attraversa **tre tier di stato** con friction crescente, ma ogni tier viene chiesto **solo quando necessario**. Mai chiedere ZK prima che l'utente abbia skin in the game emotivo.

### I tre tier

**Tier 0 — Anonymous (zero friction)**
- Solo email + passkey (Face ID / Touch ID)
- Può: creare intent BUY/SELL, browse marketplace, ricevere notifiche di match potenziali
- Non può: avviare negoziazioni, accettare offerte, chiudere deal
- Quando lo crea: al primo accesso

**Tier 1 — Identified (friction media, momento hot)**
- Aggiunge: ZK proof via Self Protocol (NFC su carta ID o passaporto)
- Trigger: quando l'utente ha **almeno 1 match potenziale** sul suo intent E vuole avviare negoziazione
- Messaggio in app: "Hai 3 match potenziali. Verifica la tua identità con la carta d'identità per attivare il tuo agente e iniziare a negoziare. 60 secondi, niente foto, solo NFC."
- Può: tutto Tier 0 + avviare negoziazioni, ricevere offerte

**Tier 2 — Mandated (friction finale, momento decisivo)**
- Aggiunge: firma del primo mandate dell'agente con WebAuthn passkey
- Trigger: prima del **primo deal da chiudere**
- Messaggio in app: "Il tuo agente ha trovato un accordo a €X. Autorizzalo con Face ID per finalizzare. Decidi tu i limiti: per quale importo massimo l'agente può chiudere, per quanti giorni, ecc."
- Può: tutto + accettare offerte, chiudere deal, autorizzare deal sopra soglia

### Conseguenze sul codice

1. **Schema `users`** ha un campo `tier` (`0`, `1`, `2`) che si incrementa monotonicamente.
2. Endpoint API hanno **gating per tier minimo richiesto**:
   - `POST /api/intents` → tier ≥ 0
   - `POST /api/negotiations/start` → tier ≥ 1
   - `POST /api/deals/sign` → tier ≥ 2
3. Quando una request fallisce per tier insufficiente, l'API ritorna **`HTTP 402 Tier Upgrade Required`** con il payload del prossimo step di onboarding (Self challenge, mandate draft, ecc.).
4. La mobile app gestisce il 402 mostrando il **flow di upgrade in-context** (modal, non redirect).

### Cosa NON cambia

- Schema dati: `User.nullifier_hash` resta **nullable** finché tier=0, popolato a tier=1.
- Mandate: invariato, viene firmato solo a tier=2.
- Agente: creato automaticamente all'upgrade a tier=1 (keypair generato, attivazione differita finché mandate firmato).
- Audit log: invariato, traccia ogni transizione di tier.

### Nuovi requisiti di test

- Test che un utente tier=0 può creare intent ma riceve 402 su `negotiations/start`.
- Test che un utente tier=1 senza mandate riceve 402 su `deals/sign`.
- Test del flow completo tier 0 → 1 → 2 con upgrade graceful.

---

## 3. Decisioni di design fissate (NON riaprire)

### Identità
- Provider unico V0: **Self Protocol** via NFC + ZK
- Selective disclosure: chiediamo SOLO `is_adult`, `country`, `document_valid`
- **Mai** memorizzare nome, CF, data nascita, foto
- Identità interna = `nullifier_hash` opaco (popolato solo a tier ≥ 1)
- Email **obbligatoria** a tier 0 per notifiche e recovery, mai come identificatore di marketplace
- Passkey **obbligatoria** a tier 0 per accesso device-based
- Recovery passkey persa: re-scan Self con stesso documento (richiede tier ≥ 1; a tier 0 si ri-registra con stessa email)
- **Identity verification è posticipata**: vedi sezione 2.5 sui tre tier

### Mandate (autorizzazione agente)
- **Whitelist scope**, mai blacklist
- Firmato dalla passkey dell'utente con WebAuthn
- **Step-up obbligatorio** sopra soglie (default €100/deal)
- Auto-revoke per inattività 30 giorni
- Limiti hard-coded di piattaforma (l'utente non può andare oltre):
  - max €1000/deal
  - max €5000/mese per mandate
  - max 10 deal/giorno
  - geo_scope V0: `["IT"]`
  - categories_forbidden hard: `["adult", "weapons", "alcohol", "drugs", "nft_crypto"]`

### Marketplace
- Tutto è `Intent` con side `buy`/`sell` (NO listings vs buy_requests separati)
- Ogni Intent ha `reservation_price` (limite) e `ideal_price` (target)
- Matching semantico via embedding (1536 dim, OpenAI `text-embedding-3-small`)
- **Trasparenza parziale**: utente vede numero match compatibili, NON i prezzi degli altri
- Multi-match: **mini-asta in parallelo**, accetta migliore
- Hard cap negoziazione: **6 round**, al 5° round forced "best and final"
- Comportamento sotto-ideale: persegui ideal, rispetta floor, chiedi all'utente se floor irraggiungibile dopo metà tempo

### Deal & Pagamento V0
- **V0 NON gestisce denaro**. Sistema crediti / gift card style
- **Step-up signature da entrambe le parti** per confermare deal (passkey)
- Chat post-deal pseudonimizzata, E2E encrypted, mai PII
- Logistica: delegata agli umani via chat post-deal

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

### 🔲 Da risolvere in implementazione
- **EC5 race condition due deal simultanei sullo stesso intent**
  - Pattern: optimistic locking + idempotency_key sulla creazione deal
  - Quando match.status passa a 'agreed', tutti gli altri match sullo stesso intent vanno a 'expired'
- **EC6 agente in loop counter-offer**
  - Hard cap 6 round, già nel `negotiation_service`
  - Al 5° round, marca la negoziazione come "final round" e Claude deve chiudere
- **EC7 Self.xyz / Aztec down**
  - Cache della proof verificata, verifica solo a onboarding e step-up
  - Graceful degradation: marketplace continua a girare anche se Self verifier è down

---

## 5. Componenti già scritti (NON riscrivere, leggi e usa)

I file seguenti sono già nel repo. Sono lo scheletro definitivo dell'architettura. Costruisci sopra di essi, **non riscriverli senza motivo esplicito**.

### `backend/app/models/schema.py`
Schema completo SQLAlchemy. Contiene:
- `User` — solo nullifier_hash + passkey + attributes_proven, ZERO PII
- `Agent` — keypair (privkey in KMS ref, mai in chiaro)
- `Mandate` — JSON scope/limits/step_up, firma WebAuthn, contatori usage
- `Intent` — BUY/SELL unificato con embedding vettoriale
- `Match`, `Negotiation`, `Deal`, `DealMessage` — flow del marketplace
- `AuditLog` — immutabile, naturalmente pseudonimo

### `backend/app/services/mandate_verifier.py`
**Cuore del sistema.** Gate di autorizzazione che ogni azione agente deve attraversare. Whitelist scope, controllo limiti €, controllo constraints (geo/categoria), step-up trigger. Solleva `MandateError` se non autorizzato, `StepUpRequired` se serve conferma utente. Tracking automatico dei contatori giornalieri/totali.

### `backend/app/agents/tool_layer.py`
Definizione dei tool che Claude può chiamare nel marketplace e dispatcher dei tool call. 9 tool definiti:
- `create_intent`, `search_matches`, `send_offer`, `send_counter_offer`
- `accept_offer`, `reject_offer`
- `check_state`, `read_inbox`, `ask_user`

Ogni tool passa per `MandateVerifier.authorize()` → esegue → `record_usage()`.

### `backend/app/agents/orchestrator.py`
Loop di Claude che gira un singolo "tick" per un agente. Costruisce system prompt dal mandate, dà a Claude fino a 10 turni di tool use, persiste risultato. Niente memoria conversazionale tra tick — ogni tick è isolato.

---

## 6. TODO ordinato (procedi sequenzialmente)

> **Regole**: Una task alla volta. Completa, testa, committa, **fermati**, attendi via libera, vai alla prossima.
> **Commit message**: `[fase.task] descrizione breve` — es. `[2.1] add Self Protocol verification endpoint`

### FASE 1 — Foundations

#### ✅ 1.1 Setup progetto
- `pyproject.toml` con dipendenze: fastapi, uvicorn, sqlalchemy>=2.0, pydantic>=2, anthropic, openai, pgvector, alembic, webauthn, structlog, pytest, pytest-asyncio, httpx, apscheduler
- `.env.example` con tutte le variabili
- `docker-compose.yml` con Postgres 15 + pgvector per dev locale
- `backend/app/core/config.py` con Pydantic settings
- `backend/app/core/db.py` con engine async + session
- `backend/app/main.py` con FastAPI app + healthcheck endpoint
- Configurazione logging strutturato (structlog → JSON)

#### ✅ 1.2 Migrazioni database
- Setup Alembic
- Prima migration che crea tutto lo schema da `models/schema.py`
- Script `scripts/seed_dev.py` con: 3 utenti finti, 5 intent (mix BUY/SELL), 1 match potenziale

#### ✅ 1.3 Test infrastructure
- Fixture pytest per DB di test (SQLite in-memory)
- Fixture per Anthropic client mockato (response canned)
- Fixture per Self Protocol verifier mockato
- 1 smoke test che gira il `MandateVerifier` su un mandate finto (caso happy + 1 caso failure)

### FASE 2 — Identity & Auth (sequenza posticipata)

> **Importante**: l'onboarding non è più un flow unico, ma tre upgrade progressivi (vedi sezione 2.5). Ogni tier è un endpoint separato che si attiva solo quando serve.

#### ✅ 2.1 Tier 0 — Anonymous onboarding (solo email + passkey)
- `services/auth_service.py`
- Endpoint `POST /api/auth/register/begin` riceve email, ritorna challenge WebAuthn
- Endpoint `POST /api/auth/register/complete` verifica firma WebAuthn, crea user con `tier=0`, `nullifier_hash=NULL`
- Endpoint `POST /api/auth/login/begin` e `/api/auth/login/complete` per re-login
- JWT session 15 min + refresh token
- Test: utente tier=0 può autenticarsi e ricevere JWT valido

#### ✅ 2.2 Tier-based gating middleware
- `core/security.py` con dependency FastAPI `require_tier(min_tier: int)`
- Quando tier insufficiente → solleva `HTTPException(402, detail={"required_tier": N, "next_step": "..."})`
- Test: gating funziona su endpoint dummy con tutti i tre tier

#### ✅ 2.3 Tier 1 — Identity upgrade via Self Protocol
- `services/identity_service.py`
- Endpoint `POST /api/identity/verify-self` (richiede tier ≥ 0)
- Riceve la ZK proof generata dall'app mobile via Self SDK
- Verifica via wrap HTTP del Self verifier (URL configurabile)
- Aggiorna `users.nullifier_hash` + `attributes_proven`, incrementa `tier=1`
- Genera **automaticamente** keypair agente (privkey in KMS, status=`pending_mandate`)
- Test: con proof mockata, l'utente passa da tier=0 a tier=1 e ha un agente associato

#### ✅ 2.4 Tier 2 — Mandate signing
- `services/mandate_service.py`
- Endpoint `POST /api/mandates/draft` (richiede tier ≥ 1) ritorna JSON canonicalizzato + WebAuthn challenge
- Endpoint `POST /api/mandates/submit` riceve firma, verifica, salva mandate, attiva agente, incrementa `tier=2`
- Validazione hard limits di piattaforma (sezione 3)
- Test: utente tier=1 firma mandate e passa a tier=2; agente diventa `active`

#### 🔲 2.5 Mandate revocation & step-up
- Endpoint `POST /api/mandates/{id}/revoke` (richiede tier ≥ 2)
- Endpoint `POST /api/step-up/{action_id}/sign` per confermare azione step-up
- Funzione `resume_pending_action(action_id, signature)` che riprende l'azione dell'agente in attesa

#### 🔲 2.6 Test completo MandateVerifier
- Coverage 100% di `mandate_verifier.py`
- Casi: scope ok/ko, limit hit, step-up trigger, expired, revoked, daily reset, tier insufficiente

### FASE 4 — Marketplace core

> Nota: la vecchia Fase 3 ("Mandate management") è stata fusa nella Fase 2 dato che mandate è ora parte integrante del flow di tier upgrade. La numerazione 4-5-6-7 resta invariata per compatibilità con riferimenti esistenti.

#### 🔲 4.1 Intent service
- `services/intent_service.py`
- `create_intent()` (richiede **tier ≥ 0**) genera embedding via OpenAI
- `update_intent()` modifica reservation/ideal price (richiede step-up se sale, quindi tier ≥ 2)
- `cancel_intent()` chiude intent attivo
- `get_user_intents()` lista intent per utente
- API endpoints: `POST /api/intents`, `GET /api/intents`, `PATCH /api/intents/{id}`, `DELETE /api/intents/{id}`
- **Importante**: utenti tier=0 ricevono notifiche di match potenziali ma NON possono avviare negoziazioni — vedono solo "Hai N match potenziali, verifica per attivare il tuo agente"

#### 🔲 4.2 Embedding service
- `services/embedding_service.py`
- Wrapper su OpenAI `text-embedding-3-small` (1536 dim)
- Cache LRU in-memory per stringhe già viste
- Batch processing per multipli intent simultanei
- Retry con backoff su rate limit

#### 🔲 4.3 Match service
- `services/match_service.py`
- `find_matches(intent_id)`:
  1. Query intent della parte opposta in stessa categoria, attivi
  2. Cosine similarity sui `description_embedding` (top N)
  3. Filtra dove price_overlap esiste (BUY cap >= SELL floor)
  4. Score combinato: 0.7 * similarity + 0.3 * price proximity
- Persiste nuovi match (UniqueConstraint evita duplicati)
- Job apscheduler che gira ogni 60s e refresh match per intent attivi
- API endpoint: `GET /api/intents/{id}/matches`

### FASE 5 — Negoziazione

#### 🔲 5.1 Negotiation service
- `services/negotiation_service.py`
- `start_or_continue(match_id, agent_id, price_cents, message)` (richiede **tier ≥ 1**):
  - Crea Negotiation se non esiste
  - Append turn a `state` JSONB
  - Increment `rounds_used`
  - Se `rounds_used == max_rounds - 1`: marca come "final round"
- `add_counter_offer()` (richiede **tier ≥ 1**) — simile, valida che la negoziazione sia attiva
- `accept_offer()` (richiede **tier ≥ 2**) — chiude come "agreed", crea Deal pending
- `reject_offer()` (richiede **tier ≥ 1**) — chiude come "rejected"
- Logica best-and-final automatica al 5° round

#### 🔲 5.2 Mini-asta logic
- Quando un agente ha N>1 match, il tool layer permette offerte parallele
- Quando una controparte accetta, le altre negoziazioni sullo stesso intent → status `expired`
- **EC5 race condition**: optimistic locking via `SELECT ... FOR UPDATE` su `intents.status` quando si crea il Deal. Solo il primo passa, gli altri ricevono error e l'agente viene notificato

#### 🔲 5.3 Deal service
- `services/deal_service.py`
- `create_pending_deal()` con `idempotency_key` univoca
- `sign_buyer()` / `sign_seller()` per step-up signatures
- Quando entrambe le firme presenti → status `confirmed`
- Apertura chat E2E pseudonimizzata
- API endpoints associati

### FASE 6 — Agent runtime

#### 🔲 6.1 Notification service
- `services/notification_service.py`
- `push_step_up_request(agent_id, action, reason)` — push all'app utente
- `push_question(agent_id, question, context)` — per `ask_user` tool
- V0: stub di console log + endpoint `/api/dev/notifications` per inspection
- V1+: APNs / FCM reale

#### 🔲 6.2 Agent state & inbox services
- `services/agent_state_service.py` con `get_full_state(agent_id)` che ritorna:
  - mandate attivo (con limiti e contatori)
  - budget/limiti rimasti
  - intent attivi
  - negoziazioni in corso
  - notifiche pendenti
- `services/inbox_service.py` — offerte ricevute, controproposte, deal pending

#### 🔲 6.3 Scheduler agent ticks
- `backend/app/agents/scheduler.py`
- Job apscheduler che ogni 60s:
  1. Trova agenti con "lavoro pendente" (nuova offerta, intent senza match recenti, ecc.)
  2. Per ognuno, chiama `orchestrator.run_tick(agent_id)`
  3. Rate limiting per evitare cost explosions Claude API
- V0: simple loop in-process
- V1+: RQ o Celery con worker pool

### FASE 7 — Hardening & ship

#### 🔲 7.1 Rate limiting & abuse
- Rate limit per endpoint con `slowapi`
- Pattern detection: troppi intent ravvicinati, troppe offerte rigettate
- Soft suspend + alert (anche solo log con `level=ALERT`)

#### 🔲 7.2 Observability
- structlog → JSON output
- Metriche Prometheus: agent ticks/sec, deal/min, error rates, latenza LLM
- OpenTelemetry tracing per request mobili → backend → LLM

#### 🔲 7.3 Cost monitoring
- Tracking costo Claude API per agente, per utente, per mandate
- Soft cap: utente che eccede X€ di costi LLM/mese viene rate-limited
- CLI script `scripts/cost_report.py` per dashboard interna

#### 🔲 7.4 Pre-launch checklist
- Privacy policy + ToS (custom, non template generici)
- Sentry / error tracking
- Backup DB automatico
- Disaster recovery plan minimo
- Soft launch con 5-10 amici prima della prima cohort
- **Email uniqueness DB-level** (DQ-9): `CREATE UNIQUE INDEX ix_users_email_unique ON users (lower(notification_email)) WHERE notification_email IS NOT NULL`. Trigger ~1k+ utenti.
- **Refresh token revocation list** (DB table o Redis blocklist per `jti` revocati). Trigger ~500 utenti registrati. 30gg di refresh non revocabile è acceptable V0, inacceptable launch.
- **JWT_SECRET rotation strategy**: rolling key con `kid` claim. 2-3 ore di lavoro stimate. Rotate il secret default `change-me-in-dev-...` pre-launch.
- **Verify email-normalization applicata ovunque** (defensive grep su `email.lower()` / `_normalize_email`). Aggiunto in 2.2 al boundary `auth_service`; controllare che future feature non bypassino.

---

## 7. Principi di codice

- **Type hints ovunque** (Python 3.11+ syntax: `list[str]`, `X | None`)
- **SQLAlchemy 2.0 style** queries (no legacy `query()`, usa `select()`)
- **Pydantic v2** per request/response models
- **Async** dove sensato (endpoint FastAPI, chiamate LLM)
- **Test pytest** per ogni service (almeno happy path + 1 edge case)
- **Niente `print`**, usa `structlog`
- **Niente segreti hardcoded**, tutto via env var con Pydantic settings
- **Money: SEMPRE in cents** (BigInteger), MAI float
- **Datetime: SEMPRE UTC**, naive datetime evitati (usare `datetime.utcnow()` o `timezone.utc`)
- **Niente import circolari** — services possono importare da models, mai viceversa
- **Idempotency keys** su tutte le operazioni che creano Deal o transazioni
- **Audit log su ogni azione di stato** che impatta utente/mandato

---

## 8. Testing approach

- **Unit test**: per service, DB SQLite in-memory, ~70% del coverage
- **Integration test**: per endpoint critici, Postgres reale via testcontainers
- **Mock Anthropic API** in test (fixtures di response canned)
- **Mock Self Protocol verifier** in test
- **End-to-end test** del flow onboarding completo (Fase 2.3)

Coverage target: 80%+ sui service, 100% su `mandate_verifier.py`.

---

## 9. Domande aperte per il founder (chiedere prima di assumere)

Quando incontri queste domande, **non assumere — chiedi al founder via `DESIGN_QUESTIONS.md`** e procedi col design fissato finché non ricevi risposta.

- **Self Protocol SDK Python**: il verifier ufficiale Self è in TS/JS. V0 facciamo wrap HTTP. Se trovi una libreria Python decente, segnala ma non swappare senza ok.
- **Branding/naming**: il nome del prodotto è ancora TBD. Usa `MARKETPLACE` come placeholder ovunque.
- **Pagamenti V2**: il founder deciderà tra Stripe Connect, Mangopay o x402 al momento opportuno. V0 ignora completamente i pagamenti reali.
- **Domanda emergente**: se trovi qualcosa di sostanziale che ti sembra sbagliato nel design, scrivi nota dettagliata in `DESIGN_QUESTIONS.md` ma **procedi col design fissato**.

---

## 10. Cosa serve al founder dopo ogni task

Quando completi una task del TODO:

1. **Aggiorna lo stato** della task in questo file (`🔲` → `✅`)
2. **Commit** con message format `[fase.task] descrizione`
3. **Scrivi una nota breve** in `PROGRESS.md` (crealo se non esiste) con:
   - Cosa hai fatto
   - Decisioni prese che non erano nel brief
   - Test scritti e copertura raggiunta
   - Eventuali blocker o dubbi
4. **Fermati** e attendi via libera prima della task successiva

---

## 11. Note operative finali

- **Una task alla volta.** Completa, testa, committa, fermati, vai avanti.
- **Test obbligatori.** Almeno happy path + 1 edge per ogni service.
- **Non riaprire decisioni di design.** Se trovi qualcosa di sbagliato, nota in `DESIGN_QUESTIONS.md` e procedi.
- **Quando blocco**: aggiungi nota dettagliata su cosa hai tentato e perché non funziona. Non procedere oltre senza chiarimento.
- **Niente over-engineering.** V0 deve girare e fare deal reali con 100 utenti. Niente Kubernetes, niente microservizi, niente ottimizzazioni premature.

---

**Versione brief**: 1.1
**Data**: aprile 2026
**Lingua codice**: inglese (variabili, commenti, test)
**Lingua brief**: italiano (questo doc serve al founder)

### Changelog

**v1.1** — Introdotta sequenza onboarding posticipato (sezione 2.5). Friction di identity verification spostata al momento di engagement utente. Tre tier (0/1/2). Rinumerazione Fase 2 (Identity & Auth fuse con Mandate management). Aggiunti requisiti di tier sui service Marketplace e Negotiation.

**v1.0** — Versione iniziale con onboarding monolitico upfront.
