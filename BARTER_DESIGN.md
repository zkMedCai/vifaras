# BARTER_DESIGN.md — TRADE V1+

> Design del baratto AI-mediato.
> V0: schema-ready, non implementato.
> V1: TRADE bilaterale (FASE 8).
> V2: catene multi-hop.

---

## Il principio fondamentale

Il valore di un oggetto **non è una proprietà intrinseca**, è una funzione del **bisogno specifico** della persona che lo riceve in quel momento.

**Esempio**:
- La mia bici "vale" €400 sul mercato (prezzo medio Vinted)
- Per Mario, che ha bisogno disperato di una bici per andare al lavoro domani, "vale" €600
- Per Luigi, che ha già 3 bici, "vale" €200

Sul mercato monetario standard questa varianza è invisibile. Sul baratto è proprio dove sta il valore della negoziazione AI: l'agente trova combinazioni dove la mia "moneta" (un oggetto) vale per l'altro più di quanto valga per me, e viceversa. **Win-win simmetrico** (scambio Pareto-migliorativo).

In termini accademici: *subjective value theory*.

---

## Le tre forme di TRADE

| Forma | Descrizione | V1 | V2 |
|-------|-------------|----|----|
| **Baratto puro** | Cambio bici con amplificatore, no cash | ✅ | ✅ |
| **Baratto + conguaglio** | Bici + €50 con ampli | ✅ | ✅ |
| **Bundle vs bundle** | Bici + casco con chitarra + custodia | ❌ | ✅ |

---

## Schema dati (V0 schema-ready)

`Intent.side` è enum a tre valori. V0 implementa solo `'buy'` e `'sell'`. `'trade'` accetta lo schema ma il service rifiuta operativamente con `NotImplementedError`.

V1 estensione:

```sql
-- Estensione di intents per side='trade'
ALTER TABLE intents ADD COLUMN offered_market_value_cents BIGINT;
ALTER TABLE intents ADD COLUMN offered_sentimental_value INT;  -- 0-10
ALTER TABLE intents ADD COLUMN offered_urgency_to_give INT;    -- 0-10

CREATE TABLE intent_wishlist (
  id UUID PRIMARY KEY,
  intent_id UUID NOT NULL REFERENCES intents(id) ON DELETE CASCADE,
  priority INT NOT NULL,                -- 1, 2, 3
  description TEXT NOT NULL,
  description_embedding VECTOR(1536),
  urgency_to_have INT NOT NULL,         -- 0-10
  created_at TIMESTAMPTZ DEFAULT NOW()
);

ALTER TABLE intents ADD COLUMN cash_will_pay_up_to_cents BIGINT DEFAULT 0;
ALTER TABLE intents ADD COLUMN cash_will_accept_down_to_cents BIGINT DEFAULT 0;
ALTER TABLE intents ADD COLUMN cash_preference TEXT;  -- 'no_cash' | 'flexible' | 'cash_preferred'
```

---

## Tre tipi di match

### TRADE ↔ TRADE
Bidirezionalità: il mio `offered` matcha il tuo `wishlist` E il tuo `offered` matcha il mio `wishlist`.

### TRADE ↔ SELL
Il mio `wishlist` matcha il tuo `offered` (lato SELL). Io ti propongo il mio oggetto + eventuale conguaglio invece di soldi puri.

### TRADE ↔ BUY
Tu cerchi una bici, io offro la mia in scambio del tuo X (X può essere tutto, anche cash come BUY classico).

---

## Mutual Surplus Score (Pareto matching)

Quando il match-engine valuta TRADE↔TRADE tra Alice e Bob:

```
Per Alice:
  surplus_alice = (
    desire_for_bob_offer  // priority + urgency_to_have del wishlist Alice
    * semantic_match_score(bob.offered, alice.wishlist)
    
    - cost_to_give_alice_offer  // sentimental + opportunity cost
    
    + cash_alice_receives  // se conguaglio in suo favore
    - cash_alice_pays      // se conguaglio in sfavore
  )

Per Bob: simmetrico → surplus_bob

if surplus_alice > 0 AND surplus_bob > 0:
  → Pareto-improving deal, l'agente lo propone
  
if solo uno positivo:
  → "ingiusto", l'agente non lo propone
  → unless mandate.allow_unfavorable_trades = true (advanced setting V2)
```

---

## Negoziazione multi-dimensionale

Su BUY/SELL: una variabile (prezzo).
Su TRADE: tuple di variabili.

```python
# Ogni round della negotiation
{
  "round": 3,
  "from_agent": "agt_abc",
  "type": "counter_offer",
  "trade_offer": {
    "what_i_give_intent_id": "...",
    "what_i_receive_intent_id": "...",
    "cash_adjustment_cents": 5000,    # 50 euro di conguaglio
    "cash_direction": "to_other"      # "from_other" | "to_other" | "none"
  },
  "message": "Posso scendere a €40 se ritiri tu",
  "timestamp": "..."
}
```

Le contro-offerte possono modificare:
- Il conguaglio (più o meno cash)
- La direzione del conguaglio
- (V2+) Sostituire l'oggetto con altro nel wishlist
- (V2+) Aggiungere oggetto secondario come "sweetener"

**V1 sostiene solo modifica del conguaglio.** Cambiare l'oggetto = nuova negoziazione.

---

## UX di configurazione TRADE

### Schermata: "Configura il baratto"

```
**Cosa offri?**

[Foto + descrizione]

Valore di mercato:  €400 [auto-suggerito]
Quanto ci tieni?    [slider: poco — molto]
Quanto urge?        [slider: ho tempo — subito]

**Cosa cerchi?**

Aggiungi 1-3 oggetti che ti interessano:

1. Una chitarra elettrica       [molto urgente] [×]
2. Una bici da corsa             [non urgente]   [×]
3. Un mobile vintage             [poco urgente]  [×]
   [+ Aggiungi]

**Conguaglio in denaro:**

Sei disposto a pagare fino a:    €[200]
Sei disposto a ricevere almeno:  €[100]

           [ Avvia il tuo agente → ]
```

L'utente esprime preferenze in modo umano, l'AI fa il calcolo Pareto.

---

## Mandate impatto

Il mandate deve esplicitamente abilitare TRADE. Default in V0/V1: **disabilitato**.

```json
{
  "scope": {
    "allowed_actions": [
      "create_intent",
      // V1+ aggiunte:
      "create_trade_intent",
      "propose_swap",
      "accept_swap"
    ]
  }
}
```

UX configurazione mandate (Schermata 5 di MANDATE_UX_FLOW):

```
**Cosa può fare il tuo agente?**

☑ Comprare e vendere oggetti
☐ Accettare scambi (baratto)

💡 Lo scambio permette al tuo agente di proporre o accettare
   scambi oggetto-contro-oggetto, eventualmente con conguaglio.
   Decidi tu se attivarlo.
```

---

## Decisione: Opzione β (TRADE come proprietà di SELL)

Quando l'utente crea un Intent SELL, può aggiungere flag "Accetto anche scambi".

**Vantaggi**:
1. Massimizza la liquidità (ogni SELL è anche potenziale TRADE candidate)
2. Nasconde la complessità del baratto a chi non la cerca
3. Discovery progressiva: "ah, accetta anche baratto, ho qualcosa per lui!"

Invece di Opzione α (TRADE come modalità separata che l'utente deve scegliere esplicitamente).

---

## Liquidità e cold start

**Doppio coincidence-of-wants problem**: io devo trovare qualcuno che (a) ha quello che voglio e (b) vuole quello che ho.

Sui marketplace di baratto puro (Bunz, Reoose), conversion 1-3% vs 5-15% dei marketplace di vendita.

**Mitigazione V1**: TRADE non è il default. BUY/SELL è il default, TRADE è opzione che si infiltra (Opzione β). Gli utenti con wishlist match ricevono notifica "abbiamo trovato qualcuno che ha quello che cerchi".

**V2 — catene multi-hop**: io do la bici a Marco, Marco da il libro a Lucia, Lucia da l'ampli a me. Nessuno riceve direttamente da chi dà. È graph cycle detection problem applicato a embedding semantici. Difficile ma potente: è **lì che il valore reale del baratto AI emerge**.

---

## Considerazioni regolatorie

### Italia — permuta tra privati (Codice Civile art. 1552)

Lo scambio peer-to-peer di oggetti senza denaro è generalmente non tassato (permuta). Però:
- Frequente scambio comincia a sembrare attività commerciale
- Soglia "venditore occasionale" italiana: **€5.000/anno** lordi
- Sotto: privato. Sopra: imprenditore con obblighi fiscali.

**Mitigazione**: nel mandate aggiungere cap annuale (es. €3.000/anno di volume cash totale per utente non-VAT) per mantenere automaticamente sotto soglia commerciale.

### TVA / IVA su conguaglio

Il conguaglio cash potrebbe essere classificato come "vendita parziale" e soggetto a regole IVA. Da verificare con commercialista quando V1 attivo.

---

## Logistica del baratto

Spedire DUE oggetti tra parti diverse è 2x i problemi: chi spedisce per primo? Cosa succede se uno arriva e l'altro no?

**V1**: solo baratto in stesso comune o ritiro a mano (vincolo nel mandate). Spedizione di scambi a V2 con Trustee Service (vedi `TRADE_WINDOW_FLOW.md`).

---

## Roadmap implementazione

| Fase | Cosa | Quando |
|------|------|--------|
| Schema-ready | `Intent.side` enum a 3 valori | V0 (FASE 4) |
| TRADE bilaterale base | TRADE↔TRADE / TRADE↔SELL/BUY, valore self-declared singolo, niente urgency | V1 (FASE 8) |
| Subjective value | Wishlist a 3 oggetti, urgency, sentimental, Pareto matching | V1.5 |
| Trade Window logistico | Spedizione baratto via Trustee Service | V1.5 (FASE 9) |
| Multi-hop chains | Catene a 3+ persone | V2 |

---

## Storytelling per il lancio V1

> "Vinted ti dà un prezzo. Subito ti dà un prezzo. Noi ti diamo qualcosa di diverso: il tuo agente AI sa che il valore di un oggetto cambia in base a chi lo riceve, e trova le persone per cui il tuo oggetto vale tanto, in cambio di qualcosa che vale tanto per te. È baratto del 21° secolo, fatto da AI che capisce i bisogni."

Una storia *vera* economicamente, *romantica* culturalmente, *tecnicamente impressionante*.

---

## Versionamento

**v1.0** (post-FASE 2) — Design TRADE V1+. Schema V0-ready in FASE 4. Implementazione FASE 8.
