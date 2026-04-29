# TRADE_WINDOW_FLOW.md — Trustee Service V1.5

> Sistema di escrow + delivery confirmation per deal completati.
> Ispirato a Cardmarket Trustee Service e MMO trade window pattern.
> V0/V1: logistica delegata agli umani via chat post-deal.
> V1.5 (FASE 9): Trustee Service obbligatorio per tutti i deal.

---

## Principio fondamentale

**Swap quasi-atomico**: anche se i pacchi viaggiano in tempi diversi, il sistema garantisce che **se uno dei due non parte, l'altro lo sa e può fermare**. Nessuna parte spedisce per prima senza protezione.

Modello combinato:
- **Cardmarket** (Trustee Service): cash escrow + delivery confirmation
- **MMO trade window**: dual stash con timer
- **Identity verificata ZK**: pattern detection cross-nullifier per dispute

---

## I tre tipi di deal supportati

### 1. Cash → oggetto (BUY/SELL classico)

Buyer paga, fondi in escrow Stripe. Seller spedisce con tracking. Buyer conferma ricezione → fondi rilasciati.

### 2. Oggetto → oggetto (TRADE puro)

Entrambi caricano tracking. Sistema monitora dual-shipping. Entrambi confermano ricezione → deal completato.

### 3. Oggetto + cash → oggetto (TRADE con conguaglio)

Combinazione: chi dà conguaglio paga in escrow + entrambi tracciano. Doppio rilascio: cash all'altro + conferma deal.

---

## Le 4 fasi del Trade Window

### Fase 1 — Setup spedizione (24 ore)

Subito dopo che il deal è confermato (entrambe le parti hanno firmato step-up).

**Cosa devono fare entrambi**:
- Caricare tracking number da corriere certificato **OPPURE**
- Confermare ritiro a mano (luogo, data, ora prefissati)

**Caso cash escrow** (BUY/SELL): buyer carica `payment_intent_id` Stripe (escrow attivo) entro 24h, altrimenti deal annullato.

**Caso TRADE**: entrambi caricano tracking. Se uno carica e l'altro no entro 24h, il primo riceve notifica "controparte non ha caricato tracking, deal annullato senza penalità".

**Trigger automatici**:
- Notifica push push a entrambi al deal confirm
- Reminder push a 12h e 22h se action mancante
- Auto-cancel a 24h se action mancante

### Fase 2 — Spedizione attiva (max 14 giorni)

Sistema monitora i tracking via API corriere ogni 4 ore.

**Stati possibili monitorati**:
- `created` → seller ha generato etichetta ma non spedito
- `in_transit` → pacco in viaggio
- `out_for_delivery` → in consegna
- `delivered` → consegnato
- `returned` → restituito al mittente
- `lost` → bloccato in stato anomalo > 5 giorni
- `cancelled` → spedizione annullata

**Killer feature dual-tracking**: se uno dei due tracking risulta `cancelled` o `returned`, sistema **blocca automaticamente** anche l'altra spedizione con notifica al titolare ("controparte ha annullato, richiama il pacco se possibile").

### Fase 3 — Conferma ricezione (delivery + 7 giorni)

Quando un pacco è `delivered`, notifica push al destinatario:

> "📦 Il tuo pacco da [pseudonimo] è stato consegnato.
> Conferma che l'oggetto corrisponda a quanto pattuito.
> Hai 7 giorni."

Tre azioni possibili:
- **Conferma** (tutto ok) → deal step completato
- **Apri dispute** (oggetto difettoso, mancante, non corrispondente) → flow dispute
- **Nessuna azione entro 7 giorni** → conferma automatica

**Per BUY/SELL**: una sola conferma del buyer rilascia i fondi.
**Per TRADE**: entrambe le conferme richieste, deal completato solo quando entrambi confermano (o entrambe le 7-day deadline scadono).

### Fase 4 — Settlement & rating

**Cash escrow** (BUY/SELL):
- Fondi rilasciati al seller via Stripe Connect
- Trattenuta nostra fee (V1.5: 5% del valore)
- Stripe processing fee a carico del buyer (~1.4% + €0.25)

**Reputation update** (entrambi):
- Successful deal → counter `successful_deals` +1
- Loss rate aggiornato
- Pattern detection per behavior anomalo

---

## Stati del Trade Window

```
created → setup_open → shipping_in_progress → pending_confirmation → completed
   ↓           ↓              ↓                       ↓
cancelled  cancelled     dispute_opened          dispute_opened
                                                       ↓
                                                  resolved_buyer / resolved_seller
```

**Schema dati**:

```sql
CREATE TABLE trade_windows (
  id UUID PRIMARY KEY,
  deal_id UUID NOT NULL REFERENCES deals(id),
  status TEXT NOT NULL,  -- vedi sopra
  
  setup_deadline TIMESTAMPTZ NOT NULL,           -- created_at + 24h
  shipping_deadline TIMESTAMPTZ,                 -- popolato in fase 2
  confirmation_deadlines JSONB,                  -- per parte
  
  payment_escrow_id TEXT,                        -- Stripe payment_intent_id
  payment_released_at TIMESTAMPTZ,
  
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at TIMESTAMPTZ,
  
  dispute_id UUID REFERENCES disputes(id)
);

CREATE TABLE trade_stashes (
  id UUID PRIMARY KEY,
  trade_window_id UUID NOT NULL REFERENCES trade_windows(id),
  user_id UUID NOT NULL REFERENCES users(id),  -- chi possiede questa stash
  
  -- Per spedizione
  tracking_number TEXT,
  courier TEXT,                                  -- 'poste', 'brt', 'gls', 'inpost'
  
  -- Per ritiro a mano
  pickup_location_text TEXT,                     -- "Stazione Termini, biglietteria"
  pickup_geo_lat DECIMAL,
  pickup_geo_lng DECIMAL,
  pickup_datetime TIMESTAMPTZ,
  pickup_qr_code TEXT,                           -- QR per check-in scambio
  
  current_tracking_status TEXT,
  last_tracking_check TIMESTAMPTZ,
  
  confirmed_at TIMESTAMPTZ,                      -- quando l'utente ha confermato ricezione
  status TEXT NOT NULL DEFAULT 'empty',
  -- 'empty' | 'pending' | 'in_transit' | 'delivered' | 'confirmed' | 'disputed'
  
  created_at TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Corrieri italiani certificati (V1.5)

Lista chiusa V1.5: **Poste Italiane, BRT, GLS, InPost (Locker)**.

Niente altro. Standardizziamo l'integrazione, copriamo ~95% del traffico nazionale, ci teniamo fuori da problemi con corrieri minori.

| Corriere | Tracking API | Note |
|----------|--------------|------|
| Poste Italiane | API ufficiale + scraping fallback | Più diffuso, più lento |
| BRT | API ufficiale (B2B token) | Veloce, affidabile |
| GLS | API ufficiale | Buona DDA EU |
| InPost | API + locker network | Pratico per oggetti piccoli |

**V2+**: aggiunta progressiva di altri corrieri se richiesti dalla user base.

---

## Tier-based tracking requirement (Cardmarket-style)

Replicato pattern Cardmarket: discriminazione intelligente del rischio.

| Condizione | Tracking obbligatorio? |
|------------|------------------------|
| Deal sotto €30 + seller con > 5 deal completati | No, opzionale |
| Deal sopra €30 | Sì |
| Seller nuovo (< 5 deal) | Sì sempre |
| Seller con loss rate > 2% | Sì sempre |
| Ritiro a mano | N/A (QR check-in) |

Chi è dimostrato affidabile ha meno friction. Chi è nuovo o problematico paga il costo del tracking.

---

## Ritiro a mano

Per scambi locali (stesso comune, baratto puro spesso).

**Flow**:
1. Entrambi accordano luogo + data + ora durante setup
2. Sistema genera **QR code univoco** per il deal
3. Allo scambio, entrambi scansionano il QR dell'altro
4. Sistema registra timestamp + GPS check (entrambi vicini al luogo concordato)
5. Entrambi confermano "scambio avvenuto" entro 24h dalla data fissata

Pattern simile a delivery confirmation di GLS/InPost.

**No-show handling**: se uno dei due non si presenta entro 24h dalla data fissata, deal annullato senza penalità ma counter `no_show_count` +1 (impatta reputation).

---

## Cash escrow tecnico

Stack: **Stripe Connect Express**.

**Razionale legal**: detenere fondi di terzi ci porrebbe a rischio classification come PSP sotto PSD2. Stripe Connect transfeasce la liability legale a Stripe. Noi siamo "marketplace facilitator", Stripe è custode legale dei fondi. Stesso pattern di Vinted, Depop, Etsy.

**Costi**:
- Stripe processing: ~2.9% + €0.25 per transazione
- Stripe Connect fee: trasferimento al seller incluso
- **Nostra fee piattaforma**: 5% del valore deal (V1.5 default, A/B testabile)

**Refund logic**:
- Deal cancellato in fase 1 (setup) → refund completo automatico
- Deal cancellato in fase 2 (shipping) → refund completo, manual review se dispute
- Dispute risolto pro-buyer → refund completo
- Dispute risolto pro-seller → fondi rilasciati al seller

---

## Dispute resolution

### V1.5 — Manual review

Stack: te, founder, leggi i ticket. Decisione binaria.

**Workflow ticket**:
1. Utente apre dispute (durante fase 3, entro 7 giorni dalla delivery)
2. Carica foto, descrive il problema (max 1000 char)
3. Controparte ha 72h per rispondere con sua versione + foto
4. Tu (o team CS) review entrambe le versioni
5. Decisione binaria: refund/reverse o mantieni deal
6. Tempo medio target: 5 giorni lavorativi

**Aspetto economico V1.5**: 2-3% dei deal in dispute è realistico. Con 1000 deal/mese, ~20-30 dispute. Manageable da founder solo se le ore di review sono concentrate.

### V2+ — AI-assisted

Vision AI per controllo foto (oggetto consegnato matcha listing?).
Pattern detection cross-nullifier per identificare dispute serial-abusers.
Manual review solo per casi ambigui.

---

## Postal claim handoff (lost shipment)

Pattern Cardmarket:

1. Pacco non arriva entro 14 giorni dalla spedizione → buyer può aprire ticket "shipment lost"
2. **Tu (piattaforma) rifondi il buyer immediatamente** — è il punto di trust del Trustee Service
3. **Seller deve aprire claim al corriere** entro tempo limite (variabile per corriere, tipicamente 30-90 giorni)
4. Il corriere indaga (può richiedere fino a 3 mesi)
5. Se il corriere risarcisce il seller → tu ricevi il rimborso del refund che hai dato al buyer
6. Trattieni la fee del 5%, restituisci il resto al seller
7. Se il corriere non risarcisce (es. seller non ha rispettato procedure di spedizione) → seller ne assume il costo

**Documenta in CONTRIBUTING.md o ToS**: i seller sono responsabili di seguire le procedure di spedizione corretta del corriere scelto. Tracking obbligatorio sopra €30 protegge il seller stesso.

---

## Decisione: Trustee obbligatorio (non opt-in)

**Tutti i deal V1.5+ passano per il Trustee Service.** Pagano fee piattaforma sempre.

**Razionale**:
- Sicurezza è il nostro posizionamento
- Vinted è "opt-in" e per questo è piena di truffe
- Cardmarket è "default-on" ed è considerato standard d'oro nel suo settore
- Adottare Cardmarket-pattern al day 1 ci differenzia immediatamente

**Trade-off accettato**: deal piccoli (< €15) hanno fee proporzionalmente alta. Mitigazione: lower fee tier per deal sotto €15 (es. fee fissa €0.50) o tracking opzionale per ridurre costi seller.

---

## UX panel "I tuoi deal in corso"

```
📦 Deal in corso

  ────────────────────────────────────
  🤖 Bici Trek Marlin → Marco        
  Stato: in transito                  
  Spedito 3 giorni fa, consegna prevista domani
  ⏱  Trade window: 11/14 giorni
  
  Marco ha spedito: in transito (BRT 1234567890)
  Tu hai spedito: ✅ consegnato ieri
  
  Aspettando: la consegna del tuo pacco a Marco
  
  [ Apri dispute ]   [ Chat con Marco ]
  ────────────────────────────────────
  
  💰 Vendita iPhone 13 → Lucia       
  Stato: pagamento in escrow          
  Devi spedire entro 22 ore
  
  [ Carica tracking ]   [ Annulla ]
  ────────────────────────────────────
```

---

## Roadmap implementazione

| Componente | Fase |
|------------|------|
| Stripe Connect Express integration | FASE 9 |
| Schema `trade_windows`, `trade_stashes`, `disputes` | FASE 9 |
| Tracking API integration (4 corrieri) | FASE 9 |
| Background job tracking polling | FASE 9 |
| Trade Window state machine | FASE 9 |
| QR check-in per ritiro a mano | FASE 9 |
| Manual dispute UI panel (admin) | FASE 9 |
| Refund logic | FASE 9 |
| Postal claim handoff workflow | FASE 9.5 |
| AI-assisted dispute (V2) | V2 |

---

## Versionamento

**v1.0** (post-FASE 2) — Design Trustee Service V1.5. Implementazione FASE 9 (post-PMF V0).
