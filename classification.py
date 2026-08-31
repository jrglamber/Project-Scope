ENERGY_TERMS = {
    "offshore wind": 5, "onshore wind": 4, "wind farm": 4, "windfarm": 4,
    "renewable": 2, "subsea": 3, "offshore": 2, "energy": 1,
    "electricity": 1, "transmission": 3, "grid": 2, "substation": 3,
    "cable": 2, "interconnector": 4, "hydrogen": 3, "oil and gas": 3,
    "oil & gas": 3, "decommission": 3, "port": 1, "marine": 1,
    "fabrication": 2, "turbine": 3,
}
QUALITY_TERMS = {
    "quality assurance": 5, "quality control": 5, "qa/qc": 5,
    "inspection": 4, "inspector": 4, "ndt": 4, "non-destructive": 4,
    "vendor surveillance": 5, "expediting": 4, "document control": 5,
    "document controller": 5, "ncr": 3, "non-conformance": 3,
    "welding": 2, "coating": 2, "fabrication": 2, "commissioning": 2,
}

def score_terms(text, terms):
    text = (text or "").lower()
    hits, score = [], 0
    for term, weight in terms.items():
        if term in text:
            hits.append({"term": term, "weight": weight})
            score += weight
    return score, hits

def classify_energy(title, description="", cpv_text=""):
    score, hits = score_terms(" ".join([title or "", description or "", cpv_text or ""]), ENERGY_TERMS)
    return min(score, 20), hits

def score_quality_fit(title, description=""):
    return score_terms(" ".join([title or "", description or ""]), QUALITY_TERMS)
