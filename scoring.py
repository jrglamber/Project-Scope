from classification import score_quality_fit, CLASSIFIER_VERSION

SCORING_VERSION = "0.7.2"

FIRST_PARTY_SOURCES = {
    "public_contracts_scotland",
    "find_a_tender",
    "nsta_energy_pathfinder",
}

INFERRED_DOWNSTREAM_SCORE_CAP = 64

# Customer target-sector families used by the v0.7.1 pilot-quality gate.
TARGET_SECTOR_FAMILIES = {
    "OFFSHORE_WIND": {
        "offshore wind","floating offshore wind","wind farm","windfarm",
        "wind turbine","offshore substation","array cable","export cable",
    },
    "ONSHORE_WIND": {"onshore wind","wind farm","windfarm","wind turbine"},
    "OIL_GAS": {
        "oil and gas","oil & gas","offshore platform","offshore installation",
        "oil platform","gas platform","fpso","floating production storage",
        "well intervention","well services","drilling rig","offshore drilling",
        "completion services","well completion","wireline","slickline",
        "coiled tubing","wellhead","subsea production","subsea tree",
        "subsea manifold","subsea umbilical","subsea flowline","subsea riser",
        "subsea pipeline","oil pipeline","gas pipeline","hydrocarbon",
        "lng terminal","gas terminal","refinery","petrochemical",
        "offshore decommissioning","oil and gas decommissioning",
    },
    "GRID_POWER": {
        "electricity transmission","power transmission","transmission network",
        "electricity distribution network","subsea power cable","interconnector",
        "hvdc","high voltage substation","grid substation",
        "battery energy storage","battery energy storage system","bess",
    },
    "HYDROGEN_CCS": {
        "green hydrogen","blue hydrogen","hydrogen production",
        "hydrogen pipeline","carbon capture and storage",
        "carbon capture storage","carbon capture","ccus",
    },
    "MARINE_ENERGY": {"marine energy","tidal energy","wave energy"},
    "SOLAR": {"solar farm","solar pv","photovoltaic"},
}

GENERIC_TARGET_SECTORS = {"energy","engineering","industrial","infrastructure"}

CUSTOMER_SECTOR_ALIASES = {
    "OFFSHORE_WIND": {"offshore wind","floating offshore wind"},
    "ONSHORE_WIND": {"onshore wind"},
    "WIND_ANY": {"wind","wind energy","renewable wind"},
    "OIL_GAS": {"oil and gas","oil gas","oil & gas","o&g","upstream","north sea oil and gas"},
    "GRID_POWER": {"grid","power grid","electricity","transmission","power transmission","electricity transmission"},
    "HYDROGEN_CCS": {"hydrogen","ccs","ccus","carbon capture","carbon capture and storage"},
    "MARINE_ENERGY": {"marine energy","tidal","wave energy"},
    "SOLAR": {"solar","solar energy","solar pv"},
}


def _customer_target_families(customer):
    sectors = [_normalise(x) for x in (customer.get("sectors") or []) if _normalise(x)]
    families = set()
    generic = []
    for sector in sectors:
        if sector in GENERIC_TARGET_SECTORS:
            generic.append(sector)
            continue
        for family, aliases in CUSTOMER_SECTOR_ALIASES.items():
            if sector in aliases:
                if family == "WIND_ANY":
                    families.update({"OFFSHORE_WIND","ONSHORE_WIND"})
                else:
                    families.add(family)
    return sectors, families, generic


def _procurement_sector_families(full_text, proc):
    text = _normalise(full_text)
    families = set()
    evidence = []
    for family, terms in TARGET_SECTOR_FAMILIES.items():
        for term in terms:
            term_n = _normalise(term)
            if term_n and term_n in text:
                families.add(family)
                evidence.append(term)
                break

    for item in proc.get("energy_relevance_reasons") or []:
        if not isinstance(item, dict):
            continue
        if item.get("category") not in {"strong_sector","strong_cpv"}:
            continue
        term = _normalise(item.get("term"))
        if not term:
            continue
        for family, terms in TARGET_SECTOR_FAMILIES.items():
            if any(_normalise(candidate) in term or term in _normalise(candidate) for candidate in terms):
                families.add(family)
                evidence.append(item.get("term"))
                break
    return families, list(dict.fromkeys(evidence))


def target_sector_alignment(full_text, proc, customer):
    sectors, targets, generic = _customer_target_families(customer)
    detected, evidence = _procurement_sector_families(full_text, proc)
    if not sectors:
        return {"passed":True,"configured":False,"customer_sectors":[],"target_families":[],
                "detected_families":sorted(detected),"matched_families":[],"evidence_terms":evidence,
                "reason":"No customer target sectors configured."}

    matched = targets.intersection(detected)
    if matched:
        return {"passed":True,"configured":True,"customer_sectors":sectors,
                "target_families":sorted(targets),"detected_families":sorted(detected),
                "matched_families":sorted(matched),"evidence_terms":evidence,
                "reason":"Procurement contains explicit evidence for a configured customer target-sector family."}

    only_generic = bool(generic) and not targets
    if only_generic and bool(proc.get("sector_gate_passed")):
        return {"passed":True,"configured":True,"customer_sectors":sectors,
                "target_families":[],"detected_families":sorted(detected),
                "matched_families":[],"evidence_terms":evidence,
                "reason":"Customer targets broad energy/industrial work and the strict sector classifier accepted the record."}

    if (proc.get("source") == "nsta_energy_pathfinder"
            and "energy" in generic
            and not targets
            and bool(proc.get("sector_gate_passed"))):
        return {"passed":True,"configured":True,"customer_sectors":sectors,
                "target_families":[],"detected_families":sorted(detected),
                "matched_families":[],"evidence_terms":evidence,
                "authoritative_source_override":True,
                "reason":"NSTA is authoritative energy-sector evidence and the customer has only a broad energy target; no specific target-sector family is being overridden."}

    return {"passed":False,"configured":True,"customer_sectors":sectors,
            "target_families":sorted(targets),"detected_families":sorted(detected),
            "matched_families":[],"evidence_terms":evidence,
            "reason":"No explicit procurement evidence matched the customer's configured target-sector families."}


def _lower_list(value):
    return [str(x).lower() for x in (value or [])]


def _normalise(value):
    return " ".join(
        str(value or "")
        .lower()
        .replace("/", " ")
        .replace("-", " ")
        .split()
    )


def _term_matches_capability(term, capability):
    """
    Conservative customer-specific capability match.

    Exact/contained phrases count. A few common QA/QC abbreviations are
    normalised so customer profiles do not need every spelling variant.
    """
    term_n = _normalise(term)
    cap_n = _normalise(capability)

    if not term_n or not cap_n:
        return False

    aliases = {
        "qa qc": {
            "qa qc",
            "quality assurance",
            "quality control",
            "quality assurance quality control",
        },
        "ndt": {
            "ndt",
            "non destructive testing",
            "nondestructive testing",
        },
        "document control": {
            "document control",
            "document controller",
        },
        "vendor surveillance": {
            "vendor surveillance",
            "vendor inspection",
        },
    }

    for family in aliases.values():
        if term_n in family and cap_n in family:
            return True

    if term_n == cap_n:
        return True

    # Avoid accidental substring matches on very short tokens.
    if len(term_n) >= 5 and term_n in cap_n:
        return True
    if len(cap_n) >= 5 and cap_n in term_n:
        return True

    return False


def _notice_is_award(proc):
    return "award" in (proc.get("notice_type") or "").lower()


def _customer_exclusion_hits(full_text, customer):
    hits = []
    for scope in _lower_list(customer.get("excluded_scopes")):
        scope_n = _normalise(scope)
        if scope_n and scope_n in _normalise(full_text):
            hits.append(scope)
    return hits


def score_procurement_for_customer(
    proc,
    customer,
    inferred_capabilities=None,
):
    """
    Score a procurement for one customer.

    v0.6.7 rules:
    - Sector evidence remains a hard gate.
    - Customer-facing signals require customer-specific capability evidence.
    - Direct capability evidence is strongest.
    - Award-derived downstream capability may be inferred only when the
      inferred scopes have already been matched to the customer's profile.
    - Inferred-only opportunities are capped below HIGH PRIORITY.
    """
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

    customer_caps = _lower_list(customer.get("capabilities"))

    q_score, q_hits = score_quality_fit(title, description)

    direct_text_hits = [
        cap
        for cap in customer_caps
        if cap and _normalise(cap) in _normalise(full_text)
    ]

    matched_quality_hits = []
    matched_quality_weight = 0

    for hit in q_hits:
        if hit.get("category") != "strong_capability":
            continue

        term = hit.get("term") or ""
        matching_caps = [
            cap
            for cap in customer_caps
            if _term_matches_capability(term, cap)
        ]

        if matching_caps:
            matched_quality_hits.append({
                "term": term,
                "weight": hit.get("weight", 0),
                "customer_capabilities": matching_caps,
            })
            matched_quality_weight += int(hit.get("weight") or 0)

    direct_capability = 0
    if direct_text_hits or matched_quality_hits:
        direct_capability = min(
            35,
            matched_quality_weight * 3
            + len(set(direct_text_hits)) * 4,
        )

    inferred_caps = []
    for capability in inferred_capabilities or []:
        capability = str(capability).strip()
        if capability and capability.lower() not in {
            x.lower() for x in inferred_caps
        }:
            inferred_caps.append(capability)

    inferred_capability = 0
    fit_tier = "NONE"

    if direct_capability > 0:
        fit_tier = "DIRECT"
        capability = direct_capability
    elif inferred_caps:
        fit_tier = "INFERRED_DOWNSTREAM"
        inferred_capability = min(
            16,
            6 + 2 * len(inferred_caps),
        )
        capability = inferred_capability
    else:
        capability = 0

    score += capability

    reasons["capability_fit"] = {
        "score": capability,
        "fit_type": fit_tier,
        "direct_score": direct_capability,
        "inferred_score": inferred_capability,
        "classifier_quality_score": q_score,
        "keyword_hits": q_hits,
        "customer_capability_hits": direct_text_hits,
        "matched_quality_hits": matched_quality_hits,
        "inferred_customer_capabilities": inferred_caps,
    }

    reasons["customer_fit"] = {
        "tier": fit_tier,
        "direct": fit_tier == "DIRECT",
        "inferred_downstream": fit_tier == "INFERRED_DOWNSTREAM",
    }

    energy_raw = int(proc.get("energy_relevance_score") or 0)
    sector = min(25, round(energy_raw * 1.5))
    score += sector
    reasons["sector_fit"] = {
        "score": sector,
        "energy_relevance_score": energy_raw,
    }

    target_sector = target_sector_alignment(full_text, proc, customer)
    reasons["target_sector_fit"] = target_sector

    geography = _lower_list(customer.get("geography"))
    location = (proc.get("location_text") or "").lower()
    geo_hits = [
        g
        for g in geography
        if g and g in location
    ]
    if not geo_hits:
        geo_hits = [
            g
            for g in geography
            if g
            and g not in {"uk", "united kingdom"}
            and g in full_text
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
            min_f = (
                float(minv)
                if minv is not None
                else None
            )
            max_f = (
                float(maxv)
                if maxv is not None
                else None
            )

            if (
                (min_f is None or value_f >= min_f)
                and (max_f is None or value_f <= max_f)
            ):
                value_score = 10
            elif (
                max_f is not None
                and value_f > max_f * 10
            ):
                value_score = 4
            else:
                value_score = 6
        except Exception:
            value_score = 3

    score += value_score
    reasons["contract_value_fit"] = {
        "score": value_score,
        "value": (
            str(value)
            if value is not None
            else None
        ),
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

    evidence = (
        5
        if proc.get("source") in FIRST_PARTY_SOURCES
        else 2
    )
    score += evidence
    reasons["evidence_quality"] = {
        "score": evidence,
        "source": proc.get("source"),
    }

    raw_score = min(100, score)
    final_score = raw_score

    sector_gate = bool(
        proc.get("sector_gate_passed")
    )

    if not sector_gate:
        final_score = min(final_score, 34)
        reasons["sector_gate"] = {
            "applied": True,
            "passed": False,
            "reason": (
                "No authoritative energy/oil-and-gas/industrial sector "
                "evidence. Geography, value and deadline cannot override "
                "this gate."
            ),
            "raw_score_before_gate": raw_score,
        }
    else:
        reasons["sector_gate"] = {
            "applied": False,
            "passed": True,
        }

    if sector_gate and not target_sector.get("passed"):
        before_target_gate = final_score
        final_score = min(final_score, 34)
        reasons["target_sector_gate"] = {
            "applied": True,
            "raw_score_before_gate": before_target_gate,
            "reason": target_sector.get("reason"),
            "customer_sectors": target_sector.get("customer_sectors"),
            "target_families": target_sector.get("target_families"),
            "detected_families": target_sector.get("detected_families"),
        }
    else:
        reasons["target_sector_gate"] = {
            "applied": False,
            "reason": target_sector.get("reason"),
        }

    exclusion_hits = _customer_exclusion_hits(
        full_text,
        customer,
    )
    if exclusion_hits:
        final_score = min(final_score, 34)
        reasons["customer_exclusion_gate"] = {
            "applied": True,
            "hits": exclusion_hits,
            "reason": (
                "The opportunity matches a scope explicitly excluded by "
                "the customer profile."
            ),
        }
    else:
        reasons["customer_exclusion_gate"] = {
            "applied": False,
            "hits": [],
        }

    if sector_gate and fit_tier == "NONE":
        final_score = min(final_score, 34)
        reasons["capability_gate"] = {
            "applied": True,
            "reason": (
                "No customer-specific direct capability evidence and no "
                "validated customer-matched downstream scope."
            ),
            "raw_score_before_gate": raw_score,
        }
    else:
        reasons["capability_gate"] = {
            "applied": False,
        }

    if (
        sector_gate
        and fit_tier == "INFERRED_DOWNSTREAM"
    ):
        before = final_score
        final_score = min(
            final_score,
            INFERRED_DOWNSTREAM_SCORE_CAP,
        )
        reasons["inferred_downstream_cap"] = {
            "applied": True,
            "cap": INFERRED_DOWNSTREAM_SCORE_CAP,
            "score_before_cap": before,
            "reason": (
                "Downstream fit is inferred from the awarded package, not a "
                "direct requirement. It can be surfaced as intelligence but "
                "cannot become HIGH PRIORITY on inference alone."
            ),
        }
    else:
        reasons["inferred_downstream_cap"] = {
            "applied": False,
        }

    reasons["total"] = {
        "raw_score": raw_score,
        "final_score": final_score,
    }

    return final_score, reasons
