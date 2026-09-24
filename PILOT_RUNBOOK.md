# Project Scope — Real Pilot Runbook

## Purpose

Validate whether Project Scope helps a real SME identify commercial opportunities worth acting on. The classifier is frozen at scoring v0.8.7 / intelligence v0.6.9 during the first pilot unless a clear defect is demonstrated.

## Pilot setup

For each pilot company, capture:

- business summary and actual services sold;
- explicit capabilities;
- scopes the company does **not** sell;
- target sectors;
- realistic geography;
- minimum and maximum opportunity value;
- certifications / approvals actually held;
- preferred buyers;
- known buyer / Tier-1 access routes and barriers;
- direct, framework, vendor-registration and subcontract routes.

Do not invent qualifications or buyer access. Unknown is a valid state.

## Review rule

For every surfaced opportunity, answer:

> Would the company's BD/commercial manager spend roughly 15–30 minutes investigating this today?

Use:

- **RELEVANT** — a realistic route to revenue may exist and active investigation is justified.
- **WATCH** — commercially plausible, but too early, incomplete or uncertain for active BD effort.
- **NOT_RELEVANT** — not worth this company's commercial time even after considering subcontract/downstream routes.

For NOT_RELEVANT, record the closest reason:
WRONG_SECTOR, WRONG_CAPABILITY, WRONG_GEOGRAPHY, CONTRACT_VALUE,
NO_REALISTIC_ROUTE, DUPLICATE_OR_STALE, or OTHER.

## First-pilot operating period

Run for 14 calendar days with normal FTS / PCS / NSTA schedules.

During the pilot:

1. Review every surviving customer-facing signal.
2. Record a label rather than tuning the classifier immediately.
3. Resolve route-to-market only for commercially meaningful opportunities.
4. Keep historical or weak records in Research; do not promote them to make the dashboard look busy.
5. Record any opportunity the company already knew about that Scope should reasonably have found.

## Pilot metrics

Primary:
- number of customer-facing opportunities;
- percentage labelled RELEVANT / WATCH / NOT_RELEVANT;
- false-positive reasons;
- number of opportunities that caused a real BD action;
- time from source publication to Scope surfacing the opportunity;
- number of useful route-to-market investigations.

Guardrails:
- zero opportunities is acceptable if the market contains no qualified work;
- do not improve apparent precision by hiding plausible misses;
- do not tune on one unusual notice;
- require repeatable evidence before changing scoring weights/rules.

## Success evidence

The first pilot is encouraging if the business owner can point to opportunities they would genuinely investigate, the irrelevant-survivor rate is manageable, and the output saves meaningful search/triage time.

Commercial validation is stronger if at least one signal leads to a concrete action such as:
- contacting a buyer or package holder;
- vendor registration / prequalification;
- joining or monitoring a framework;
- approaching a Tier-1/subcontract route;
- deciding to bid / no-bid with better information.

## Change control

During the pilot, scoring/intelligence stays frozen unless:
- a reproducible false-positive pattern is found;
- a clearly relevant known opportunity is systematically missed; or
- an operational bug corrupts the output.

Any classifier change should be regression-tested against prior cases before deployment.

## End-of-pilot review

At day 14, review:
- labels and rejection reasons;
- useful commercial actions;
- known misses;
- buyer-access findings;
- source coverage;
- speed and usability;
- whether the company would continue using / pay for the service.

Only then decide whether the next investment is:
- better ranking,
- another data source,
- route-to-market intelligence,
- alerts/delivery,
- collaboration/authentication,
- or a broader market/sector.
