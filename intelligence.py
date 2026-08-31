INTELLIGENCE_VERSION = "0.3.0"

DOWNSTREAM_PACKAGE_TERMS = {
    "foundation": 6, "foundations": 6, "monopile": 7,
    "transition piece": 7, "fabrication": 6, "fabricate": 5,
    "manufacture": 5, "manufacturing": 5, "export cable": 6,
    "array cable": 6, "subsea cable": 5, "high voltage cable": 6,
    "cable installation": 5, "substation": 6, "offshore substation": 7,
    "onshore substation": 5, "turbine supply": 6,
    "turbine installation": 6, "tower manufacture": 6, "jacket": 6,
    "topsides": 5, "offshore platform": 6, "pipeline": 5,
    "pipework": 4, "module fabrication": 6, "epc": 6,
    "engineering procurement construction": 6, "construction contract": 4,
    "marine installation": 5, "port upgrade": 4,
    "port infrastructure": 4, "vessel charter": 3, "commissioning": 4,
}

LOW_DOWNSTREAM_TERMS = {
    "noise impact assessment": -8, "ecological survey": -7,
    "ecology survey": -7, "bird survey": -7, "ornithology": -7,
    "planning consultancy": -6, "legal services": -7,
    "public relations": -7, "communications consultancy": -7,
    "economic impact assessment": -6, "landscape assessment": -6,
    "visual impact assessment": -6, "archaeological": -6,
    "archaeology": -6,
}

DIRECT_CAPABILITY_TERMS = {
    "quality assurance": 6, "quality control": 6, "qa/qc": 7,
    "inspection": 5, "inspector": 5, "vendor surveillance": 7,
    "expediting": 6, "document control": 7, "document controller": 7,
    "ndt": 6, "non-destructive testing": 6,
    "non destructive testing": 6, "welding inspection": 6,
    "coating inspection": 6, "quality engineer": 6,
    "quality inspector": 6,
}

LIKELY_DOWNSTREAM_SCOPES = {
    "foundation": ["QA/QC", "fabrication inspection", "NDT", "document control", "expediting"],
    "monopile": ["QA/QC", "fabrication inspection", "NDT", "coating inspection", "document control"],
    "transition piece": ["QA/QC", "fabrication inspection", "NDT", "coating inspection", "document control"],
    "fabrication": ["QA/QC", "fabrication inspection", "NDT", "document control", "expediting"],
    "manufacture": ["QA/QC", "vendor surveillance", "inspection", "document control", "expediting"],
    "export cable": ["QA/QC", "vendor surveillance", "inspection", "document control"],
    "array cable": ["QA/QC", "vendor surveillance", "inspection", "document control"],
    "subsea cable": ["QA/QC", "vendor surveillance", "inspection", "document control"],
    "substation": ["QA/QC", "inspection", "document control", "commissioning support"],
    "turbine": ["QA/QC", "vendor surveillance", "inspection", "document control"],
    "pipeline": ["QA/QC", "welding inspection", "NDT", "document control"],
    "epc": ["QA/QC", "vendor surveillance", "inspection", "document control", "expediting"],
    "commissioning": ["QA/QC", "inspection", "document control"],
}

def _hits(text, mapping, category):
    text = (text or '').lower(); score = 0; hits = []
    for term, weight in mapping.items():
        if term in text:
            score += weight
            hits.append({'term': term, 'weight': weight, 'category': category})
    return score, hits

def classify_award_intelligence(title, description=''):
    text = ' '.join([title or '', description or ''])
    direct_score, direct_hits = _hits(text, DIRECT_CAPABILITY_TERMS, 'direct_capability')
    downstream_score, downstream_hits = _hits(text, DOWNSTREAM_PACKAGE_TERMS, 'downstream_package')
    negative_score, negative_hits = _hits(text, LOW_DOWNSTREAM_TERMS, 'low_downstream')
    downstream_score = max(0, downstream_score + negative_score)

    likely_scopes = []
    lower = text.lower()
    for trigger, scopes in LIKELY_DOWNSTREAM_SCOPES.items():
        if trigger in lower:
            for scope in scopes:
                if scope not in likely_scopes: likely_scopes.append(scope)

    if direct_score >= 5:
        kind, customer_facing, confidence = 'DIRECT', True, min(95, 60 + direct_score * 4)
    elif downstream_score >= 5:
        kind, customer_facing, confidence = 'DOWNSTREAM', True, min(90, 50 + downstream_score * 4)
    else:
        kind, customer_facing, confidence = 'RESEARCH_ONLY', False, (65 if negative_hits else 50)

    return {
        'kind': kind, 'customer_facing': customer_facing, 'confidence': confidence,
        'direct_score': direct_score, 'downstream_score': downstream_score,
        'direct_hits': direct_hits, 'downstream_hits': downstream_hits,
        'negative_hits': negative_hits, 'likely_downstream_scopes': likely_scopes,
        'version': INTELLIGENCE_VERSION,
    }
