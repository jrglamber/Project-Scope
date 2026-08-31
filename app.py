import os
from typing import Literal, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from db import connection

APP_VERSION = "0.2.0"
DEFAULT = os.environ.get(
    "DEFAULT_CUSTOMER_SLUG",
    "northsea-quality-demo",
)

app = FastAPI(title="Project Scope", version=APP_VERSION)


class FeedbackRequest(BaseModel):
    label: Literal["RELEVANT", "NOT_RELEVANT", "WATCH"]
    note: Optional[str] = None


def ensure_v02_schema():
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS opportunity_feedback (
                    id BIGSERIAL PRIMARY KEY,
                    signal_id BIGINT NOT NULL
                        REFERENCES opportunity_signals(id) ON DELETE CASCADE,
                    customer_profile_id BIGINT NOT NULL
                        REFERENCES customer_profiles(id) ON DELETE CASCADE,
                    label TEXT NOT NULL CHECK (
                        label IN ('RELEVANT','NOT_RELEVANT','WATCH')
                    ),
                    note TEXT,
                    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    UNIQUE(signal_id, customer_profile_id)
                )
            """)
            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_feedback_customer_label
                ON opportunity_feedback(customer_profile_id, label)
            """)


@app.on_event("startup")
def startup():
    ensure_v02_schema()


@app.get("/health")
def health():
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT NOW() AS now")
            row = cur.fetchone()
    return {
        "ok": True,
        "app": "Project Scope",
        "version": APP_VERSION,
        "database_time": row["now"],
    }


@app.get("/api/opportunities")
def opportunities(
    customer: str = Query(DEFAULT),
    min_score: int = Query(35, ge=0, le=100),
    limit: int = Query(100, ge=1, le=500),
    include_reviewed: bool = Query(True),
):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    s.id,
                    s.signal_type,
                    s.title,
                    s.relevance_score,
                    s.confidence,
                    s.timing_label,
                    s.recommended_action,
                    s.reason_json,
                    s.first_seen_at_utc,
                    s.last_updated_at_utc,
                    p.id AS procurement_id,
                    p.description,
                    p.buyer_name,
                    p.published_at_utc,
                    p.deadline_at_utc,
                    p.value_amount,
                    p.value_currency,
                    p.location_text,
                    p.notice_type,
                    p.energy_relevance_score,
                    p.energy_relevance_reasons,
                    p.cpv_codes,
                    r.source_url,
                    f.label AS feedback_label,
                    f.note AS feedback_note,
                    f.updated_at_utc AS feedback_updated_at
                FROM opportunity_signals s
                JOIN customer_profiles c
                    ON c.id = s.customer_profile_id
                LEFT JOIN procurements p
                    ON p.id = s.procurement_id
                LEFT JOIN raw_events r
                    ON r.id = p.raw_event_id
                LEFT JOIN opportunity_feedback f
                    ON f.signal_id = s.id
                   AND f.customer_profile_id = c.id
                WHERE
                    c.slug = %s
                    AND s.status = 'ACTIVE'
                    AND s.relevance_score >= %s
                    AND (%s OR f.id IS NULL)
                ORDER BY
                    s.relevance_score DESC,
                    s.last_updated_at_utc DESC
                LIMIT %s
            """, (
                customer,
                min_score,
                include_reviewed,
                limit,
            ))
            return cur.fetchall()


@app.get("/api/opportunities/{signal_id}")
def opportunity_detail(signal_id: int):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    s.*,
                    c.slug AS customer_slug,
                    c.name AS customer_name,
                    p.description,
                    p.buyer_name,
                    p.published_at_utc,
                    p.deadline_at_utc,
                    p.value_amount,
                    p.value_currency,
                    p.location_text,
                    p.notice_type,
                    p.energy_relevance_score,
                    p.energy_relevance_reasons,
                    p.cpv_codes,
                    r.source_url,
                    f.label AS feedback_label,
                    f.note AS feedback_note
                FROM opportunity_signals s
                JOIN customer_profiles c
                    ON c.id = s.customer_profile_id
                LEFT JOIN procurements p
                    ON p.id = s.procurement_id
                LEFT JOIN raw_events r
                    ON r.id = p.raw_event_id
                LEFT JOIN opportunity_feedback f
                    ON f.signal_id = s.id
                   AND f.customer_profile_id = c.id
                WHERE s.id = %s
            """, (signal_id,))
            row = cur.fetchone()

    if not row:
        raise HTTPException(status_code=404, detail="Signal not found")
    return row


@app.post("/api/opportunities/{signal_id}/feedback")
def save_feedback(signal_id: int, request: FeedbackRequest):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, customer_profile_id
                FROM opportunity_signals
                WHERE id = %s
            """, (signal_id,))
            signal = cur.fetchone()

            if not signal:
                raise HTTPException(
                    status_code=404,
                    detail="Signal not found",
                )

            cur.execute("""
                INSERT INTO opportunity_feedback(
                    signal_id,
                    customer_profile_id,
                    label,
                    note
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT(signal_id, customer_profile_id)
                DO UPDATE SET
                    label = EXCLUDED.label,
                    note = EXCLUDED.note,
                    updated_at_utc = NOW()
                RETURNING *
            """, (
                signal_id,
                signal["customer_profile_id"],
                request.label,
                request.note,
            ))
            saved = cur.fetchone()

    return {"ok": True, "feedback": saved}


@app.get("/api/stats")
def stats(customer: str = Query(DEFAULT)):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    COUNT(*) FILTER(
                        WHERE s.status = 'ACTIVE'
                    ) AS active,
                    COUNT(*) FILTER(
                        WHERE s.status = 'ACTIVE'
                        AND s.relevance_score >= 75
                    ) AS high_priority,
                    COUNT(*) FILTER(
                        WHERE s.signal_type = 'LIVE'
                        AND s.status = 'ACTIVE'
                    ) AS live,
                    COUNT(*) FILTER(
                        WHERE s.signal_type = 'EMERGING'
                        AND s.status = 'ACTIVE'
                    ) AS emerging,
                    COUNT(*) FILTER(
                        WHERE s.signal_type = 'INTELLIGENCE'
                        AND s.status = 'ACTIVE'
                    ) AS intelligence,
                    COUNT(f.id) AS reviewed,
                    COUNT(*) FILTER(
                        WHERE f.label = 'RELEVANT'
                    ) AS marked_relevant,
                    COUNT(*) FILTER(
                        WHERE f.label = 'NOT_RELEVANT'
                    ) AS marked_not_relevant
                FROM opportunity_signals s
                JOIN customer_profiles c
                    ON c.id = s.customer_profile_id
                LEFT JOIN opportunity_feedback f
                    ON f.signal_id = s.id
                   AND f.customer_profile_id = c.id
                WHERE c.slug = %s
            """, (customer,))
            result = cur.fetchone()

            cur.execute("""
                SELECT
                    collector,
                    status,
                    started_at_utc,
                    finished_at_utc,
                    fetched_count,
                    processed_count,
                    error_count
                FROM collector_runs
                ORDER BY id DESC
                LIMIT 5
            """)
            runs = cur.fetchall()

    return {
        "customer": customer,
        "app_version": APP_VERSION,
        "signals": result,
        "collector_runs": runs,
    }


@app.get("/", response_class=HTMLResponse)
def home():
    return """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project Scope v0.2</title>
<style>
:root{color-scheme:dark}
body{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#111318;color:#f4f4f5;max-width:1250px;margin:34px auto;padding:0 20px}
h1{font-size:34px;margin-bottom:4px}.muted{color:#a1a1aa}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin:22px 0}
.card{background:#1b1e25;border:1px solid #30343d;border-radius:13px;padding:16px;min-width:145px}
.num{font-size:30px;font-weight:750}
.signal{background:#181b21;border:1px solid #30343d;border-radius:14px;padding:19px;margin:14px 0}
.topline{display:flex;justify-content:space-between;gap:20px}.score{font-size:30px;font-weight:800}
.LIVE{color:#ff7b72}.EMERGING{color:#f2cc60}.INTELLIGENCE{color:#79c0ff}
.meta{display:flex;gap:9px;flex-wrap:wrap;margin:9px 0}.pill{background:#252932;border-radius:999px;padding:5px 9px;font-size:12px;color:#d4d4d8}
.why{background:#121419;border-radius:10px;padding:12px;margin-top:12px}.breakdown{display:flex;gap:8px;flex-wrap:wrap}
a{color:#8ab4ff}
button{border:1px solid #454a55;background:#262a33;color:white;border-radius:9px;padding:9px 12px;margin:6px 5px 0 0;cursor:pointer}
button:hover{background:#333844}button.good{border-color:#2f7d4a}button.bad{border-color:#8e3c3c}button.watch{border-color:#8c742e}
.feedback{font-size:13px;margin-top:8px}
</style>
</head>
<body>
<h1>Project Scope <span class="muted">v0.2</span></h1>
<p class="muted">Commercial opportunity intelligence — private research dashboard.</p>
<div id="cards" class="cards"></div>
<div id="signals"></div>
<script>
const esc=(s)=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;');

function money(v,c){
 if(v===null||v===undefined||v==='') return '';
 const n=Number(v);
 if(Number.isNaN(n)) return esc(v);
 return new Intl.NumberFormat('en-GB',{style:'currency',currency:c||'GBP',maximumFractionDigits:0}).format(n);
}

function breakdown(r){
 const x=r.reason_json||{};
 const entries=[
   ['Capability',x.capability_fit?.score],
   ['Sector',x.sector_fit?.score],
   ['Geography',x.geography_fit?.score],
   ['Value',x.contract_value_fit?.score],
   ['Actionability',x.actionability?.score],
   ['Evidence',x.evidence_quality?.score]
 ].filter(x=>x[1]!==undefined);
 return entries.map(x=>`<span class="pill">${x[0]} ${x[1]}</span>`).join('');
}

function reasonText(r){
 const rr=r.energy_relevance_reasons||[];
 const terms=rr
   .filter(x=>x.category==='strong_sector'||x.category==='support_sector'||x.category==='hard_negative')
   .map(x=>x.term);
 const gate=r.reason_json?.capability_gate;
 let out=[];
 if(terms.length) out.push('Sector evidence: '+[...new Set(terms)].join(', '));
 if(gate?.applied) out.push('Capability gate applied: '+gate.reason);
 return out.join('<br>') || 'Scored from sector, capability, geography, value and actionability evidence.';
}

async function feedback(id,label){
 const res=await fetch(`/api/opportunities/${id}/feedback`,{
   method:'POST',
   headers:{'Content-Type':'application/json'},
   body:JSON.stringify({label})
 });
 if(!res.ok){alert('Feedback failed');return;}
 await load();
}

async function load(){
 const stats=await (await fetch('/api/stats')).json();
 const s=stats.signals||{};
 const cards=[
   ['Active',s.active],['High priority',s.high_priority],['Live',s.live],
   ['Emerging',s.emerging],['Intelligence',s.intelligence],['Reviewed',s.reviewed]
 ];
 document.getElementById('cards').innerHTML=cards.map(x=>
   `<div class="card"><div class="num">${x[1]||0}</div><div class="muted">${x[0]}</div></div>`
 ).join('');

 const rows=await (await fetch('/api/opportunities?min_score=35&limit=100')).json();
 document.getElementById('signals').innerHTML=rows.map(r=>{
   const meta=[
     r.buyer_name,
     r.notice_type,
     r.deadline_at_utc ? 'Deadline '+new Date(r.deadline_at_utc).toLocaleDateString('en-GB') : null,
     r.value_amount ? money(r.value_amount,r.value_currency) : null,
     r.location_text
   ].filter(Boolean);

   return `<div class="signal">
     <div class="topline">
       <div><b class="${esc(r.signal_type)}">${esc(r.signal_type)}</b><h3>${esc(r.title)}</h3></div>
       <div class="score">${esc(r.relevance_score)}</div>
     </div>
     <div class="meta">${meta.map(x=>`<span class="pill">${esc(x)}</span>`).join('')}</div>
     <div class="breakdown">${breakdown(r)}</div>
     <div class="why"><b>Why Scope flagged it</b><br>${reasonText(r)}</div>
     <p>${esc(r.recommended_action||'')}</p>
     ${r.source_url ? `<a href="${esc(r.source_url)}" target="_blank" rel="noopener">Open official source ↗</a>` : ''}
     <div>
       <button class="good" onclick="feedback(${r.id},'RELEVANT')">✓ Relevant</button>
       <button class="bad" onclick="feedback(${r.id},'NOT_RELEVANT')">✕ Not relevant</button>
       <button class="watch" onclick="feedback(${r.id},'WATCH')">◉ Watch</button>
     </div>
     <div class="feedback muted">${r.feedback_label ? 'Your label: '+esc(r.feedback_label.replaceAll('_',' ')) : 'Not reviewed yet'}</div>
   </div>`;
 }).join('') || '<p class="muted">No active signals above the current threshold.</p>';
}
load();
</script>
</body>
</html>"""
