CLASSIFIER_VERSION = "0.2.0"

STRONG_SECTOR_TERMS = {
    "offshore wind": 8,
    "floating offshore wind": 9,
    "wind farm": 7,
    "windfarm": 7,
    "wind turbine": 6,
    "renewable energy": 5,
    "electricity transmission": 6,
    "power transmission": 6,
    "transmission network": 6,
    "substation": 6,
    "interconnector": 7,
    "subsea power cable": 7,
    "export cable": 6,
    "array cable": 6,
    "high voltage": 5,
    "hvdc": 6,
    "hydrogen": 6,
    "oil and gas": 6,
    "oil & gas": 6,
    "offshore platform": 6,
    "subsea pipeline": 6,
    "oil pipeline": 5,
    "gas pipeline": 5,
    "decommissioning": 5,
    "decommission": 5,
    "marine energy": 6,
}

SUPPORT_SECTOR_TERMS = {
    "offshore": 1,
    "subsea": 2,
    "marine": 1,
    "port": 1,
    "cable": 1,
    "fabrication": 2,
    "commissioning": 2,
    "electricity": 1,
    "energy": 1,
    "generator": 1,
    "turbine": 2,
    "grid": 2,
    "power": 1,
}

HARD_NEGATIVE_TERMS = {
    "underwater tv": -12,
    "television cable": -12,
    "tv cable": -12,
    "cctv": -8,
    "audio visual": -8,
    "audiovisual": -8,
    "hdmi": -8,
    "broadcast": -7,
    "computer network": -6,
    "structured cabling": -6,
    "data cabling": -6,
}

QUALITY_TERMS = {
    "quality assurance": 5,
    "quality control": 5,
    "qa/qc": 5,
    "inspection": 4,
    "inspector": 4,
    "ndt": 4,
    "non-destructive": 4,
    "non destructive": 4,
    "vendor surveillance": 5,
    "expediting": 4,
    "document control": 5,
    "document controller": 5,
    "ncr": 3,
    "non-conformance": 3,
    "non conformance": 3,
    "welding inspection": 4,
    "welding": 2,
    "coating inspection": 4,
    "coating": 2,
    "fabrication inspection": 5,
    "commissioning": 2,
    "quality engineer": 5,
    "quality inspector": 5,
}

CPV_SUPPORT_PREFIXES = {
    "3132": ("power distribution cable CPV", 2),
    "45231": ("power/pipeline construction CPV", 2),
    "653": ("electricity distribution CPV", 2),
    "7652": ("offshore services CPV", 2),
}


def _term_hits(text, terms, category):
    text = (text or "").lower()
    score = 0
    hits = []
    for term, weight in terms.items():
        if term in text:
            score += weight
            hits.append({
                "term": term,
                "weight": weight,
                "category": category,
            })
    return score, hits


def _cpv_hits(text):
    compact = (text or "").replace(" ", "")
    score = 0
    hits = []
    for prefix, (label, weight) in CPV_SUPPORT_PREFIXES.items():
        if prefix in compact:
            score += weight
            hits.append({
                "term": label,
                "weight": weight,
                "category": "cpv_support",
            })
    return score, hits


def classify_energy(title, description="", cpv_text=""):
    text = " ".join([title or "", description or "", cpv_text or ""])

    strong_score, strong_hits = _term_hits(
        text, STRONG_SECTOR_TERMS, "strong_sector"
    )
    support_score, support_hits = _term_hits(
        text, SUPPORT_SECTOR_TERMS, "support_sector"
    )
    negative_score, negative_hits = _term_hits(
        text, HARD_NEGATIVE_TERMS, "hard_negative"
    )
    cpv_score, cpv_hits = _cpv_hits(cpv_text)

    hits = strong_hits + support_hits + cpv_hits + negative_hits

    if negative_hits and strong_score < 8:
        hits.append({
            "term": "hard-negative gate",
            "weight": 0,
            "category": "decision",
            "decision": "rejected",
        })
        return 0, hits

    if strong_score <= 0:
        score = min(2, support_score + cpv_score)
        hits.append({
            "term": "no strong sector evidence",
            "weight": 0,
            "category": "decision",
            "decision": "weak_only",
        })
        return score, hits

    score = max(
        0,
        min(20, strong_score + min(6, support_score + cpv_score) + negative_score),
    )

    hits.append({
        "term": f"classifier {CLASSIFIER_VERSION}",
        "weight": 0,
        "category": "version",
    })
    return score, hits


def score_quality_fit(title, description=""):
    return _term_hits(
        " ".join([title or "", description or ""]),
        QUALITY_TERMS,
        "capability",
    )
