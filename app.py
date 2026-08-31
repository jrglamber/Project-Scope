import os
from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse
from db import connection

app=FastAPI(title="Project Scope",version="0.1.0")
DEFAULT=os.environ.get("DEFAULT_CUSTOMER_SLUG","northsea-quality-demo")

@app.get("/health")
def health():
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT NOW() AS now")
            row=cur.fetchone()
    return {"ok":True,"database_time":row["now"]}

@app.get("/api/opportunities")
def opportunities(customer:str=Query(DEFAULT),min_score:int=Query(35,ge=0,le=100),limit:int=Query(100,ge=1,le=500)):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT s.id,s.signal_type,s.title,s.relevance_score,s.confidence,s.timing_label,
                       s.recommended_action,s.reason_json,s.first_seen_at_utc,s.last_updated_at_utc,
                       p.buyer_name,p.deadline_at_utc,p.value_amount,p.value_currency,p.location_text,p.notice_type
                FROM opportunity_signals s
                JOIN customer_profiles c ON c.id=s.customer_profile_id
                LEFT JOIN procurements p ON p.id=s.procurement_id
                WHERE c.slug=%s AND s.status='ACTIVE' AND s.relevance_score>=%s
                ORDER BY s.relevance_score DESC,s.last_updated_at_utc DESC LIMIT %s
            """,(customer,min_score,limit))
            return cur.fetchall()

@app.get("/api/stats")
def stats(customer:str=Query(DEFAULT)):
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*) FILTER(WHERE s.status='ACTIVE') active,
                       COUNT(*) FILTER(WHERE s.status='ACTIVE' AND s.relevance_score>=75) high_priority,
                       COUNT(*) FILTER(WHERE s.signal_type='LIVE' AND s.status='ACTIVE') live,
                       COUNT(*) FILTER(WHERE s.signal_type='EMERGING' AND s.status='ACTIVE') emerging,
                       COUNT(*) FILTER(WHERE s.signal_type='INTELLIGENCE' AND s.status='ACTIVE') intelligence
                FROM opportunity_signals s JOIN customer_profiles c ON c.id=s.customer_profile_id
                WHERE c.slug=%s
            """,(customer,))
            return cur.fetchone()

@app.get("/",response_class=HTMLResponse)
def home():
    return """<!doctype html><html><head><meta charset="utf-8"><title>Opportunity Radar v0.1</title>
<style>body{font-family:system-ui;background:#111;color:#eee;max-width:1100px;margin:40px auto;padding:0 20px}
.cards{display:flex;gap:12px;flex-wrap:wrap}.card,.signal{background:#1b1b1b;border:1px solid #333;border-radius:12px;padding:16px;margin:10px 0}
.card{min-width:150px}.num{font-size:30px;font-weight:700}.score{float:right;font-size:24px;font-weight:700}
.LIVE{color:#ff7b72}.EMERGING{color:#f2cc60}.INTELLIGENCE{color:#79c0ff}.muted{color:#aaa}</style></head>
<body><h1>Project Scope <span class="muted">v0.1.1</span></h1><p class="muted">Private research dashboard.</p>
<div id="cards" class="cards"></div><div id="signals"></div><script>
async function go(){let s=await (await fetch('/api/stats')).json();
document.getElementById('cards').innerHTML=[['Active',s.active],['High priority',s.high_priority],['Live',s.live],['Emerging',s.emerging],['Intelligence',s.intelligence]]
.map(x=>`<div class="card"><div class="num">${x[1]||0}</div><div class="muted">${x[0]}</div></div>`).join('');
let r=await (await fetch('/api/opportunities?min_score=35&limit=50')).json();
document.getElementById('signals').innerHTML=r.map(x=>`<div class="signal"><div class="score">${x.relevance_score}</div>
<b class="${x.signal_type}">${x.signal_type}</b><h3>${x.title}</h3><div>${x.buyer_name||''}</div>
<p>${x.recommended_action||''}</p></div>`).join('')||'<p class="muted">No signals yet.</p>';}go();</script></body></html>"""
