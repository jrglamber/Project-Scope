from classification import score_quality_fit, CLASSIFIER_VERSION

SCORING_VERSION = "0.5.0"


def _lower_list(value):
    return [str(x).lower() for x in (value or [])]


def _notice_is_award(proc):
    return "award" in (proc.get("notice_type") or "").lower()


def score_procurement_for_customer(proc, customer):
    score = 0
    reasons = {
        "versions": {
            "classifier": CLASSIFIER_VERSION,
            "scoring": SCORING_VERSION,
        }
    }

    title = proc.get("title") or ""
    description = proc.get("description") or ""
    full_text = f"{title} {description}".lower()

    q_score, q_hits = score_quality_fit(title, description)
    customer_caps = _lower_list(customer.get("capabilities"))
    direct_hits = [
        cap for cap in customer_caps
        if cap and cap in full_text
    ]
    capability = min(
        35,
        q_score * 3 + len(direct_hits) * 4,
    )
    score += capability
    reasons["capability_fit"] = {
        "score": capability,
        "keyword_hits": q_hits,
        "customer_capability_hits": direct_hits,
    }

    energy_raw = int(proc.get("energy_relevance_score") or 0)
    sector = min(25, round(energy_raw * 1.5))
    score += sector
    reasons["sector_fit"] = {
        "score": sector,
        "energy_relevance_score": energy_raw,
    }

    geography = _lower_list(customer.get("geography"))
    location = (proc.get("location_text") or "").lower()
    geo_hits = [g for g in geography if g and g in location]
    if not geo_hits:
        geo_hits = [
            g for g in geography
            if g and g not in {"uk", "united kingdom"} and g in full_text
        ]
    geo = 15 if geo_hits else 0
    score += geo
    reasons["geography_fit"] = {
        "score": geo,
        "location": proc.get("location_text"),
        "hits": geo_hits,
    }

    value_score = 3
    value = proc.get("value_amount")
    minv = customer.get("min_contract_value_gbp")
    maxv = customer.get("max_contract_value_gbp")

    if value is not None:
        try:
            value_f = float(value)
            min_f = float(minv) if minv is not None else None
            max_f = float(maxv) if maxv is not None else None

            if (
                (min_f is None or value_f >= min_f)
                and (max_f is None or value_f <= max_f)
            ):
                value_score = 10
            elif max_f is not None and value_f > max_f * 10:
                value_score = 4
            else:
                value_score = 6
        except Exception:
            value_score = 3

    score += value_score
    reasons["contract_value_fit"] = {
        "score": value_score,
        "value": str(value) if value is not None else None,
    }

    if _notice_is_award(proc):
        actionability = 5
    elif proc.get("deadline_at_utc"):
        actionability = 10
    else:
        actionability = 5
    score += actionability
    reasons["actionability"] = {
        "score": actionability,
        "deadline": (
            str(proc.get("deadline_at_utc"))
            if proc.get("deadline_at_utc")
            else None
        ),
    }

    evidence = 5 if proc.get("source") in {
    "public_contracts_scotland",
    "find_a_tender",
    "nsta_energy_pathfinder",
} else 2
    score += evidence
    reasons["evidence_quality"] = {
        "score": evidence,
        "source": proc.get("source"),
    }

    raw_score = min(100, score)

    sector_gate = bool(proc.get("sector_gate_passed"))
    if not sector_gate:
        final_score = min(raw_score, 34)
        reasons["sector_gate"] = {
            "applied": True,
            "passed": False,
            "reason": (
                "No authoritative energy/oil-and-gas/industrial sector evidence. "
                "Geography, value and deadline cannot override this gate."
            ),
            "raw_score_before_gate": raw_score,
        }
    else:
        final_score = raw_score
        reasons["sector_gate"] = {
            "applied": False,
            "passed": True,
        }

    if sector_gate and not _notice_is_award(proc) and capability == 0:
        final_score = min(raw_score, 34)
        reasons["capability_gate"] = {
            "applied": True,
            "reason": (
                "Live procurement has no direct evidence matching the "
                "customer's capabilities."
            ),
            "raw_score_before_gate": raw_score,
        }
    else:
        reasons["capability_gate"] = {"applied": False}

    reasons["total"] = {
        "raw_score": raw_score,
        "final_score": final_score,
    }

    return final_score, reasons
