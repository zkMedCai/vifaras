# Informativa sulla privacy — Vifaras

**Versione**: 1.0.0
**Data di entrata in vigore**: [TBD pre-launch]
**Lingua principale**: Italiano

> ⚠ **Documento V0 alpha — draft strutturato**
> Questa informativa è una bozza redatta secondo le best practice GDPR
> pubbliche (Reg. UE 2016/679 + D.Lgs. 196/2003 + provvedimenti del Garante
> italiano). **Richiede review da legale qualificato GDPR + diritto digitale
> italiano prima di qualsiasi launch alpha esterno.** Ogni voce marcata
> "[TBD pre-launch]" indica una decisione operativa o legale ancora aperta.

---

## 1. Titolare del trattamento

- **Ragione sociale**: [TBD pre-launch — entità legale del founder]
- **Sede legale**: [TBD pre-launch — indirizzo fisico richiesto dalla normativa IT/EU]
- **Email per richieste privacy**: privacy@vifaras.com (TBD pre-launch alias attivo)
- **Responsabile della Protezione dei Dati (DPO)**: [TBD pre-launch — designazione richiesta solo se il trigger Art. 37 GDPR viene raggiunto: trattamento sistematico su larga scala di dati identificativi o categorie speciali]

## 2. Cosa è Vifaras

Vifaras è un marketplace mediato da intelligenza artificiale: gli utenti
dichiarano intenti di acquisto o di vendita, agenti software autonomi
("agenti") li negoziano per loro conto, l'utente firma il deal finale
tramite autenticazione biometrica WebAuthn.

L'utente conferisce un mandato esplicito al proprio agente, che opera
entro limiti contrattuali predefiniti (importo massimo per deal, volume
giornaliero, tipologia di azioni consentite). Le decisioni economicamente
rilevanti — la firma di un deal — restano sempre nelle mani dell'utente.

## 3. Dati raccolti e finalità

I dati sono organizzati per categoria. Per ciascuna riportiamo: cosa
raccogliamo, su quale base giuridica (Art. 6 GDPR) e per quanto tempo.

> Le voci marcate **[TBD pre-launch]** rappresentano decisioni di
> retention non ancora finalizzate. Saranno definite prima del launch
> alpha esterno con il supporto di un legale qualificato. Le retention
> proposte come "default V0" sono basate su best practice e sulla
> normativa italiana applicabile (Codice del Consumo, obblighi fiscali),
> ma richiedono validazione legale.

### 3.1 Dati di account

- Identificativo univoco utente (UUID), tier (0/1/2), stato dell'account,
  data di creazione, ultimo accesso
- Email per notifiche (opzionale — non è l'identificativo primario)
- Token push per notifiche mobile (opzionale, V0.5+)

**Base giuridica**: Art. 6.1.b GDPR (esecuzione di un contratto).
**Retention**: fino alla cancellazione dell'account. [TBD pre-launch — flow
di cancellazione self-service in fase di sviluppo, V0 cancellazione su
richiesta via email a privacy@vifaras.com.]

### 3.2 Credenziali di autenticazione

- Identificativo della credenziale WebAuthn, chiave pubblica, contatore
  anti-replay
- Vifaras **non memorizza password**: l'autenticazione avviene
  esclusivamente tramite passkey FIDO2/WebAuthn (biometria del dispositivo)

**Base giuridica**: Art. 6.1.b GDPR (esecuzione di un contratto —
autenticazione necessaria al servizio).
**Retention**: fino alla rimozione della credenziale da parte dell'utente.
[TBD pre-launch — UI di rimozione credenziali in fase di sviluppo.]

### 3.3 Verifica dell'identità (Tier 1)

Per accedere alle funzionalità di livello 1 (creazione di intenti) e
livello 2 (mandato all'agente), l'utente verifica la propria identità
tramite **Self Protocol** — un sistema basato su Zero-Knowledge proof.

Self Protocol non trasmette i dati personali del documento (passaporto,
carta d'identità). Trasmette solo:

- Un **identificativo opaco** (`nullifier_hash`) — non riconducibile al
  documento originale
- **Attributi dimostrati** in forma booleana (es. `{adult: true,
  country: "IT", documentValid: true}`) — non i valori sottostanti

**Base giuridica**: Art. 6.1.b GDPR (esecuzione di un contratto — KYC
necessario per accesso ai livelli superiori).
**Retention**: legata alla scadenza del documento sottostante. [TBD
pre-launch — decisione operativa.]

### 3.4 Mandato firmato (Tier 2)

Quando l'utente attiva un agente autonomo, firma un **mandato digitale**
tramite WebAuthn step-up biometrico. Il mandato include: ambito (azioni
consentite e proibite), limiti economici (importo massimo per deal,
volume giornaliero), regole di step-up.

**Base giuridica**: Art. 6.1.b GDPR (esecuzione di un contratto).
**Retention**: **10 anni dalla revoca** — proposta basata su Codice del
Consumo IT (D.Lgs. 206/2005) e obblighi di conservazione contratti
commerciali. [TBD pre-launch validare con legale; la sub-categoria
"mandate" potrebbe ricevere aggiustamenti.]

### 3.5 Dati di marketplace

- **Intenti**: titolo, descrizione, categoria, lato (buy/sell), prezzo
  riserva e prezzo ideale
- **Embedding vettoriali** derivati dalla descrizione (per ricerca di
  compatibilità)
- **Match**: identificativi degli intenti di controparte, score di
  similarità, stato
- **Negoziazioni**: round di scambio agente-agente, messaggi strutturati
- **Deal**: identificativi delle parti, importo, valuta, firma WebAuthn
  di entrambe le parti, stato, scadenza

**Base giuridica**: Art. 6.1.b GDPR (esecuzione di un contratto).
**Retention**:
- Intenti, match, negoziazioni: [TBD pre-launch — proposta default "fino
  a completamento del deal o scadenza dell'intento; eventuale soft-delete
  retention 30 giorni"]
- **Deal**: **10 anni dal completamento** — proposta basata su D.P.R.
  633/1972 (fatturazione e tax retention) + Codice del Consumo. [TBD
  pre-launch validare con legale.]

### 3.6 Dati di sicurezza e antiabuso

- **Audit log**: azione eseguita, identificativo utente, identificativo
  agente, identificativo mandato, parametri non sensibili, indirizzo IP
  di provenienza, timestamp
- **Refresh token**: solo l'hash SHA-256 (mai il token in chiaro), catena
  parent_id, stato, scadenza
- **Tracking dei costi giornalieri**: costo aggregato per utente per
  giorno (in USD) e numero di tick agent

**Base giuridica**: Art. 6.1.f GDPR (legittimo interesse — sicurezza,
prevenzione frodi, prevenzione abuso) + Art. 32 GDPR (obbligo di
sicurezza del trattamento).
**Retention**:
- Audit log: **12 mesi** (best practice security; [TBD founder se
  preferenza più stringente 6 mesi o più estesa 24 mesi]).
- Refresh token attivi: fino a scadenza (massimo 30 giorni) o revoca.
  Token consumati/revocati: [TBD pre-launch — purge schedule da
  definire].
- Cost tracking: **90 giorni** (operational best practice).

### 3.7 Chiavi crittografiche dell'agente

L'agente possiede una coppia di chiavi crittografiche ed25519 utilizzata
per firmare messaggi A2A. La chiave privata è memorizzata **encrypted at
rest** tramite AES-256-GCM con master key gestita esternamente al
database. La chiave pubblica viene esposta al sistema di matching per
verificare l'autenticità delle firme.

**Base giuridica**: Art. 6.1.b GDPR (esecuzione di un contratto).
**Retention**: legata al ciclo di vita dell'agente.

### 3.8 Notifiche utente

Notifiche di servizio relative ad attività dell'agente, deal in attesa
di firma, eventi di sicurezza. Vifaras V0 **non invia comunicazioni
commerciali o di marketing**.

**Base giuridica**: Art. 6.1.b GDPR (esecuzione di un contratto).
**Retention**: [TBD pre-launch — auto-delete dopo X giorni da definire.]

### 3.9 Domande aperte agente → utente

L'agente può porre all'utente domande per chiarire intenti o decisioni
intermedie (es. "Confermi questa controproposta?"). Il testo della
domanda è generato dall'agente; la risposta dell'utente segue il flow
delle notifiche e dei messaggi di deal.

**Base giuridica**: Art. 6.1.b GDPR (esecuzione di un contratto).
**Retention**: [TBD pre-launch.]

## 4. Decisioni automatizzate (Art. 22 GDPR)

Vifaras utilizza **decisioni automatizzate** in due fasi:

1. **Matching tra intenti** — basato su similarity vettoriale (HNSW vector
   search) e filtri categoriali deterministici.
2. **Negoziazione tra agenti** — ogni agente è un'istanza di un modello
   di linguaggio (Anthropic Claude) che opera entro i limiti del mandato
   firmato dall'utente.

**Le decisioni economicamente rilevanti — la firma del deal — sono SEMPRE
umane.** Ogni deal richiede uno step-up biometrico WebAuthn esplicito
dell'utente. L'agente può proporre, solo l'utente dispone.

Diritti dell'utente in relazione alle decisioni automatizzate:

- **Diritto a una review umana** della decisione automatica: contattando
  privacy@vifaras.com l'utente può richiedere il riesame manuale di un
  match o di una scelta dell'agente.
- **Diritto di opposizione**: l'utente può escludere i propri intenti dal
  matching cancellandoli (V0). Una funzionalità esplicita "metti in
  pausa" è in fase di sviluppo (V0.5+).

## 5. Misure di mitigazione tecniche già implementate (Privacy by Design — Art. 25 GDPR)

Anche se l'alpha V0 è in evoluzione, sono già state implementate
mitigazioni tecniche concrete:

- **Truncation della descrizione** — il testo dell'intento viene troncato
  a 300 caratteri prima dell'invio al modello AI per inference (data
  minimization Art. 5.1.c).
- **Pseudonimizzazione delle comunicazioni A2A** — gli agenti utilizzano
  pseudonimi opachi (`nullifier_pseudonym` troncato), mai identificativi
  utente raw o email.
- **Privacy invariants enforced al view-builder layer** — informazioni
  sensibili come il prezzo di riserva della controparte non sono mai
  esposte all'altro agente (regola architetturale documentata e testata).
- **Encryption at rest delle chiavi crittografiche** — chiavi private
  ed25519 cifrate con AES-256-GCM; master key esterna al database.
- **Hash-only storage dei refresh token** — i token sono memorizzati come
  SHA-256 hex, mai in chiaro. Una compromissione del database non rende
  utilizzabili i token.
- **Decisioni economicamente rilevanti sempre umane** — ogni firma di
  deal richiede WebAuthn step-up biometrico (Art. 22 GDPR human-in-the-
  loop).
- **Audit log degli eventi di sicurezza** — tracciamento di rate-limit
  hit, abuse detection, riusi di refresh token.
- **Rate limiting + cost cap per-utente** — protezione contro abuso e
  contro blow-up di costo per singolo utente.

## 6. Trasferimenti internazionali e fornitori esterni

Alcuni trattamenti coinvolgono fornitori che potrebbero processare dati
fuori dall'Unione Europea. Per ciascuno indichiamo cosa viene trasmesso e
in quale forma.

### 6.1 Anthropic (modello Claude)

- **Cosa fa**: inference del modello di linguaggio per la negoziazione
  agente.
- **Cosa riceve**: snapshot dello stato dell'agente (intenti
  troncati a 300 caratteri, mandato, limiti residui, inbox di
  negoziazione, identificativi opachi UUID). **Non riceve** email,
  password, indirizzo IP, nullifier_hash raw, dati del documento di
  identità.
- **Region**: [TBD pre-launch — probabilmente Stati Uniti].
- **Base legale del trasferimento**: [TBD pre-launch — Standard
  Contractual Clauses (SCC) e Data Processing Agreement da firmare].

### 6.2 OpenAI (text-embedding-3-small)

- **Cosa fa**: generazione di embedding vettoriali dei testi degli
  intenti per la ricerca di compatibilità.
- **Cosa riceve**: titolo + descrizione completa dell'intento (non
  troncata). [TBD pre-launch — decisione su eventuale anonimizzazione
  light pre-embedding o switch a modello locale.]
- **Region**: [TBD pre-launch — probabilmente Stati Uniti].
- **Base legale del trasferimento**: [TBD pre-launch — SCC + DPA].

### 6.3 Self Protocol (verifica identità Tier 1)

- **Cosa fa**: verifica Zero-Knowledge dell'identità dell'utente.
- **Cosa riceve**: la prova ZK generata dal dispositivo dell'utente. **Non
  riceve** i dati personali del documento — l'architettura Zero-Knowledge
  garantisce che soltanto l'esito della verifica e gli attributi
  dichiarati (in forma booleana) vengano comunicati a Vifaras.
- **Region**: [TBD pre-launch].
- **Base legale del trasferimento**: [TBD pre-launch — verifica
  architettura Self DPA].

### 6.4 Fornitori in fase di valutazione (V0.5+)

I seguenti servizi saranno valutati prima del launch alpha esterno: hosting
del database (preferenza per region UE), email transazionale, backend di
osservabilità. Per ciascuno sarà firmato un Data Processing Agreement
(Art. 28 GDPR) prima del go-live.

### 6.5 Caveat sulla pseudonimizzazione

Gli embedding vettoriali derivati dalla descrizione dell'intento sono
**dati derivati**, ma **reversibili tramite similarity search**: un
attaccante con accesso al database degli embedding potrebbe ricostruire
descrizioni simili. Si tratta quindi di **pseudonimizzazione** (Art. 4.5
GDPR), non di anonimizzazione.

## 7. Diritti dell'utente

Ai sensi degli articoli 15-22 GDPR l'utente ha diritto a:

- **Accesso (Art. 15)** — ricevere copia dei propri dati.
- **Rettifica (Art. 16)** — correggere dati inesatti.
- **Cancellazione (Art. 17)** — esercitare il "diritto all'oblio".
- **Limitazione (Art. 18)** — limitare il trattamento.
- **Portabilità (Art. 20)** — ricevere i dati in formato strutturato
  comune.
- **Opposizione (Art. 21)** — opporsi al trattamento basato su legittimo
  interesse.
- **Decisioni automatizzate (Art. 22)** — vedere sezione 4.

**Modalità di esercizio V0**: invio di richiesta scritta a
**privacy@vifaras.com**. Tempo di risposta: entro 30 giorni dalla
ricezione (Art. 12.3 GDPR), prorogabile di ulteriori 60 giorni in casi
complessi con previa comunicazione all'utente.

> Endpoint self-service per l'esercizio dei diritti GDPR sono in fase di
> sviluppo (V0.5+ pre-launch). V0 alpha l'esercizio è gestito su
> richiesta via email.

**Reclamo (Art. 77 GDPR)**: l'utente può proporre reclamo all'Autorità
Garante per la protezione dei dati personali (www.garanteprivacy.it).

## 8. Cookie e tecnologie analoghe

V0 alpha utilizza **esclusivamente cookie funzionali strettamente
necessari** all'autenticazione e alla sessione. Nessun cookie di
profilazione, nessun cookie di terze parti, nessun analytics.

V0.5+ pre-launch: in caso di adozione di analytics, sarà introdotto un
banner di consenso GDPR-compliant con possibilità di opt-in granulare.

## 9. Modifiche all'informativa

Versioning semantico:

- **Major (X.0.0)** — modifiche sostanziali (nuovi processor, nuove
  finalità, modifiche alla base giuridica).
- **Minor (1.X.0)** — estensioni o chiarimenti.
- **Patch (1.0.X)** — refusi, riformulazioni.

Modifiche con impatto materiale: comunicazione via email + richiesta di
acknowledgement al login successivo (V0.5+).

## 10. Sicurezza del trattamento (Art. 32 GDPR)

Vifaras adotta misure tecniche e organizzative appropriate al rischio
del trattamento. Vedere sezione 5 ("Misure di mitigazione tecniche")
per il dettaglio.

In caso di **data breach** (Art. 33 GDPR): notifica all'Autorità Garante
entro 72 ore dalla scoperta + comunicazione agli utenti interessati
qualora il rischio per i diritti e le libertà sia elevato (Art. 34 GDPR).

## 11. Contatti

- **Email per richieste privacy**: privacy@vifaras.com (alias TBD
  pre-launch attivo)
- **Indirizzo postale**: [TBD pre-launch — sede legale del titolare]
- **Responsabile Protezione Dati (DPO)**: [TBD pre-launch — designazione
  se il trigger Art. 37 viene raggiunto]

---

**Versione documento**: 1.0.0
**Ultima revisione**: 2026-05-02
**Stato**: Draft V0 alpha — review legale richiesta prima del launch
esterno.
