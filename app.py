import os
import json
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from db import connection
from access import assess_access, VALID_ACCESS_STATUSES, VALID_BARRIER_TYPES
from scoring import (
    score_procurement_for_customer,
    SCORING_VERSION,
)
from intelligence import (
    classify_award_intelligence,
    match_downstream_scopes_to_customer,
    INTELLIGENCE_VERSION,
)

APP_VERSION = "0.7.9"
DEFAULT = os.environ.get("DEFAULT_CUSTOMER_SLUG", "northsea-quality-demo")
app = FastAPI(title="Project Scope", version=APP_VERSION)


class FeedbackRequest(BaseModel):
    label: Literal["RELEVANT", "NOT_RELEVANT", "WATCH"]
    reason_code: Optional[Literal[
        "WRONG_SECTOR","WRONG_CAPABILITY","WRONG_GEOGRAPHY","CONTRACT_VALUE",
        "NO_REALISTIC_ROUTE","DUPLICATE_OR_STALE","OTHER"
    ]] = None
    note: Optional[str] = None


class AccessRuleRequest(BaseModel):
    buyer_name_pattern: str
    access_status: Literal["UNKNOWN","APPROVED","NOT_APPROVED","IN_PROGRESS","INDIRECT_ONLY"]
    barrier_type: Literal[
        "NONE","APPROVED_VENDOR_LIST","FRAMEWORK","CERTIFICATION","INSURANCE",
        "LOCAL_CONTENT","GEOGRAPHY","COMMERCIAL_SCALE","OTHER"
    ] = "NONE"
    note: Optional[str] = None
    evidence_source: Optional[str] = None


class CustomerProfileRequest(BaseModel):
    name: str
    geography: list[str] = []
    sectors: list[str] = []
    capabilities: list[str] = []
    preferred_buyers: list[str] = []
    excluded_scopes: list[str] = []
    min_contract_value_gbp: Optional[float] = None
    max_contract_value_gbp: Optional[float] = None
    company_summary: Optional[str] = None
    certifications: list[str] = []
    preferred_routes: list[str] = []
    notes: Optional[str] = None
    exclusions_confirmed: bool = False


def ensure_v05_schema():
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS opportunity_feedback (
                    id BIGSERIAL PRIMARY KEY,
                    signal_id BIGINT NOT NULL REFERENCES opportunity_signals(id) ON DELETE CASCADE,
                    customer_profile_id BIGINT NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
                    label TEXT NOT NULL CHECK(label IN ('RELEVANT','NOT_RELEVANT','WATCH')),
                    note TEXT,created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(signal_id,customer_profile_id)
                )
            """)
            cur.execute("""
                ALTER TABLE opportunity_feedback
                ADD COLUMN IF NOT EXISTS reason_code TEXT
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS research_intelligence (
                    id BIGSERIAL PRIMARY KEY,
                    procurement_id BIGINT NOT NULL REFERENCES procurements(id) ON DELETE CASCADE,
                    project_id BIGINT REFERENCES projects(id),buyer_company_id BIGINT REFERENCES companies(id),
                    title TEXT NOT NULL,intelligence_kind TEXT NOT NULL CHECK(
                        intelligence_kind IN ('DIRECT','DOWNSTREAM','RESEARCH_ONLY')),
                    customer_facing BOOLEAN NOT NULL DEFAULT FALSE,
                    confidence INTEGER NOT NULL DEFAULT 50 CHECK(confidence BETWEEN 0 AND 100),
                    likely_downstream_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
                    reason_json JSONB NOT NULL DEFAULT '{}'::jsonb,evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',first_seen_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(procurement_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_buyer_access (
                    id BIGSERIAL PRIMARY KEY,
                    customer_profile_id BIGINT NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
                    buyer_name_pattern TEXT NOT NULL,
                    access_status TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK(
                        access_status IN ('UNKNOWN','APPROVED','NOT_APPROVED','IN_PROGRESS','INDIRECT_ONLY')),
                    barrier_type TEXT NOT NULL DEFAULT 'NONE' CHECK(
                        barrier_type IN ('NONE','APPROVED_VENDOR_LIST','FRAMEWORK','CERTIFICATION','INSURANCE',
                        'LOCAL_CONTENT','GEOGRAPHY','COMMERCIAL_SCALE','OTHER')),
                    note TEXT,evidence_source TEXT,created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(customer_profile_id,buyer_name_pattern)
                )
            """)

            cur.execute("""
                ALTER TABLE procurements
                ADD COLUMN IF NOT EXISTS sector_gate_passed BOOLEAN NOT NULL DEFAULT FALSE
            """)
            cur.execute("""
                ALTER TABLE procurements
                ADD COLUMN IF NOT EXISTS classifier_version TEXT
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_profile_revisions (
                    id BIGSERIAL PRIMARY KEY,
                    customer_profile_id BIGINT NOT NULL
                        REFERENCES customer_profiles(id) ON DELETE CASCADE,
                    snapshot_json JSONB NOT NULL,
                    change_note TEXT,
                    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_customer_profile_revisions_customer
                ON customer_profile_revisions(
                    customer_profile_id,
                    created_at_utc DESC
                )
            """)



def rescore_stored_active_signals():
    """
    One-time compatibility backfill for stored ACTIVE signals created by an
    older scoring/intelligence version.

    This matters because FTS/PCS collectors use rolling lookback windows:
    changing scoring.py does not automatically revisit an older notice that is
    still stored as ACTIVE. The app now upgrades those stored signals itself.

    No raw procurement, research history, feedback or access rules are deleted.
    """
    checked = 0
    rescored = 0
    deactivated = 0
    errors = 0

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    s.id AS signal_id,
                    s.signal_type,
                    s.reason_json,
                    s.procurement_id,
                    to_jsonb(p) AS procurement_json,
                    to_jsonb(cp) AS customer_json
                FROM opportunity_signals s
                JOIN procurements p
                  ON p.id=s.procurement_id
                JOIN customer_profiles cp
                  ON cp.id=s.customer_profile_id
                WHERE
                    s.status='ACTIVE'
                    AND cp.active=TRUE
                ORDER BY s.id
                """
            )
            rows = cur.fetchall()

            for row in rows:
                checked += 1

                old_reason = (
                    row.get("reason_json")
                    or {}
                )
                versions = (
                    old_reason.get("versions")
                    or {}
                )
                old_scoring = versions.get(
                    "scoring"
                )
                old_intelligence = (
                    old_reason.get(
                        "intelligence"
                    )
                    or {}
                ).get("version")

                signal_type = (
                    row.get("signal_type")
                    or ""
                )

                needs_rescore = (
                    old_scoring
                    != SCORING_VERSION
                    or (
                        signal_type
                        == "INTELLIGENCE"
                        and old_intelligence
                        != INTELLIGENCE_VERSION
                    )
                )

                if not needs_rescore:
                    continue

                cur.execute(
                    "SAVEPOINT scope_rescore"
                )

                try:
                    procurement = dict(
                        row.get(
                            "procurement_json"
                        )
                        or {}
                    )
                    customer = dict(
                        row.get(
                            "customer_json"
                        )
                        or {}
                    )

                    award_intel = None
                    downstream_match = {
                        "matched_scopes": [],
                        "matches": [],
                        "match_count": 0,
                    }
                    inferred_capabilities = None

                    if (
                        signal_type
                        == "INTELLIGENCE"
                    ):
                        award_intel = (
                            classify_award_intelligence(
                                procurement.get(
                                    "title"
                                ) or "",
                                procurement.get(
                                    "description"
                                ) or "",
                            )
                        )

                        if (
                            award_intel[
                                "kind"
                            ]
                            == "DOWNSTREAM"
                        ):
                            downstream_match = (
                                match_downstream_scopes_to_customer(
                                    award_intel[
                                        "likely_downstream_scopes"
                                    ],
                                    customer.get(
                                        "capabilities"
                                    ) or [],
                                )
                            )
                            inferred_capabilities = (
                                downstream_match[
                                    "matched_scopes"
                                ]
                            )

                    score, reasons = (
                        score_procurement_for_customer(
                            procurement,
                            customer,
                            inferred_capabilities=(
                                inferred_capabilities
                                if (
                                    award_intel
                                    and award_intel[
                                        "kind"
                                    ]
                                    == "DOWNSTREAM"
                                )
                                else None
                            ),
                        )
                    )

                    if (
                        procurement.get("source")
                        == "nsta_energy_pathfinder"
                    ):
                        reasons[
                            "source_intelligence"
                        ] = {
                            "source": (
                                "NSTA Energy Pathfinder"
                            ),
                            "authoritative_energy_source": True,
                        }

                    if award_intel:
                        reasons[
                            "intelligence"
                        ] = {
                            **award_intel,
                            "customer_downstream_match": (
                                downstream_match
                            ),
                        }

                        # Keep the customer-independent research classification
                        # current as well. The row already exists for stored
                        # award intelligence, so this update is deliberately
                        # non-destructive.
                        cur.execute(
                            """
                            UPDATE research_intelligence
                            SET
                                intelligence_kind=%s,
                                customer_facing=%s,
                                confidence=%s,
                                likely_downstream_scopes=%s::jsonb,
                                reason_json=%s::jsonb,
                                last_updated_at_utc=NOW()
                            WHERE procurement_id=%s
                            """,
                            (
                                award_intel[
                                    "kind"
                                ],
                                award_intel[
                                    "customer_facing"
                                ],
                                award_intel[
                                    "confidence"
                                ],
                                json.dumps(
                                    award_intel[
                                        "likely_downstream_scopes"
                                    ],
                                    default=str,
                                ),
                                json.dumps(
                                    award_intel,
                                    default=str,
                                ),
                                procurement.get(
                                    "id"
                                ),
                            ),
                        )

                    fit_tier = (
                        reasons.get(
                            "customer_fit",
                            {},
                        ).get(
                            "tier",
                            "NONE",
                        )
                    )

                    min_signal_score = (
                        45
                        if fit_tier
                        == "INFERRED_DOWNSTREAM"
                        else 35
                    )

                    should_deactivate = (
                        fit_tier == "NONE"
                        or score
                        < min_signal_score
                        or (
                            award_intel
                            and not award_intel[
                                "customer_facing"
                            ]
                        )
                        or (
                            award_intel
                            and award_intel[
                                "kind"
                            ]
                            == "DOWNSTREAM"
                            and not inferred_capabilities
                        )
                    )

                    if should_deactivate:
                        cur.execute(
                            """
                            UPDATE opportunity_signals
                            SET
                                status='INACTIVE',
                                relevance_score=%s,
                                reason_json=%s::jsonb,
                                last_updated_at_utc=NOW()
                            WHERE id=%s
                            """,
                            (
                                score,
                                json.dumps(
                                    reasons,
                                    default=str,
                                ),
                                row[
                                    "signal_id"
                                ],
                            ),
                        )
                        deactivated += 1
                    else:
                        if (
                            signal_type
                            == "EMERGING"
                        ):
                            timing = (
                                "Early / pre-tender"
                            )
                            action = (
                                "Review this early-stage notice, identify "
                                "the buyer/procurement route and consider "
                                "early engagement."
                            )
                        elif (
                            signal_type
                            == "INTELLIGENCE"
                            and fit_tier
                            == "INFERRED_DOWNSTREAM"
                        ):
                            timing = (
                                "Downstream watch / supplier entry"
                            )
                            scopes = ", ".join(
                                downstream_match[
                                    "matched_scopes"
                                ][:5]
                            )
                            action = (
                                "Monitor this award for downstream "
                                "supplier-entry opportunities specifically "
                                "matching the customer's capabilities"
                                + (
                                    f": {scopes}. "
                                    if scopes
                                    else ". "
                                )
                                + (
                                    "Confirm the actual subcontract package "
                                    "and route to market before treating it "
                                    "as actionable."
                                )
                            )
                        elif (
                            signal_type
                            == "INTELLIGENCE"
                        ):
                            timing = (
                                "Direct capability review"
                            )
                            action = (
                                "Review this award because the source text "
                                "contains direct customer-capability evidence. "
                                "Confirm the route-to-market/access position "
                                "before engagement."
                            )
                        else:
                            timing = "Now"
                            action = (
                                "Review the notice, procurement route and "
                                "named buyer/contact before deciding whether "
                                "to engage."
                            )

                        confidence = (
                            award_intel[
                                "confidence"
                            ]
                            if award_intel
                            else (
                                80
                                if score >= 75
                                else 65
                            )
                        )
                        if (
                            fit_tier
                            == "INFERRED_DOWNSTREAM"
                        ):
                            confidence = min(
                                confidence,
                                75,
                            )

                        cur.execute(
                            """
                            UPDATE opportunity_signals
                            SET
                                relevance_score=%s,
                                confidence=%s,
                                timing_label=%s,
                                reason_json=%s::jsonb,
                                recommended_action=%s,
                                status='ACTIVE',
                                last_updated_at_utc=NOW()
                            WHERE id=%s
                            """,
                            (
                                score,
                                confidence,
                                timing,
                                json.dumps(
                                    reasons,
                                    default=str,
                                ),
                                action,
                                row[
                                    "signal_id"
                                ],
                            ),
                        )

                    rescored += 1
                    cur.execute(
                        "RELEASE SAVEPOINT scope_rescore"
                    )

                except Exception as exc:
                    errors += 1
                    cur.execute(
                        "ROLLBACK TO SAVEPOINT scope_rescore"
                    )
                    cur.execute(
                        "RELEASE SAVEPOINT scope_rescore"
                    )
                    print(
                        "Project Scope stored-signal rescore error:",
                        row.get("signal_id"),
                        type(exc).__name__,
                        str(exc),
                        flush=True,
                    )

    print(
        "Project Scope stored-signal rescore:",
        {
            "checked": checked,
            "rescored": rescored,
            "deactivated": deactivated,
            "errors": errors,
            "scoring_version": SCORING_VERSION,
            "intelligence_version": INTELLIGENCE_VERSION,
        },
        flush=True,
    )


@app.on_event("startup")
def startup():
    ensure_v05_schema()
    try:
        rescore_stored_active_signals()
    except Exception as exc:
        # Never make the web app unavailable because a maintenance backfill
        # failed. The error is visible in Railway logs and can be retried on
        # the next deployment.
        print(
            "Project Scope startup rescore failed:",
            type(exc).__name__,
            str(exc),
            flush=True,
        )


def customer_row(cur, slug):
    cur.execute("SELECT * FROM customer_profiles WHERE slug=%s", (slug,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Customer profile not found")
    return row


def access_rules(cur, customer_id):
    cur.execute("SELECT * FROM customer_buyer_access WHERE customer_profile_id=%s ORDER BY buyer_name_pattern", (customer_id,))
    return cur.fetchall()


ROUTE_SCORE_PENALTIES = {
    "APPROVED": 0,
    "IN_PROGRESS": 3,
    "INDIRECT_ONLY": 6,
    "UNKNOWN": 10,
    "NOT_APPROVED": 25,
}

ACCESS_REVIEW_MIN_SCORE = max(
    35,
    int(os.environ.get("ACCESS_REVIEW_MIN_SCORE", "50")),
)


def customer_fit_tier(reason_json):
    reasons = reason_json or {}

    explicit = (
        reasons.get("customer_fit") or {}
    ).get("tier")

    if explicit in {
        "DIRECT",
        "INFERRED_DOWNSTREAM",
        "NONE",
    }:
        return explicit

    # Backwards-compatible interpretation for signals created before v0.6.7.
    capability = (
        reasons.get("capability_fit")
        or {}
    )
    if int(capability.get("score") or 0) > 0:
        return "DIRECT"

    if int(
        capability.get("inferred_score")
        or 0
    ) > 0:
        return "INFERRED_DOWNSTREAM"

    return "NONE"


def signal_match_explanation(reason_json):
    reasons = reason_json or {}
    tier = customer_fit_tier(reasons)
    capability = reasons.get("capability_fit") or {}
    target_sector = reasons.get("target_sector_fit") or {}

    customer_caps = []
    evidence_terms = []

    for cap in capability.get("customer_capability_hits") or []:
        value = " ".join(str(cap).split())
        if value and value not in customer_caps:
            customer_caps.append(value)

    for hit in capability.get("matched_quality_hits") or []:
        term = " ".join(str(hit.get("term") or "").split())
        if term and term not in evidence_terms:
            evidence_terms.append(term)
        for cap in hit.get("customer_capabilities") or []:
            value = " ".join(str(cap).split())
            if value and value not in customer_caps:
                customer_caps.append(value)

    if tier == "INFERRED_DOWNSTREAM":
        for cap in capability.get("inferred_customer_capabilities") or []:
            value = " ".join(str(cap).split())
            if value and value not in customer_caps:
                customer_caps.append(value)

    families = target_sector.get("matched_families") or []

    if tier == "DIRECT":
        if evidence_terms:
            text = "Direct fit: explicit procurement evidence " + ", ".join(evidence_terms[:4])
            if customer_caps:
                text += " matches customer capabilities " + ", ".join(customer_caps[:4]) + "."
            else:
                text += "."
        elif customer_caps:
            text = "Direct fit: procurement text directly matches customer capabilities " + ", ".join(customer_caps[:4]) + "."
        else:
            text = "Direct capability evidence was recorded by the scoring engine."
    elif tier == "INFERRED_DOWNSTREAM":
        text = "Inferred downstream fit: likely downstream scopes match customer capabilities"
        text += " " + ", ".join(customer_caps[:5]) + "." if customer_caps else "."
    else:
        text = "No customer-specific capability fit."

    if families:
        if target_sector.get(
            "authoritative_project_context"
        ):
            text += " Target-sector match from NSTA parent-project metadata: "
        else:
            text += " Target-sector match: "
        text += ", ".join(
            x.replace("_", " ").title()
            for x in families
        ) + "."
    elif target_sector.get("authoritative_source_override"):
        text += " NSTA authoritative-source evidence is being used because the exact family is not explicit in the terse record."

    return {
        "tier": tier,
        "customer_capabilities": customer_caps,
        "evidence_terms": evidence_terms,
        "target_sector_families": families,
        "sector_evidence_terms": target_sector.get("evidence_terms") or [],
        "text": text,
    }


def _dedupe_value(value):
    return " ".join(
        str(value or "")
        .lower()
        .split()
    )


def opportunity_dedupe_key(row):
    published = row.get(
        "published_at_utc"
    )
    published_day = (
        str(published)[:10]
        if published
        else ""
    )

    return (
        _dedupe_value(
            row.get("source")
        ),
        _dedupe_value(
            row.get("title")
        ),
        _dedupe_value(
            row.get("buyer_name")
        ),
        published_day,
        _dedupe_value(
            row.get("description")
        ),
    )


def dedupe_opportunity_rows(rows):
    kept = {}
    duplicates = 0

    ordered = sorted(
        rows or [],
        key=lambda row: (
            int(
                row.get(
                    "effective_score",
                    row.get(
                        "relevance_score",
                        0,
                    ),
                )
                or 0
            ),
            int(
                row.get(
                    "relevance_score"
                )
                or 0
            ),
            bool(
                row.get(
                    "feedback_id"
                )
                or row.get(
                    "feedback_label"
                )
            ),
            str(
                row.get(
                    "last_updated_at_utc"
                )
                or ""
            ),
        ),
        reverse=True,
    )

    for row in ordered:
        key = opportunity_dedupe_key(
            row
        )

        if key in kept:
            duplicates += 1
            kept[key][
                "duplicate_records_hidden"
            ] = int(
                kept[key].get(
                    "duplicate_records_hidden"
                )
                or 0
            ) + 1
            continue

        row[
            "duplicate_records_hidden"
        ] = 0
        kept[key] = row

    return list(
        kept.values()
    ), duplicates


def route_target_for_signal(
    row,
    tier,
):
    package_holder = " ".join(
        str(
            row.get(
                "package_holder_name"
            )
            or ""
        ).split()
    )
    buyer = " ".join(
        str(
            row.get(
                "buyer_name"
            )
            or ""
        ).split()
    )

    if (
        tier == "INFERRED_DOWNSTREAM"
        and package_holder
    ):
        return (
            package_holder,
            "PACKAGE_HOLDER",
        )

    return (
        buyer,
        "BUYER",
    )


def route_adjusted_score(raw_score, access_assessment):
    status = (
        access_assessment or {}
    ).get("status") or "UNKNOWN"

    penalty = ROUTE_SCORE_PENALTIES.get(
        status,
        10,
    )

    effective = max(
        0,
        int(raw_score or 0) - penalty,
    )

    return effective, penalty


def _clean_list(values):
    output = []
    seen = set()

    for value in values or []:
        item = " ".join(str(value or "").strip().split())
        if not item:
            continue
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        output.append(item)

    return output


def profile_snapshot(customer):
    metadata = customer.get("metadata") or {}

    return {
        "id": customer.get("id"),
        "slug": customer.get("slug"),
        "name": customer.get("name"),
        "active": customer.get("active"),
        "geography": customer.get("geography") or [],
        "sectors": customer.get("sectors") or [],
        "capabilities": customer.get("capabilities") or [],
        "preferred_buyers": customer.get("preferred_buyers") or [],
        "excluded_scopes": customer.get("excluded_scopes") or [],
        "min_contract_value_gbp": customer.get("min_contract_value_gbp"),
        "max_contract_value_gbp": customer.get("max_contract_value_gbp"),
        "metadata": metadata,
        "updated_at_utc": customer.get("updated_at_utc"),
    }


def profile_completeness(customer, rule_count=0):
    metadata = customer.get("metadata") or {}

    checks = [
        (
            "Capabilities",
            bool(customer.get("capabilities")),
            "Define the services the customer can actually sell.",
        ),
        (
            "Target sectors",
            bool(customer.get("sectors")),
            "Define the sectors the customer wants Scope to pursue.",
        ),
        (
            "Geography",
            bool(customer.get("geography")),
            "Define realistic operating geography.",
        ),
        (
            "Contract range",
            (
                customer.get("min_contract_value_gbp") is not None
                and customer.get("max_contract_value_gbp") is not None
            ),
            "Set a realistic contract-value range.",
        ),
        (
            "Business summary",
            bool(str(metadata.get("company_summary") or "").strip()),
            "Add a short commercial description of the business.",
        ),
        (
            "Certifications",
            bool(metadata.get("certifications")),
            "Record relevant certifications / approvals.",
        ),
        (
            "Exclusions confirmed",
            bool(
                customer.get("excluded_scopes")
                or metadata.get("exclusions_confirmed")
            ),
            "Confirm what Scope should explicitly avoid.",
        ),
        (
            "Buyer route knowledge",
            bool(
                customer.get("preferred_buyers")
                or rule_count > 0
            ),
            "Add preferred buyers or resolve at least one buyer-access route.",
        ),
    ]

    completed = sum(
        1 for _name, ok, _help in checks
        if ok
    )
    total = len(checks)

    return {
        "completed": completed,
        "total": total,
        "percent": round(
            100 * completed / total
        ) if total else 100,
        "items": [
            {
                "name": name,
                "complete": ok,
                "help": help_text,
            }
            for name, ok, help_text in checks
        ],
    }


def is_high_priority(
    tier,
    effective_score,
    access_assessment,
):
    route_status = (
        access_assessment or {}
    ).get("status") or "UNKNOWN"

    return (
        tier == "DIRECT"
        and int(effective_score or 0) >= 75
        and route_status in {
            "APPROVED",
            "IN_PROGRESS",
            "INDIRECT_ONLY",
        }
    )


def feedback_calibration_rows(rows):
    summary = {
        "total_reviewed": 0,
        "relevant": 0,
        "not_relevant": 0,
        "watch": 0,
        "decisive_reviews": 0,
        "relevance_rate": None,
        "learning_threshold": 20,
        "learning_ready": False,
        "reviews_needed": 20,
        "by_fit": {},
        "by_signal_type": {},
        "by_source": {},
        "rejection_reasons": {},
    }

    def bucket(container, key):
        if key not in container:
            container[key] = {
                "RELEVANT": 0,
                "NOT_RELEVANT": 0,
                "WATCH": 0,
            }
        return container[key]

    for row in rows:
        label = row.get("label")
        if label not in {
            "RELEVANT",
            "NOT_RELEVANT",
            "WATCH",
        }:
            continue

        summary["total_reviewed"] += 1

        if label == "RELEVANT":
            summary["relevant"] += 1
        elif label == "NOT_RELEVANT":
            summary["not_relevant"] += 1
            reason_code = row.get("reason_code") or "UNSPECIFIED"
            summary["rejection_reasons"][reason_code] = (
                summary["rejection_reasons"].get(reason_code, 0) + 1
            )
        elif label == "WATCH":
            summary["watch"] += 1

        tier = customer_fit_tier(
            row.get("reason_json")
        )
        signal_type = (
            row.get("signal_type")
            or "UNKNOWN"
        )
        source = (
            row.get("source")
            or "unknown"
        )

        bucket(
            summary["by_fit"],
            tier,
        )[label] += 1
        bucket(
            summary["by_signal_type"],
            signal_type,
        )[label] += 1
        bucket(
            summary["by_source"],
            source,
        )[label] += 1

    decisive = (
        summary["relevant"]
        + summary["not_relevant"]
    )
    summary["decisive_reviews"] = decisive

    if decisive:
        summary["relevance_rate"] = round(
            100 * summary["relevant"] / decisive,
            1,
        )

    threshold = summary[
        "learning_threshold"
    ]
    summary["learning_ready"] = (
        decisive >= threshold
    )
    summary["reviews_needed"] = max(
        0,
        threshold - decisive,
    )

    return summary


@app.get("/health")
def health():
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT NOW() AS now")
            row=cur.fetchone()
    return {"ok":True,"app":"Project Scope","version":APP_VERSION,"database_time":row["now"]}


@app.get("/api/customer-profile")
def get_customer_profile(
    customer: str = Query(DEFAULT),
):
    with connection() as conn:
        with conn.cursor() as cur:
            cust = customer_row(
                cur,
                customer,
            )

            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM customer_buyer_access
                WHERE customer_profile_id=%s
                """,
                (cust["id"],),
            )
            rule_count = int(
                cur.fetchone()["n"] or 0
            )

    result = profile_snapshot(cust)
    result["completeness"] = (
        profile_completeness(
            cust,
            rule_count,
        )
    )
    return result


@app.put("/api/customer-profile")
def update_customer_profile(
    request: CustomerProfileRequest,
    customer: str = Query(DEFAULT),
):
    name = " ".join(
        request.name.strip().split()
    )
    if not name:
        raise HTTPException(
            status_code=400,
            detail="Customer name is required",
        )

    min_value = (
        request.min_contract_value_gbp
    )
    max_value = (
        request.max_contract_value_gbp
    )

    if (
        min_value is not None
        and max_value is not None
        and min_value > max_value
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                "Minimum contract value cannot "
                "exceed maximum contract value"
            ),
        )

    with connection() as conn:
        with conn.cursor() as cur:
            cust = customer_row(
                cur,
                customer,
            )
            old_snapshot = profile_snapshot(
                cust
            )

            metadata = dict(
                cust.get("metadata") or {}
            )
            metadata.update({
                "company_summary": (
                    request.company_summary
                    or ""
                ).strip(),
                "certifications": _clean_list(
                    request.certifications
                ),
                "preferred_routes": _clean_list(
                    request.preferred_routes
                ),
                "notes": (
                    request.notes or ""
                ).strip(),
                "exclusions_confirmed": bool(
                    request.exclusions_confirmed
                ),
                "profile_version": "0.7.0",
            })

            cur.execute(
                """
                INSERT INTO customer_profile_revisions(
                    customer_profile_id,
                    snapshot_json,
                    change_note
                )
                VALUES(
                    %s,
                    %s::jsonb,
                    'Profile updated from Project Scope pilot setup'
                )
                """,
                (
                    cust["id"],
                    json.dumps(
                        old_snapshot,
                        default=str,
                    ),
                ),
            )

            cur.execute(
                """
                UPDATE customer_profiles
                SET
                    name=%s,
                    geography=%s::jsonb,
                    sectors=%s::jsonb,
                    capabilities=%s::jsonb,
                    preferred_buyers=%s::jsonb,
                    excluded_scopes=%s::jsonb,
                    min_contract_value_gbp=%s,
                    max_contract_value_gbp=%s,
                    metadata=%s::jsonb,
                    updated_at_utc=NOW()
                WHERE id=%s
                RETURNING *
                """,
                (
                    name,
                    json.dumps(
                        _clean_list(
                            request.geography
                        )
                    ),
                    json.dumps(
                        _clean_list(
                            request.sectors
                        )
                    ),
                    json.dumps(
                        _clean_list(
                            request.capabilities
                        )
                    ),
                    json.dumps(
                        _clean_list(
                            request.preferred_buyers
                        )
                    ),
                    json.dumps(
                        _clean_list(
                            request.excluded_scopes
                        )
                    ),
                    min_value,
                    max_value,
                    json.dumps(
                        metadata,
                        default=str,
                    ),
                    cust["id"],
                ),
            )
            updated = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM customer_buyer_access
                WHERE customer_profile_id=%s
                """,
                (cust["id"],),
            )
            rule_count = int(
                cur.fetchone()["n"] or 0
            )

    result = profile_snapshot(updated)
    result["completeness"] = (
        profile_completeness(
            updated,
            rule_count,
        )
    )

    return {
        "ok": True,
        "profile": result,
        "rescore_required": True,
        "collectors_to_rerun": [
            "NSTA-Collector",
            "FTS-Collector",
            "PCS-Collector",
        ],
        "note": (
            "Buyer access changes apply immediately. "
            "Profile capability/sector/value changes "
            "need collector reprocessing to fully "
            "rescore retained procurements."
        ),
    }


@app.get("/api/feedback-calibration")
def feedback_calibration(
    customer: str = Query(DEFAULT),
):
    with connection() as conn:
        with conn.cursor() as cur:
            cust = customer_row(
                cur,
                customer,
            )

            cur.execute(
                """
                SELECT
                    f.label,
                    f.reason_code,
                    f.created_at_utc,
                    f.updated_at_utc,
                    s.signal_type,
                    s.reason_json,
                    p.source
                FROM opportunity_feedback f
                JOIN opportunity_signals s
                  ON s.id=f.signal_id
                LEFT JOIN procurements p
                  ON p.id=s.procurement_id
                WHERE
                    f.customer_profile_id=%s
                ORDER BY
                    f.updated_at_utc DESC
                """,
                (cust["id"],),
            )
            rows = cur.fetchall()

    return feedback_calibration_rows(
        rows
    )


@app.get("/api/opportunities")
def opportunities(
    customer: str = Query(DEFAULT),
    min_score: int = Query(35, ge=0, le=100),
    limit: int = Query(100, ge=1, le=500),
    include_reviewed: bool = Query(True),
):
    raw_limit = min(
        500,
        max(
            100,
            limit * 3,
        ),
    )

    with connection() as conn:
        with conn.cursor() as cur:
            cust = customer_row(
                cur,
                customer,
            )
            rules = access_rules(
                cur,
                cust["id"],
            )

            cur.execute(
                """
                SELECT
                    s.id,
                    s.signal_type,
                    s.title,
                    s.relevance_score,
                    s.confidence,
                    s.timing_label,
                    s.recommended_action,
                    s.reason_json,
                    s.first_seen_at_utc,
                    s.last_updated_at_utc,
                    p.id AS procurement_id,
                    p.source,
                    p.description,
                    p.buyer_name,
                    p.published_at_utc,
                    p.deadline_at_utc,
                    p.value_amount,
                    p.value_currency,
                    p.location_text,
                    p.notice_type,
                    p.energy_relevance_score,
                    p.energy_relevance_reasons,
                    p.sector_gate_passed,
                    p.classifier_version,
                    p.cpv_codes,
                    aw.supplier_name AS package_holder_name,
                    r.source_url,
                    f.label AS feedback_label,
                    f.reason_code AS feedback_reason_code,
                    f.note AS feedback_note,
                    f.updated_at_utc AS feedback_updated_at
                FROM opportunity_signals s
                JOIN customer_profiles c
                  ON c.id=s.customer_profile_id
                LEFT JOIN procurements p
                  ON p.id=s.procurement_id
                LEFT JOIN raw_events r
                  ON r.id=p.raw_event_id
                LEFT JOIN LATERAL(
                    SELECT
                        ca.supplier_name
                    FROM contract_awards ca
                    WHERE
                        ca.procurement_id=p.id
                        AND ca.supplier_name IS NOT NULL
                        AND BTRIM(ca.supplier_name)<>''
                    ORDER BY ca.id DESC
                    LIMIT 1
                ) aw ON TRUE
                LEFT JOIN opportunity_feedback f
                  ON f.signal_id=s.id
                 AND f.customer_profile_id=c.id
                WHERE
                    c.slug=%s
                    AND s.status='ACTIVE'
                    AND s.relevance_score>=%s
                    AND (%s OR f.id IS NULL)
                ORDER BY
                    s.relevance_score DESC,
                    s.last_updated_at_utc DESC
                LIMIT %s
                """,
                (
                    customer,
                    min_score,
                    include_reviewed,
                    raw_limit,
                ),
            )
            rows = cur.fetchall()

    visible = []

    for row in rows:
        tier = customer_fit_tier(
            row.get("reason_json")
        )

        # A customer-facing dashboard must have direct or validated inferred
        # customer fit. Sector relevance alone belongs in Research.
        if tier == "NONE":
            continue

        (
            route_target,
            route_target_type,
        ) = route_target_for_signal(
            row,
            tier,
        )

        access = assess_access(
            route_target,
            rules,
        )
        effective, penalty = (
            route_adjusted_score(
                row.get("relevance_score"),
                access,
            )
        )

        if effective < min_score:
            continue

        row["access_assessment"] = access
        row["route_target_name"] = (
            route_target
        )
        row["route_target_type"] = (
            route_target_type
        )
        row["raw_relevance_score"] = (
            row.get("relevance_score")
        )
        row["effective_score"] = effective
        row["route_penalty"] = penalty
        row["customer_fit_tier"] = tier
        row["match_explanation"] = signal_match_explanation(
            row.get("reason_json")
        )
        row["high_priority"] = is_high_priority(
            tier,
            effective,
            access,
        )

        visible.append(row)

    visible.sort(
        key=lambda row: (
            row.get("effective_score") or 0,
            row.get("raw_relevance_score") or 0,
            row.get("last_updated_at_utc"),
        ),
        reverse=True,
    )

    (
        visible,
        _duplicates_hidden,
    ) = dedupe_opportunity_rows(
        visible
    )

    return visible[:limit]


@app.get("/api/research-intelligence")
def research_intelligence(limit:int=Query(50,ge=1,le=500)):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ri.id,ri.title,ri.intelligence_kind,ri.customer_facing,ri.confidence,
                       ri.likely_downstream_scopes,ri.reason_json,ri.first_seen_at_utc,ri.last_updated_at_utc,
                       p.source,p.buyer_name,p.notice_type,p.published_at_utc,p.value_amount,p.value_currency,
                       p.location_text,aw.supplier_name AS package_holder_name,r.source_url
                FROM research_intelligence ri JOIN procurements p ON p.id=ri.procurement_id
                LEFT JOIN LATERAL(
                    SELECT ca.supplier_name
                    FROM contract_awards ca
                    WHERE ca.procurement_id=p.id
                    ORDER BY ca.id DESC
                    LIMIT 1
                ) aw ON TRUE
                LEFT JOIN raw_events r ON r.id=p.raw_event_id WHERE ri.status='ACTIVE'
                ORDER BY p.published_at_utc DESC NULLS LAST,ri.last_updated_at_utc DESC LIMIT %s
            """,(limit,))
            return cur.fetchall()


@app.post("/api/opportunities/{signal_id}/feedback")
def save_feedback(signal_id:int, request:FeedbackRequest):
    if request.label == "NOT_RELEVANT" and not request.reason_code:
        raise HTTPException(
            status_code=400,
            detail="A rejection reason is required for NOT_RELEVANT feedback.",
        )

    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id,customer_profile_id FROM opportunity_signals WHERE id=%s",
                (signal_id,),
            )
            signal = cur.fetchone()
            if not signal:
                raise HTTPException(status_code=404,detail="Signal not found")

            cur.execute("""
                INSERT INTO opportunity_feedback(
                    signal_id,customer_profile_id,label,reason_code,note
                )
                VALUES(%s,%s,%s,%s,%s)
                ON CONFLICT(signal_id,customer_profile_id) DO UPDATE SET
                    label=EXCLUDED.label,
                    reason_code=EXCLUDED.reason_code,
                    note=EXCLUDED.note,
                    updated_at_utc=NOW()
                RETURNING *
            """,(
                signal_id,
                signal["customer_profile_id"],
                request.label,
                request.reason_code,
                request.note,
            ))
            saved = cur.fetchone()

    return {"ok":True,"feedback":saved}


@app.get("/api/access-rules")
def get_access_rules(customer:str=Query(DEFAULT)):
    with connection() as conn:
        with conn.cursor() as cur:
            cust=customer_row(cur,customer)
            return access_rules(cur,cust["id"])


@app.get("/api/access-candidates")
def get_access_candidates(
    customer: str = Query(DEFAULT),
    limit: int = Query(
        50,
        ge=1,
        le=200,
    ),
):
    with connection() as conn:
        with conn.cursor() as cur:
            cust = customer_row(
                cur,
                customer,
            )
            rules = access_rules(
                cur,
                cust["id"],
            )

            cur.execute(
                """
                SELECT
                    s.id,
                    s.signal_type,
                    s.relevance_score,
                    s.reason_json,
                    p.buyer_name,
                    aw.supplier_name AS package_holder_name
                FROM opportunity_signals s
                LEFT JOIN procurements p
                  ON p.id=s.procurement_id
                LEFT JOIN LATERAL(
                    SELECT ca.supplier_name
                    FROM contract_awards ca
                    WHERE ca.procurement_id=p.id
                    ORDER BY ca.id DESC
                    LIMIT 1
                ) aw ON TRUE
                WHERE
                    s.customer_profile_id=%s
                    AND s.status='ACTIVE'
                ORDER BY
                    s.relevance_score DESC,
                    s.last_updated_at_utc DESC
                LIMIT 1000
                """,
                (cust["id"],),
            )
            rows = cur.fetchall()

    buyers = {}

    for row in rows:
        tier = customer_fit_tier(
            row.get("reason_json")
        )
        if tier == "NONE":
            continue

        (
            route_target,
            route_target_type,
        ) = route_target_for_signal(
            row,
            tier,
        )
        if not route_target:
            continue

        access = assess_access(
            route_target,
            rules,
        )

        # Only unresolved route targets belong in this queue.
        if access.get("rule_id"):
            continue

        effective, _penalty = (
            route_adjusted_score(
                row.get("relevance_score"),
                access,
            )
        )
        if effective < ACCESS_REVIEW_MIN_SCORE:
            continue

        name = route_target
        if not name:
            continue

        key = name.lower()
        item = buyers.setdefault(
            key,
            {
                "buyer_name": name,
                "route_target_type": (
                    route_target_type
                ),
                "signal_count": 0,
                "direct_fit": 0,
                "inferred_downstream": 0,
                "best_raw_score": 0,
                "best_effective_score": 0,
                "signal_types": set(),
            },
        )

        item["signal_count"] += 1
        item["best_raw_score"] = max(
            item["best_raw_score"],
            int(
                row.get(
                    "relevance_score"
                ) or 0
            ),
        )
        item[
            "best_effective_score"
        ] = max(
            item[
                "best_effective_score"
            ],
            effective,
        )
        item["signal_types"].add(
            row.get("signal_type")
            or "UNKNOWN"
        )

        if tier == "DIRECT":
            item["direct_fit"] += 1
        elif tier == "INFERRED_DOWNSTREAM":
            item[
                "inferred_downstream"
            ] += 1

    result = []

    for item in buyers.values():
        item["signal_types"] = sorted(
            item["signal_types"]
        )
        result.append(item)

    result.sort(
        key=lambda item: (
            item["direct_fit"] > 0,
            item["best_effective_score"],
            item["signal_count"],
        ),
        reverse=True,
    )

    return result[:limit]


@app.post("/api/access-rules")
def save_access_rule(request:AccessRuleRequest,customer:str=Query(DEFAULT)):
    pattern=request.buyer_name_pattern.strip()
    if not pattern: raise HTTPException(status_code=400,detail="buyer_name_pattern is required")
    with connection() as conn:
        with conn.cursor() as cur:
            cust=customer_row(cur,customer)
            cur.execute("""
                INSERT INTO customer_buyer_access(
                    customer_profile_id,buyer_name_pattern,access_status,barrier_type,note,evidence_source
                ) VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT(customer_profile_id,buyer_name_pattern) DO UPDATE SET
                    access_status=EXCLUDED.access_status,barrier_type=EXCLUDED.barrier_type,
                    note=EXCLUDED.note,evidence_source=EXCLUDED.evidence_source,updated_at_utc=NOW()
                RETURNING *
            """,(cust["id"],pattern,request.access_status,request.barrier_type,request.note,request.evidence_source))
            row=cur.fetchone()
    return {"ok":True,"rule":row}


@app.delete("/api/access-rules/{rule_id}")
def delete_access_rule(rule_id:int,customer:str=Query(DEFAULT)):
    with connection() as conn:
        with conn.cursor() as cur:
            cust=customer_row(cur,customer)
            cur.execute("DELETE FROM customer_buyer_access WHERE id=%s AND customer_profile_id=%s RETURNING id",(rule_id,cust["id"]))
            row=cur.fetchone()
            if not row: raise HTTPException(status_code=404,detail="Access rule not found")
    return {"ok":True}


@app.get("/api/stats")
def stats(
    customer: str = Query(DEFAULT),
):
    with connection() as conn:
        with conn.cursor() as cur:
            cust = customer_row(
                cur,
                customer,
            )
            rules = access_rules(
                cur,
                cust["id"],
            )

            cur.execute(
                """
                SELECT
                    s.id,
                    s.signal_type,
                    s.title,
                    s.relevance_score,
                    s.reason_json,
                    s.last_updated_at_utc,
                    p.source,
                    p.description,
                    p.published_at_utc,
                    p.buyer_name,
                    aw.supplier_name AS package_holder_name,
                    f.id AS feedback_id
                FROM opportunity_signals s
                LEFT JOIN procurements p
                  ON p.id=s.procurement_id
                LEFT JOIN LATERAL(
                    SELECT ca.supplier_name
                    FROM contract_awards ca
                    WHERE ca.procurement_id=p.id
                    ORDER BY ca.id DESC
                    LIMIT 1
                ) aw ON TRUE
                LEFT JOIN opportunity_feedback f
                  ON f.signal_id=s.id
                 AND f.customer_profile_id=s.customer_profile_id
                WHERE
                    s.customer_profile_id=%s
                    AND s.status='ACTIVE'
                """,
                (cust["id"],),
            )
            signal_rows = cur.fetchall()
            raw_signal_count = len(
                signal_rows
            )
            (
                signal_rows,
                duplicate_rows_suppressed,
            ) = dedupe_opportunity_rows(
                signal_rows
            )

            signals = {
                "raw_active": raw_signal_count,
                "duplicates_suppressed": (
                    duplicate_rows_suppressed
                ),
                "active": 0,
                "high_priority": 0,
                "direct_fit": 0,
                "inferred_downstream": 0,
                "live": 0,
                "emerging": 0,
                "intelligence": 0,
                "reviewed": 0,
                "suppressed_no_customer_fit": 0,
                "suppressed_by_route_adjustment": 0,
            }

            for row in signal_rows:
                if row.get("feedback_id"):
                    signals["reviewed"] += 1

                tier = customer_fit_tier(
                    row.get("reason_json")
                )

                if tier == "NONE":
                    signals[
                        "suppressed_no_customer_fit"
                    ] += 1
                    continue

                (
                    route_target,
                    _route_target_type,
                ) = route_target_for_signal(
                    row,
                    tier,
                )
                access = assess_access(
                    route_target,
                    rules,
                )
                effective, _penalty = (
                    route_adjusted_score(
                        row.get(
                            "relevance_score"
                        ),
                        access,
                    )
                )

                if effective < 35:
                    signals[
                        "suppressed_by_route_adjustment"
                    ] += 1
                    continue

                signals["active"] += 1

                if tier == "DIRECT":
                    signals["direct_fit"] += 1
                elif (
                    tier
                    == "INFERRED_DOWNSTREAM"
                ):
                    signals[
                        "inferred_downstream"
                    ] += 1

                signal_type = row.get(
                    "signal_type"
                )
                if signal_type == "LIVE":
                    signals["live"] += 1
                elif signal_type == "EMERGING":
                    signals["emerging"] += 1
                elif signal_type == "INTELLIGENCE":
                    signals[
                        "intelligence"
                    ] += 1

                if is_high_priority(
                    tier,
                    effective,
                    access,
                ):
                    signals[
                        "high_priority"
                    ] += 1

            cur.execute(
                """
                SELECT COUNT(*) AS research_retained
                FROM research_intelligence
                WHERE status='ACTIVE'
                """
            )
            research = cur.fetchone()

            cur.execute(
                """
                SELECT COUNT(*) AS access_rules
                FROM customer_buyer_access
                WHERE customer_profile_id=%s
                """,
                (cust["id"],),
            )
            access = cur.fetchone()

            cur.execute(
                """
                SELECT
                    source,
                    COUNT(*) AS procurements
                FROM procurements
                GROUP BY source
                ORDER BY source
                """
            )
            sources = cur.fetchall()

            cur.execute(
                """
                SELECT
                    collector,
                    status,
                    started_at_utc,
                    finished_at_utc,
                    fetched_count,
                    processed_count,
                    error_count
                FROM collector_runs
                ORDER BY id DESC
                LIMIT 8
                """
            )
            runs = cur.fetchall()

            cur.execute(
                """
                SELECT
                    COUNT(*) FILTER(
                        WHERE published_at_utc >=
                        NOW() - INTERVAL '7 days'
                    ) AS sourced_7d,
                    COUNT(*) FILTER(
                        WHERE published_at_utc >=
                        NOW() - INTERVAL '7 days'
                        AND sector_gate_passed=TRUE
                    ) AS sector_accepted_7d,
                    COUNT(*) FILTER(
                        WHERE published_at_utc >=
                        NOW() - INTERVAL '7 days'
                        AND sector_gate_passed=FALSE
                    ) AS sector_rejected_7d
                FROM procurements
                """
            )
            classifier_funnel = (
                cur.fetchone()
            )

            cur.execute(
                """
                SELECT
                    f.label,
                    f.reason_code,
                    s.signal_type,
                    s.reason_json,
                    p.source
                FROM opportunity_feedback f
                JOIN opportunity_signals s
                  ON s.id=f.signal_id
                LEFT JOIN procurements p
                  ON p.id=s.procurement_id
                WHERE f.customer_profile_id=%s
                """,
                (cust["id"],),
            )
            feedback_rows = cur.fetchall()

            calibration = (
                feedback_calibration_rows(
                    feedback_rows
                )
            )

            profile = profile_completeness(
                cust,
                int(
                    access.get(
                        "access_rules"
                    ) or 0
                ),
            )

            unresolved_buyers = set()

            for row in signal_rows:
                tier = customer_fit_tier(
                    row.get("reason_json")
                )
                if tier == "NONE":
                    continue

                (
                    route_target,
                    _route_target_type,
                ) = route_target_for_signal(
                    row,
                    tier,
                )
                if not route_target:
                    continue

                assessment = assess_access(
                    route_target,
                    rules,
                )
                if assessment.get("rule_id"):
                    continue

                effective, _penalty = (
                    route_adjusted_score(
                        row.get(
                            "relevance_score"
                        ),
                        assessment,
                    )
                )
                if effective >= ACCESS_REVIEW_MIN_SCORE:
                    unresolved_buyers.add(
                        " ".join(
                            str(route_target).lower().split()
                        )
                    )

            signals["unresolved_buyers"] = (
                len(unresolved_buyers)
            )
            signals["unreviewed"] = max(
                0,
                signals["active"]
                - signals["reviewed"],
            )

    return {
        "customer": customer,
        "app_version": APP_VERSION,
        "signals": signals,
        "research": research,
        "access": access,
        "sources": sources,
        "collector_runs": runs,
        "classifier_funnel": (
            classifier_funnel
        ),
        "route_score_penalties": (
            ROUTE_SCORE_PENALTIES
        ),
        "profile_completeness": profile,
        "feedback_calibration": calibration,
        "access_review_min_score": ACCESS_REVIEW_MIN_SCORE,
    }



@app.get("/api/classifier-review")
def classifier_review(
    accepted: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    p.id,
                    p.source,
                    p.title,
                    p.buyer_name,
                    p.notice_type,
                    p.published_at_utc,
                    p.location_text,
                    p.value_amount,
                    p.value_currency,
                    p.energy_relevance_score,
                    p.energy_relevance_reasons,
                    p.sector_gate_passed,
                    p.classifier_version,
                    r.source_url
                FROM procurements p
                LEFT JOIN raw_events r ON r.id=p.raw_event_id
                WHERE p.sector_gate_passed=%s
                ORDER BY COALESCE(p.published_at_utc,p.updated_at_utc) DESC
                LIMIT %s
            """,(accepted,limit))
            return cur.fetchall()


@app.get("/classifier-review",response_class=HTMLResponse)
def classifier_review_page():
    return """<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Project Scope Classifier Review</title>
<style>
:root{color-scheme:dark}body{font-family:system-ui;background:#111318;color:#f4f4f5;max-width:1200px;margin:34px auto;padding:0 20px}
.muted{color:#a1a1aa}.item{background:#181b21;border:1px solid #30343d;border-radius:14px;padding:17px;margin:12px 0}
.pill{background:#252932;border-radius:999px;padding:5px 9px;font-size:12px;color:#d4d4d8;margin-right:6px}
a{color:#8ab4ff}button{background:#252932;color:white;border:1px solid #454a55;border-radius:8px;padding:8px 11px;margin-right:6px}
</style></head>
<body><h1>Classifier Review</h1>
<p class="muted">Rejected procurements stay in the database. Review them here to detect false negatives while the customer feed stays strict.</p>
<p><a href="/">← Back to Scope</a></p>
<button onclick="load(false)">Rejected</button><button onclick="load(true)">Accepted</button>
<div id="items"></div>
<script>
const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
async function load(accepted=false){
 const rows=await (await fetch('/api/classifier-review?accepted='+accepted+'&limit=100')).json();
 items.innerHTML=rows.map(r=>{
   const hits=(r.energy_relevance_reasons||[]).filter(x=>['strong_sector','strong_cpv','support_sector','hard_negative','decision'].includes(x.category));
   return `<div class=item><span class=pill>${esc(r.source)}</span><span class=pill>${r.sector_gate_passed?'ACCEPTED':'REJECTED'}</span>
   <span class=pill>sector ${esc(r.energy_relevance_score)}</span><h3>${esc(r.title)}</h3><div>${esc(r.buyer_name||'')}</div>
   <p class=muted>${hits.map(x=>esc(x.term)+(x.reason?' — '+esc(x.reason):'')).join(' · ')}</p>
   ${r.source_url?`<a href="${esc(r.source_url)}" target=_blank rel=noopener>Open source ↗</a>`:''}</div>`;
 }).join('')||'<p class=muted>No records.</p>';
}load(false);
</script></body></html>"""

@app.get("/",response_class=HTMLResponse)
def home():
    return """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Project Scope v0.7.9</title><style>
:root{color-scheme:dark}body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#111318;color:#f4f4f5;max-width:1250px;margin:34px auto;padding:0 20px}h1{font-size:34px;margin-bottom:4px}.muted{color:#a1a1aa}.cards{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}.card{background:#1b1e25;border:1px solid #30343d;border-radius:13px;padding:16px;min-width:145px}.num{font-size:30px;font-weight:750}.signal{background:#181b21;border:1px solid #30343d;border-radius:14px;padding:19px;margin:14px 0}.topline{display:flex;justify-content:space-between;gap:20px}.score{font-size:30px;font-weight:800}.LIVE{color:#ff7b72}.EMERGING{color:#f2cc60}.INTELLIGENCE{color:#79c0ff}.meta,.breakdown{display:flex;gap:9px;flex-wrap:wrap;margin:9px 0}.pill{background:#252932;border-radius:999px;padding:5px 9px;font-size:12px;color:#d4d4d8}.access-bad{border:1px solid #8e3c3c}.access-good{border:1px solid #2f7d4a}.why{background:#121419;border-radius:10px;padding:12px;margin-top:12px}a{color:#8ab4ff}button{border:1px solid #454a55;background:#262a33;color:white;border-radius:9px;padding:9px 12px;margin:6px 5px 0 0;cursor:pointer}.nav{display:flex;gap:14px;margin:12px 0 0}.feedback{font-size:13px;margin-top:8px}.filters{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 18px}.filters button.active{border-color:#8ab4ff}.priority{border:1px solid #c69026;color:#f2cc60}.reject-select{background:#20242c;color:#fff;border:1px solid #454a55;border-radius:8px;padding:8px;margin:6px 6px 6px 0;max-width:220px}.match-why{border-left:3px solid #8ab4ff}</style></head><body>
<h1>Project Scope <span class='muted'>v0.7.9</span></h1><p class='muted'>Commercial opportunity intelligence — private research dashboard.</p><div class='nav'><a href='/research'>Research intelligence</a><a href='/access'>Buyer access / barriers</a><a href='/pilot'>Pilot setup</a><a href="/classifier-review">Classifier review</a><a href="/review-export">Export review pack ↓</a></div><div id='cards' class='cards'></div><div class='filters'><button id='f-all' class='active' onclick="setFilter('ALL')">All</button><button id='f-unreviewed' onclick="setFilter('UNREVIEWED')">Unreviewed</button><button id='f-direct' onclick="setFilter('DIRECT')">Direct fit</button><button id='f-watch' onclick="setFilter('WATCH')">Watch</button></div><div id='signals'></div>
<script>
const esc=(s)=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
function money(v,c){if(v===null||v===undefined||v==='')return'';const n=Number(v);return Number.isNaN(n)?esc(v):new Intl.NumberFormat('en-GB',{style:'currency',currency:c||'GBP',maximumFractionDigits:0}).format(n)}
function breakdown(r){const x=r.reason_json||{};const tier=r.customer_fit_tier||x.customer_fit?.tier||x.capability_fit?.fit_type||'NONE';const pills=[['Capability',x.capability_fit?.score],['Sector',x.sector_fit?.score],['Geography',x.geography_fit?.score],['Value',x.contract_value_fit?.score],['Actionability',x.actionability?.score],['Evidence',x.evidence_quality?.score]].filter(x=>x[1]!==undefined).map(x=>`<span class='pill'>${x[0]} ${x[1]}</span>`);pills.unshift(`<span class='pill'>Fit ${esc(tier.replaceAll('_',' '))}</span>`);const ts=x.target_sector_fit||{};if(ts.configured){const sectorLabel=ts.authoritative_source_override?'Broad energy source':(ts.authoritative_project_context?'Target sector ✓ NSTA project':('Target sector '+(ts.passed?'✓':'✕')));pills.push(`<span class='pill'>${sectorLabel}${(ts.matched_families||[]).length?' '+esc(ts.matched_families.join(', ').replaceAll('_',' ')):''}</span>`);}const lc=x.lifecycle_gate||{};if(lc.status&&lc.status!=='CURRENT')pills.push(`<span class='pill'>Lifecycle ${esc(lc.status.replaceAll('_',' '))}${lc.award_age_days!==null&&lc.award_age_days!==undefined?' · '+esc(lc.award_age_days)+'d':''}</span>`);if((x.capability_fit?.inferred_customer_capabilities||[]).length)pills.push(`<span class='pill'>Matched ${esc(x.capability_fit.inferred_customer_capabilities.join(', '))}</span>`);return pills.join('')}
function sourceName(s){return s==='find_a_tender'?'Find a Tender':s==='public_contracts_scotland'?'PCS':s==='nsta_energy_pathfinder'?'NSTA Energy Pathfinder':s||''}
function accessPill(a){if(!a)return'';const bad=a.status==='NOT_APPROVED',good=a.status==='APPROVED';return `<span class='pill ${bad?'access-bad':good?'access-good':''}'>Route: ${esc(a.status.replaceAll('_',' '))}${a.barrier_type&&a.barrier_type!=='NONE'?' · '+esc(a.barrier_type.replaceAll('_',' ')):''}</span>`}
async function feedback(id,label,reasonCode=null){const r=await fetch(`/api/opportunities/${id}/feedback`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label,reason_code:reasonCode})});if(!r.ok){alert(await r.text());return}load()}function rejectFeedback(id){const el=document.getElementById('reject-'+id);const reason=el?el.value:'';if(!reason){alert('Choose why this is not relevant. Scope will use these labels for later calibration.');return}feedback(id,'NOT_RELEVANT',reason)}
let currentFilter='ALL',latestRows=[];function setFilter(v){currentFilter=v;document.querySelectorAll('.filters button').forEach(b=>b.classList.remove('active'));const id={'ALL':'f-all','UNREVIEWED':'f-unreviewed','DIRECT':'f-direct','WATCH':'f-watch'}[v];if(id)document.getElementById(id).classList.add('active');renderRows()}function renderRows(){let rows=latestRows;if(currentFilter==='UNREVIEWED')rows=rows.filter(r=>!r.feedback_label);if(currentFilter==='DIRECT')rows=rows.filter(r=>r.customer_fit_tier==='DIRECT');if(currentFilter==='WATCH')rows=rows.filter(r=>r.feedback_label==='WATCH');document.getElementById('signals').innerHTML=rows.map(r=>{const m=[sourceName(r.source),r.buyer_name?`Buyer: ${r.buyer_name}`:null,r.package_holder_name&&r.package_holder_name!==r.buyer_name?`Package holder: ${r.package_holder_name}`:null,r.notice_type,r.published_at_utc?'Published / awarded '+new Date(r.published_at_utc).toLocaleDateString('en-GB'):null,r.deadline_at_utc?'Deadline '+new Date(r.deadline_at_utc).toLocaleDateString('en-GB'):null,r.value_amount?money(r.value_amount,r.value_currency):null,r.location_text,r.duplicate_records_hidden?`${r.duplicate_records_hidden} duplicate hidden`:null].filter(Boolean);const a=r.access_assessment||{};const raw=Number(r.raw_relevance_score??r.relevance_score??0),effective=Number(r.effective_score??raw),routePenalty=Number(r.route_penalty||0),mx=r.match_explanation||{};const rejectReasons=[['WRONG_SECTOR','Wrong sector'],['WRONG_CAPABILITY','Wrong capability'],['WRONG_GEOGRAPHY','Wrong geography'],['CONTRACT_VALUE','Contract value'],['NO_REALISTIC_ROUTE','No realistic route'],['DUPLICATE_OR_STALE','Duplicate / stale'],['OTHER','Other']];return `<div class='signal'><div class='topline'><div><b class='${esc(r.signal_type)}'>${esc(r.signal_type)}</b>${r.high_priority?` <span class='pill priority'>HIGH PRIORITY</span>`:''}<h3>${esc(r.title)}</h3></div><div><div class='score'>${esc(effective)}</div>${routePenalty?`<span class='pill'>Raw ${esc(raw)} · route −${esc(routePenalty)}</span>`:''}</div></div><div class='meta'>${m.map(x=>`<span class='pill'>${esc(x)}</span>`).join('')}${accessPill(a)}</div><div class='breakdown'>${breakdown(r)}</div><div class='why match-why'><b>Why this matches my business</b><br>${esc(mx.text||'No match explanation available yet.')}${(mx.customer_capabilities||[]).length?`<br><span class='muted'>Customer capability: ${esc(mx.customer_capabilities.join(', '))}</span>`:''}${(mx.evidence_terms||[]).length?`<br><span class='muted'>Procurement evidence: ${esc(mx.evidence_terms.join(', '))}</span>`:''}</div>${a.note?`<div class='why'><b>Route-to-market note</b><br>${esc(a.note)}</div>`:''}<p>${esc(r.recommended_action||'')}</p>${a.status==='UNKNOWN'&&r.route_target_name&&effective>=50?`<p><a href='/access?buyer=${encodeURIComponent(r.route_target_name)}'>Resolve ${r.route_target_type==='PACKAGE_HOLDER'?'package-holder':'buyer'} access →</a></p>`:''}${r.source_url?`<a href='${esc(r.source_url)}' target='_blank' rel='noopener'>Open official source ↗</a>`:''}<div><button onclick="feedback(${r.id},'RELEVANT')">✓ Relevant</button><select class='reject-select' id='reject-${r.id}'><option value=''>Reject reason…</option>${rejectReasons.map(([v,l])=>`<option value='${v}' ${r.feedback_reason_code===v?'selected':''}>${l}</option>`).join('')}</select><button onclick="rejectFeedback(${r.id})">✕ Not relevant</button><button onclick="feedback(${r.id},'WATCH')">◉ Watch</button></div><div class='feedback muted'>${r.feedback_label?'Your label: '+esc(r.feedback_label.replaceAll('_',' '))+(r.feedback_reason_code?' · '+esc(r.feedback_reason_code.replaceAll('_',' ')):''):'Not reviewed yet'}</div></div>`}).join('')||"<p class='muted'>No signals in this view.</p>"}async function load(){const st=await(await fetch('/api/stats')).json();const s=st.signals||{},rr=st.research||{},aa=st.access||{},pc=st.profile_completeness||{};const cards=[['Active',s.active],['High priority',s.high_priority],['Direct fit',s.direct_fit],['Inferred downstream',s.inferred_downstream],['Duplicates hidden',s.duplicates_suppressed||0],['Unreviewed',s.unreviewed],['Unresolved routes',s.unresolved_buyers],['Profile complete',(pc.percent??0)+'%'],['Live',s.live],['Emerging',s.emerging],['Intelligence',s.intelligence],['Research retained',rr.research_retained],['Access rules',aa.access_rules]];document.getElementById('cards').innerHTML=cards.map(x=>`<div class='card'><div class='num'>${x[1]??0}</div><div class='muted'>${x[0]}</div></div>`).join('');latestRows=await(await fetch('/api/opportunities?min_score=35&limit=100')).json();renderRows()}load();
</script></body></html>"""



@app.get("/review-export", response_class=HTMLResponse)
def review_export_page():
    return """<!doctype html>
<html>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width,initial-scale=1'>
<title>Project Scope Review Export</title>
<style>
:root{color-scheme:dark}
body{font-family:system-ui;background:#111318;color:#f4f4f5;max-width:900px;margin:34px auto;padding:0 20px}
.card{background:#181b21;border:1px solid #30343d;border-radius:14px;padding:18px;margin:14px 0}
button{background:#1d2a40;color:#b9d1ff;border:1px solid #5b76a8;border-radius:9px;padding:11px 14px;font:inherit;font-weight:650;cursor:pointer}
button:disabled{opacity:.55;cursor:wait}
a{color:#8ab4ff}
.muted{color:#a1a1aa}
.ok{color:#7ddc9b}
.err{color:#ff8c8c;white-space:pre-wrap}
code{background:#252932;padding:2px 5px;border-radius:5px}
</style>
</head>
<body>
<h1>Export review pack</h1>
<p><a href='/'>← Back to Project Scope</a></p>

<div class='card'>
  <p>This creates one JSON file containing the pilot profile, current active opportunities,
  scoring evidence, buyer-access rules and the commercial review brief.</p>
  <button id='exportBtn' onclick='exportReviewPack()'>Download review pack ↓</button>
  <p id='status' class='muted'>Ready.</p>
</div>

<div class='card'>
  <b>Review context</b>
  <p class='muted'>The reviewer should act as the commercial / business-development manager
  of the configured small engineering-services company and judge whether each opportunity
  deserves roughly 15–30 minutes of BD investigation.</p>
</div>

<script>
function setStatus(text, cls){
  const el=document.getElementById('status');
  el.textContent=text;
  el.className=cls||'muted';
}

async function fetchJson(url){
  const response=await fetch(url,{cache:'no-store'});
  if(!response.ok){
    const body=await response.text();
    throw new Error(url+' returned HTTP '+response.status+'\\n'+body);
  }
  return await response.json();
}

async function exportReviewPack(){
  const btn=document.getElementById('exportBtn');
  btn.disabled=true;
  setStatus('Building review pack…','muted');

  try{
    const results=await Promise.all([
      fetchJson('/api/customer-profile'),
      fetchJson('/api/stats'),
      fetchJson('/api/opportunities?min_score=35&limit=500&include_reviewed=true'),
      fetchJson('/api/access-rules'),
      fetchJson('/api/feedback-calibration')
    ]);

    const profile=results[0];
    const dashboard=results[1];
    const opportunities=results[2];
    const accessRules=results[3];
    const calibration=results[4];
    const generated=new Date();

    const reviewContext={
      reviewer_role:
        'Act as the commercial / business-development manager of the small engineering-services company described in customer_profile. Do not review these as a Tier-1 EPC or major operator.',
      commercial_question:
        "For each opportunity, decide whether it is worth roughly 15-30 minutes of a BD person's time to investigate further.",
      labels:{
        RELEVANT:
          'A realistic route to revenue may exist. It is worth actively investigating the package holder, buyer, subcontract route or next commercial step.',
        WATCH:
          'Commercially plausible but too early, incomplete or uncertain for meaningful BD effort yet. Keep it under observation.',
        NOT_RELEVANT:
          "Even considering downstream/subcontract routes, this is not worth this company's commercial time."
      },
      important_rules:[
        'Do not reject solely because the direct buyer route is UNKNOWN or the company is not yet on an approved-vendor list.',
        'DIRECT means explicit source evidence matches the customer capabilities.',
        'INFERRED_DOWNSTREAM means the headline package is not the customer scope, but a credible downstream requirement may match.',
        'Be sceptical of inferred downstream opportunities and require a credible causal link to the actual services offered.',
        'For NOT_RELEVANT use the best reason: WRONG_SECTOR, WRONG_CAPABILITY, WRONG_GEOGRAPHY, CONTRACT_VALUE, NO_REALISTIC_ROUTE, DUPLICATE_OR_STALE, or OTHER.'
      ],
      requested_output:
        'Review every active opportunity. Return RELEVANT, WATCH or NOT_RELEVANT; concise rationale; rejection reason where applicable; and next BD action where relevant. Then summarise false-positive patterns and evidence-supported scoring/classifier changes.'
    };

    const pack={
      export_schema_version:3,
      project:'Project Scope',
      app_version:'0.7.9',
      generated_at_utc:generated.toISOString(),
      review_context:reviewContext,
      customer_profile:profile,
      dashboard_stats:dashboard,
      buyer_access_rules:accessRules,
      feedback_calibration:calibration,
      active_opportunity_count:Array.isArray(opportunities)?opportunities.length:0,
      active_opportunities:opportunities
    };

    const text=JSON.stringify(pack,null,2);
    const blob=new Blob([text],{type:'application/json'});
    const objectUrl=URL.createObjectURL(blob);
    const link=document.createElement('a');
    const stamp=generated.toISOString().slice(0,16).replace(/[-:T]/g,'');
    link.href=objectUrl;
    link.download='project-scope-review-'+stamp+'.json';
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function(){URL.revokeObjectURL(objectUrl);},1000);

    setStatus(
      'Downloaded '+pack.active_opportunity_count+' active opportunities. Upload the JSON file to ChatGPT.',
      'ok'
    );
  }catch(error){
    console.error(error);
    setStatus(
      'Export failed:\\n'+error.message,
      'err'
    );
  }finally{
    btn.disabled=false;
  }
}
</script>
</body>
</html>"""


@app.get("/research",response_class=HTMLResponse)
def research_page():
    return """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Project Scope Research</title><style>:root{color-scheme:dark}body{font-family:system-ui;background:#111318;color:#f4f4f5;max-width:1100px;margin:34px auto;padding:0 20px}.muted{color:#a1a1aa}.item{background:#181b21;border:1px solid #30343d;border-radius:14px;padding:17px;margin:12px 0}.pill{background:#252932;border-radius:999px;padding:5px 9px;font-size:12px;color:#d4d4d8;margin-right:6px}a{color:#8ab4ff}</style></head><body><h1>Retained Industry Intelligence</h1><p><a href='/'>← Opportunities</a> · <a href='/access'>Buyer access</a></p><div id='items'></div><script>const esc=(s)=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');function sn(s){return s==='find_a_tender'?'Find a Tender':s==='public_contracts_scotland'?'PCS':s==='nsta_energy_pathfinder'?'NSTA Energy Pathfinder':s||''}async function load(){const rows=await(await fetch('/api/research-intelligence?limit=100')).json();document.getElementById('items').innerHTML=rows.map(r=>`<div class='item'><span class='pill'>${esc(sn(r.source))}</span><span class='pill'>${esc(r.intelligence_kind)}</span><span class='pill'>${esc(r.confidence)}% confidence</span>${r.published_at_utc?`<span class='pill'>Published / awarded ${new Date(r.published_at_utc).toLocaleDateString('en-GB')}</span>`:''}<h3>${esc(r.title)}</h3><div>${r.buyer_name?`Buyer: ${esc(r.buyer_name)}`:''}${r.package_holder_name?` · Package holder: ${esc(r.package_holder_name)}`:''}</div>${(r.likely_downstream_scopes||[]).length?`<p>Likely downstream: ${esc((r.likely_downstream_scopes||[]).join(', '))}</p>`:''}${r.source_url?`<a href='${esc(r.source_url)}' target='_blank'>Open official source ↗</a>`:''}</div>`).join('')||'<p class=muted>No retained intelligence yet.</p>'}load()</script></body></html>"""


@app.get("/access",response_class=HTMLResponse)
def access_page():
    return """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Project Scope Access</title><style>:root{color-scheme:dark}body{font-family:system-ui;background:#111318;color:#f4f4f5;max-width:1050px;margin:34px auto;padding:0 20px}input,select,textarea,button{background:#20242c;color:#fff;border:1px solid #454a55;border-radius:8px;padding:10px;margin:5px}input{min-width:280px}.row{background:#181b21;border:1px solid #30343d;border-radius:12px;padding:14px;margin:10px 0}.candidate{border-left:4px solid #f2cc60}.muted{color:#a1a1aa}a{color:#8ab4ff}.pill{display:inline-block;background:#252932;border-radius:999px;padding:5px 9px;font-size:12px;margin:3px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:22px}@media(max-width:760px){.grid{grid-template-columns:1fr}input,textarea{width:92%;min-width:0}}</style></head><body><h1>Buyer access / route-to-market</h1><p class='muted'>Resolve the commercial route target with meaningful customer-fit signals (effective score 50+). For inferred downstream work this prefers the awarded package holder over the headline buyer. Low-score unknown routes stay out of this investigation queue. Access decisions apply immediately — no collector rerun is required.</p><p><a href='/'>← Opportunities</a> · <a href='/pilot'>Pilot setup</a></p><div class='grid'><section><h2>Unresolved route targets</h2><div id='candidates'></div></section><section><h2>Add / update rule</h2><div><input id='buyer' placeholder='Buyer name e.g. Halliburton'><br><select id='status'><option>UNKNOWN</option><option>APPROVED</option><option>NOT_APPROVED</option><option>IN_PROGRESS</option><option>INDIRECT_ONLY</option></select><select id='barrier'><option>NONE</option><option>APPROVED_VENDOR_LIST</option><option>FRAMEWORK</option><option>CERTIFICATION</option><option>INSURANCE</option><option>LOCAL_CONTENT</option><option>GEOGRAPHY</option><option>COMMERCIAL_SCALE</option><option>OTHER</option></select><br><textarea id='note' rows='4' cols='58' placeholder='Barrier, evidence and alternative route'></textarea><br><input id='evidence' placeholder='Evidence source / who confirmed it'><br><button onclick='save()'>Save rule</button></div></section></div><h2>Current access rules</h2><div id='rows'></div><script>const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');async function load(){const [rows,cands]=await Promise.all([(await fetch('/api/access-rules')).json(),(await fetch('/api/access-candidates?limit=50')).json()]);document.getElementById('rows').innerHTML=rows.map(r=>`<div class='row'><b>${esc(r.buyer_name_pattern)}</b> · ${esc(r.access_status)} · ${esc(r.barrier_type)}<p>${esc(r.note||'')}</p>${r.evidence_source?`<p class=muted>Evidence: ${esc(r.evidence_source)}</p>`:''}<button onclick='editRule(${JSON.stringify(r.buyer_name_pattern)},${JSON.stringify(r.access_status)},${JSON.stringify(r.barrier_type)},${JSON.stringify(r.note||'')},${JSON.stringify(r.evidence_source||'')})'>Edit</button><button onclick='del(${r.id})'>Delete</button></div>`).join('')||'<p class=muted>No buyer access rules yet.</p>';document.getElementById('candidates').innerHTML=cands.map(c=>`<div class='row candidate'><b>${esc(c.buyer_name)}</b><div><span class=pill>${esc((c.route_target_type||'BUYER').replaceAll('_',' '))}</span><span class=pill>${c.signal_count} signals</span><span class=pill>${c.direct_fit} direct</span><span class=pill>best ${c.best_effective_score}</span></div><button onclick='quick(${JSON.stringify(c.buyer_name)},"APPROVED","NONE")'>Approved</button><button onclick='quick(${JSON.stringify(c.buyer_name)},"IN_PROGRESS","NONE")'>In progress</button><button onclick='quick(${JSON.stringify(c.buyer_name)},"INDIRECT_ONLY","OTHER")'>Indirect only</button><button onclick='editRule(${JSON.stringify(c.buyer_name)},"NOT_APPROVED","OTHER","","")'>Not approved…</button></div>`).join('')||'<p class=muted>All current customer-facing buyers have a route decision.</p>'}function editRule(b,s,bar,n,e){buyer.value=b;status.value=s;barrier.value=bar;note.value=n||'';evidence.value=e||'';window.scrollTo({top:0,behavior:'smooth'})}async function quick(b,s,bar){const body={buyer_name_pattern:b,access_status:s,barrier_type:bar,note:'',evidence_source:'Project Scope access review'};const r=await fetch('/api/access-rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok){alert(await r.text());return}load()}async function save(){const body={buyer_name_pattern:buyer.value,access_status:status.value,barrier_type:barrier.value,note:note.value,evidence_source:evidence.value};const r=await fetch('/api/access-rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok){alert(await r.text());return}buyer.value='';note.value='';evidence.value='';load()}async function del(id){await fetch('/api/access-rules/'+id,{method:'DELETE'});load()}const q=new URLSearchParams(location.search).get('buyer');if(q)buyer.value=q;load()</script></body></html>"""


@app.get("/pilot",response_class=HTMLResponse)
def pilot_page():
    return """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Project Scope Pilot Setup</title><style>:root{color-scheme:dark}body{font-family:system-ui;background:#111318;color:#f4f4f5;max-width:1100px;margin:30px auto;padding:0 18px}.muted{color:#a1a1aa}a{color:#8ab4ff}.panel{background:#181b21;border:1px solid #30343d;border-radius:14px;padding:18px;margin:14px 0}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.field{margin:9px 0}label{display:block;font-weight:650;margin-bottom:4px}input,textarea{width:96%;background:#20242c;color:white;border:1px solid #454a55;border-radius:8px;padding:10px}button{background:#262a33;color:white;border:1px solid #454a55;border-radius:9px;padding:10px 13px;cursor:pointer}.cards{display:flex;gap:10px;flex-wrap:wrap}.card{background:#20242c;border-radius:10px;padding:12px;min-width:135px}.num{font-size:27px;font-weight:800}.ok{color:#56d364}.warn{color:#f2cc60}.bad{color:#ff7b72}.item{padding:7px 0;border-bottom:1px solid #30343d}@media(max-width:760px){.grid{grid-template-columns:1fr}}</style></head><body><h1>Pilot setup</h1><p class=muted>Make Scope understand one real business, then collect labelled feedback before tuning rankings.</p><p><a href='/'>← Opportunities</a> · <a href='/access'>Buyer access</a></p><div class=panel><h2>Readiness</h2><div id=readiness class=cards></div><div id=checklist></div></div><div class=panel><h2>Customer profile</h2><div class=grid><div><div class=field><label>Business name</label><input id=name></div><div class=field><label>Company summary</label><textarea id=summary rows=4></textarea></div><div class=field><label>Capabilities — comma or new line separated</label><textarea id=capabilities rows=7></textarea></div><div class=field><label>Excluded scopes</label><textarea id=excluded rows=5></textarea></div></div><div><div class=field><label>Target sectors</label><textarea id=sectors rows=4></textarea></div><div class=field><label>Geography</label><textarea id=geography rows=4></textarea></div><div class=field><label>Preferred buyers</label><textarea id=buyers rows=4></textarea></div><div class=field><label>Certifications / approvals</label><textarea id=certifications rows=4></textarea></div><div class=field><label>Preferred routes</label><textarea id=routes rows=3 placeholder='direct, Tier 1 subcontract, framework...'></textarea></div><div class=field><label>Min contract value (£)</label><input id=minv type=number></div><div class=field><label>Max contract value (£)</label><input id=maxv type=number></div><div class=field><label><input id=excConfirm type=checkbox style='width:auto'> Exclusions reviewed / confirmed</label></div></div></div><div class=field><label>Commercial notes</label><textarea id=notes rows=4></textarea></div><button onclick=saveProfile()>Save customer profile</button><p id=saveNote class=muted></p></div><div class=panel><h2>Feedback calibration</h2><p class=muted>Scope records feedback now, but does not automatically change weights. We wait for enough decisive reviews first.</p><div id=calCards class=cards></div><div id=calBreakdown></div></div><script>const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');const list=v=>Array.isArray(v)?v:[];const join=v=>list(v).join('\\n');const split=v=>String(v||'').split(/[\\n,]+/).map(x=>x.trim()).filter(Boolean);function card(n,l){return `<div class=card><div class=num>${esc(n)}</div><div class=muted>${esc(l)}</div></div>`}async function load(){const [p,c,s]=await Promise.all([(await fetch('/api/customer-profile')).json(),(await fetch('/api/feedback-calibration')).json(),(await fetch('/api/stats')).json()]);const m=p.metadata||{};name.value=p.name||'';summary.value=m.company_summary||'';capabilities.value=join(p.capabilities);excluded.value=join(p.excluded_scopes);sectors.value=join(p.sectors);geography.value=join(p.geography);buyers.value=join(p.preferred_buyers);certifications.value=join(m.certifications);routes.value=join(m.preferred_routes);minv.value=p.min_contract_value_gbp??'';maxv.value=p.max_contract_value_gbp??'';notes.value=m.notes||'';excConfirm.checked=!!m.exclusions_confirmed;const pc=p.completeness||{};const unresolved=(s.signals||{}).unresolved_buyers||0;readiness.innerHTML=card((pc.percent??0)+'%','Profile complete')+card(unresolved,'Unresolved buyers')+card(c.decisive_reviews||0,'Decisive reviews')+card(c.learning_ready?'READY':'COLLECTING','Learning status');checklist.innerHTML=(pc.items||[]).map(x=>`<div class=item><span class='${x.complete?'ok':'warn'}'>${x.complete?'✓':'○'}</span> <b>${esc(x.name)}</b> <span class=muted>— ${esc(x.help)}</span></div>`).join('');calCards.innerHTML=card(c.total_reviewed||0,'Reviewed')+card(c.relevant||0,'Relevant')+card(c.not_relevant||0,'Not relevant')+card(c.watch||0,'Watch')+card(c.relevance_rate==null?'—':c.relevance_rate+'%','Relevance rate')+card(c.reviews_needed||0,'Decisive reviews needed');function bucket(title,obj){const entries=Object.entries(obj||{});if(!entries.length)return'';return `<h3>${esc(title)}</h3>`+entries.map(([k,v])=>`<div class=item><b>${esc(k.replaceAll('_',' '))}</b> <span class=muted>Relevant ${v.RELEVANT||0} · Not relevant ${v.NOT_RELEVANT||0} · Watch ${v.WATCH||0}</span></div>`).join('')}calBreakdown.innerHTML=bucket('By fit type',c.by_fit)+bucket('By signal type',c.by_signal_type)+bucket('By source',c.by_source)+((Object.entries(c.rejection_reasons||{}).length)?`<h3>Why signals were rejected</h3>`+Object.entries(c.rejection_reasons||{}).sort((a,b)=>b[1]-a[1]).map(([k,v])=>`<div class=item><b>${esc(k.replaceAll('_',' '))}</b> <span class=muted>${v}</span></div>`).join(''):'')}async function saveProfile(){const body={name:name.value,company_summary:summary.value,capabilities:split(capabilities.value),excluded_scopes:split(excluded.value),sectors:split(sectors.value),geography:split(geography.value),preferred_buyers:split(buyers.value),certifications:split(certifications.value),preferred_routes:split(routes.value),min_contract_value_gbp:minv.value===''?null:Number(minv.value),max_contract_value_gbp:maxv.value===''?null:Number(maxv.value),notes:notes.value,exclusions_confirmed:excConfirm.checked};const r=await fetch('/api/customer-profile',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});const j=await r.json();if(!r.ok){alert(j.detail||'Profile save failed');return}saveNote.textContent='Saved. Access changes are immediate. Re-run NSTA, FTS and PCS once after capability/sector/value changes to fully rescore retained records.';load()}load()</script></body></html>"""
