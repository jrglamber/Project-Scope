CREATE TABLE IF NOT EXISTS collector_runs (
    id BIGSERIAL PRIMARY KEY,
    collector TEXT NOT NULL,
    started_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at_utc TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running',
    fetched_count INTEGER NOT NULL DEFAULT 0,
    processed_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    error_text TEXT
);

CREATE TABLE IF NOT EXISTS raw_events (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    source_event_id TEXT,
    source_url TEXT,
    event_type TEXT NOT NULL,
    published_at_utc TIMESTAMPTZ,
    collected_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    content_hash TEXT NOT NULL,
    title TEXT,
    raw_json JSONB NOT NULL,
    UNIQUE (source, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_raw_events_source_event
ON raw_events(source, source_event_id);

CREATE TABLE IF NOT EXISTS companies (
    id BIGSERIAL PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    company_type TEXT,
    website TEXT,
    country TEXT,
    aliases JSONB NOT NULL DEFAULT '[]'::jsonb,
    capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS projects (
    id BIGSERIAL PRIMARY KEY,
    canonical_name TEXT NOT NULL UNIQUE,
    sector TEXT,
    location_text TEXT,
    project_stage TEXT,
    expected_construction TEXT,
    expected_operation TEXT,
    estimated_value_gbp NUMERIC,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS project_participants (
    id BIGSERIAL PRIMARY KEY,
    project_id BIGINT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    scope TEXT,
    confidence NUMERIC,
    evidence_raw_event_id BIGINT REFERENCES raw_events(id),
    UNIQUE (project_id, company_id, role, scope)
);

CREATE TABLE IF NOT EXISTS procurements (
    id BIGSERIAL PRIMARY KEY,
    source TEXT NOT NULL,
    ocid TEXT,
    release_id TEXT,
    notice_type TEXT,
    title TEXT NOT NULL,
    description TEXT,
    buyer_name TEXT,
    buyer_company_id BIGINT REFERENCES companies(id),
    project_id BIGINT REFERENCES projects(id),
    published_at_utc TIMESTAMPTZ,
    deadline_at_utc TIMESTAMPTZ,
    status TEXT,
    procurement_method TEXT,
    cpv_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
    location_text TEXT,
    value_amount NUMERIC,
    value_currency TEXT,
    raw_event_id BIGINT NOT NULL REFERENCES raw_events(id),
    energy_relevance_score INTEGER NOT NULL DEFAULT 0,
    energy_relevance_reasons JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, ocid, release_id)
);

CREATE INDEX IF NOT EXISTS idx_procurements_deadline
ON procurements(deadline_at_utc);

CREATE INDEX IF NOT EXISTS idx_procurements_relevance
ON procurements(energy_relevance_score DESC);

CREATE TABLE IF NOT EXISTS contract_awards (
    id BIGSERIAL PRIMARY KEY,
    procurement_id BIGINT REFERENCES procurements(id) ON DELETE SET NULL,
    source TEXT NOT NULL,
    ocid TEXT,
    award_id TEXT,
    buyer_name TEXT,
    supplier_name TEXT,
    supplier_company_id BIGINT REFERENCES companies(id),
    title TEXT,
    description TEXT,
    award_date DATE,
    value_amount NUMERIC,
    value_currency TEXT,
    raw_event_id BIGINT NOT NULL REFERENCES raw_events(id),
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (source, ocid, award_id, supplier_name)
);

CREATE TABLE IF NOT EXISTS customer_profiles (
    id BIGSERIAL PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    geography JSONB NOT NULL DEFAULT '[]'::jsonb,
    sectors JSONB NOT NULL DEFAULT '[]'::jsonb,
    capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    preferred_buyers JSONB NOT NULL DEFAULT '[]'::jsonb,
    excluded_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    min_contract_value_gbp NUMERIC,
    max_contract_value_gbp NUMERIC,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS opportunity_signals (
    id BIGSERIAL PRIMARY KEY,
    customer_profile_id BIGINT NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
    signal_type TEXT NOT NULL CHECK (signal_type IN ('LIVE','EMERGING','INTELLIGENCE')),
    procurement_id BIGINT REFERENCES procurements(id) ON DELETE CASCADE,
    project_id BIGINT REFERENCES projects(id),
    buyer_company_id BIGINT REFERENCES companies(id),
    title TEXT NOT NULL,
    relevance_score INTEGER NOT NULL CHECK (relevance_score BETWEEN 0 AND 100),
    confidence INTEGER NOT NULL DEFAULT 50 CHECK (confidence BETWEEN 0 AND 100),
    timing_label TEXT,
    reason_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    recommended_action TEXT,
    evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    first_seen_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (customer_profile_id, signal_type, procurement_id)
);

CREATE INDEX IF NOT EXISTS idx_signals_rank
ON opportunity_signals(customer_profile_id, status, relevance_score DESC);

CREATE TABLE IF NOT EXISTS predictions (
    id BIGSERIAL PRIMARY KEY,
    customer_profile_id BIGINT REFERENCES customer_profiles(id),
    project_id BIGINT REFERENCES projects(id),
    company_id BIGINT REFERENCES companies(id),
    predicted_scope TEXT NOT NULL,
    prediction_text TEXT NOT NULL,
    predicted_from DATE,
    predicted_to DATE,
    confidence INTEGER NOT NULL CHECK (confidence BETWEEN 0 AND 100),
    evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    model_version TEXT NOT NULL,
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at_utc TIMESTAMPTZ,
    outcome TEXT CHECK (
        outcome IN ('CONFIRMED','PARTIAL','MISSED','EXPIRED')
        OR outcome IS NULL
    ),
    outcome_evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb
);

CREATE TABLE IF NOT EXISTS opportunity_feedback (
    id BIGSERIAL PRIMARY KEY,
    signal_id BIGINT NOT NULL REFERENCES opportunity_signals(id) ON DELETE CASCADE,
    customer_profile_id BIGINT NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
    label TEXT NOT NULL CHECK (label IN ('RELEVANT','NOT_RELEVANT','WATCH')),
    note TEXT,
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(signal_id, customer_profile_id)
);

CREATE INDEX IF NOT EXISTS idx_feedback_customer_label
ON opportunity_feedback(customer_profile_id, label);


CREATE TABLE IF NOT EXISTS research_intelligence (
    id BIGSERIAL PRIMARY KEY,
    procurement_id BIGINT NOT NULL REFERENCES procurements(id) ON DELETE CASCADE,
    project_id BIGINT REFERENCES projects(id),
    buyer_company_id BIGINT REFERENCES companies(id),
    title TEXT NOT NULL,
    intelligence_kind TEXT NOT NULL CHECK (intelligence_kind IN ('DIRECT','DOWNSTREAM','RESEARCH_ONLY')),
    customer_facing BOOLEAN NOT NULL DEFAULT FALSE,
    confidence INTEGER NOT NULL DEFAULT 50 CHECK (confidence BETWEEN 0 AND 100),
    likely_downstream_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    reason_json JSONB NOT NULL DEFAULT '{}'::jsonb,
    evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
    status TEXT NOT NULL DEFAULT 'ACTIVE',
    first_seen_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(procurement_id)
);
CREATE INDEX IF NOT EXISTS idx_research_intelligence_recent
ON research_intelligence(status, last_updated_at_utc DESC);
CREATE INDEX IF NOT EXISTS idx_research_intelligence_kind
ON research_intelligence(intelligence_kind, customer_facing);


CREATE TABLE IF NOT EXISTS customer_buyer_access (
    id BIGSERIAL PRIMARY KEY,
    customer_profile_id BIGINT NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
    buyer_name_pattern TEXT NOT NULL,
    access_status TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK (
        access_status IN ('UNKNOWN','APPROVED','NOT_APPROVED','IN_PROGRESS','INDIRECT_ONLY')
    ),
    barrier_type TEXT NOT NULL DEFAULT 'NONE' CHECK (
        barrier_type IN (
            'NONE','APPROVED_VENDOR_LIST','FRAMEWORK','CERTIFICATION','INSURANCE',
            'LOCAL_CONTENT','GEOGRAPHY','COMMERCIAL_SCALE','OTHER'
        )
    ),
    note TEXT,
    evidence_source TEXT,
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE(customer_profile_id, buyer_name_pattern)
);

CREATE INDEX IF NOT EXISTS idx_customer_buyer_access_customer
ON customer_buyer_access(customer_profile_id, access_status);
