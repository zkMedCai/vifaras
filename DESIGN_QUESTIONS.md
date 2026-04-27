# DESIGN QUESTIONS — Marketplace V0

Tracker delle decisioni di design che si sono presentate durante l'implementazione e di quelle che il founder ha già risolto. Pensato per la rilettura del repo a +4 mesi: non per riaprire decisioni, ma per ricordare *perché* il codice è fatto così.

Convenzione:
- **DECIDED** = il founder ha chiuso la decisione, niente da fare.
- **OPEN** = decisione ancora aperta, richiederà discussione.
- **DEFERRED** = decisione consapevolmente rimandata a una task specifica.

---

## DQ-1 — Tensione scaffold §5 ↔ principi §7 (DECIDED)

**Contesto.** La sezione §5 del brief consegna 4 file scaffold (`models/schema.py`, `services/mandate_verifier.py`, `agents/tool_layer.py`, `agents/orchestrator.py`) con direttiva "leggi e usa, non riscrivere senza motivo esplicito". I principi §7 dicono però:
- "SQLAlchemy 2.0 style queries (no legacy `query()`, usa `select()`)"
- "Datetime sempre UTC, naive evitati (usare `datetime.utcnow()` o `timezone.utc`)" — `datetime.utcnow()` è deprecato 3.12+
- Async dove sensato

Lo scaffold usa il pattern legacy: `declarative_base()`, `Session`, `.query()`, `datetime.utcnow()`.

**Decisione del founder (2026-04-27).** Gli scaffold restano **legacy as-is**. NON riscrivere. Il nuovo codice (services, API, agent runtime) segue §7 pieno: async + `select()` + `Mapped[]` + Pydantic v2.

Motivazione del founder: "la loro logica è già testata mentalmente e funziona; riscriverli ora è solo overhead di stile senza guadagno funzionale". Mantenere coerenza interna di ogni file, non coerenza globale. Pattern brutto ma pragmatico, debt accettato per V0, eventualmente unificato in V1.

**Conseguenze pratiche.**
- `MandateVerifier` resta sync con `.query()`. Servizi async che dovranno chiamarlo manterranno una **sync session pool a parte** (via `run_in_executor`, `anyio.to_thread`, o una dependency `get_sync_db()`).
- I nuovi modelli (se mai ce ne saranno) usano la 2.0 `DeclarativeBase` class style. **Due `Base` classes coesistenti nello stesso schema sono accettabili** in V0.
- Warning di deprecation `datetime.utcnow()` filtrate via `pyproject.toml [tool.pytest.ini_options].filterwarnings` per non sporcare l'output dei test.

---

## DQ-2 — Test architecture: testcontainers, non SQLite (DECIDED)

**Contesto.** Brief §8 dice "Unit test: per service, DB SQLite in-memory". Ma:
- Lo schema è Postgres-pure: `JSONB`, `UUID`, `pgvector.Vector(1536)`.
- I service futuri useranno feature Postgres-specifiche: cosine similarity, JSONB queries su `mandate.scope`, `SELECT FOR UPDATE` per optimistic locking dei deal (EC5).
- TypeDecorator dual-dialect richiederebbe di toccare lo scaffold (vietato da DQ-1) o aggiungere uno strato di astrazione "solo per i test" (debt).

**Decisione del founder (2026-04-27).** Il brief è in tensione con sé stesso su questo punto. **Override**: tutti i test che toccano il DB girano su `pgvector/pgvector:pg16` via testcontainers. Niente SQLite, niente TypeDecorator di compatibilità.

Motivazione: 
1. Schema Postgres-only, lo scaffold non si tocca.
2. Test su SQLite testerebbero un prodotto diverso (i bug più subdoli — JSON vs JSONB — si nascondono lì).
3. testcontainers nel 2026 sono veloci: ~2-3 secondi a container, immagini cached, session-scoped fixture amortizza per tutta la run.

**Pattern di fixture (in `backend/tests/conftest.py`).**
- Session-scoped lazy: `_pg_container` parte solo se un test richiede `db_session` (direttamente o transitivamente). `pytest -m "not db"` non boota nulla.
- Function-scoped: `db_session` apre transaction esterna + Session con `join_transaction_mode="create_savepoint"`. Rollback al teardown — niente cleanup, niente stato condiviso tra test.
- Marker `@pytest.mark.db` su ogni test che tocca il DB. `pytest -m "not db"` per fast-track.

**Eccezione SQLite-style.** Test puramente computazionali (parsing string, aritmetica Decimal, canonicalizzazione JSON) niente fixture, niente container. Plain `def test_xxx(): ...`.

---

## DQ-3 — Vector index su `intents.description_embedding` (DEFERRED → 4.3)

**Contesto.** Lo schema dichiara la colonna `description_embedding Vector(1536)` ma **nessun indice vettoriale**. Per V0 con ~100 utenti il sequential scan è OK — probabilmente più veloce dell'indice su corpus piccoli.

**Decisione del founder (2026-04-27).** Pianificato HNSW con `vector_cosine_ops` da creare in una migration separata **prima di task 4.3 (Match service)**, quando si sa la dimensione del corpus e i parametri ottimali. A 5K-10K vettori HNSW è il default sano (m=16, ef_construction=64).

**Migration nota di promemoria** (da scrivere in 4.x):
```sql
CREATE INDEX intents_description_embedding_hnsw_cosine_idx
  ON intents USING hnsw (description_embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
```

Cosine perché brief §3 Marketplace parla di "Cosine similarity sui description_embedding".

---

## DQ-4 — `/health` vs `/ready` (DEFERRED → 7.x)

**Contesto.** Brief 1.1 chiede "healthcheck endpoint", senza specificare se liveness o readiness.

**Decisione del founder (2026-04-27).** `/health` resta **liveness** (sempre 200 con campo `db: ok|down`). Un endpoint separato `/ready` che ritorna **503** quando il DB è down sarà aggiunto in **task 7.x** (Hardening & ship), pattern K8s-style anche se non siamo su K8s.

---

## DQ-5 — Posizionamento dei platform hard limits del brief §3 (DEFERRED → 2.4 e 4.1)

**Contesto.** Brief §3 elenca limiti hard-coded di piattaforma (max €1000/deal, €5000/mese per mandate, 10 deal/giorno, geo IT, categorie proibite). Sembrano configurazione globale.

**Decisione del founder (2026-04-27).** **NON in `core/config.py`**. Vivono dove sono enforced:
- Cap sui deal/volume → `services/mandate_service.py` (task 2.4) quando l'utente firma un mandate, validando che i suoi limiti non superino i platform caps.
- Limiti su intent attivi → `services/intent_service.py` (task 4.1).

Motivazione: "Config morta è debt, non resilienza."

---

## DQ-6 — Drop dell'estensione `vector` su downgrade Alembic (DECIDED)

**Contesto.** La migration 1.2 fa `CREATE EXTENSION IF NOT EXISTS vector`. Logica simmetrica suggerirebbe `DROP EXTENSION` su downgrade.

**Decisione del founder (2026-04-27).** Il downgrade **NON droppa l'extension**. L'extension è per-database e potrebbe essere usata da altri schema. `CREATE EXTENSION IF NOT EXISTS` è idempotente all'upgrade successivo, quindi nessun problema operativo. `DROP EXTENSION` rischierebbe di rompere altri schema condivisi → trade-off non vale.

---

## DQ-7 — Driver Postgres: doppio sync + async (DECIDED)

**Contesto.** Lo scaffold §5 (`mandate_verifier.py`, `tool_layer.py`, `orchestrator.py`) usa `sqlalchemy.orm.Session` sync. Brief §7 vuole "async dove sensato (endpoint FastAPI, chiamate LLM)". Alembic è natively sync (async è possibile ma rumoroso).

**Decisione del founder (2026-04-27).** `app/core/db.py` espone **due engine sullo stesso DB**:
- `engine` async (asyncpg) → endpoint FastAPI, nuovi service async.
- `sync_engine` (psycopg) → scaffold, Alembic, test.

La coesistenza è intenzionale, non disordine. Documentata in docstring del modulo. Pattern unificato eventualmente in V1 se diventa fastidioso.
