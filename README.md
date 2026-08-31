# Project Scope v0.1.1

Private commercial-intelligence engine for Scottish / UK energy supply-chain opportunities.

## v0.1 purpose
- ingest official Public Contracts Scotland OCDS notices
- retain immutable raw source data
- normalise procurements and awards into Postgres
- apply transparent energy / supplier-fit scoring
- rank opportunities for a configurable supplier profile
- expose a small private FastAPI inspection dashboard

This is deliberately not a customer-facing SaaS product yet.

## Railway services
1. Postgres
2. Web — `uvicorn main:app --host 0.0.0.0 --port $PORT`
3. PCS Collector — `python collector_pcs.py` on a Railway cron

Use `python init_db.py` once, then `python seed_demo_customer.py`, then run the collector.
