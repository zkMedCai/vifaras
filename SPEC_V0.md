# SPEC_V0.md — Vifaras V0 Architecture Specification

**Date**: 2026-05-03
**Status**: LOCKED (V0 platform-managed AI decision, sections 1–4 + 6.1 locked)
**Supersedes sections**: PROJECT_BRIEF v1.3 §2.5 (onboarding), §2.8 (OAuth roadmap), §3.1 (USP), §4.3 (audience), §6.4 (monetization)
**Authors**: Teodoro Domenico (founder)

---

## 1. Architecture Context

### 1.1 Decision summary

Il 3 maggio 2026, durante discovery FASE 10.2, il founder ha confermato **Path A: V0 platform-managed AI**.

Vifaras gestisce direttamente i provider AI tramite account API propri (Anthropic per agente, OpenAI per embedding). Gli utenti consumer non collegano abbonamenti Claude Pro/Max o ChatGPT Plus/Pro. BYOK API key, connector locale e MCP pubblico restano estensioni power-user V0.5+/V1+.

Decisione motivante: Anthropic vieta esplicitamente a prodotti terzi di offrire Claude.ai login o routare richieste tramite credenziali Free/Pro/Max; OpenAI mantiene billing ChatGPT e API separati. Quindi il modello "utente porta subscription consumer" non è una base legale/operativa per V0.

### 1.2 Cosa cambia da PROJECT_BRIEF v1.3

| Dimensione | Brief v1.3 | SPEC_V0 |
|------------|------------|---------|
| LLM provider V0 | Platform-managed (Anthropic + OpenAI) | Confermato: Vifaras-managed API accounts |
| OAuth roadmap | V1.5+ optional power user | Consumer OAuth rimosso; solo API/BYOK o connector futuri se consentiti |
| Free tier | 5 negoziazioni/mese sui crediti platform | AI inclusa con limiti bassi + crediti/piani extra |
| Modalità | Manuale + agente AI | Agent-first consumer; umano governa con mandate + HITL |
| Audience target | Mass-market reseller flipper IT | Consumer marketplace IT, onboarding no-API-key |
| Cost LLM platform | ~€300/anno fino 5K user | Costo variabile da controllare con cap/crediti |
| Monetization | 5–8% blended | 5% seller fee + AI credits/subscription guardrail |

### 1.3 Razionale

Il prodotto consumer non può chiedere all'utente medio di creare account developer, attivare billing API, generare API key e capire costi/token. Quell'onboarding sposta Vifaras verso una nicchia power-user e riduce drasticamente conversione.

La tesi V0 torna quindi consumer:

> Vifaras crea e gestisce il tuo agente di compravendita. Tu imposti limiti e mandato; l'AI è inclusa.

Trade-off accettato: UX molto più semplice e mercato più largo, in cambio di costo LLM platform che richiede pricing, cap e observability stringenti.

### 1.4 Cosa NON cambia

- Backend FASE 1–7 (auth + identity + mandate + intent + match + negotiation + deal + orchestrator + step-up + production-grade hardening)
- Schema DB attuale: nessun `ai_provider_link` richiesto per V0
- Frontend FASE 10.0–10.1.2 (auth + mandate creation + intent CRUD UI)
- USP marketplace + identity ZK + privacy by design
- HITL minimal A3 design intent (vedi §5)
- Cost monitoring FASE 7.3 resta fondamentale per V0

---

## 2. Business Logic V0

### 2.1 Tesi prodotto

> Vifaras V0 è il marketplace italiano agent-native dove gli utenti creano un agente di compravendita in pochi minuti. Vifaras fornisce l'AI via API provider ufficiali, mentre l'utente definisce limiti, firma il mandate e approva le azioni sopra soglia.

### 2.2 Differentiation

**Vifaras NON è**:
- Marketplace umano-to-umano (Vinted/eBay/Subito)
- Rivendita di Claude/OpenAI come prodotto standalone
- Manual chat-and-deal platform (HITL solo per governance, niente commerce diretto)

**Vifaras È**:
- Marketplace agent-native (categoria nuova)
- Servizio consumer che include l'agente AI come infrastruttura interna
- Piattaforma con identità ZK + mandate criptografico + audit by design

### 2.3 Promessa core

> "Crea agente → imposta limiti → firma mandato → l'agente opera entro regole verificabili"

### 2.4 Scope V0 locked

| Dimensione | V0 |
|------------|-----|
| Categoria | Libera (usato + nuovo + servizi) — vincoli per categoria gestiti via mandate `categories_allowed` |
| Geografia | Italia |
| Lingua UI | Italiano + Inglese |
| Modalità operativa | Agent-first; umano governa tramite mandate/HITL, non negozia manualmente |
| HITL | Minimal A3 (deferred design, vedi §5) |
| AI provider | Vifaras-managed Anthropic/OpenAI API accounts |
| Monetization | 5% seller fee + AI usage caps/credits/subscription |

### 2.5 USP riformulata

> "Vifaras: crea il tuo agente di compravendita in 2 minuti."

---

## 3. Onboarding Flow V0

### 3.1 Sequence

```
ENTRY → Landing page (pubblica)
        ↓
SIGNUP → Email + passkey WebAuthn
        ↓ (Tier 0 acquired)

TIER 0: Spectator
        - Browse public deals feed
        - Account settings
        - Agent settings (vuoto, prompt to upgrade)
        ↓

VERIFY → Self Protocol ZK proof
        ↓ (Tier 1 acquired)

TIER 1: Verified Human
        - Crea bozze intent (no submit)
        - Configura mandate parameters (no signing yet)
        ↓

MANDATE → Firma mandate (WebAuthn step-up)
        ↓ (mandate_signed = true → Tier 2 acquired)

TIER 2: Agent Mandated (operativo)
        - Submit intent
        - View HITL approvals pending
        - Sign deal (WebAuthn step-up)
        - Manage agent (pause/resume)
```

### 3.2 Tier promotion gates

```
Tier 1 = identity_verified ∧ Tier 0 capabilities
Tier 2 = identity_verified ∧ mandate_signed
```

### 3.3 Runtime status (orthogonal a tier)

```
identity_verified: bool
mandate_signed: bool
llm_service_available: bool  // platform-level, not per-user capability
agent_status: enum {
  active,
  paused_provider_outage,   // platform AI provider degraded/cost cap hit
  paused_user_request,      // user click "pause agent"
  paused_mandate_revoked    // mandate cleanup post-revoke
}
```

### 3.4 Capability downgrade logic

| Trigger | Effect |
|---------|--------|
| Platform AI outage/cost cap | `agent_status` unchanged; scheduler skips tick and surfaces degraded state |
| Mandate revoke deliberate | Tier downgrade Tier 1, `agent_status = paused_mandate_revoked` |
| Identity revoke deliberate | Tier downgrade Tier 0 (cascading) |

### 3.5 Operational condition (formula finale)

```
agent operates IFF
  tier == 2
  ∧ identity_verified
  ∧ mandate_signed
  ∧ agent_status == active
  ∧ platform_llm_budget_available
```

### 3.6 Note

- V0 non ha linking AI utente. Provider outage/cost cap è condizione operativa platform, non gate tier.
- Tier promotion è atomica: Tier 2 acquired quando identity + mandate signed passano nella stessa verifica backend.

---

## 4. Tier System V0 (capability matrix formal)

### 4.1 Tier 0 — Spectator

```
Promotion gate: signup completed (email + passkey WebAuthn)
Downgrade: account deletion only

CAPABILITIES
✓ Browse public deals feed (anonymized aggregate)
✓ Browse marketing pages
✓ Account settings (passkey, email, privacy preferences)
✓ Upgrade flow start (verify identity)
✗ Crea intent
✗ Configura mandate
```

### 4.2 Tier 1 — Verified Human

```
Promotion gate: Tier 0 + identity_verified (Self ZK proof)
Downgrade: identity revoke deliberate → Tier 0

CAPABILITIES
✓ All Tier 0 capabilities
✓ Crea bozze intent (draft, no submit)
✓ Configura mandate parameters (no signing yet)
✓ Upgrade flow start (sign mandate)
✗ Submit intent
✗ Operate agent
```

### 4.3 Tier 2 — Agent Mandated

```
Promotion gate: Tier 1
  + mandate_signed = true (WebAuthn step-up)
Downgrade:
  - mandate revoke deliberate → Tier 1
  - identity revoke deliberate → Tier 0 (cascading)

CAPABILITIES
✓ All Tier 1 capabilities
✓ Submit intent (agent operates)
✓ View pending HITL approvals
✓ Approve/reject HITL actions
✓ View match feed (own intents → counterparties)
✓ Sign deal (WebAuthn step-up)
✓ Manage agent (pause/resume, modify mandate)
✓ View deal history
```

### 4.4 V0 design constraints

- **No consumer subscription linking**: niente Claude Pro/Max OAuth, niente ChatGPT Plus/Pro come motore agentico.
- **Provider account platform-managed**: API key Anthropic/OpenAI custodite come secrets di deploy, non su profilo utente.
- **BYOK/connector futuri**: ammessi solo tramite API ufficiali o runtime locale consentito; non bloccano V0.
- **Tier promotion atomica**: backend mandate verifier è idempotent + retry-safe. Concorrenza su gates verificata transactionally.

---

## 5. HITL Approval V0 (deferred design, intent locked)

### 5.1 Decision

V0 implementa HITL minimal **A3** (compromesso scope):

> Sotto soglia → agente agisce automaticamente
> Sopra soglia → richiede approvazione umana
> Fuori mandate → azione bloccata

Pattern: agente entro mandate firmato + approval umano solo per azioni economiche sopra soglia configurabile.

### 5.2 3 preset locked

```
Prudente: auto-approve per action sotto 20% mandate cap
Bilanciato: auto-approve per action sotto 50% mandate cap
Autonomo: auto-approve per action sotto 80% mandate cap
```

### 5.3 Regola prodotto chiave

> "Human approval ≠ manual trading. È governance dell'agente."

User può approvare/rifiutare action proposed dall'agente, MA niente può:
- Modificare offerta (counter-offer manuale)
- Iniziare counter manualmente
- Negoziare manualmente

Altrimenti rientra in manual trading (vietato in Modello A).

### 5.4 Approval UI minima

```
Pending approval card:
  - Action type: fai offerta / accetta offerta / chiudi deal
  - Importo
  - Oggetto
  - Motivo sintetico (LLM-generated)
  - Confronto col mandate (% di cap)
  - Scadenza TTL

Action: Approva | Rifiuta
```

### 5.5 Design questions DEFERRED to FASE implementation

1. **Preset structure**: 3 preset (Prudente/Bilanciato/Autonomo) vs 4° opzione "Mai auto-approvare" come modalità HITL completa
2. **Soglia type**: importo deal totale vs offer-by-offer vs delta-from-previous
3. **Multi-step negotiation**: per-action approval vs aggregate per-negotiation
4. **Notification mechanism**: polling V0 desktop, mobile edge case + WebSocket V1+
5. **Approval TTL**: 24h/48h/configurable + auto-reject vs auto-pause-agent post-TTL

---

## 6. V0 AI Operations

### 6.1 Platform-managed AI scope ✅ LOCKED

V0 usa provider AI ufficiali tramite account API controllati da Vifaras:

**1. Anthropic API per agent runtime**
- Backend `AgentOrchestrator` usa `AsyncAnthropic`.
- API key come secret di deploy (`ANTHROPIC_API_KEY`), mai esposta al client.
- Cost tracking FASE 7.3 rimane enforcement primario.
- Scheduler agente resta opt-in (`ENABLE_AGENT_SCHEDULER=true`) per evitare costi accidentali.

**2. Matching semantico**
- Default storico: OpenAI `text-embedding-3-small` + pgvector
  (`MATCHING_BACKEND=embedding`, richiede `OPENAI_API_KEY`).
- Path Anthropic-only V0: SQL pre-filter + Claude semantic scoring
  (`MATCHING_BACKEND=anthropic`, non richiede OpenAI embeddings).
- `EMBEDDING_BACKEND=fake` resta dev/test escape hatch solo per rehearsal.

**NON supportato in V0**:
- Claude Pro/Max OAuth o credenziali Claude.ai consumer.
- ChatGPT Plus/Pro come motore agentico.
- Browser automation, cookie/session scraping, reverse-engineered consumer APIs.
- BYOK utente nel backend.
- Connector locale Ollama/LM Studio/LocalAI.
- MCP server pubblico.

**Ammesso V0.5+/V1+ solo se compliant**:
- BYOK API key utente via API ufficiali, con storage cifrato KMS o custody locale.
- Connector locale che usa modello locale o API key ufficiale custodita localmente.
- MCP server Vifaras come tool surface per agent client esterni consentiti.

### 6.2 Monetization guardrails

V0 platform-managed AI richiede un modello economico che copra costo variabile LLM:

- 5% seller fee sui deal chiusi.
- AI inclusa con limiti bassi per onboarding.
- Crediti o piano mensile per uso extra.
- Per-user soft cap giornaliero (`daily_user_cost_cap_usd`) già implementato.
- Global hard cap giornaliero (`max_daily_llm_cost_usd`) già implementato.
- Per-tick circuit breaker (`agent_tick_cost_cap_usd`) dentro `AgentOrchestrator`, così anche script/dev hook/futuri trigger manuali non bypassano i cap scheduler.
- Max round negoziazione e max turns per tick restano hard guardrail tecnici.
- Modello forte solo dove serve; modello economico per parsing/triage se introdotto.

Decisione V0: nessuna promessa "AI illimitata". Ogni claim marketing deve restare compatibile con cap e fair-use.

### 6.3 Connector App architecture (V0.5+/V1+)

Decisioni open, non blocking V0:
- Tauri vs Electron vs Python CLI tool?
- Distribution strategy (npm? GitHub releases? auto-update?)
- OS support (Linux + macOS + Windows?)
- Connector lifetime (always-on daemon vs on-demand?)
- Connector ↔ Marketplace protocol (HTTP polling vs WebSocket vs SSE?)
- Local AI endpoint discovery (Ollama, LM Studio, LocalAI)
- Authentication connector ↔ Vifaras (API token, mTLS, signed JWT)

### 6.4 Roadmap V0 nuova ✅ LOCKED (Sequence C — consumer platform-managed)

**✅ COMPLETATE**

- Backend FASE 1-7 (production-grade)
- Frontend FASE 10.0 (auth)
- Frontend FASE 10.1.1 (mandate creation, pre-pivot semantically valid)
- Frontend FASE 10.1.2 (intent CRUD UI, pre-pivot — S2 commit 159bcc4)

**🔲 PENDING — Sequence C (consumer platform-managed)**

1. **FASE 10.2 — Platform AI production setup**
   - Backend: production env checklist (`ANTHROPIC_API_KEY`, matching backend, caps)
   - Backend: static launch sanity via `scripts/check_launch_config.py`
   - Backend: provider health/cost visibility via dev-gated `/api/_dev/ai/status`
   - Frontend: remove/avoid provider-linking UX; explain AI included + fair-use
   - Effort stima: 2-4 days

2. **FASE 10.1.3 — Match view + negotiation read-only**
   - Frontend: match feed + negotiation transcripts read-only
   - Backend already implemented FASE 5
   - Effort stima: 1-2 weeks

3. **FASE 10.1.3.1 — Public market board**
   - Backend: `GET /api/market` public safe listing surface
   - Frontend: `/market` public board with basic filters
   - Hide strategic fields (`ideal_price_eur`, owner id, soft preferences, transcripts)
   - Effort stima: 0.5-1 day

4. **FASE 10.1.4 — Deal pending signature step-up**
   - Frontend: deal sign UI + WebAuthn step-up
   - Backend already implemented FASE 6
   - Effort stima: 1 week

5. **FASE 10.3 — HITL approval implementation**
   - Backend: mandate v2 (auto_approve_threshold) + agent_action_pending_approval table
   - Frontend: approval UI (list pending + detail card + approve/reject)
   - Effort stima: 2-3 weeks

6. **FASE 11 — i18n IT/EN bootstrap**
   - Frontend: next-intl setup + traduzione UI strings + locale toggle
   - Backend: error code + email templates localized
   - Effort stima: 3-5 days

**V0 ALPHA LAUNCH**

- Cumulative effort estimate: ~5-8 weeks dev solo
- Plus integration testing + smoke verify cycles + stabilization
- Realistic launch window: 2-3 mesi from platform-managed decision (2026-07 / 2026-08)

**Razionale Sequence C**:
- Coerenza consumer: niente provider linking in V0.
- Backend platform-managed AI è già implementato; serve production setup, non schema nuovo.
- Match/deal UI diventano più urgenti perché completano il marketplace loop già costruito.
- HITL rimane importante ma può seguire il read-only surface minimo.
- i18n V0 last (polish pre-launch, niente blocking)

---

## 7. Cross-references PROJECT_BRIEF v1.3 → v2.0 update needed

Sezioni da rivedere/riscrivere in PROJECT_BRIEF v2.0:

| Sezione | Update needed |
|---------|---------------|
| §2.5 Onboarding flow | Riscrivere: signup → identity → mandate → Tier 2; niente AI link |
| §2.8 OAuth provider linking | Rimuovere consumer OAuth; BYOK/connector solo V0.5+/V1+ compliant |
| §3.1 USP | Riscrivere: "crea il tuo agente di compravendita in 2 minuti" |
| §3.x Modalità operativa | Documentare agent-first + HITL governance, non manual trading |
| §4.1 TAM/SAM/SOM | Tornare consumer-marketplace; non restringere a AI-native BYOK users |
| §4.3 Customer segments | Consumer/reseller IT + power-user future segment |
| §6.4 Monetization | Aggiungere AI credits/subscription guardrail oltre seller fee |
| §8 GTM | Consumer marketplace channels + trust/privacy narrative |
| §11 Financial projections | Includere costo LLM variabile e cap/fair-use assumptions |

Effort stima update brief: 4-8h scrittura + 2h review + version bump v1.3 → v2.0.

---

## Changelog

- **2026-05-03**: SPEC_V0 v1.0 created. AI-only BYOK pivot drafted.
- **2026-05-03**: SPEC_V0 v1.1 corrected after provider ToS discovery. V0 locked to platform-managed AI; consumer OAuth/BYOK removed from V0.

---

## Cross-references

- `PROJECT_BRIEF.md` v1.3 (current, sezioni 2.5/2.8/3.1/4.3/6.4 superseded by this doc)
- `BARTER_DESIGN.md` (still valid, V1+ feature)
- `MANDATE_UX_FLOW.md` (will need update post-pivot, V0.5+)
- `TRADE_WINDOW_FLOW.md` (still valid, niente impact pivot)
- `DESIGN_QUESTIONS.md` (review needed: alcune DQ deferred adesso decided in SPEC_V0)
- Frontend `PROGRESS.md` FASE 10.1.2 entry (platform-managed prerequisites remain valid)
- Frontend `IDEAS_BACKLOG.md` provider-linking entries need platform-managed correction
