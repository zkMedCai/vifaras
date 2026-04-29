# MANDATE_UX_FLOW.md — Tier 2 Onboarding

> Spec UX del flow di configurazione e firma del primo mandate dell'agente.
> Triggered automaticamente al passaggio Tier 1 → Tier 2.
> Frontend deliverable: FASE 10 (Web) e FASE 11 (Mobile).

---

## Pre-condizioni

- Utente autenticato come Tier 1
- Self Protocol verificato (nullifier_hash popolato)
- Agent record esistente con `status='pending_mandate'`
- Nessun mandate attivo esistente per l'agent (V0: one mandate per agent)

## Post-condizioni (success path)

- Mandate row creato con firma WebAuthn valida
- Agent `status='pending_mandate'` → `status='active'`
- User `tier=1` → `tier=2`
- Nuovo access token JWT con `tier=2` ritornato al client
- Audit log entry `MANDATE_SIGNED`

---

## Le 7 schermate

### Schermata 1 — Welcome al tuo agente

Layout: full-screen, badge AI animato, testo centrato.

```
🤖 Il tuo agente è quasi pronto

Da ora in poi il tuo agente AI agirà per te nel marketplace:
cercherà oggetti, riceverà offerte, negozierà prezzi.

Ma decide tu cosa può fare e con quali limiti.
Configuriamo insieme.

⏱  Tempo richiesto: 90 secondi

           [ Inizia → ]
```

**Razionale**: introduzione concettuale rapida. L'utente capisce che configurare l'agente è impostare vincoli operativi, non formalità burocratica.

**API**: nessuna chiamata. Solo navigation locale.

---

### Schermata 2 — Limite per singolo deal

Layout: titolo + slider + descrizione + CTA.

```
Quanto può spendere il tuo agente
per un singolo deal?

Sopra questa cifra ti chiederemo conferma con Face ID
prima di chiudere.

  [───●─────────────] €100
  €20             €1000

  Default: €100

💡 Sotto €100 il tuo agente decide da solo. Sopra, decidi tu.

           [ Avanti → ]
```

**Razionale**: limite più importante e più intuitivo. Metafora "decide da solo / decidi tu" chiara, niente gergo tecnico tipo "step-up threshold".

**Default**: €100. **Range**: 20-1000 (max è hard cap di piattaforma).

**API**: nessuna chiamata. State locale finché submit finale.

---

### Schermata 3 — Budget mensile totale

```
Quanto può movimentare in totale al mese?

Quando questa cifra è esaurita, il tuo agente si ferma
e ti chiede di riconfermare.

  [─────●────────] €500
  €50         €5000

  Default: €500

💡 È il tuo "stop loss". Se qualcosa va storto,
   perdi al massimo questa cifra.

           [ Avanti → ]
```

**Razionale**: "budget cap" come garanzia psicologica. Rischio massimo = quello impostato.

**Default**: €500. **Range**: 50-5000.

---

### Schermata 4 — Deal al giorno

```
Quanti deal al giorno può chiudere?

Limite anti-imprevisto: se l'agente sta facendo
troppi deal in un giorno, qualcosa non va.

  [──●──────] 3 deal
  1       10

  Default: 3

💡 Per la maggior parte degli utenti 3 al giorno bastano.
   I venditori professionisti scelgono 5-10.

           [ Avanti → ]
```

**Razionale**: protezione contro bug software o casi edge dove l'agente entra in loop. Secondo "stop loss" temporale, oltre a quello economico.

**Default**: 3. **Range**: 1-10.

---

### Schermata 5 — Categorie e geografia

```
Cosa può comprare e vendere?

✅ Tutte le categorie del marketplace

Esclusi automaticamente:
  alcolici, armi, prodotti per adulti,
  sostanze regolamentate

Dove?

✅ Solo Italia

In futuro potrai estendere ad altri paesi EU.
Per ora il tuo agente opera solo con altri utenti italiani.

           [ Avanti → ]
```

**Razionale**: la geo-restrizione è regolatoriamente importante (compliance EU AI Act), ma all'utente glielo dici come "solo Italia per ora" senza menzionare AI Act. Categorie escluse trasparenti, costruisce fiducia.

**V0**: categorie e geo non modificabili dall'utente (hard-coded). V1+: configurabili.

---

### Schermata 6 — Riepilogo

Layout: card con icone + lista campi + CTA primaria + secondaria.

```
Riepiloghiamo. Il tuo agente potrà:

💰 Spendere fino a €100 per singolo deal in autonomia
💰 Movimentare €500 in totale al mese
📅 Chiudere fino a 3 deal al giorno
🌍 Operare in Italia
🔐 Per qualsiasi deal sopra €100, ti chiederà Face ID
⏰ Configurazione valida per 30 giorni, poi riconferma
❌ Puoi sempre revocare tutto in qualsiasi momento

         [ ⬅ Modifica ]   [ Confermo → ]
```

**Razionale**: schermata più importante. Riepilogo umano-leggibile di quello che firmerà. Niente JSON, niente termini tecnici.

**Tre voci che rassicurano**: scadenza, revoca, conferma per cifre alte.

**API**: `POST /api/mandates/draft`
- Request body: `{agent_id, limits, expires_in_days, constraints}`
- Response: `{draft_id, payload, payload_summary, challenge}`

**`payload_summary.human_readable`** è il testo della schermata. Server-side per consistency, mai costruito dal client.

---

### Schermata 7 — Firma con Face ID

Layout: animazione biometric + testo esplicativo.

```
Conferma con il tuo volto

🔐 Stai per firmare il tuo "patto" con l'agente.

Questa firma è crittografica e legalmente vincolante:
dimostra che sei tu — e solo tu — ad aver dato
queste istruzioni.

       [ 👤 Face ID animation ]

       Avvicina il telefono...
```

**API**: WebAuthn `navigator.credentials.get()` invocato dal client.

Dopo signature ottenuta:

**API**: `POST /api/mandates/submit`
- Request body: `{draft_id, webauthn_assertion}`
- Response: `{mandate_id, agent_id, agent_status: "active", new_access_token, next_step}`

Il client salva il `new_access_token` (sostituisce quello vecchio con tier=1).

---

### Schermata 8 — Successo

```
✅ Patto firmato.
   Il tuo agente è attivo.

Ora torna alla home e crea il tuo primo intent:
  cosa vuoi comprare o vendere?

           [ Vai alla home → ]
```

---

## Stati di errore

### Errore: WebAuthn signature failed

```
⚠️ Firma non riuscita

Riproviamo? Avvicina di nuovo il telefono.

         [ ⬅ Indietro ]   [ Riprova → ]
```

**API response**: 422 Unprocessable Entity con `{error: "webauthn_verification_failed"}`.

### Errore: draft scaduto (5 minuti TTL)

```
⏰ Sessione scaduta

La configurazione era valida per 5 minuti.
Riprova dall'inizio, ci vorrà solo un minuto.

           [ Ricomincia → ]
```

**API response**: 410 Gone con `{error: "draft_expired"}`.
Client torna a Schermata 2.

### Errore: limite oltre hard cap di piattaforma

```
⚠️ Valore non permesso

Il limite massimo per singolo deal è €1.000.
Per cifre più alte, il marketplace richiede
un account business (disponibile prossimamente).

           [ Modifica ]
```

**API response**: 422 con `{error: "exceeds_platform_limit"}`.

---

## Backend mapping

Ogni schermata è puramente UI fino a Schermata 6. Le chiamate API sono solo:

1. **Schermata 6 → 7**: `POST /api/mandates/draft` (genera payload server-side)
2. **Schermata 7**: WebAuthn locale + `POST /api/mandates/submit` (verifica e salva)

Tutto il resto è state locale del client. Niente roundtrip prematuro.

---

## Considerazioni per l'implementazione web (Next.js V0)

- Stato locale: React `useReducer` o Zustand piccolo
- WebAuthn API: `@simplewebauthn/browser`
- Routing: dynamic step routing (`/onboarding/mandate/[step]`)
- Validazione client-side dei range slider matchata con hard cap di piattaforma (sync con `core/platform_limits.py`)
- Fallback se WebAuthn non disponibile (browser non supportato): mostrare errore amichevole con redirect a download mobile app

## Considerazioni per l'implementazione mobile (V0.5)

- React Native con `react-native-passkey` o equivalente
- Native Face ID / Touch ID API
- Push notification setup post-completion (per ricevere step-up requests futuri)
- Deep link handling per QR code handoff (V1, da web a mobile per Tier 1 NFC)

---

## Versionamento

**v1.0** (post-FASE 2) — 7 schermate base + 1 successo + 3 stati errore.
Implementazione FASE 10/11.
