# JWT Secret Rotation Procedure

Pattern: zero-downtime rotation con overlap window. Access token in flight
continuano a funzionare durante la rotation finché la loro TTL non scade
naturalmente.

Implementation reference: task `[7.4.3]` in `PROGRESS.md`. Code in
`backend/app/core/security.py::_encode` + `_decode`.

## Quando ruotare

V0 manual procedure. Trigger:

- **Pre-launch alpha esterno** — rotare il dev placeholder
  (`change-me-in-dev-and-always-rotate-in-prod`) con un secret production-grade
  generato fresh.
- **Post-incident** — secret leak suspected o exposure (env var leaked in
  logs, repo accidentally pushed, host compromise).
- **Periodic policy** (V0.5+) — cadenza quarterly o post-deploy major.

V0.5+ automation: rotation scheduled cron. Vedi entry "JWT secret rotation
automation" in `IDEAS_BACKLOG.md`.

## Stato pre-rotation (steady state)

Backend running con:

```
JWT_SECRET_CURRENT=<secret_X>
JWT_SECRET_PREVIOUS=
```

Verifica baseline:

```bash
curl -s http://localhost:8000/metrics | grep vifaras_jwt_decode_fallback_total
# Atteso: vifaras_jwt_decode_fallback_total 0.0
```

Se il counter è > 0 in steady state, una rotation è ancora aperta — chiudila
(Step 5) prima di iniziarne una nuova.

## Step 1 — Generate new secret

```bash
NEW_SECRET=$(openssl rand -base64 32)
```

Salva il valore in secure storage (1Password, AWS Secrets Manager, GCP
Secret Manager, ecc.). NON loggarlo, NON committarlo, NON includerlo in
chat / issue tracker / screenshot.

## Step 2 — Update env vars (atomic)

Update `.env` (V0 dev) o secrets manager (V0.5+ prod) in un singolo
deploy/commit:

```
JWT_SECRET_CURRENT=<NEW_SECRET>     # firma i nuovi token
JWT_SECRET_PREVIOUS=<secret_X>      # verifica i token in flight pre-rotation
```

**Critical**: i due valori vanno aggiornati nello **stesso** atto di deploy.
Se `JWT_SECRET_CURRENT` cambia ma `JWT_SECRET_PREVIOUS` resta vuoto, c'è
una window in cui ogni token in flight pre-rotation fallisce signature
verify → user kickati.

V0 dev locale: edit manuale di `.env`, poi reload. Una sola finestra di
modifica, atomicità garantita dalla sequenzialità del filesystem.

V0.5+ prod: il deploy tool deve garantire atomicità (atomic config swap o
two-phase secret update).

## Step 3 — Reload backend

```bash
# Strategy depends on deployment:
# - dev locale:   Ctrl+C uvicorn → restart con env aggiornata
# - systemd:      systemctl reload <service>
# - docker:       docker compose restart backend
# - kubernetes:   kubectl rollout restart deployment/<name>
```

Backend post-reload:

- Firma ogni nuovo token con `JWT_SECRET_CURRENT` (= NEW_SECRET)
- Verifica i token in arrivo provando prima `current`, poi fallback a
  `previous` (= secret_X) se la signature non matcha

## Step 4 — Monitor overlap window

Window duration = max access token TTL (`jwt_access_ttl_min`, default
**15 min**) + safety margin → totale **30 min** raccomandato.

Durante la window:

```bash
# Metric: quanti decode hanno usato il fallback
curl -s http://localhost:8000/metrics | grep vifaras_jwt_decode_fallback_total
```

Andamento atteso:

- **T+0s** (post-rotation): counter inizia a incrementare — i token
  pre-rotation in flight passano per il fallback.
- **T+15min**: i token pre-rotation iniziano a scadere naturalmente
  (TTL access = 15 min).
- **T+30min**: counter dovrebbe essere stabile — niente più nuovi
  fallback.

Se il counter continua a incrementare dopo T+30min, **fermati e
investiga** prima di proseguire allo Step 5. Possibili cause:

- Client con cache stale (mobile app non re-loggata?)
- Bug applicativo che firma con il secret sbagliato (regression)
- Time skew tra client e server (token con `iat` falsato)
- Scheduled job long-running che ha cached un access token

## Step 5 — Retire previous secret

Una volta confermato il counter stabile (default T+30min):

```
JWT_SECRET_CURRENT=<NEW_SECRET>     # invariato
JWT_SECRET_PREVIOUS=                # vuoto = no rotation in corso
```

Reload backend (stesso comando dello Step 3). Rotation **completata**.

Verifica final:

```bash
curl -s http://localhost:8000/metrics | grep vifaras_jwt_decode_fallback_total
# Counter resta al valore X (cumulative since process start);
# zero NUOVI increment dopo lo Step 5.
```

## Audit trail

V0: niente audit row dedicato per rotation events. La procedure è
founder-side, manuale; l'unico signal observability è il counter
Prometheus.

V0.5+: rotation events logged in `audit_log` con
`SecurityActions.JWT_SECRET_ROTATED` (entry pianificata in
IDEAS_BACKLOG, non implementata).

## Rollback procedure

Se durante la window emergono issue (es. counter spike inaspettato, errori
401 burst), rollback:

```
JWT_SECRET_CURRENT=<secret_X>       # revert al vecchio
JWT_SECRET_PREVIOUS=<NEW_SECRET>    # NEW_SECRET diventa "previous" temporaneo
```

Reload backend. Analisi:

- Token in flight emessi PRE-Step 3 ora vengono verificati direttamente
  da `current` (= secret_X) → niente fallback necessario.
- Token in flight emessi POST-Step 3 (firmati con NEW_SECRET nei pochi
  minuti tra rotation e rollback) vengono verificati via fallback su
  `previous` (= NEW_SECRET).

Stessa window 30 min, poi `JWT_SECRET_PREVIOUS=` per chiudere il rollback.

## Checklist pre-rotation

- [ ] NEW_SECRET generato + salvato in secure storage (NON loggato)
- [ ] Backup di `.env` (V0) o snapshot del secrets manager (V0.5+)
- [ ] Baseline metric verificato (`vifaras_jwt_decode_fallback_total 0.0`)
- [ ] Window monitor strategy definita (chi guarda il counter, ogni quanto)
- [ ] Communication plan (rotation pianificata = niente surprise per il team)
- [ ] Rollback path mentalmente provata (sai cosa fare se Step 4 esplode)

## Reference

- Implementation: `[7.4.3]` in `PROGRESS.md`
- Code: `backend/app/core/security.py::_encode` + `_decode`
- Settings: `jwt_secret_current` + `jwt_secret_previous` in
  `backend/app/core/config.py`
- Metric: `vifaras_jwt_decode_fallback_total` (Prometheus counter)
