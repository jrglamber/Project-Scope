# Project Scope — Railway setup

## Postgres
Create a Railway PostgreSQL service.

## Web service
Start command:
`uvicorn main:app --host 0.0.0.0 --port $PORT`

Variable:
`DATABASE_URL=${{Postgres.DATABASE_URL}}`

Recommended pre-deploy:
`python init_db.py`

## Collector service
Use the same repo.

Start command:
`python collector_pcs.py`

Variables:
- `DATABASE_URL=${{Postgres.DATABASE_URL}}`
- `PCS_API_BASE=https://api.publiccontractsscotland.gov.uk/v1`
- `COLLECT_MONTHS_BACK=1`
- `ENERGY_MIN_SCORE=2`

Initial cron:
`17 */2 * * *`

Run once after the schema exists:
`python seed_demo_customer.py`

## Acceptance checks
- `/health` returns ok.
- a `collector_runs` row completes.
- `raw_events` contains raw OCDS data.
- `procurements` contains normalized notices.
- awards populate `contract_awards` where supplier data exists.
- relevant notices create `opportunity_signals`.
- rerunning does not duplicate the same release.

## Planned v0.2
- Find a Tender OCDS collector
- CPV taxonomy
- entity alias resolution
- developer / Tier-1 event sources
- AI structured extraction
- first EMERGING opportunity inference rules
- prediction outcome tracking
