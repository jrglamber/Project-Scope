"""
Project Scope - Find a Tender collector
Version: 0.5.1

Official source:
https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages

The collector uses a rolling lookback window and OCDS stages planning,tender,award.
Planning -> EMERGING
Tender   -> LIVE
Award    -> INTELLIGENCE / retained research intelligence
"""

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import certifi
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from db import connection
from classification import classify_energy, sector_gate_passed, CLASSIFIER_VERSION
from scoring import score_procurement_for_customer
from intelligence import classify_award_intelligence

COLLECTOR_VERSION = "0.5.1"
BASE = os.environ.get(
    "FTS_API_BASE",
    "https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages",
)
LOOKBACK_HOURS = max(1, int(os.environ.get("FTS_LOOKBACK_HOURS", "24")))
ZERO_RESULT_FALLBACK_HOURS = max(LOOKBACK_HOURS, int(os.environ.get("FTS_ZERO_RESULT_FALLBACK_HOURS", "168")))
STAGES = os.environ.get("FTS_STAGES", "planning,tender,award")
MAX_PAGES = max(1, int(os.environ.get("FTS_MAX_PAGES", "10")))
ENERGY_MIN_SCORE = int(os.environ.get("ENERGY_MIN_SCORE", "2"))
USER_AGENT = f"Project-Scope/{COLLECTOR_VERSION}"



def build_http_session():
    session = requests.Session()

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
        respect_retry_after_header=True,
    )

    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=4,
        pool_maxsize=8,
    )

    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    })

    return session



def utcnow():
    return datetime.now(timezone.utc)


def stable_hash(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def get(data, *path):
    cur = data
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
        if cur is None:
            return None
    return cur


def parse_dt(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def release_tags(release):
    return [str(x).lower() for x in (release.get("tag") or [])]


def notice_type(release):
    tags = release_tags(release)
    if "award" in tags:
        return "Find a Tender Award"
    if "planning" in tags:
        return "Find a Tender Planning"
    if "tender" in tags:
        return "Find a Tender Tender"
    return "Find a Tender Notice"


def signal_type_for_release(release):
    tags = release_tags(release)
    if "award" in tags:
        return "INTELLIGENCE"
    if "planning" in tags:
        return "EMERGING"
    return "LIVE"


def buyer_name(release):
    direct = get(release, "buyer", "name")
    if direct:
        return direct
    buyer_id = get(release, "buyer", "id")
    for party in release.get("parties") or []:
        roles = party.get("roles") or []
        if "buyer" in roles or (buyer_id and party.get("id") == buyer_id):
            return party.get("name")
    return None


def cpv_codes(release):
    out = []
    for item in get(release, "tender", "items") or []:
        classes = [item.get("classification")] + (item.get("additionalClassifications") or [])
        for c in classes:
            if c and c.get("id"):
                out.append({
                    "scheme": c.get("scheme"),
                    "id": c.get("id"),
                    "description": c.get("description"),
                })
    # Some FTS releases expose classification at tender level.
    c = get(release, "tender", "classification")
    if c and c.get("id"):
        out.append({"scheme": c.get("scheme"), "id": c.get("id"), "description": c.get("description")})
    return out


def location_text(release):
    parts = []
    for item in get(release, "tender", "items") or []:
        addr = item.get("deliveryAddress") or item.get("deliveryAddresses") or {}
        addresses = addr if isinstance(addr, list) else [addr]
        for a in addresses:
            if not isinstance(a, dict):
                continue
            for key in ("streetAddress", "locality", "region", "postalCode", "countryName"):
                if a.get(key):
                    parts.append(str(a[key]))
    return ", ".join(dict.fromkeys(parts)) or None


def source_url(release):
    ocid = release.get("ocid")
    if ocid:
        return f"https://www.find-tender.service.gov.uk/procurement/{ocid}"
    return "https://www.find-tender.service.gov.uk/"


def upsert_company(cur, name, kind):
    if not name:
        return None
    cur.execute(
        """
        INSERT INTO companies(canonical_name, company_type)
        VALUES (%s,%s)
        ON CONFLICT(canonical_name) DO UPDATE SET
            updated_at_utc=NOW(),
            company_type=COALESCE(companies.company_type, EXCLUDED.company_type)
        RETURNING id
        """,
        (name.strip(), kind),
    )
    return cur.fetchone()["id"]


def customers(cur):
    cur.execute("SELECT * FROM customer_profiles WHERE active=TRUE")
    return cur.fetchall()


def ensure_research_schema(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS research_intelligence (
            id BIGSERIAL PRIMARY KEY,
            procurement_id BIGINT NOT NULL REFERENCES procurements(id) ON DELETE CASCADE,
            project_id BIGINT REFERENCES projects(id),
            buyer_company_id BIGINT REFERENCES companies(id),
            title TEXT NOT NULL,
            intelligence_kind TEXT NOT NULL CHECK (
                intelligence_kind IN ('DIRECT','DOWNSTREAM','RESEARCH_ONLY')
            ),
            customer_facing BOOLEAN NOT NULL DEFAULT FALSE,
            confidence INTEGER NOT NULL DEFAULT 50 CHECK (confidence BETWEEN 0 AND 100),
            likely_downstream_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
            reason_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            first_seen_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(procurement_id)
        )
        """
    )


def upsert_research_intelligence(cur, procurement, buyer_id, raw_event_id, url, intelligence):
    cur.execute(
        """
        INSERT INTO research_intelligence(
            procurement_id,project_id,buyer_company_id,title,intelligence_kind,
            customer_facing,confidence,likely_downstream_scopes,reason_json,evidence_json
        ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb)
        ON CONFLICT(procurement_id) DO UPDATE SET
            intelligence_kind=EXCLUDED.intelligence_kind,
            customer_facing=EXCLUDED.customer_facing,
            confidence=EXCLUDED.confidence,
            likely_downstream_scopes=EXCLUDED.likely_downstream_scopes,
            reason_json=EXCLUDED.reason_json,
            evidence_json=EXCLUDED.evidence_json,
            status='ACTIVE',last_updated_at_utc=NOW()
        """,
        (
            procurement["id"], procurement.get("project_id"), buyer_id, procurement["title"],
            intelligence["kind"], intelligence["customer_facing"], intelligence["confidence"],
            json.dumps(intelligence["likely_downstream_scopes"]),
            json.dumps(intelligence, default=str),
            json.dumps([{"raw_event_id":raw_event_id,"source":"Find a Tender","url":url}]),
        ),
    )


def process_release(cur, release):
    ocid = release.get("ocid")
    release_id = release.get("id") or stable_hash(release)[:32]
    title = get(release,"tender","title") or release.get("title") or "(untitled notice)"
    description = get(release,"tender","description") or ""
    published = parse_dt(release.get("date"))
    deadline = parse_dt(get(release,"tender","tenderPeriod","endDate"))
    buyer = buyer_name(release)
    buyer_id = upsert_company(cur, buyer, "Buyer")
    cpv = cpv_codes(release)
    cpv_text = " ".join(
        " ".join([str(x.get("id") or ""), str(x.get("description") or "")]).strip()
        for x in cpv
    )
    energy_score, energy_hits = classify_energy(title, description, cpv_text)
    sector_pass = sector_gate_passed(energy_hits)
    url = source_url(release)
    raw_hash = stable_hash(release)
    ntype = notice_type(release)

    cur.execute(
        """
        INSERT INTO raw_events(
            source,source_event_id,source_url,event_type,published_at_utc,content_hash,title,raw_json
        ) VALUES('find_a_tender',%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT(source,content_hash) DO UPDATE SET
            collected_at_utc=NOW(),source_url=EXCLUDED.source_url
        RETURNING id
        """,
        (ocid or release_id, url, ntype, published, raw_hash, title, json.dumps(release)),
    )
    raw_event_id = cur.fetchone()["id"]

    cur.execute(
        """
        INSERT INTO procurements(
            source,ocid,release_id,notice_type,title,description,buyer_name,buyer_company_id,
            published_at_utc,deadline_at_utc,status,procurement_method,cpv_codes,location_text,
            value_amount,value_currency,raw_event_id,energy_relevance_score,energy_relevance_reasons,
            sector_gate_passed,classifier_version
        ) VALUES(
            'find_a_tender',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb,%s,%s
        )
        ON CONFLICT(source,ocid,release_id) DO UPDATE SET
            notice_type=EXCLUDED.notice_type,title=EXCLUDED.title,description=EXCLUDED.description,
            buyer_name=EXCLUDED.buyer_name,buyer_company_id=EXCLUDED.buyer_company_id,
            published_at_utc=EXCLUDED.published_at_utc,deadline_at_utc=EXCLUDED.deadline_at_utc,
            status=EXCLUDED.status,procurement_method=EXCLUDED.procurement_method,
            cpv_codes=EXCLUDED.cpv_codes,location_text=EXCLUDED.location_text,
            value_amount=EXCLUDED.value_amount,value_currency=EXCLUDED.value_currency,
            raw_event_id=EXCLUDED.raw_event_id,energy_relevance_score=EXCLUDED.energy_relevance_score,
            energy_relevance_reasons=EXCLUDED.energy_relevance_reasons,
            sector_gate_passed=EXCLUDED.sector_gate_passed,
            classifier_version=EXCLUDED.classifier_version,updated_at_utc=NOW()
        RETURNING *
        """,
        (
            ocid,release_id,ntype,title,description,buyer,buyer_id,published,deadline,
            get(release,"tender","status"),get(release,"tender","procurementMethod"),json.dumps(cpv),
            location_text(release),get(release,"tender","value","amount"),
            get(release,"tender","value","currency"),raw_event_id,energy_score,json.dumps(energy_hits),
            sector_pass,CLASSIFIER_VERSION,
        ),
    )
    procurement = cur.fetchone()

    for award in release.get("awards") or []:
        for supplier in award.get("suppliers") or []:
            sname = supplier.get("name")
            if not sname:
                continue
            sid = upsert_company(cur, sname, "Supplier")
            aid = award.get("id") or stable_hash(award)[:24]
            adate = parse_dt(award.get("date"))
            cur.execute(
                """
                INSERT INTO contract_awards(
                    procurement_id,source,ocid,award_id,buyer_name,supplier_name,supplier_company_id,
                    title,description,award_date,value_amount,value_currency,raw_event_id
                ) VALUES(%s,'find_a_tender',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(source,ocid,award_id,supplier_name) DO NOTHING
                """,
                (
                    procurement["id"],ocid,aid,buyer,sname,sid,award.get("title") or title,
                    award.get("description") or description,adate.date() if adate else None,
                    get(award,"value","amount"),get(award,"value","currency"),raw_event_id,
                ),
            )

    stype = signal_type_for_release(release)
    active_customers = customers(cur)

    if (not sector_pass) or energy_score < ENERGY_MIN_SCORE:
        for customer in active_customers:
            cur.execute(
                """
                UPDATE opportunity_signals SET status='INACTIVE',last_updated_at_utc=NOW()
                WHERE customer_profile_id=%s AND procurement_id=%s AND signal_type=%s
                """,
                (customer["id"], procurement["id"], stype),
            )
        return

    award_intel = None
    if stype == "INTELLIGENCE":
        award_intel = classify_award_intelligence(title, description)
        upsert_research_intelligence(cur, procurement, buyer_id, raw_event_id, url, award_intel)
        if not award_intel["customer_facing"]:
            for customer in active_customers:
                cur.execute(
                    """
                    UPDATE opportunity_signals SET status='INACTIVE',last_updated_at_utc=NOW()
                    WHERE customer_profile_id=%s AND procurement_id=%s AND signal_type='INTELLIGENCE'
                    """,
                    (customer["id"], procurement["id"]),
                )
            return

    for customer in active_customers:
        score, reasons = score_procurement_for_customer(procurement, customer)
        if award_intel:
            reasons["intelligence"] = award_intel
            if award_intel["kind"] == "DOWNSTREAM" and score < 45:
                score = min(70, max(45, score + award_intel["downstream_score"] * 2))
        if score < 35:
            cur.execute(
                """
                UPDATE opportunity_signals SET status='INACTIVE',relevance_score=%s,
                    reason_json=%s::jsonb,last_updated_at_utc=NOW()
                WHERE customer_profile_id=%s AND procurement_id=%s AND signal_type=%s
                """,
                (score,json.dumps(reasons,default=str),customer["id"],procurement["id"],stype),
            )
            continue

        if stype == "EMERGING":
            action = "Review this early-stage notice, identify the buyer/procurement route and consider early engagement."
            timing = "Early / pre-tender"
        elif stype == "INTELLIGENCE":
            if award_intel and award_intel["kind"] == "DOWNSTREAM":
                scopes = ", ".join(award_intel["likely_downstream_scopes"][:5])
                action = "Review the award for downstream supplier-entry opportunities" + (f" in {scopes}." if scopes else ".")
            else:
                action = "Review this award for direct capability relevance and possible supplier-entry opportunities."
            timing = "Review downstream"
        else:
            action = "Review the notice, procurement route and named buyer/contact before deciding whether to engage."
            timing = "Now"

        cur.execute(
            """
            INSERT INTO opportunity_signals(
                customer_profile_id,signal_type,procurement_id,buyer_company_id,title,relevance_score,
                confidence,timing_label,reason_json,recommended_action,evidence_json
            ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)
            ON CONFLICT(customer_profile_id,signal_type,procurement_id) DO UPDATE SET
                relevance_score=EXCLUDED.relevance_score,confidence=EXCLUDED.confidence,
                timing_label=EXCLUDED.timing_label,reason_json=EXCLUDED.reason_json,
                recommended_action=EXCLUDED.recommended_action,evidence_json=EXCLUDED.evidence_json,
                last_updated_at_utc=NOW(),status='ACTIVE'
            """,
            (
                customer["id"],stype,procurement["id"],buyer_id,title,score,
                award_intel["confidence"] if award_intel else (70 if score>=70 else 55),
                timing,json.dumps(reasons,default=str),action,
                json.dumps([{"raw_event_id":raw_event_id,"source":"Find a Tender","url":url}]),
            ),
        )


def next_cursor(payload):
    for key in ("nextCursor", "next_cursor", "cursor"):
        value = payload.get(key) if isinstance(payload, dict) else None
        if value:
            return str(value)
    links = payload.get("links") if isinstance(payload, dict) else None
    if isinstance(links, dict) and links.get("next"):
        try:
            qs = parse_qs(urlparse(links["next"]).query)
            return (qs.get("cursor") or [None])[0]
        except Exception:
            return None
    return None


def fetch_pages(session, lookback_hours):
    updated_to = utcnow()
    updated_from = updated_to - timedelta(hours=lookback_hours)

    frozen_window = {
        "updated_from": updated_from.strftime("%Y-%m-%dT%H:%M:%S"),
        "updated_to": updated_to.strftime("%Y-%m-%dT%H:%M:%S"),
        "lookback_hours": lookback_hours,
    }

    print(
        "Find a Tender query window:",
        json.dumps(frozen_window),
        flush=True,
    )

    cursor = None

    for page in range(1, MAX_PAGES + 1):
        params = {
            "updatedFrom": frozen_window["updated_from"],
            "updatedTo": frozen_window["updated_to"],
            "stages": STAGES,
            "limit": 100,
        }

        if cursor:
            params["cursor"] = cursor

        response = session.get(
            BASE,
            params=params,
            timeout=60,
            verify=certifi.where(),
        )
        response.raise_for_status()

        payload = response.json()
        batch = payload.get("releases") or []

        yield page, batch, response.url, frozen_window

        new_cursor = next_cursor(payload)

        if not new_cursor or new_cursor == cursor:
            break

        cursor = new_cursor


def main():
    with connection() as conn:
        with conn.cursor() as cur:
            ensure_research_schema(cur)
            cur.execute("INSERT INTO collector_runs(collector) VALUES('find_a_tender') RETURNING id")
            run_id = cur.fetchone()["id"]
        conn.commit()

        session = build_http_session()
        fetched = processed = errors = 0
        messages = []
        pages = 0
        effective_lookback_hours = LOOKBACK_HOURS
        zero_result_fallback_used = False

        def collect_window(lookback_hours):
            nonlocal fetched, processed, errors, pages

            window_fetched = 0

            for page, batch, _, _window in fetch_pages(session, lookback_hours):
                pages = page
                fetched += len(batch)
                window_fetched += len(batch)

                print(
                    f"Find a Tender page {page}: {len(batch)} releases",
                    flush=True,
                )

                for idx, release in enumerate(batch, start=1):
                    try:
                        with conn.cursor() as cur:
                            process_release(cur, release)

                        conn.commit()
                        processed += 1

                    except Exception as exc:
                        conn.rollback()
                        errors += 1
                        messages.append(
                            f"page {page} item {idx}: "
                            f"{type(exc).__name__}: {exc}"
                        )

            return window_fetched

        try:
            primary_fetched = collect_window(LOOKBACK_HOURS)

            if (
                primary_fetched == 0
                and ZERO_RESULT_FALLBACK_HOURS > LOOKBACK_HOURS
            ):
                zero_result_fallback_used = True
                effective_lookback_hours = ZERO_RESULT_FALLBACK_HOURS

                print(
                    "Find a Tender primary window returned 0 releases; "
                    f"retrying once with {ZERO_RESULT_FALLBACK_HOURS}h lookback.",
                    flush=True,
                )

                collect_window(ZERO_RESULT_FALLBACK_HOURS)

        except Exception as exc:
            conn.rollback()
            errors += 1
            messages.append(
                f"collector fetch: {type(exc).__name__}: {exc}"
            )

        status = "ok" if errors == 0 else "partial" if processed > 0 else "failed"
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE collector_runs SET finished_at_utc=NOW(),status=%s,fetched_count=%s,
                    processed_count=%s,error_count=%s,error_text=%s WHERE id=%s
                """,
                (status,fetched,processed,errors,"\n".join(messages)[-12000:] if messages else None,run_id),
            )
        conn.commit()

    print(json.dumps({
        "collector":"find_a_tender","collector_version":COLLECTOR_VERSION,
        "status":status,"pages":pages,"fetched":fetched,"processed":processed,"errors":errors,
        "lookback_hours":LOOKBACK_HOURS,
        "effective_lookback_hours":effective_lookback_hours,
        "zero_result_fallback_used":zero_result_fallback_used,
        "zero_result_fallback_hours":ZERO_RESULT_FALLBACK_HOURS,
        "http_retry_policy":"4 attempts with exponential backoff",
        "stages":STAGES,
    }))
    if messages:
        print("collector_diagnostics:", "\n".join(messages)[-12000:])


if __name__ == "__main__":
    main()
