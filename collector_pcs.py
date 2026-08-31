import os, json, hashlib
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
import requests
from db import connection
from classification import classify_energy
from scoring import score_procurement_for_customer

BASE = os.environ.get("PCS_API_BASE", "https://api.publiccontractsscotland.gov.uk/v1").rstrip("/")
MONTHS_BACK = max(0, int(os.environ.get("COLLECT_MONTHS_BACK", "1")))
ENERGY_MIN_SCORE = int(os.environ.get("ENERGY_MIN_SCORE", "2"))
NOTICE_TYPES = [1,2,3,4,5,6,101,102,103,104]

LABELS = {
    1:"Prior Information Notice",2:"Contract Notice",3:"Contract Award Notice",
    4:"Prior Information Notice (Utilities)",5:"Contract Notice (Utilities)",
    6:"Contract Award Notice (Utilities)",101:"Site Prior Information Notice",
    102:"Site Contract Notice",103:"Site Contract Award Notice",104:"Site Quick Quote Award"
}

def now(): return datetime.now(timezone.utc)

def stable_hash(x):
    return hashlib.sha256(json.dumps(x,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()).hexdigest()

def get(d, *path):
    cur=d
    for k in path:
        if not isinstance(cur,dict): return None
        cur=cur.get(k)
        if cur is None: return None
    return cur

def parse_dt(v):
    if not v: return None
    try:
        d=datetime.fromisoformat(str(v).replace("Z","+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None

def releases(payload):
    if isinstance(payload,dict) and isinstance(payload.get("releases"),list):
        return payload["releases"]
    if isinstance(payload,dict) and isinstance(payload.get("records"),list):
        out=[]
        for r in payload["records"]: out.extend(r.get("releases") or [])
        return out
    if isinstance(payload,list):
        out=[]
        for x in payload:
            out.extend(x.get("releases") or [x]) if isinstance(x,dict) else None
        return out
    return [payload] if isinstance(payload,dict) and (payload.get("ocid") or payload.get("id")) else []

def buyer_name(r):
    buyer_id=get(r,"buyer","id")
    for p in r.get("parties") or []:
        if "buyer" in (p.get("roles") or []) or (buyer_id and p.get("id")==buyer_id):
            return p.get("name")
    return get(r,"buyer","name")

def cpv_codes(r):
    out=[]
    for item in get(r,"tender","items") or []:
        for c in [item.get("classification")] + (item.get("additionalClassifications") or []):
            if c and c.get("id"):
                out.append({"scheme":c.get("scheme"),"id":c.get("id"),"description":c.get("description")})
    return out

def location(r):
    parts=[]
    for item in get(r,"tender","items") or []:
        a=item.get("deliveryAddress") or {}
        for k in ("streetAddress","locality","region","postalCode","countryName"):
            if a.get(k): parts.append(str(a[k]))
    return ", ".join(dict.fromkeys(parts)) or None

def upsert_company(cur,name,kind):
    if not name: return None
    cur.execute("""
        INSERT INTO companies(canonical_name,company_type)
        VALUES (%s,%s)
        ON CONFLICT(canonical_name) DO UPDATE SET updated_at_utc=NOW()
        RETURNING id
    """,(name.strip(),kind))
    return cur.fetchone()["id"]

def customers(cur):
    cur.execute("SELECT * FROM customer_profiles WHERE active=TRUE")
    return cur.fetchall()

def process(cur,r,notice_type,source_url):
    ocid=r.get("ocid")
    rid=r.get("id") or stable_hash(r)[:24]
    title=get(r,"tender","title") or r.get("title") or "(untitled notice)"
    desc=get(r,"tender","description") or ""
    published=parse_dt(r.get("date"))
    deadline=parse_dt(get(r,"tender","tenderPeriod","endDate"))
    buyer=buyer_name(r)
    cpv=cpv_codes(r)
    es,ehits=classify_energy(title,desc," ".join((x.get("description") or "") for x in cpv))
    h=stable_hash(r)

    cur.execute("""
        INSERT INTO raw_events(source,source_event_id,source_url,event_type,published_at_utc,content_hash,title,raw_json)
        VALUES('public_contracts_scotland',%s,%s,%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT(source,content_hash) DO UPDATE SET collected_at_utc=NOW()
        RETURNING id
    """,(ocid or rid,source_url,LABELS.get(notice_type,str(notice_type)),published,h,title,json.dumps(r)))
    raw_id=cur.fetchone()["id"]
    buyer_id=upsert_company(cur,buyer,"Buyer")

    cur.execute("""
        INSERT INTO procurements(
            source,ocid,release_id,notice_type,title,description,buyer_name,buyer_company_id,
            published_at_utc,deadline_at_utc,status,procurement_method,cpv_codes,location_text,
            value_amount,value_currency,raw_event_id,energy_relevance_score,energy_relevance_reasons
        ) VALUES(
            'public_contracts_scotland',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s::jsonb
        )
        ON CONFLICT(source,ocid,release_id) DO UPDATE SET
            title=EXCLUDED.title,description=EXCLUDED.description,buyer_name=EXCLUDED.buyer_name,
            buyer_company_id=EXCLUDED.buyer_company_id,published_at_utc=EXCLUDED.published_at_utc,
            deadline_at_utc=EXCLUDED.deadline_at_utc,status=EXCLUDED.status,
            procurement_method=EXCLUDED.procurement_method,cpv_codes=EXCLUDED.cpv_codes,
            location_text=EXCLUDED.location_text,value_amount=EXCLUDED.value_amount,
            value_currency=EXCLUDED.value_currency,raw_event_id=EXCLUDED.raw_event_id,
            energy_relevance_score=EXCLUDED.energy_relevance_score,
            energy_relevance_reasons=EXCLUDED.energy_relevance_reasons,updated_at_utc=NOW()
        RETURNING *
    """,(
        ocid,rid,LABELS.get(notice_type,str(notice_type)),title,desc,buyer,buyer_id,
        published,deadline,get(r,"tender","status"),get(r,"tender","procurementMethod"),
        json.dumps(cpv),location(r),get(r,"tender","value","amount"),get(r,"tender","value","currency"),
        raw_id,es,json.dumps(ehits)
    ))
    proc=cur.fetchone()

    for award in r.get("awards") or []:
        for sup in award.get("suppliers") or []:
            sname=sup.get("name")
            if not sname: continue
            sid=upsert_company(cur,sname,"Supplier")
            aid=award.get("id") or stable_hash(award)[:24]
            ad=parse_dt(award.get("date"))
            cur.execute("""
                INSERT INTO contract_awards(
                    procurement_id,source,ocid,award_id,buyer_name,supplier_name,supplier_company_id,
                    title,description,award_date,value_amount,value_currency,raw_event_id
                ) VALUES(%s,'public_contracts_scotland',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT(source,ocid,award_id,supplier_name) DO NOTHING
            """,(proc["id"],ocid,aid,buyer,sname,sid,award.get("title") or title,
                 award.get("description") or desc,ad.date() if ad else None,
                 get(award,"value","amount"),get(award,"value","currency"),raw_id))

    if es>=ENERGY_MIN_SCORE:
        for c in customers(cur):
            score,reasons=score_procurement_for_customer(proc,c)
            if score<35: continue
            st="INTELLIGENCE" if "award" in (proc.get("notice_type") or "").lower() else "LIVE"
            action=("Review this award for downstream subcontracting and supplier-entry opportunities."
                    if st=="INTELLIGENCE" else
                    "Review the notice, procurement route and named buyer/contact before deciding whether to engage.")
            cur.execute("""
                INSERT INTO opportunity_signals(
                    customer_profile_id,signal_type,procurement_id,buyer_company_id,title,
                    relevance_score,confidence,timing_label,reason_json,recommended_action,evidence_json
                ) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb)
                ON CONFLICT(customer_profile_id,signal_type,procurement_id) DO UPDATE SET
                    relevance_score=EXCLUDED.relevance_score,reason_json=EXCLUDED.reason_json,
                    recommended_action=EXCLUDED.recommended_action,last_updated_at_utc=NOW(),status='ACTIVE'
            """,(c["id"],st,proc["id"],buyer_id,title,score,70 if score>=70 else 55,
                 "Now" if st=="LIVE" else "Review downstream",json.dumps(reasons),action,
                 json.dumps([{"raw_event_id":raw_id,"source":"Public Contracts Scotland"}])))

def month_list():
    return [(now()-relativedelta(months=i)).strftime("%m-%Y") for i in range(MONTHS_BACK+1)]

def fetch(month,nt):
    r=requests.get(f"{BASE}/Notices",params={"dateFrom":month,"noticeType":nt,"outputType":0},timeout=60)
    r.raise_for_status()
    return r.json(),r.url

def main():
    with connection() as conn:
        with conn.cursor() as cur:
            cur.execute("INSERT INTO collector_runs(collector) VALUES('public_contracts_scotland') RETURNING id")
            run_id=cur.fetchone()["id"]
        conn.commit()

        fetched=processed=errors=0
        messages=[]
        for month in month_list():
            for nt in NOTICE_TYPES:
                try:
                    payload,url=fetch(month,nt)
                    rr=releases(payload)
                    fetched+=len(rr)
                    with conn.cursor() as cur:
                        for item in rr:
                            process(cur,item,nt,url)
                            processed+=1
                    conn.commit()
                except Exception as exc:
                    conn.rollback()
                    errors+=1
                    messages.append(f"{month}/type {nt}: {type(exc).__name__}: {exc}")

        with conn.cursor() as cur:
            cur.execute("""
                UPDATE collector_runs SET finished_at_utc=NOW(),status=%s,fetched_count=%s,
                processed_count=%s,error_count=%s,error_text=%s WHERE id=%s
            """,("ok" if errors==0 else "partial",fetched,processed,errors,
                 "\n".join(messages)[-12000:] if messages else None,run_id))
        conn.commit()

    print(json.dumps({"collector":"public_contracts_scotland","fetched":fetched,
                      "processed":processed,"errors":errors}))

if __name__=="__main__":
    main()
