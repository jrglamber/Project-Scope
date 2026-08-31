"""
Project Scope - Public Contracts Scotland collector
Version: 0.3.0

Collection strategy:
1. Try the official PCS OCDS API once with normal TLS verification.
2. If Railway cannot validate the PCS API certificate chain, automatically
   switch to the official Public Contracts Scotland website.
3. Never use verify=False.
4. Feed API and website records through the same Postgres/scoring pipeline.

The website fallback uses:
https://www.publiccontractsscotland.gov.uk/search/search_30daysResultsList.aspx
and the linked official notice detail pages.
"""

import os
import json
import hashlib
import html as html_lib
import re
from datetime import datetime, timezone
from urllib.parse import urljoin

import certifi
import requests
from dateutil.relativedelta import relativedelta

from db import connection
from classification import classify_energy
from scoring import score_procurement_for_customer
from intelligence import classify_award_intelligence


COLLECTOR_VERSION = "0.3.0"

API_BASE = os.environ.get(
    "PCS_API_BASE",
    "https://api.publiccontractsscotland.gov.uk/v1",
).rstrip("/")

WEB_BASE = "https://www.publiccontractsscotland.gov.uk"
WEB_LATEST_URL = (
    f"{WEB_BASE}/search/search_30daysResultsList.aspx"
)

MONTHS_BACK = max(
    0,
    int(os.environ.get("COLLECT_MONTHS_BACK", "1")),
)

ENERGY_MIN_SCORE = int(
    os.environ.get("ENERGY_MIN_SCORE", "2")
)

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

USER_AGENT = (
    f"Project-Scope/{COLLECTOR_VERSION} "
    "(commercial-opportunity-research)"
)


# ---------------------------------------------------------------------------
# Generic helpers
# ---------------------------------------------------------------------------

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

    raw = str(value).strip()

    try:
        parsed = datetime.fromisoformat(
            raw.replace("Z", "+00:00")
        )
        if parsed.tzinfo:
            return parsed
        return parsed.replace(tzinfo=timezone.utc)
    except Exception:
        pass

    for fmt in (
        "%d/%m/%Y",
        "%d/%m/%y",
        "%d-%b-%y",
        "%d-%b-%Y",
        "%d-%m-%Y",
        "%d-%m-%y",
    ):
        try:
            return datetime.strptime(
                raw,
                fmt,
            ).replace(tzinfo=timezone.utc)
        except Exception:
            continue

    return None


def strip_html(raw_html):
    text = re.sub(
        r"(?is)<(script|style).*?>.*?</\1>",
        " ",
        raw_html,
    )

    text = re.sub(
        r"(?s)<[^>]+>",
        " ",
        text,
    )

    text = html_lib.unescape(text)

    return re.sub(
        r"\s+",
        " ",
        text,
    ).strip()


def extract_between(text, start_label, end_labels):
    start = re.search(
        re.escape(start_label),
        text,
        flags=re.IGNORECASE,
    )

    if not start:
        return None

    tail = text[start.end():].lstrip(" :\t\r\n")
    positions = []

    for label in end_labels:
        match = re.search(
            re.escape(label),
            tail,
            flags=re.IGNORECASE,
        )

        if match:
            positions.append(match.start())

    end = min(positions) if positions else len(tail)

    value = tail[:end].strip(" :\t\r\n")

    return value or None


# ---------------------------------------------------------------------------
# OCDS helpers
# ---------------------------------------------------------------------------

def releases(payload):
    if (
        isinstance(payload, dict)
        and isinstance(payload.get("releases"), list)
    ):
        return payload["releases"]

    if (
        isinstance(payload, dict)
        and isinstance(payload.get("records"), list)
    ):
        output = []

        for record in payload["records"]:
            output.extend(
                record.get("releases") or []
            )

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

    if (
        isinstance(payload, dict)
        and (
            payload.get("ocid")
            or payload.get("id")
        )
    ):
        return [payload]

    return []


def buyer_name(release):
    buyer_id = get(
        release,
        "buyer",
        "id",
    )

    for party in release.get("parties") or []:
        roles = party.get("roles") or []

        if (
            "buyer" in roles
            or (
                buyer_id
                and party.get("id") == buyer_id
            )
        ):
            return party.get("name")

    return get(
        release,
        "buyer",
        "name",
    )


def cpv_codes(release):
    output = []

    for item in (
        get(
            release,
            "tender",
            "items",
        )
        or []
    ):
        classifications = [
            item.get("classification")
        ]

        classifications.extend(
            item.get(
                "additionalClassifications"
            )
            or []
        )

        for classification in classifications:
            if (
                classification
                and classification.get("id")
            ):
                output.append(
                    {
                        "scheme": (
                            classification.get(
                                "scheme"
                            )
                        ),
                        "id": (
                            classification.get(
                                "id"
                            )
                        ),
                        "description": (
                            classification.get(
                                "description"
                            )
                        ),
                    }
                )

    return output


def location(release):
    parts = []

    for item in (
        get(
            release,
            "tender",
            "items",
        )
        or []
    ):
        address = (
            item.get("deliveryAddress")
            or {}
        )

        for key in (
            "streetAddress",
            "locality",
            "region",
            "postalCode",
            "countryName",
        ):
            if address.get(key):
                parts.append(
                    str(address[key])
                )

    return (
        ", ".join(
            dict.fromkeys(parts)
        )
        or None
    )


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def upsert_company(cur, name, kind):
    if not name:
        return None

    cur.execute(
        """
        INSERT INTO companies(
            canonical_name,
            company_type
        )
        VALUES (%s, %s)
        ON CONFLICT(canonical_name)
        DO UPDATE SET
            updated_at_utc = NOW(),
            company_type = COALESCE(
                companies.company_type,
                EXCLUDED.company_type
            )
        RETURNING id
        """,
        (
            name.strip(),
            kind,
        ),
    )

    return cur.fetchone()["id"]


def customers(cur):
    cur.execute(
        """
        SELECT *
        FROM customer_profiles
        WHERE active = TRUE
        """
    )

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


def upsert_research_intelligence(cur, procurement, buyer_id, raw_event_id, source_url, intelligence):
    cur.execute(
        """
        INSERT INTO research_intelligence(
            procurement_id, project_id, buyer_company_id, title,
            intelligence_kind, customer_facing, confidence,
            likely_downstream_scopes, reason_json, evidence_json
        ) VALUES(
            %s, %s, %s, %s, %s, %s, %s,
            %s::jsonb, %s::jsonb, %s::jsonb
        )
        ON CONFLICT(procurement_id) DO UPDATE SET
            intelligence_kind = EXCLUDED.intelligence_kind,
            customer_facing = EXCLUDED.customer_facing,
            confidence = EXCLUDED.confidence,
            likely_downstream_scopes = EXCLUDED.likely_downstream_scopes,
            reason_json = EXCLUDED.reason_json,
            evidence_json = EXCLUDED.evidence_json,
            status = 'ACTIVE',
            last_updated_at_utc = NOW()
        """,
        (
            procurement['id'], procurement.get('project_id'), buyer_id,
            procurement['title'], intelligence['kind'],
            intelligence['customer_facing'], intelligence['confidence'],
            json.dumps(intelligence['likely_downstream_scopes']),
            json.dumps(intelligence, default=str),
            json.dumps([{'raw_event_id': raw_event_id, 'source': 'Public Contracts Scotland', 'url': source_url}]),
        ),
    )

def process(cur, release, notice_type, source_url):
    ocid = release.get("ocid")

    release_id = (
        release.get("id")
        or stable_hash(release)[:24]
    )

    title = (
        get(
            release,
            "tender",
            "title",
        )
        or release.get("title")
        or "(untitled notice)"
    )

    description = (
        get(
            release,
            "tender",
            "description",
        )
        or ""
    )

    published = parse_dt(
        release.get("date")
    )

    deadline = parse_dt(
        get(
            release,
            "tender",
            "tenderPeriod",
            "endDate",
        )
    )

    buyer = buyer_name(release)
    cpv = cpv_codes(release)

    cpv_text = " ".join(
        " ".join([
            str(item.get("id") or ""),
            str(item.get("description") or ""),
        ]).strip()
        for item in cpv
    )

    energy_score, energy_hits = (
        classify_energy(
            title,
            description,
            cpv_text,
        )
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
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s::jsonb
        )
        ON CONFLICT(source, content_hash)
        DO UPDATE SET
            collected_at_utc = NOW(),
            source_url = EXCLUDED.source_url
        RETURNING id
        """,
        (
            ocid or release_id,
            source_url,
            LABELS.get(
                notice_type,
                str(notice_type),
            ),
            published,
            content_hash,
            title,
            json.dumps(release),
        ),
    )

    raw_event_id = cur.fetchone()["id"]

    buyer_id = upsert_company(
        cur,
        buyer,
        "Buyer",
    )

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
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s::jsonb,
            %s,
            %s,
            %s,
            %s,
            %s,
            %s::jsonb
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
            LABELS.get(
                notice_type,
                str(notice_type),
            ),
            title,
            description,
            buyer,
            buyer_id,
            published,
            deadline,
            get(
                release,
                "tender",
                "status",
            ),
            get(
                release,
                "tender",
                "procurementMethod",
            ),
            json.dumps(cpv),
            location(release),
            get(
                release,
                "tender",
                "value",
                "amount",
            ),
            get(
                release,
                "tender",
                "value",
                "currency",
            ),
            raw_event_id,
            energy_score,
            json.dumps(energy_hits),
        ),
    )

    procurement = cur.fetchone()

    # Awards are fully available in API/OCDS mode.
    for award in release.get("awards") or []:
        for supplier in award.get("suppliers") or []:
            supplier_value = supplier.get("name")

            if not supplier_value:
                continue

            supplier_id = upsert_company(
                cur,
                supplier_value,
                "Supplier",
            )

            award_id = (
                award.get("id")
                or stable_hash(award)[:24]
            )

            award_date = parse_dt(
                award.get("date")
            )

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
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s,
                    %s
                )
                ON CONFLICT(
                    source,
                    ocid,
                    award_id,
                    supplier_name
                )
                DO NOTHING
                """,
                (
                    procurement["id"],
                    ocid,
                    award_id,
                    buyer,
                    supplier_value,
                    supplier_id,
                    (
                        award.get("title")
                        or title
                    ),
                    (
                        award.get("description")
                        or description
                    ),
                    (
                        award_date.date()
                        if award_date
                        else None
                    ),
                    get(
                        award,
                        "value",
                        "amount",
                    ),
                    get(
                        award,
                        "value",
                        "currency",
                    ),
                    raw_event_id,
                ),
            )

    is_award = "award" in (procurement.get("notice_type") or "").lower()
    signal_type = "INTELLIGENCE" if is_award else "LIVE"
    active_customers = customers(cur)

    if energy_score < ENERGY_MIN_SCORE:
        for customer in active_customers:
            cur.execute(
                """
                UPDATE opportunity_signals
                SET status='INACTIVE', last_updated_at_utc=NOW()
                WHERE customer_profile_id=%s AND procurement_id=%s
                  AND signal_type=%s AND status='ACTIVE'
                """,
                (customer["id"], procurement["id"], signal_type),
            )
        return

    award_intelligence = None
    if is_award:
        ensure_research_schema(cur)
        award_intelligence = classify_award_intelligence(
            procurement.get("title") or "", procurement.get("description") or ""
        )
        upsert_research_intelligence(
            cur, procurement, buyer_id, raw_event_id, source_url, award_intelligence
        )
        if not award_intelligence["customer_facing"]:
            for customer in active_customers:
                cur.execute(
                    """
                    UPDATE opportunity_signals
                    SET status='INACTIVE', last_updated_at_utc=NOW()
                    WHERE customer_profile_id=%s AND procurement_id=%s
                      AND signal_type='INTELLIGENCE'
                    """,
                    (customer["id"], procurement["id"]),
                )
            return

    for customer in active_customers:
        score, reasons = score_procurement_for_customer(procurement, customer)

        if award_intelligence:
            reasons["intelligence"] = award_intelligence
            if award_intelligence["kind"] == "DOWNSTREAM" and score < 45:
                score = min(70, max(45, score + award_intelligence["downstream_score"] * 2))
                reasons["intelligence"]["downstream_score_uplift_applied"] = True

        if score < 35:
            cur.execute(
                """
                UPDATE opportunity_signals
                SET status='INACTIVE', relevance_score=%s,
                    reason_json=%s::jsonb, last_updated_at_utc=NOW()
                WHERE customer_profile_id=%s AND procurement_id=%s AND signal_type=%s
                """,
                (score, json.dumps(reasons, default=str), customer["id"], procurement["id"], signal_type),
            )
            continue

        if signal_type == "INTELLIGENCE":
            if award_intelligence and award_intelligence["kind"] == "DOWNSTREAM":
                scopes = ", ".join(award_intelligence["likely_downstream_scopes"][:5])
                recommended_action = "Review the award for downstream supplier-entry opportunities" + (f" in {scopes}." if scopes else ".")
            else:
                recommended_action = "Review this award for direct capability relevance and possible supplier-entry opportunities."
        else:
            recommended_action = "Review the notice, procurement route and named buyer/contact before deciding whether to engage."

        cur.execute(
            """
            INSERT INTO opportunity_signals(
                customer_profile_id, signal_type, procurement_id, buyer_company_id,
                title, relevance_score, confidence, timing_label, reason_json,
                recommended_action, evidence_json
            ) VALUES(
                %s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb
            )
            ON CONFLICT(customer_profile_id,signal_type,procurement_id) DO UPDATE SET
                relevance_score=EXCLUDED.relevance_score,
                confidence=EXCLUDED.confidence,
                reason_json=EXCLUDED.reason_json,
                recommended_action=EXCLUDED.recommended_action,
                last_updated_at_utc=NOW(), status='ACTIVE'
            """,
            (
                customer["id"], signal_type, procurement["id"], buyer_id, title,
                score,
                award_intelligence["confidence"] if award_intelligence else (70 if score >= 70 else 55),
                "Review downstream" if signal_type == "INTELLIGENCE" else "Now",
                json.dumps(reasons, default=str), recommended_action,
                json.dumps([{"raw_event_id":raw_event_id,"source":"Public Contracts Scotland","url":source_url}]),
            ),
        )


# ---------------------------------------------------------------------------
# Official API
# ---------------------------------------------------------------------------

def month_list():
    return [
        (
            now()
            - relativedelta(months=i)
        ).strftime("%m-%Y")
        for i in range(
            MONTHS_BACK + 1
        )
    ]


def api_fetch(session, month, notice_type):
    response = session.get(
        f"{API_BASE}/Notices",
        params={
            "dateFrom": month,
            "noticeType": notice_type,
            "outputType": 0,
        },
        timeout=60,
        verify=certifi.where(),
    )

    response.raise_for_status()

    return (
        response.json(),
        response.url,
    )


# ---------------------------------------------------------------------------
# Official PCS website fallback
# ---------------------------------------------------------------------------

def website_notice_links(index_html):
    """
    Extract official notice detail links such as:
    /search/show/search_view.aspx?ID=AUG563381
    """

    matches = re.findall(
        r"""href\s*=\s*["']([^"']*"""
        r"""search_view\.aspx\?ID=[^"'&#\s]+)["']""",
        index_html,
        flags=re.IGNORECASE,
    )

    output = []
    seen = set()

    for href in matches:
        url = urljoin(
            WEB_BASE,
            html_lib.unescape(href),
        )

        if url in seen:
            continue

        seen.add(url)
        output.append(url)

    return output


def infer_notice_type(notice_text):
    value = (
        notice_text
        or ""
    ).lower()

    if (
        "quick quote"
        in value
        and "award"
        in value
    ):
        return 104

    if "award" in value:
        return 3

    if (
        "prior information"
        in value
        or "(pin)" in value
    ):
        return 1

    return 2


def parse_money_from_text(text):
    match = re.search(
        r"""Value\s+excluding\s+VAT:\s*"""
        r"""([0-9][0-9\s,\.]*)\s*GBP""",
        text,
        flags=re.IGNORECASE,
    )

    if not match:
        return None

    raw = (
        match.group(1)
        .replace(" ", "")
        .replace(",", "")
    )

    try:
        return float(raw)
    except Exception:
        return None


def synthetic_release_from_notice_page(
    url,
    raw_html,
):
    """
    Convert the official PCS HTML detail page into the subset of an OCDS-like
    release used by Project Scope. We preserve the official URL and parsed
    source fields inside _project_scope_source.
    """

    text = strip_html(raw_html)

    title = extract_between(
        text,
        "Title:",
        [
            "Reference No:",
            "OCID:",
        ],
    )

    reference = extract_between(
        text,
        "Reference No:",
        [
            "OCID:",
            "Published by:",
            "Published By:",
        ],
    )

    ocid = extract_between(
        text,
        "OCID:",
        [
            "Published by:",
            "Published By:",
            "Publication Date:",
        ],
    )

    buyer = extract_between(
        text,
        "Published by:",
        [
            "Publication Date:",
        ],
    )

    if not buyer:
        buyer = extract_between(
            text,
            "Published By:",
            [
                "Publication Date:",
            ],
        )

    publication_date = extract_between(
        text,
        "Publication Date:",
        [
            "Deadline Date:",
        ],
    )

    deadline_date = extract_between(
        text,
        "Deadline Date:",
        [
            "Deadline Time:",
            "Notice Type:",
        ],
    )

    notice_text = extract_between(
        text,
        "Notice Type:",
        [
            "Has Documents:",
            "Has SPD:",
            "Abstract:",
        ],
    )

    abstract = extract_between(
        text,
        "Abstract:",
        [
            "CPV:",
            "Contract Notice",
            "Contract Award Notice",
            "Prior Information Notice",
            "Further Info",
            "Contact Info",
        ],
    )

    cpv_raw = extract_between(
        text,
        "CPV:",
        [
            "Contract Notice",
            "Contract Award Notice",
            "Prior Information Notice",
            "Further Info",
            "Contact Info",
        ],
    )

    cpv_ids = []

    if cpv_raw:
        for code in re.findall(
            r"\b\d{8}\b",
            cpv_raw,
        ):
            if code not in cpv_ids:
                cpv_ids.append(code)

    items = []

    if cpv_ids:
        item = {
            "classification": {
                "scheme": "CPV",
                "id": cpv_ids[0],
                "description": None,
            },
            "additionalClassifications": [
                {
                    "scheme": "CPV",
                    "id": code,
                    "description": None,
                }
                for code in cpv_ids[1:]
            ],
        }

        # PCS detail pages often expose "Main site or place of performance".
        place = extract_between(
            text,
            "Main site or place of performance:",
            [
                "Description of the procurement",
                "Award criteria",
                "Duration of the contract",
            ],
        )

        if place:
            item["deliveryAddress"] = {
                "region": place,
            }

        items.append(item)

    published_dt = parse_dt(
        publication_date
    )

    deadline_dt = parse_dt(
        deadline_date
    )

    value_amount = parse_money_from_text(
        text
    )

    release = {
        "ocid": (
            ocid
            or f"pcs-{reference or stable_hash(url)[:16]}"
        ),
        "id": (
            reference
            or stable_hash(url)[:24]
        ),
        "date": (
            published_dt.isoformat()
            if published_dt
            else None
        ),
        "buyer": (
            {"name": buyer}
            if buyer
            else {}
        ),
        "tender": {
            "title": (
                title
                or "(untitled notice)"
            ),
            "description": (
                abstract
                or ""
            ),
            "tenderPeriod": {
                "endDate": (
                    deadline_dt.isoformat()
                    if deadline_dt
                    else None
                )
            },
            "items": items,
            "status": (
                "active"
                if deadline_dt
                else None
            ),
            "value": (
                {
                    "amount": value_amount,
                    "currency": "GBP",
                }
                if value_amount is not None
                else {}
            ),
        },
        "_project_scope_source": {
            "mode": "official_website_fallback",
            "url": url,
            "notice_text": notice_text,
            "reference": reference,
            "collector_version": COLLECTOR_VERSION,
        },
    }

    return (
        release,
        infer_notice_type(
            notice_text
        ),
    )


def collect_website_fallback(
    conn,
    session,
):
    """
    Forward collector against the official "last 30 days" PCS page.

    We intentionally process the newest page on each scheduled run. That is
    sufficient for forward monitoring at a 2-hour cadence and avoids fragile
    ASP.NET pagination logic. Historical backfill should be a separate job.
    """

    response = session.get(
        WEB_LATEST_URL,
        timeout=60,
        verify=certifi.where(),
    )

    response.raise_for_status()

    links = website_notice_links(
        response.text
    )

    if not links:
        raise RuntimeError(
            "PCS official website loaded but no "
            "notice detail links were found."
        )

    fetched = 0
    processed = 0
    errors = 0
    messages = []

    total_links = len(links)
    print(
        f"PCS official website fallback: found {total_links} notice links.",
        flush=True,
    )

    for url in links:
        fetched += 1

        if fetched == 1 or fetched % 10 == 0 or fetched == total_links:
            print(
                f"PCS fallback progress: {fetched}/{total_links}",
                flush=True,
            )

        try:
            detail = session.get(
                url,
                timeout=60,
                verify=certifi.where(),
            )

            detail.raise_for_status()

            (
                release,
                notice_type,
            ) = synthetic_release_from_notice_page(
                url,
                detail.text,
            )

            with conn.cursor() as cur:
                process(
                    cur,
                    release,
                    notice_type,
                    url,
                )

            conn.commit()
            processed += 1

        except Exception as exc:
            conn.rollback()
            errors += 1

            messages.append(
                f"{url}: "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

    return (
        fetched,
        processed,
        errors,
        messages,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO collector_runs(
                    collector
                )
                VALUES(
                    'public_contracts_scotland'
                )
                RETURNING id
                """
            )

            run_id = cur.fetchone()["id"]

        conn.commit()

        session = requests.Session()

        session.headers.update(
            {
                "Accept": (
                    "application/json,"
                    "text/html;q=0.9,"
                    "*/*;q=0.8"
                ),
                "User-Agent": USER_AGENT,
            }
        )

        fetched = 0
        processed = 0
        errors = 0
        messages = []
        mode = "api"

        # Test one official API request first.
        try:
            test_month = month_list()[0]
            test_notice_type = 2

            (
                payload,
                source_url,
            ) = api_fetch(
                session,
                test_month,
                test_notice_type,
            )

            batch = releases(payload)

            fetched += len(batch)

            with conn.cursor() as cur:
                for item in batch:
                    process(
                        cur,
                        item,
                        test_notice_type,
                        source_url,
                    )
                    processed += 1

            conn.commit()

            # API is usable; collect the rest normally.
            for month in month_list():
                for notice_type in NOTICE_TYPES:
                    if (
                        month == test_month
                        and notice_type
                        == test_notice_type
                    ):
                        continue

                    try:
                        (
                            payload,
                            source_url,
                        ) = api_fetch(
                            session,
                            month,
                            notice_type,
                        )

                        batch = releases(
                            payload
                        )

                        fetched += len(
                            batch
                        )

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
                            f"API {month}/"
                            f"type {notice_type}: "
                            f"{type(exc).__name__}: "
                            f"{exc}"
                        )

        except requests.exceptions.SSLError as exc:
            # Known Railway <-> PCS API certificate-chain problem.
            # This is not counted as a collection failure if the verified
            # official website fallback succeeds.
            conn.rollback()

            mode = (
                "official_website_fallback"
            )

            messages.append(
                "PCS API TLS validation failed. "
                "Project Scope switched to the "
                "verified official PCS website. "
                f"API error: {exc}"
            )

            try:
                (
                    fetched,
                    processed,
                    fallback_errors,
                    fallback_messages,
                ) = collect_website_fallback(
                    conn,
                    session,
                )

                errors += fallback_errors
                messages.extend(
                    fallback_messages
                )

            except Exception as fallback_exc:
                conn.rollback()
                errors += 1

                messages.append(
                    "PCS official website fallback "
                    "failed: "
                    f"{type(fallback_exc).__name__}: "
                    f"{fallback_exc}"
                )

        except Exception as exc:
            conn.rollback()
            errors += 1

            messages.append(
                "PCS API initial request failed: "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

        if (
            mode == "official_website_fallback"
            and processed > 0
            and errors == 0
        ):
            status = "ok_fallback"

        elif (
            errors == 0
            and processed >= 0
        ):
            status = "ok"

        elif processed > 0:
            status = "partial"

        else:
            status = "failed"

        error_text = (
            "\n".join(messages)[-12000:]
            if messages
            else None
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
                    status,
                    fetched,
                    processed,
                    errors,
                    error_text,
                    run_id,
                ),
            )

        conn.commit()

    # Everything useful is printed directly into Railway Deploy Logs.
    print(
        json.dumps(
            {
                "collector": (
                    "public_contracts_scotland"
                ),
                "collector_version": (
                    COLLECTOR_VERSION
                ),
                "mode": mode,
                "status": status,
                "fetched": fetched,
                "processed": processed,
                "errors": errors,
                "tls_verification": "enabled",
                "tls_ca_bundle": (
                    certifi.where()
                ),
                "note": (
                    "API SSL errors trigger the "
                    "official_website_fallback; "
                    "verify=False is never used."
                ),
            }
        )
    )

    if error_text:
        print(
            "collector_diagnostics:",
            error_text,
        )


if __name__ == "__main__":
    main()
