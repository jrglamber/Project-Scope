from classification import score_quality_fit

def _lower_list(value):
    return [str(x).lower() for x in (value or [])]

def score_procurement_for_customer(proc, customer):
    score = 0
    reasons = {}
    title = proc.get("title") or ""
    description = proc.get("description") or ""
    full_text = f"{title} {description}".lower()

    q_score, q_hits = score_quality_fit(title, description)
    caps = _lower_list(customer.get("capabilities"))
    direct_hits = [cap for cap in caps if cap and cap in full_text]
    capability = min(35, q_score * 3 + len(direct_hits) * 5)
    score += capability
    reasons["capability_fit"] = {"score": capability, "keyword_hits": q_hits, "direct_hits": direct_hits}

    sector = min(20, int(proc.get("energy_relevance_score") or 0) * 2)
    score += sector
    reasons["sector_fit"] = {"score": sector}

    geography = _lower_list(customer.get("geography"))
    location = (proc.get("location_text") or "").lower()
    geo = 15 if any(g in location for g in geography if g) else 5 if "scotland" in geography else 0
    score += geo
    reasons["geography_fit"] = {"score": geo, "location": proc.get("location_text")}

    value_score = 5
    value = proc.get("value_amount")
    if value is not None:
        try:
            v = float(value)
            lo = customer.get("min_contract_value_gbp")
            hi = customer.get("max_contract_value_gbp")
            if (lo is None or v >= float(lo)) and (hi is None or v <= float(hi)):
                value_score = 10
            elif hi is not None and v > float(hi) * 10:
                value_score = 4
            else:
                value_score = 6
        except Exception:
            pass
    score += value_score
    reasons["contract_value_fit"] = {"score": value_score, "value": value}

    actionability = 20 if proc.get("deadline_at_utc") else 12
    if "award" in (proc.get("notice_type") or "").lower():
        actionability = 8
    score += actionability
    reasons["actionability"] = {"score": actionability}

    return min(100, score), reasons
