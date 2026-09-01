import os
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from db import connection
from access import assess_access, VALID_ACCESS_STATUSES, VALID_BARRIER_TYPES

APP_VERSION = "0.4.0"
DEFAULT = os.environ.get("DEFAULT_CUSTOMER_SLUG", "northsea-quality-demo")
app = FastAPI(title="Project Scope", version=APP_VERSION)


class FeedbackRequest(BaseModel):
    label: Literal["RELEVANT", "NOT_RELEVANT", "WATCH"]
    note: Optional[str] = None


class AccessRuleRequest(BaseModel):
    buyer_name_pattern: str
    access_status: Literal["UNKNOWN","APPROVED","NOT_APPROVED","IN_PROGRESS","INDIRECT_ONLY"]
    barrier_type: Literal[
        "NONE","APPROVED_VENDOR_LIST","FRAMEWORK","CERTIFICATION","INSURANCE",
        "LOCAL_CONTENT","GEOGRAPHY","COMMERCIAL_SCALE","OTHER"
    ] = "NONE"
    note: Optional[str] = None
    evidence_source: Optional[str] = None


def ensure_v04_schema():
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS opportunity_feedback (
                    id BIGSERIAL PRIMARY KEY,
                    signal_id BIGINT NOT NULL REFERENCES opportunity_signals(id) ON DELETE CASCADE,
                    customer_profile_id BIGINT NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
                    label TEXT NOT NULL CHECK(label IN ('RELEVANT','NOT_RELEVANT','WATCH')),
                    note TEXT,created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(signal_id,customer_profile_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS research_intelligence (
                    id BIGSERIAL PRIMARY KEY,
                    procurement_id BIGINT NOT NULL REFERENCES procurements(id) ON DELETE CASCADE,
                    project_id BIGINT REFERENCES projects(id),buyer_company_id BIGINT REFERENCES companies(id),
                    title TEXT NOT NULL,intelligence_kind TEXT NOT NULL CHECK(
                        intelligence_kind IN ('DIRECT','DOWNSTREAM','RESEARCH_ONLY')),
                    customer_facing BOOLEAN NOT NULL DEFAULT FALSE,
                    confidence INTEGER NOT NULL DEFAULT 50 CHECK(confidence BETWEEN 0 AND 100),
                    likely_downstream_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
                    reason_json JSONB NOT NULL DEFAULT '{}'::jsonb,evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',first_seen_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    last_updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),UNIQUE(procurement_id)
                )
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS customer_buyer_access (
                    id BIGSERIAL PRIMARY KEY,
                    customer_profile_id BIGINT NOT NULL REFERENCES customer_profiles(id) ON DELETE CASCADE,
                    buyer_name_pattern TEXT NOT NULL,
                    access_status TEXT NOT NULL DEFAULT 'UNKNOWN' CHECK(
                        access_status IN ('UNKNOWN','APPROVED','NOT_APPROVED','IN_PROGRESS','INDIRECT_ONLY')),
                    barrier_type TEXT NOT NULL DEFAULT 'NONE' CHECK(
                        barrier_type IN ('NONE','APPROVED_VENDOR_LIST','FRAMEWORK','CERTIFICATION','INSURANCE',
                        'LOCAL_CONTENT','GEOGRAPHY','COMMERCIAL_SCALE','OTHER')),
                    note TEXT,evidence_source TEXT,created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(customer_profile_id,buyer_name_pattern)
                )
            """)


@app.on_event("startup")
def startup():
    ensure_v04_schema()


def customer_row(cur, slug):
    cur.execute("SELECT * FROM customer_profiles WHERE slug=%s", (slug,))
    row = cur.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Customer profile not found")
    return row


def access_rules(cur, customer_id):
    cur.execute("SELECT * FROM customer_buyer_access WHERE customer_profile_id=%s ORDER BY buyer_name_pattern", (customer_id,))
    return cur.fetchall()


@app.get("/health")
def health():
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT NOW() AS now")
            row=cur.fetchone()
    return {"ok":True,"app":"Project Scope","version":APP_VERSION,"database_time":row["now"]}


@app.get("/api/opportunities")
def opportunities(customer:str=Query(DEFAULT),min_score:int=Query(35,ge=0,le=100),limit:int=Query(100,ge=1,le=500),include_reviewed:bool=Query(True)):
    with connection() as conn:
        with conn.cursor() as cur:
            cust=customer_row(cur, customer)
            rules=access_rules(cur, cust["id"])
            cur.execute("""
                SELECT s.id,s.signal_type,s.title,s.relevance_score,s.confidence,s.timing_label,
                       s.recommended_action,s.reason_json,s.first_seen_at_utc,s.last_updated_at_utc,
                       p.id AS procurement_id,p.source,p.description,p.buyer_name,p.published_at_utc,
                       p.deadline_at_utc,p.value_amount,p.value_currency,p.location_text,p.notice_type,
                       p.energy_relevance_score,p.energy_relevance_reasons,p.cpv_codes,r.source_url,
                       f.label AS feedback_label,f.note AS feedback_note,f.updated_at_utc AS feedback_updated_at
                FROM opportunity_signals s
                JOIN customer_profiles c ON c.id=s.customer_profile_id
                LEFT JOIN procurements p ON p.id=s.procurement_id
                LEFT JOIN raw_events r ON r.id=p.raw_event_id
                LEFT JOIN opportunity_feedback f ON f.signal_id=s.id AND f.customer_profile_id=c.id
                WHERE c.slug=%s AND s.status='ACTIVE' AND s.relevance_score>=%s AND (%s OR f.id IS NULL)
                ORDER BY s.relevance_score DESC,s.last_updated_at_utc DESC LIMIT %s
            """,(customer,min_score,include_reviewed,limit))
            rows=cur.fetchall()
    for row in rows:
        row["access_assessment"] = assess_access(row.get("buyer_name"), rules)
    return rows


@app.get("/api/research-intelligence")
def research_intelligence(limit:int=Query(50,ge=1,le=500)):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT ri.id,ri.title,ri.intelligence_kind,ri.customer_facing,ri.confidence,
                       ri.likely_downstream_scopes,ri.reason_json,ri.first_seen_at_utc,ri.last_updated_at_utc,
                       p.source,p.buyer_name,p.notice_type,p.published_at_utc,p.value_amount,p.value_currency,
                       p.location_text,r.source_url
                FROM research_intelligence ri JOIN procurements p ON p.id=ri.procurement_id
                LEFT JOIN raw_events r ON r.id=p.raw_event_id WHERE ri.status='ACTIVE'
                ORDER BY ri.last_updated_at_utc DESC LIMIT %s
            """,(limit,))
            return cur.fetchall()


@app.post("/api/opportunities/{signal_id}/feedback")
def save_feedback(signal_id:int,request:FeedbackRequest):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id,customer_profile_id FROM opportunity_signals WHERE id=%s",(signal_id,))
            signal=cur.fetchone()
            if not signal: raise HTTPException(status_code=404,detail="Signal not found")
            cur.execute("""
                INSERT INTO opportunity_feedback(signal_id,customer_profile_id,label,note)
                VALUES(%s,%s,%s,%s) ON CONFLICT(signal_id,customer_profile_id) DO UPDATE SET
                label=EXCLUDED.label,note=EXCLUDED.note,updated_at_utc=NOW() RETURNING *
            """,(signal_id,signal["customer_profile_id"],request.label,request.note))
            saved=cur.fetchone()
    return {"ok":True,"feedback":saved}


@app.get("/api/access-rules")
def get_access_rules(customer:str=Query(DEFAULT)):
    with connection() as conn:
        with conn.cursor() as cur:
            cust=customer_row(cur,customer)
            return access_rules(cur,cust["id"])


@app.post("/api/access-rules")
def save_access_rule(request:AccessRuleRequest,customer:str=Query(DEFAULT)):
    pattern=request.buyer_name_pattern.strip()
    if not pattern: raise HTTPException(status_code=400,detail="buyer_name_pattern is required")
    with connection() as conn:
        with conn.cursor() as cur:
            cust=customer_row(cur,customer)
            cur.execute("""
                INSERT INTO customer_buyer_access(
                    customer_profile_id,buyer_name_pattern,access_status,barrier_type,note,evidence_source
                ) VALUES(%s,%s,%s,%s,%s,%s)
                ON CONFLICT(customer_profile_id,buyer_name_pattern) DO UPDATE SET
                    access_status=EXCLUDED.access_status,barrier_type=EXCLUDED.barrier_type,
                    note=EXCLUDED.note,evidence_source=EXCLUDED.evidence_source,updated_at_utc=NOW()
                RETURNING *
            """,(cust["id"],pattern,request.access_status,request.barrier_type,request.note,request.evidence_source))
            row=cur.fetchone()
    return {"ok":True,"rule":row}


@app.delete("/api/access-rules/{rule_id}")
def delete_access_rule(rule_id:int,customer:str=Query(DEFAULT)):
    with connection() as conn:
        with conn.cursor() as cur:
            cust=customer_row(cur,customer)
            cur.execute("DELETE FROM customer_buyer_access WHERE id=%s AND customer_profile_id=%s RETURNING id",(rule_id,cust["id"]))
            row=cur.fetchone()
            if not row: raise HTTPException(status_code=404,detail="Access rule not found")
    return {"ok":True}


@app.get("/api/stats")
def stats(customer:str=Query(DEFAULT)):
    with connection() as conn:
        with conn.cursor() as cur:
            cust=customer_row(cur,customer)
            cur.execute("""
                SELECT COUNT(*) FILTER(WHERE s.status='ACTIVE') AS active,
                       COUNT(*) FILTER(WHERE s.status='ACTIVE' AND s.relevance_score>=75) AS high_priority,
                       COUNT(*) FILTER(WHERE s.signal_type='LIVE' AND s.status='ACTIVE') AS live,
                       COUNT(*) FILTER(WHERE s.signal_type='EMERGING' AND s.status='ACTIVE') AS emerging,
                       COUNT(*) FILTER(WHERE s.signal_type='INTELLIGENCE' AND s.status='ACTIVE') AS intelligence,
                       COUNT(f.id) AS reviewed
                FROM opportunity_signals s LEFT JOIN opportunity_feedback f
                  ON f.signal_id=s.id AND f.customer_profile_id=s.customer_profile_id
                WHERE s.customer_profile_id=%s
            """,(cust["id"],))
            signals=cur.fetchone()
            cur.execute("SELECT COUNT(*) AS research_retained FROM research_intelligence WHERE status='ACTIVE'")
            research=cur.fetchone()
            cur.execute("SELECT COUNT(*) AS access_rules FROM customer_buyer_access WHERE customer_profile_id=%s",(cust["id"],))
            access=cur.fetchone()
            cur.execute("""
                SELECT source,COUNT(*) AS procurements FROM procurements GROUP BY source ORDER BY source
            """)
            sources=cur.fetchall()
            cur.execute("""
                SELECT collector,status,started_at_utc,finished_at_utc,fetched_count,processed_count,error_count
                FROM collector_runs ORDER BY id DESC LIMIT 8
            """)
            runs=cur.fetchall()
    return {"customer":customer,"app_version":APP_VERSION,"signals":signals,"research":research,"access":access,"sources":sources,"collector_runs":runs}


@app.get("/",response_class=HTMLResponse)
def home():
    return """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Project Scope v0.4</title><style>
:root{color-scheme:dark}body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#111318;color:#f4f4f5;max-width:1250px;margin:34px auto;padding:0 20px}h1{font-size:34px;margin-bottom:4px}.muted{color:#a1a1aa}.cards{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}.card{background:#1b1e25;border:1px solid #30343d;border-radius:13px;padding:16px;min-width:145px}.num{font-size:30px;font-weight:750}.signal{background:#181b21;border:1px solid #30343d;border-radius:14px;padding:19px;margin:14px 0}.topline{display:flex;justify-content:space-between;gap:20px}.score{font-size:30px;font-weight:800}.LIVE{color:#ff7b72}.EMERGING{color:#f2cc60}.INTELLIGENCE{color:#79c0ff}.meta,.breakdown{display:flex;gap:9px;flex-wrap:wrap;margin:9px 0}.pill{background:#252932;border-radius:999px;padding:5px 9px;font-size:12px;color:#d4d4d8}.access-bad{border:1px solid #8e3c3c}.access-good{border:1px solid #2f7d4a}.why{background:#121419;border-radius:10px;padding:12px;margin-top:12px}a{color:#8ab4ff}button{border:1px solid #454a55;background:#262a33;color:white;border-radius:9px;padding:9px 12px;margin:6px 5px 0 0;cursor:pointer}.nav{display:flex;gap:14px;margin:12px 0 0}.feedback{font-size:13px;margin-top:8px}</style></head><body>
<h1>Project Scope <span class='muted'>v0.4</span></h1><p class='muted'>Commercial opportunity intelligence — private research dashboard.</p><div class='nav'><a href='/research'>Research intelligence</a><a href='/access'>Buyer access / barriers</a></div><div id='cards' class='cards'></div><div id='signals'></div>
<script>
const esc=(s)=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');
function money(v,c){if(v===null||v===undefined||v==='')return'';const n=Number(v);return Number.isNaN(n)?esc(v):new Intl.NumberFormat('en-GB',{style:'currency',currency:c||'GBP',maximumFractionDigits:0}).format(n)}
function breakdown(r){const x=r.reason_json||{};return [['Capability',x.capability_fit?.score],['Sector',x.sector_fit?.score],['Geography',x.geography_fit?.score],['Value',x.contract_value_fit?.score],['Actionability',x.actionability?.score],['Evidence',x.evidence_quality?.score]].filter(x=>x[1]!==undefined).map(x=>`<span class='pill'>${x[0]} ${x[1]}</span>`).join('')}
function sourceName(s){return s==='find_a_tender'?'Find a Tender':s==='public_contracts_scotland'?'PCS':s||''}
function accessPill(a){if(!a)return'';const bad=a.status==='NOT_APPROVED',good=a.status==='APPROVED';return `<span class='pill ${bad?'access-bad':good?'access-good':''}'>Route: ${esc(a.status.replaceAll('_',' '))}${a.barrier_type&&a.barrier_type!=='NONE'?' · '+esc(a.barrier_type.replaceAll('_',' ')):''}</span>`}
async function feedback(id,label){const r=await fetch(`/api/opportunities/${id}/feedback`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({label})});if(!r.ok){alert('Feedback failed');return}load()}
async function load(){const st=await(await fetch('/api/stats')).json();const s=st.signals||{},rr=st.research||{},aa=st.access||{};const cards=[['Active',s.active],['High priority',s.high_priority],['Live',s.live],['Emerging',s.emerging],['Intelligence',s.intelligence],['Research retained',rr.research_retained],['Access rules',aa.access_rules]];document.getElementById('cards').innerHTML=cards.map(x=>`<div class='card'><div class='num'>${x[1]||0}</div><div class='muted'>${x[0]}</div></div>`).join('');const rows=await(await fetch('/api/opportunities?min_score=35&limit=100')).json();document.getElementById('signals').innerHTML=rows.map(r=>{const m=[sourceName(r.source),r.buyer_name,r.notice_type,r.deadline_at_utc?'Deadline '+new Date(r.deadline_at_utc).toLocaleDateString('en-GB'):null,r.value_amount?money(r.value_amount,r.value_currency):null,r.location_text].filter(Boolean);const a=r.access_assessment||{};return `<div class='signal'><div class='topline'><div><b class='${esc(r.signal_type)}'>${esc(r.signal_type)}</b><h3>${esc(r.title)}</h3></div><div class='score'>${esc(r.relevance_score)}</div></div><div class='meta'>${m.map(x=>`<span class='pill'>${esc(x)}</span>`).join('')}${accessPill(a)}</div><div class='breakdown'>${breakdown(r)}</div>${a.note?`<div class='why'><b>Route-to-market note</b><br>${esc(a.note)}</div>`:''}<p>${esc(r.recommended_action||'')}</p>${r.source_url?`<a href='${esc(r.source_url)}' target='_blank' rel='noopener'>Open official source ↗</a>`:''}<div><button onclick="feedback(${r.id},'RELEVANT')">✓ Relevant</button><button onclick="feedback(${r.id},'NOT_RELEVANT')">✕ Not relevant</button><button onclick="feedback(${r.id},'WATCH')">◉ Watch</button></div><div class='feedback muted'>${r.feedback_label?'Your label: '+esc(r.feedback_label.replaceAll('_',' ')):'Not reviewed yet'}</div></div>`}).join('')||"<p class='muted'>No active customer-facing signals above the current threshold.</p>"}load();
</script></body></html>"""


@app.get("/research",response_class=HTMLResponse)
def research_page():
    return """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Project Scope Research</title><style>:root{color-scheme:dark}body{font-family:system-ui;background:#111318;color:#f4f4f5;max-width:1100px;margin:34px auto;padding:0 20px}.muted{color:#a1a1aa}.item{background:#181b21;border:1px solid #30343d;border-radius:14px;padding:17px;margin:12px 0}.pill{background:#252932;border-radius:999px;padding:5px 9px;font-size:12px;color:#d4d4d8;margin-right:6px}a{color:#8ab4ff}</style></head><body><h1>Retained Industry Intelligence</h1><p><a href='/'>← Opportunities</a> · <a href='/access'>Buyer access</a></p><div id='items'></div><script>const esc=(s)=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');function sn(s){return s==='find_a_tender'?'Find a Tender':s==='public_contracts_scotland'?'PCS':s||''}async function load(){const rows=await(await fetch('/api/research-intelligence?limit=100')).json();document.getElementById('items').innerHTML=rows.map(r=>`<div class='item'><span class='pill'>${esc(sn(r.source))}</span><span class='pill'>${esc(r.intelligence_kind)}</span><span class='pill'>${esc(r.confidence)}% confidence</span><h3>${esc(r.title)}</h3><div>${esc(r.buyer_name||'')}</div>${(r.likely_downstream_scopes||[]).length?`<p>Likely downstream: ${esc((r.likely_downstream_scopes||[]).join(', '))}</p>`:''}${r.source_url?`<a href='${esc(r.source_url)}' target='_blank'>Open official source ↗</a>`:''}</div>`).join('')||'<p class=muted>No retained intelligence yet.</p>'}load()</script></body></html>"""


@app.get("/access",response_class=HTMLResponse)
def access_page():
    return """<!doctype html><html><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'><title>Project Scope Access</title><style>:root{color-scheme:dark}body{font-family:system-ui;background:#111318;color:#f4f4f5;max-width:1000px;margin:34px auto;padding:0 20px}input,select,textarea,button{background:#20242c;color:#fff;border:1px solid #454a55;border-radius:8px;padding:9px;margin:5px}.row{background:#181b21;border:1px solid #30343d;border-radius:12px;padding:14px;margin:10px 0}.muted{color:#a1a1aa}a{color:#8ab4ff}</style></head><body><h1>Buyer access / route-to-market</h1><p class='muted'>Record buyer-specific barriers such as approved-vendor lists, frameworks or certifications. Rules are customer-specific.</p><p><a href='/'>← Opportunities</a></p><div><input id='buyer' placeholder='Buyer name e.g. Halliburton'><select id='status'><option>UNKNOWN</option><option>APPROVED</option><option>NOT_APPROVED</option><option>IN_PROGRESS</option><option>INDIRECT_ONLY</option></select><select id='barrier'><option>NONE</option><option>APPROVED_VENDOR_LIST</option><option>FRAMEWORK</option><option>CERTIFICATION</option><option>INSURANCE</option><option>LOCAL_CONTENT</option><option>GEOGRAPHY</option><option>COMMERCIAL_SCALE</option><option>OTHER</option></select><br><textarea id='note' rows='3' cols='70' placeholder='What is the barrier / alternative route?'></textarea><br><button onclick='save()'>Save rule</button></div><div id='rows'></div><script>const esc=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;');async function load(){const rows=await(await fetch('/api/access-rules')).json();document.getElementById('rows').innerHTML=rows.map(r=>`<div class='row'><b>${esc(r.buyer_name_pattern)}</b> · ${esc(r.access_status)} · ${esc(r.barrier_type)}<p>${esc(r.note||'')}</p><button onclick='del(${r.id})'>Delete</button></div>`).join('')||'<p class=muted>No buyer access rules yet.</p>'}async function save(){const body={buyer_name_pattern:buyer.value,access_status:status.value,barrier_type:barrier.value,note:note.value};const r=await fetch('/api/access-rules',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});if(!r.ok){alert(await r.text());return}buyer.value='';note.value='';load()}async function del(id){await fetch('/api/access-rules/'+id,{method:'DELETE'});load()}load()</script></body></html>"""
