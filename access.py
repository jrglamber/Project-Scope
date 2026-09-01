ACCESS_VERSION = "0.4.0"

VALID_ACCESS_STATUSES = {
    "UNKNOWN",
    "APPROVED",
    "NOT_APPROVED",
    "IN_PROGRESS",
    "INDIRECT_ONLY",
}

VALID_BARRIER_TYPES = {
    "NONE",
    "APPROVED_VENDOR_LIST",
    "FRAMEWORK",
    "CERTIFICATION",
    "INSURANCE",
    "LOCAL_CONTENT",
    "GEOGRAPHY",
    "COMMERCIAL_SCALE",
    "OTHER",
}


def _norm(value):
    return " ".join(str(value or "").lower().split())


def match_access_rule(buyer_name, rules):
    """Use the longest matching buyer pattern so specific rules beat broad ones."""
    buyer = _norm(buyer_name)
    matches = []
    for rule in rules or []:
        pattern = _norm(rule.get("buyer_name_pattern"))
        if not pattern or not buyer:
            continue
        if pattern in buyer or buyer in pattern:
            matches.append((len(pattern), rule))
    if not matches:
        return None
    return sorted(matches, key=lambda x: x[0], reverse=True)[0][1]


def assess_access(buyer_name, rules):
    rule = match_access_rule(buyer_name, rules)
    if not rule:
        return {
            "status": "UNKNOWN",
            "readiness_score": 50,
            "label": "Access unknown",
            "barrier_type": None,
            "note": None,
            "rule_id": None,
            "version": ACCESS_VERSION,
        }

    status = rule.get("access_status") or "UNKNOWN"
    scores = {
        "APPROVED": 100,
        "IN_PROGRESS": 65,
        "INDIRECT_ONLY": 45,
        "NOT_APPROVED": 20,
        "UNKNOWN": 50,
    }
    labels = {
        "APPROVED": "Route ready",
        "IN_PROGRESS": "Onboarding in progress",
        "INDIRECT_ONLY": "Indirect route only",
        "NOT_APPROVED": "Access barrier",
        "UNKNOWN": "Access unknown",
    }
    return {
        "status": status,
        "readiness_score": scores.get(status, 50),
        "label": labels.get(status, "Access unknown"),
        "barrier_type": rule.get("barrier_type"),
        "note": rule.get("note"),
        "rule_id": rule.get("id"),
        "buyer_name_pattern": rule.get("buyer_name_pattern"),
        "version": ACCESS_VERSION,
    }
