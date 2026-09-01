CLASSIFIER_VERSION = "0.5.0"

# Terms that are sufficiently specific to establish that the opportunity is
# genuinely inside the energy / industrial market we are targeting.
STRONG_SECTOR_TERMS = {
    # Offshore / onshore wind
    "offshore wind": 8,
    "floating offshore wind": 9,
    "onshore wind": 7,
    "wind farm": 7,
    "windfarm": 7,
    "wind turbine": 6,
    "offshore substation": 8,
    "export cable": 7,
    "array cable": 7,

    # Grid / power
    "electricity transmission": 7,
    "power transmission": 7,
    "transmission network": 7,
    "electricity distribution network": 7,
    "subsea power cable": 8,
    "interconnector": 8,
    "hvdc": 7,
    "high voltage substation": 7,
    "grid substation": 7,
    "battery energy storage": 7,
    "battery energy storage system": 7,
    "bess": 6,
    "solar farm": 6,

    # Hydrogen / CCS
    "green hydrogen": 7,
    "blue hydrogen": 7,
    "hydrogen production": 7,
    "hydrogen pipeline": 7,
    "carbon capture and storage": 8,
    "carbon capture storage": 8,
    "carbon capture": 6,
    "ccus": 8,

    # Oil & gas / offshore production
    "oil and gas": 7,
    "oil & gas": 7,
    "offshore platform": 7,
    "offshore installation": 7,
    "oil platform": 7,
    "gas platform": 7,
    "fpso": 8,
    "floating production storage": 8,
    "well intervention": 8,
    "well services": 7,
    "drilling rig": 8,
    "offshore drilling": 8,
    "completion services": 7,
    "well completion": 7,
    "wireline": 7,
    "slickline": 7,
    "coiled tubing": 7,
    "wellhead": 7,
    "subsea production": 8,
    "subsea tree": 8,
    "subsea manifold": 8,
    "subsea umbilical": 8,
    "subsea flowline": 8,
    "subsea riser": 8,
    "subsea pipeline": 8,
    "oil pipeline": 7,
    "gas pipeline": 7,
    "hydrocarbon": 6,
    "lng terminal": 7,
    "gas terminal": 6,
    "refinery": 6,
    "petrochemical": 6,

    # Decommissioning / marine energy
    "offshore decommissioning": 8,
    "oil and gas decommissioning": 8,
    "decommissioning programme": 6,
    "marine energy": 7,
    "tidal energy": 7,
    "wave energy": 7,
}

# These terms can strengthen a valid sector signal, but can NEVER establish
# sector relevance by themselves.
SUPPORT_SECTOR_TERMS = {
    "offshore": 1,
    "subsea": 2,
    "marine": 1,
    "port": 1,
    "cable": 1,
    "fabrication": 2,
    "commissioning": 1,
    "electricity": 1,
    "energy": 1,
    "generator": 1,
    "turbine": 2,
    "grid": 2,
    "power": 1,
    "pipeline": 1,
    "platform": 1,
    "vessel": 1,
}

# Obvious unrelated procurement that can accidentally contain words such as
# cable, offshore, inspection or commissioning.
HARD_NEGATIVE_TERMS = {
    "underwater tv": -12,
    "television cable": -12,
    "tv cable": -12,
    "cctv": -10,
    "access control system": -10,
    "door access control": -10,
    "security system": -8,
    "intruder alarm": -8,
    "audio visual": -8,
    "audiovisual": -8,
    "hdmi": -8,
    "broadcast": -7,
    "computer network": -7,
    "structured cabling": -7,
    "data cabling": -7,
    "it network": -7,
    "school": -4,
    "university campus": -4,
}

# CPV prefixes that are strong enough to establish sector relevance even where
# the notice text itself is terse. Keep this deliberately conservative.
STRONG_CPV_PREFIXES = {
    "7652": ("offshore services CPV", 7),
    "6531": ("electricity distribution CPV", 6),
}

CPV_SUPPORT_PREFIXES = {
    "3132": ("power distribution cable CPV", 2),
    "45231": ("pipeline/power-line construction CPV", 2),
    "653": ("electricity distribution CPV", 2),
}

# Direct customer capabilities. These can establish capability fit.
STRONG_CAPABILITY_TERMS = {
    "quality assurance": 5,
    "quality control": 5,
    "qa/qc": 6,
    "inspection": 4,
    "inspector": 4,
    "vendor surveillance": 6,
    "expediting": 5,
    "document control": 6,
    "document controller": 6,
    "ndt": 5,
    "non-destructive testing": 5,
    "non destructive testing": 5,
    "welding inspection": 5,
    "coating inspection": 5,
    "fabrication inspection": 6,
    "quality engineer": 6,
    "quality inspector": 6,
    "third party inspection": 6,
    "third-party inspection": 6,
}

# Useful context, but too generic to establish customer fit alone.
SUPPORT_CAPABILITY_TERMS = {
    "commissioning": 1,
    "welding": 1,
    "coating": 1,
    "fabrication": 1,
    "manufacturing": 1,
    "manufacture": 1,
    "quality": 1,
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


def _cpv_hits(text, mapping, category):
    compact = (text or "").replace(" ", "").lower()
    score = 0
    hits = []
    for prefix, (label, weight) in mapping.items():
        if prefix in compact:
            score += weight
            hits.append({
                "term": label,
                "weight": weight,
                "category": category,
            })
    return score, hits


def sector_gate_passed(reasons):
    """True only when the classifier found authoritative sector evidence."""
    for item in reasons or []:
        if item.get("category") == "decision":
            return item.get("decision") == "accepted"
    return False


def classify_energy(title, description="", cpv_text=""):
    """
    Return (0-20 sector score, evidence list).

    v0.5 invariant:
      Geography, contract value, deadline, generic words or capability language
      can never turn a non-energy procurement into a customer-facing signal.
    """
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
    cpv_strong_score, cpv_strong_hits = _cpv_hits(
        cpv_text, STRONG_CPV_PREFIXES, "strong_cpv"
    )
    cpv_support_score, cpv_support_hits = _cpv_hits(
        cpv_text, CPV_SUPPORT_PREFIXES, "cpv_support"
    )

    authoritative = strong_score + cpv_strong_score

    hits = (
        strong_hits
        + cpv_strong_hits
        + support_hits
        + cpv_support_hits
        + negative_hits
    )

    if negative_hits and authoritative < 8:
        hits.append({
            "term": "strict sector gate",
            "weight": 0,
            "category": "decision",
            "decision": "rejected",
            "reason": "hard negative without overriding sector evidence",
        })
        return 0, hits

    if authoritative <= 0:
        weak_score = min(2, support_score + cpv_support_score)
        hits.append({
            "term": "strict sector gate",
            "weight": 0,
            "category": "decision",
            "decision": "rejected",
            "reason": "no strong sector term or strong sector CPV",
            "weak_score": weak_score,
        })
        return weak_score, hits

    score = authoritative + min(6, support_score + cpv_support_score) + negative_score
    score = max(0, min(20, score))

    hits.append({
        "term": "strict sector gate",
        "weight": 0,
        "category": "decision",
        "decision": "accepted",
        "reason": "strong sector evidence present",
    })
    hits.append({
        "term": f"classifier {CLASSIFIER_VERSION}",
        "weight": 0,
        "category": "version",
    })
    return score, hits


def score_quality_fit(title, description=""):
    """
    Direct capability score.

    Generic words such as commissioning/fabrication are supporting evidence
    only and cannot create capability fit by themselves.
    """
    text = " ".join([title or "", description or ""])
    strong_score, strong_hits = _term_hits(
        text, STRONG_CAPABILITY_TERMS, "strong_capability"
    )
    support_score, support_hits = _term_hits(
        text, SUPPORT_CAPABILITY_TERMS, "support_capability"
    )

    hits = strong_hits + support_hits

    if strong_score <= 0:
        hits.append({
            "term": "capability gate",
            "weight": 0,
            "category": "capability_decision",
            "decision": "no_direct_fit",
        })
        return 0, hits

    score = strong_score + min(3, support_score)
    hits.append({
        "term": "capability gate",
        "weight": 0,
        "category": "capability_decision",
        "decision": "direct_fit",
    })
    return score, hits
