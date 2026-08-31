import os
import json
import hashlib
from datetime import datetime, timezone

import certifi
import requests
from dateutil.relativedelta import relativedelta

from db import connection
from classification import classify_energy
from scoring import score_procurement_for_customer


BASE = os.environ.get(
    "PCS_API_BASE",
    "https://api.publiccontractsscotland.gov.uk/v1",
).rstrip("/")

MONTHS_BACK = max(0, int(os.environ.get("COLLECT_MONTHS_BACK", "1")))
ENERGY_MIN_SCORE = int(os.environ.get("ENERGY_MIN_SCORE", "2"))

NOTICE_TYPES = [1, 2, 3, 4, 5, 6, 101, 102, 103, 104]

LABELS = {
    1: "Prior Information Notice",
    2: "Contract Notice",
    3: "Contract Award Notice",
    4: "Prior Information Notice (Utilities)",
    5: "Contract Notice (Utilities)",
    6: "Contract Award Notice (Utilities)",
    101: "Site Prior Information Notice",
    102: "Site Contract Notice",
    103: "Site Contract Award Notice",
    104: "Site Quick Quote Award",
}


def now():
    return datetime.now(timezone.utc)


def stable_hash(value):
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def get(data, *path):
    current = data
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def parse_dt(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def releases(payload):
    if isinstance(payload, dict) and isinstance(payload.get("releases"), list):
        return payload["releases"]

    if isinstance(payload, dict) and isinstance(payload.get("records"), list):
        output = []
        for record in payload["records"]:
            output.extend(record.get("releases") or [])
        return output

    if isinstance(payload, list):
        output = []
        for item in payload:
            if not isinstance(item, dict):
                continue
            if isinstance(item.get("releases"), list):
                output.extend(item["releases"])
            else:
                output.append(item)
        return output

    if isinstance(payload, dict) and (payload.get("ocid") or payload.get("id")):
        return [payload]

    return []


def buyer_name(release):
    buyer_id = get(release, "buyer", "id")

    for party in release.get("parties") or []:
        roles = party.get("roles") or []
        if "buyer" in roles or (buyer_id and party.get("id") == buyer_id):
            return party.get("name")

    return get(release, "buyer", "name")


def cpv_codes(release):
    output = []

    for item in get(release, "tender", "items") or []:
        classifications = [item.get("classification")]
        classifications.extend(item.get("additionalClassifications") or [])

        for classification in classifications:
            if classification and classification.get("id"):
                output.append(
                    {
                        "scheme": classification.get("scheme"),
                        "id": classification.get("id"),
                        "description": classification.get("description"),
                    }
                )

    return output


def location(release):
    parts = []

    for item in get(release, "tender", "items") or []:
        address = item.get("deliveryAddress") or {}

        for key in (
            "streetAddress",
            "locality",
            "region",
            "postalCode",
            "countryName",
        ):
            if address.get(key):
                parts.append(str(address[key]))

    return ", ".join(dict.fromkeys(parts)) or None


def upsert_company(cur, name, kind):
    if not name:
        return None

    cur.execute(
        """
        INSERT INTO companies(canonical_name, company_type)
        VALUES (%s, %s)
        ON CONFLICT(canonical_name)
        DO UPDATE SET updated_at_utc = NOW()
        RETURNING id
        """,
        (name.strip(), kind),
    )

    return cur.fetchone()["id"]


def customers(cur):
    cur.execute("SELECT * FROM customer_profiles WHERE active = TRUE")
    return cur.fetchall()


def process(cur, release, notice_type, source_url):
    ocid = release.get("ocid")
    release_id = release.get("id") or stable_hash(release)[:24]

    title = (
        get(release, "tender", "title")
        or release.get("title")
        or "(untitled notice)"
    )
    description = get(release, "tender", "description") or ""

    published = parse_dt(release.get("date"))
    deadline = parse_dt(get(release, "tender", "tenderPeriod", "endDate"))

    buyer = buyer_name(release)
    cpv = cpv_codes(release)

    cpv_text = " ".join(
        (item.get("description") or "")
        for item in cpv
    )

    energy_score, energy_hits = classify_energy(
        title,
        description,
        cpv_text,
    )

    content_hash = stable_hash(release)

    cur.execute(
        """
        INSERT INTO raw_events(
            source,
            source_event_id,
            source_url,
            event_type,
            published_at_utc,
            content_hash,
            title,
            raw_json
        )
        VALUES(
            'public_contracts_scotland',
            %s, %s, %s, %s, %s, %s, %s::jsonb
        )
        ON CONFLICT(source, content_hash)
        DO UPDATE SET collected_at_utc = NOW()
        RETURNING id
        """,
        (
            ocid or release_id,
            source_url,
            LABELS.get(notice_type, str(notice_type)),
            published,
            content_hash,
            title,
            json.dumps(release),
        ),
    )

    raw_event_id = cur.fetchone()["id"]
    buyer_id = upsert_company(cur, buyer, "Buyer")

    cur.execute(
        """
        INSERT INTO procurements(
            source,
            ocid,
            release_id,
            notice_type,
            title,
            description,
            buyer_name,
            buyer_company_id,
            published_at_utc,
            deadline_at_utc,
            status,
            procurement_method,
            cpv_codes,
            location_text,
            value_amount,
            value_currency,
            raw_event_id,
            energy_relevance_score,
            energy_relevance_reasons
        )
        VALUES(
            'public_contracts_scotland',
            %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s::jsonb, %s, %s, %s, %s, %s, %s::jsonb
        )
        ON CONFLICT(source, ocid, release_id)
        DO UPDATE SET
            title = EXCLUDED.title,
            description = EXCLUDED.description,
            buyer_name = EXCLUDED.buyer_name,
            buyer_company_id = EXCLUDED.buyer_company_id,
            published_at_utc = EXCLUDED.published_at_utc,
            deadline_at_utc = EXCLUDED.deadline_at_utc,
            status = EXCLUDED.status,
            procurement_method = EXCLUDED.procurement_method,
            cpv_codes = EXCLUDED.cpv_codes,
            location_text = EXCLUDED.location_text,
            value_amount = EXCLUDED.value_amount,
            value_currency = EXCLUDED.value_currency,
            raw_event_id = EXCLUDED.raw_event_id,
            energy_relevance_score = EXCLUDED.energy_relevance_score,
            energy_relevance_reasons = EXCLUDED.energy_relevance_reasons,
            updated_at_utc = NOW()
        RETURNING *
        """,
        (
            ocid,
            release_id,
            LABELS.get(notice_type, str(notice_type)),
            title,
            description,
            buyer,
            buyer_id,
            published,
            deadline,
            get(release, "tender", "status"),
            get(release, "tender", "procurementMethod"),
            json.dumps(cpv),
            location(release),
            get(release, "tender", "value", "amount"),
            get(release, "tender", "value", "currency"),
            raw_event_id,
            energy_score,
            json.dumps(energy_hits),
        ),
    )

    procurement = cur.fetchone()

    for award in release.get("awards") or []:
        for supplier in award.get("suppliers") or []:
            supplier_name = supplier.get("name")
            if not supplier_name:
                continue

            supplier_id = upsert_company(cur, supplier_name, "Supplier")
            award_id = award.get("id") or stable_hash(award)[:24]
            award_date = parse_dt(award.get("date"))

            cur.execute(
                """
                INSERT INTO contract_awards(
                    procurement_id,
                    source,
                    ocid,
                    award_id,
                    buyer_name,
                    supplier_name,
                    supplier_company_id,
                    title,
                    description,
                    award_date,
                    value_amount,
                    value_currency,
                    raw_event_id
                )
                VALUES(
                    %s,
                    'public_contracts_scotland',
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                ON CONFLICT(source, ocid, award_id, supplier_name)
                DO NOTHING
                """,
                (
                    procurement["id"],
                    ocid,
                    award_id,
                    buyer,
                    supplier_name,
                    supplier_id,
                    award.get("title") or title,
                    award.get("description") or description,
                    award_date.date() if award_date else None,
                    get(award, "value", "amount"),
                    get(award, "value", "currency"),
                    raw_event_id,
                ),
            )

    if energy_score >= ENERGY_MIN_SCORE:
        for customer in customers(cur):
            score, reasons = score_procurement_for_customer(
                procurement,
                customer,
            )

            if score < 35:
                continue

            signal_type = (
                "INTELLIGENCE"
                if "award" in (procurement.get("notice_type") or "").lower()
                else "LIVE"
            )

            recommended_action = (
                "Review this award for downstream subcontracting and "
                "supplier-entry opportunities."
                if signal_type == "INTELLIGENCE"
                else
                "Review the notice, procurement route and named buyer/contact "
                "before deciding whether to engage."
            )

            cur.execute(
                """
                INSERT INTO opportunity_signals(
                    customer_profile_id,
                    signal_type,
                    procurement_id,
                    buyer_company_id,
                    title,
                    relevance_score,
                    confidence,
                    timing_label,
                    reason_json,
                    recommended_action,
                    evidence_json
                )
                VALUES(
                    %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s, %s::jsonb
                )
                ON CONFLICT(customer_profile_id, signal_type, procurement_id)
                DO UPDATE SET
                    relevance_score = EXCLUDED.relevance_score,
                    reason_json = EXCLUDED.reason_json,
                    recommended_action = EXCLUDED.recommended_action,
                    last_updated_at_utc = NOW(),
                    status = 'ACTIVE'
                """,
                (
                    customer["id"],
                    signal_type,
                    procurement["id"],
                    buyer_id,
                    title,
                    score,
                    70 if score >= 70 else 55,
                    "Now" if signal_type == "LIVE" else "Review downstream",
                    json.dumps(reasons),
                    recommended_action,
                    json.dumps(
                        [
                            {
                                "raw_event_id": raw_event_id,
                                "source": "Public Contracts Scotland",
                            }
                        ]
                    ),
                ),
            )


def month_list():
    return [
        (now() - relativedelta(months=i)).strftime("%m-%Y")
        for i in range(MONTHS_BACK + 1)
    ]


def fetch(month, notice_type):
    response = requests.get(
        f"{BASE}/Notices",
        params={
            "dateFrom": month,
            "noticeType": notice_type,
            "outputType": 0,
        },
        timeout=60,
        verify=certifi.where(),
        headers={
            "Accept": "application/json",
            "User-Agent": "Project-Scope/0.1.2",
        },
    )

    response.raise_for_status()
    return response.json(), response.url


def main():
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collector_runs(collector)
                VALUES('public_contracts_scotland')
                RETURNING id
                """
            )
            run_id = cur.fetchone()["id"]

        conn.commit()

        fetched = 0
        processed = 0
        errors = 0
        messages = []

        for month in month_list():
            for notice_type in NOTICE_TYPES:
                try:
                    payload, source_url = fetch(month, notice_type)
                    batch = releases(payload)
                    fetched += len(batch)

                    with conn.cursor() as cur:
                        for item in batch:
                            process(
                                cur,
                                item,
                                notice_type,
                                source_url,
                            )
                            processed += 1

                    conn.commit()

                except Exception as exc:
                    conn.rollback()
                    errors += 1
                    messages.append(
                        f"{month}/type {notice_type}: "
                        f"{type(exc).__name__}: {exc}"
                    )

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE collector_runs
                SET
                    finished_at_utc = NOW(),
                    status = %s,
                    fetched_count = %s,
                    processed_count = %s,
                    error_count = %s,
                    error_text = %s
                WHERE id = %s
                """,
                (
                    "ok" if errors == 0 else "partial",
                    fetched,
                    processed,
                    errors,
                    "\n".join(messages)[-12000:] if messages else None,
                    run_id,
                ),
            )

        conn.commit()

    print(
        json.dumps(
            {
                "collector": "public_contracts_scotland",
                "fetched": fetched,
                "processed": processed,
                "errors": errors,
                "tls_ca_bundle": certifi.where(),
            }
        )
    )


if __name__ == "__main__":
    main()
