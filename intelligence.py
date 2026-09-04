INTELLIGENCE_VERSION = "0.6.8"

DOWNSTREAM_PACKAGE_TERMS = {
    "foundation": 6,
    "foundations": 6,
    "monopile": 7,
    "transition piece": 7,
    "fabrication": 6,
    "fabricate": 5,
    "manufacture": 5,
    "manufacturing": 5,
    "export cable": 6,
    "array cable": 6,
    "subsea cable": 5,
    "high voltage cable": 6,
    "cable installation": 5,
    "substation": 6,
    "offshore substation": 7,
    "onshore substation": 5,
    "turbine supply": 6,
    "turbine installation": 6,
    "tower manufacture": 6,
    "jacket": 6,
    "topsides": 5,
    "offshore platform": 6,
    "pipeline": 5,
    "pipework": 4,
    "module fabrication": 6,
    "epc": 6,
    "engineering procurement construction": 6,
    "construction contract": 4,
    "marine installation": 5,
    "port upgrade": 4,
    "port infrastructure": 4,
    "vessel charter": 3,
    "commissioning": 4,
    "transportation and installation": 7,
    "supply and installation": 7,
    "installation contract": 6,
}

LOW_DOWNSTREAM_TERMS = {
    "noise impact assessment": -8,
    "ecological survey": -7,
    "ecology survey": -7,
    "bird survey": -7,
    "ornithology": -7,
    "planning consultancy": -6,
    "legal services": -7,
    "public relations": -7,
    "communications consultancy": -7,
    "economic impact assessment": -6,
    "landscape assessment": -6,
    "visual impact assessment": -6,
    "archaeological": -6,
    "archaeology": -6,
    "concept design": -12,
    "environmental impact assessment": -10,
    "planning permission": -10,
    "planning application": -8,
    "feasibility study": -10,
    "design study": -8,
    "front end engineering design": -10,
    "feed study": -8,
}

DIRECT_CAPABILITY_TERMS = {
    "quality assurance": 6,
    "quality control": 6,
    "qa/qc": 7,
    "inspection": 5,
    "inspector": 5,
    "vendor surveillance": 7,
    "expediting": 6,
    "document control": 7,
    "document controller": 7,
    "ndt": 6,
    "non-destructive testing": 6,
    "non destructive testing": 6,
    "welding inspection": 6,
    "coating inspection": 6,
    "quality engineer": 6,
    "quality inspector": 6,
}

LIKELY_DOWNSTREAM_SCOPES = {
    "foundation": [
        "QA/QC",
        "fabrication inspection",
        "NDT",
        "document control",
        "expediting",
    ],
    "monopile": [
        "QA/QC",
        "fabrication inspection",
        "NDT",
        "coating inspection",
        "document control",
    ],
    "transition piece": [
        "QA/QC",
        "fabrication inspection",
        "NDT",
        "coating inspection",
        "document control",
    ],
    "fabrication": [
        "QA/QC",
        "fabrication inspection",
        "NDT",
        "document control",
        "expediting",
    ],
    "manufacture": [
        "QA/QC",
        "vendor surveillance",
        "inspection",
        "document control",
        "expediting",
    ],
    "export cable": [
        "QA/QC",
        "vendor surveillance",
        "inspection",
        "document control",
    ],
    "array cable": [
        "QA/QC",
        "vendor surveillance",
        "inspection",
        "document control",
    ],
    "subsea cable": [
        "QA/QC",
        "vendor surveillance",
        "inspection",
        "document control",
    ],
    "substation": [
        "QA/QC",
        "inspection",
        "document control",
        "commissioning support",
    ],
    "turbine": [
        "QA/QC",
        "vendor surveillance",
        "inspection",
        "document control",
    ],
    "pipeline": [
        "QA/QC",
        "welding inspection",
        "NDT",
        "document control",
    ],
    "epc": [
        "QA/QC",
        "vendor surveillance",
        "inspection",
        "document control",
        "expediting",
    ],
    "commissioning": [
        "QA/QC",
        "inspection",
        "document control",
    ],
    "transportation and installation": [
        "QA/QC",
        "inspection",
        "document control",
        "vendor surveillance",
    ],
    "supply and installation": [
        "QA/QC",
        "inspection",
        "document control",
        "vendor surveillance",
    ],
}


def _normalise(value):
    return " ".join(
        str(value or "")
        .lower()
        .replace("/", " ")
        .replace("-", " ")
        .split()
    )


def _hits(text, mapping, category):
    text = (text or "").lower()
    score = 0
    hits = []

    for term, weight in mapping.items():
        if term in text:
            score += weight
            hits.append({
                "term": term,
                "weight": weight,
                "category": category,
            })

    return score, hits


def _scope_matches_customer(scope, capability):
    scope_n = _normalise(scope)
    cap_n = _normalise(capability)

    if not scope_n or not cap_n:
        return False

    alias_families = (
        {
            "qa qc",
            "quality assurance",
            "quality control",
            "quality assurance quality control",
        },
        {
            "ndt",
            "non destructive testing",
            "nondestructive testing",
        },
        {
            "document control",
            "document controller",
        },
        {
            "vendor surveillance",
            "vendor inspection",
        },
    )

    for family in alias_families:
        if scope_n in family and cap_n in family:
            return True

    if scope_n == cap_n:
        return True

    # Generic customer capability "inspection" legitimately covers more
    # specific inferred scopes such as fabrication/coating/welding inspection.
    if len(scope_n) >= 5 and scope_n in cap_n:
        return True
    if len(cap_n) >= 5 and cap_n in scope_n:
        return True

    return False


def match_downstream_scopes_to_customer(
    likely_scopes,
    customer_capabilities,
):
    matched_scopes = []
    matches = []

    for scope in likely_scopes or []:
        matching_caps = [
            str(cap)
            for cap in customer_capabilities or []
            if _scope_matches_customer(
                scope,
                cap,
            )
        ]

        if not matching_caps:
            continue

        if scope not in matched_scopes:
            matched_scopes.append(scope)

        matches.append({
            "scope": scope,
            "customer_capabilities": matching_caps,
        })

    return {
        "matched_scopes": matched_scopes,
        "matches": matches,
        "match_count": len(matched_scopes),
        "version": INTELLIGENCE_VERSION,
    }


def _dedupe_nested_downstream_hits(
    hits,
):
    """
    Do not let one phrase such as "onshore substation" score twice merely
    because it also contains the generic word "substation".
    """
    ordered = sorted(
        hits or [],
        key=lambda h: len(
            str(h.get("term") or "")
        ),
        reverse=True,
    )

    kept = []
    for hit in ordered:
        term = str(
            hit.get("term") or ""
        ).lower()
        if not term:
            continue

        if any(
            term in str(
                existing.get("term")
                or ""
            ).lower()
            for existing in kept
        ):
            continue

        kept.append(hit)

    return kept


def classify_award_intelligence(
    title,
    description="",
):
    text = " ".join([
        title or "",
        description or "",
    ])

    direct_score, direct_hits = _hits(
        text,
        DIRECT_CAPABILITY_TERMS,
        "direct_capability",
    )
    downstream_score, downstream_hits = _hits(
        text,
        DOWNSTREAM_PACKAGE_TERMS,
        "downstream_package",
    )
    downstream_hits = (
        _dedupe_nested_downstream_hits(
            downstream_hits
        )
    )
    downstream_score = sum(
        int(hit.get("weight") or 0)
        for hit in downstream_hits
    )

    negative_score, negative_hits = _hits(
        text,
        LOW_DOWNSTREAM_TERMS,
        "low_downstream",
    )

    downstream_score = max(
        0,
        downstream_score + negative_score,
    )

    likely_scopes = []
    lower = text.lower()

    for trigger, scopes in (
        LIKELY_DOWNSTREAM_SCOPES.items()
    ):
        if trigger not in lower:
            continue

        for scope in scopes:
            if scope not in likely_scopes:
                likely_scopes.append(scope)

    if direct_score >= 5:
        kind = "DIRECT"
        customer_facing = True
        confidence = min(
            95,
            60 + direct_score * 4,
        )
    elif downstream_score >= 8:
        kind = "DOWNSTREAM"
        customer_facing = True
        confidence = min(
            90,
            50 + downstream_score * 4,
        )
    else:
        kind = "RESEARCH_ONLY"
        customer_facing = False
        confidence = (
            65
            if negative_hits
            else 50
        )

    return {
        "kind": kind,
        "customer_facing": customer_facing,
        "confidence": confidence,
        "direct_score": direct_score,
        "downstream_score": downstream_score,
        "downstream_threshold": 8,
        "direct_hits": direct_hits,
        "downstream_hits": downstream_hits,
        "negative_hits": negative_hits,
        "likely_downstream_scopes": likely_scopes,
        "version": INTELLIGENCE_VERSION,
    }
