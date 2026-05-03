# SPEC_V0.md — Vifaras V0 Architecture Specification

**Date**: 2026-05-03
**Status**: LOCKED (post-pivot decision sezioni 1–4, OPEN sezioni 5–7)
**Supersedes sections**: PROJECT_BRIEF v1.3 §2.5 (onboarding), §2.8 (OAuth roadmap), §3.1 (USP), §4.3 (audience), §6.4 (take rate)
**Authors**: Teodoro Domenico (founder)

---

## 1. Pivot Context

### 1.1 Decision summary

Il 3 maggio 2026, durante FASE 10.1.2 S3 closure, il founder ha deciso di pivotare l'architettura V0 da **platform-managed AI** a **AI-only OpenCode-style**.

### 1.2 Cosa cambia da PROJECT_BRIEF v1.3

| Dimensione | Brief v1.3 | SPEC_V0 |
|------------|------------|---------|
| LLM provider V0 | Platform-managed (Anthropic + OpenAI) | User-bring (cloud OAuth + API key + AI locale via connector) |
| OAuth roadmap | V1.5+ optional power user | V0 prerequisite per agent operation |
| Free tier | 5 negoziazioni/mese sui crediti platform | N/A (utente porta proprio motore) |
| Modalità | Manuale + agente AI | AI-only operativo (no manual trading) |
| Audience target | Mass-market reseller flipper IT | Tech-aware AI-native users IT |
| Cost LLM platform | ~€300/anno fino 5K user | ~€0 (utente paga proprio provider) |
| Take rate | 8% blended | 5% seller-only |

### 1.3 Razionale del pivot

Il pivot riflette la convinzione fondamentale del founder che il marketplace agent-native sia **categoria nuova**, non un Vinted-with-AI-feature. La piattaforma è infrastructure + protocol per coordinamento di agenti autonomi; l'AI è capability dell'utente, non del marketplace.

Pattern di riferimento: OpenCode (sst.dev/opencode), Cursor BYOK mode, Cline, Continue.dev. Tutti costruiti sul principio "user porta proprio motore AI".

Trade-off accettato: audience target ridotta drastically (~80–300K total IT vs milioni), revenue projection ridimensionata (~€2-7K Anno 1 vs €32K brief), GTM strategy ridisegnata (canale tech-aware: r/LocalLLaMA, Hacker News, X tech-AI invece di r/Vinted).

### 1.4 Cosa NON cambia

- Backend FASE 1–7 (auth + identity + mandate + intent + match + negotiation + deal + orchestrator + step-up + production-grade hardening)
- Schema DB (eventuale aggiunta `user.ai_provider_link` table)
- Frontend FASE 10.0–10.1.2 (auth + mandate creation + intent CRUD UI)
- USP marketplace + identity ZK + privacy by design
- HITL minimal A3 design intent (vedi §5)
- Roadmap sequence B locked (vedi §6.3)

---

## 2. Business Logic V0

### 2.1 Tesi prodotto

> Vifaras V0 è il marketplace italiano agent-native dove gli utenti collegano la propria AI per attivare agenti autonomi che cercano, negoziano e chiudono deal entro limiti pre-firmati. Niente trading manuale: chi entra senza AI guarda, configura il proprio agente e prepara intenti. Chi collega un motore AI compatibile abilita azioni economiche reali.

### 2.2 Differentiation

**Vifaras NON è**:
- Marketplace umano-to-umano (Vinted/eBay/Subito)
- LLM-as-a-service (Vifaras niente vende AI)
- Manual chat-and-deal platform (HITL solo per governance, niente commerce diretto)

**Vifaras È**:
- Marketplace agent-native (categoria nuova)
- Infrastructure + protocol per coordinamento agenti
- Piattaforma con identità ZK + mandate criptografico + audit by design

### 2.3 Promessa core

> "Connect AI → configure agent → set mandate → agent operates within limits"

### 2.4 Scope V0 locked

| Dimensione | V0 |
|------------|-----|
| Categoria | Libera (usato + nuovo + servizi) — vincoli per categoria gestiti via mandate `categories_allowed` |
| Geografia | Italia |
| Lingua UI | Italiano + Inglese |
| Modalità operativa | AI-only (Modello A — no manual trading) |
| HITL | Minimal A3 (deferred design, vedi §5) |
| Monetization | Take rate 5% seller-only su deal chiusi |

### 2.5 USP riformulata

> "Vifaras: agenti AI negoziano deal per te. Porta tuo motore AI."

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
        - Link AI provider (cloud OAuth o connector setup)
        ↓

AI LINK → Provider scelta + linking flow
        ↓ (ai_connection_active = true)

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
Tier 2 = identity_verified ∧ ai_connection_active ∧ mandate_signed
```

### 3.3 Runtime status (orthogonal a tier)

```
identity_verified: bool
mandate_signed: bool
ai_connection_active: bool
agent_status: enum {
  active,
  paused_ai_unavailable,    // AI disconnect transient (PC offline, OAuth refresh)
  paused_user_request,      // user click "pause agent"
  paused_mandate_revoked    // mandate cleanup post-revoke
}
```

### 3.4 Capability downgrade logic

| Trigger | Effect |
|---------|--------|
| AI disconnect transient | `agent_status = paused_ai_unavailable`, tier intact |
| AI unlink definitivo | `agent_status = paused_ai_unavailable`, tier intact (V0 simple) |
| Mandate revoke deliberate | Tier downgrade Tier 1, `agent_status = paused_mandate_revoked` |
| Identity revoke deliberate | Tier downgrade Tier 0 (cascading) |

### 3.5 Operational condition (formula finale)

```
agent operates IFF
  tier == 2
  ∧ identity_verified
  ∧ mandate_signed
  ∧ ai_connection_active
  ∧ agent_status == active
```

### 3.6 Note

- Stati di setup intermedi (`provider_link_pending`, `oauth_in_progress`, `connector_waiting_heartbeat`) possono esistere nella UI/backend come stati di setup, ma NON contano come capability.
- AI link può precedere mandate signing (Tier 1 può fare entrambi prep work, ordine UX preferito è AI link → mandate).
- Tier promotion è atomica: Tier 2 acquired SOLO se identity + AI active + mandate signed passano nella stessa verifica backend.

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
✗ Link AI
```

### 4.2 Tier 1 — Verified Human

```
Promotion gate: Tier 0 + identity_verified (Self ZK proof)
Downgrade: identity revoke deliberate → Tier 0

CAPABILITIES
✓ All Tier 0 capabilities
✓ Crea bozze intent (draft, no submit)
✓ Configura mandate parameters (no signing yet)
✓ Link AI provider (cloud OAuth o connector setup)
✓ Upgrade flow start (sign mandate)
✗ Submit intent
✗ Operate agent
```

### 4.3 Tier 2 — Agent Mandated

```
Promotion gate: Tier 1
  + ai_connection_active = true
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

- **Single provider per user**: V0 single AI provider attivo per account/agente. Multi-provider/fallback policy è V1+.
- **`ai_connection_active` boolean**: false finché OAuth/connector non completano davvero. Per connector locale: true SOLO dopo first heartbeat.
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

## 6. Open Sections (post-PAUSA)

Da definire in continuation session:

### 6.1 Provider Linking V0 scope ✅ LOCKED

V0 supporta DUE provider AI:

**1. Anthropic Claude Pro/Max via OAuth**
- 1-click flow frictionless
- Audience: ~5-15K Anthropic Pro/Max IT 2026
- Token storage backend (access_token + refresh_token)
- Heartbeat verification ongoing

**2. OpenAI via API key paste**
- User crea account platform.openai.com (separato da ChatGPT consumer)
- User genera API key
- User paste in Vifaras Settings → "Connetti OpenAI"
- Backend stores encrypted (KMS pattern, vedi backend FASE 7.2)
- Audience: ~30-80K developer OpenAI API IT

**Total V0 audience addressable**: ~35-95K IT.

**OUT OF SCOPE V0**:
- Google Gemini (TOS vieta 3rd-party)
- Connector locale (Ollama/LM Studio/LocalAI) — entry V0.5+ IDEAS_BACKLOG
- Multi-provider per agent (single provider V0)
- Bedrock/Vertex/Azure (enterprise, niente target consumer)

**Effort**: ~2-3 weeks dev (FASE 10.2)

**Open design questions deferred to FASE 10.2 implementation**:
1. User può linkare entrambi o singolo per agent? (mio bias: V0 single per agent)
2. Token rotation Anthropic OAuth: TTL custom o default?
3. OpenAI API key validation pre-storage: test call /models endpoint?
4. UI "Connetti OpenAI": text input mascherato + helper link
5. Heartbeat failure handling: re-link prompt UX

### 6.2 Connector App architecture

Decisioni open:
- Tauri vs Electron vs Python CLI tool?
- Distribution strategy (npm? GitHub releases? auto-update?)
- OS support V0 (Linux + macOS + Windows?)
- Connector lifetime (always-on daemon vs on-demand?)
- Connector ↔ Marketplace protocol (HTTP polling vs WebSocket vs SSE?)

### 6.3 Roadmap V0 nuova ✅ LOCKED (Sequence B — AI-first prerequisite)

**✅ COMPLETATE**

- Backend FASE 1-7 (production-grade)
- Frontend FASE 10.0 (auth)
- Frontend FASE 10.1.1 (mandate creation, pre-pivot semantically valid)
- Frontend FASE 10.1.2 (intent CRUD UI, pre-pivot — S2 commit 159bcc4)

**🔲 PENDING — Sequence B (AI-first prerequisite)**

1. **FASE 10.2 — AI provider linking**
   - Backend: AIProvider abstraction + Anthropic OAuth + OpenAI key encrypted storage
   - Frontend: Settings UI dual ("Connetti Claude" + "Connetti OpenAI")
   - Effort stima: 2-3 weeks

2. **FASE 10.3 — HITL approval implementation**
   - Backend: mandate v2 (auto_approve_threshold) + agent_action_pending_approval table
   - Frontend: approval UI (list pending + detail card + approve/reject)
   - Effort stima: 2-3 weeks

3. **FASE 10.1.3 — Match view + negotiation read-only**
   - Frontend: match feed + negotiation transcripts read-only
   - Backend already implemented FASE 5
   - Effort stima: 1-2 weeks

4. **FASE 10.1.4 — Deal pending signature step-up**
   - Frontend: deal sign UI + WebAuthn step-up
   - Backend already implemented FASE 6
   - Effort stima: 1 week

5. **FASE 11 — i18n IT/EN bootstrap**
   - Frontend: next-intl setup + traduzione UI strings + locale toggle
   - Backend: error code + email templates localized
   - Effort stima: 3-5 days

**V0 ALPHA LAUNCH**

- Cumulative effort estimate: ~7-10 weeks dev solo
- Plus integration testing + smoke verify cycles + stabilization
- Realistic launch window: 3-4 mesi from pivot decision (2026-08 / 2026-09)

**Razionale Sequence B**:
- Coerenza con pivot V0 AI-only — utenti senza AI link niente raggiungono Tier 2
- Validation tecnica precoce su parts riskier (OAuth + HITL)
- Match view + deal sign sono frontend builds-on-top, niente blocking AI integration
- i18n V0 last (polish pre-launch, niente blocking)

---

## 7. Cross-references PROJECT_BRIEF v1.3 → v2.0 update needed

Sezioni da rivedere/riscrivere in PROJECT_BRIEF v2.0:

| Sezione | Update needed |
|---------|---------------|
| §2.5 Onboarding flow | Riscrivere: signup → identity → AI link + mandate → Tier 2 |
| §2.8 OAuth provider linking | RIBALTARE: V0 prerequisite, niente più V1.5+ optional. Tabella provider + connector locale |
| §3.1 USP | Riscrivere: "Marketplace agent-native, porta tuo motore AI" |
| §3.x Modalità operativa | NUOVO: documentare AI-only Modello A, tier system formale |
| §4.1 TAM/SAM/SOM | Ricalibrare: ~80-300K total IT vs milioni Vinted-style. SOM realistic V0 alpha 500-2K |
| §4.3 Customer segments | RIBALTARE: tech-aware AI-native users (Anthropic Pro/Max + OpenAI API + local AI capable) |
| §6.4 Take rate | Update: 5% seller-only (era 8% blended) |
| §8 GTM | Riscrivere: canali tech-aware (r/LocalLLaMA, Hacker News, X tech-AI). Niente più Reddit r/Vinted |
| §11 Financial projections | Ricalibrare: revenue Anno 1 ~€2-7K, break-even Anno 2 |

Effort stima update brief: 4-8h scrittura + 2h review + version bump v1.3 → v2.0.

---

## Changelog

- **2026-05-03**: SPEC_V0 v1.0 created. Pivot decision locked. §1–4 LOCKED, §5 intent LOCKED design DEFERRED, §6–7 OPEN.

---

## Cross-references

- `PROJECT_BRIEF.md` v1.3 (current, sezioni 2.5/2.8/3.1/4.3/6.4 superseded by this doc)
- `BARTER_DESIGN.md` (still valid, V1+ feature)
- `MANDATE_UX_FLOW.md` (will need update post-pivot, V0.5+)
- `TRADE_WINDOW_FLOW.md` (still valid, niente impact pivot)
- `DESIGN_QUESTIONS.md` (review needed: alcune DQ deferred adesso decided in SPEC_V0)
- Frontend `PROGRESS.md` FASE 10.1.2 entry (S2 implementation pre-pivot, valid storico)
- Frontend `IDEAS_BACKLOG.md` 13 entries uncommitted (review post-pivot needed)
