import json
from db import connection

p={
 "slug":"northsea-quality-demo",
 "name":"NorthSea Quality Services Ltd (Demo)",
 "geography":["scotland","aberdeen","fife","dundee","north sea","uk"],
 "sectors":["offshore wind","energy","oil and gas","grid","marine"],
 "capabilities":["qa/qc","quality assurance","quality control","inspection","vendor surveillance",
                 "fabrication inspection","ndt","document control","expediting","ncr"],
 "preferred_buyers":[],
 "excluded_scopes":[],
 "min_contract_value_gbp":25000,
 "max_contract_value_gbp":2000000
}
with connection() as conn:
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO customer_profiles(
              slug,name,geography,sectors,capabilities,preferred_buyers,excluded_scopes,
              min_contract_value_gbp,max_contract_value_gbp
            ) VALUES(%s,%s,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s::jsonb,%s,%s)
            ON CONFLICT(slug) DO UPDATE SET
              name=EXCLUDED.name,geography=EXCLUDED.geography,sectors=EXCLUDED.sectors,
              capabilities=EXCLUDED.capabilities,preferred_buyers=EXCLUDED.preferred_buyers,
              excluded_scopes=EXCLUDED.excluded_scopes,min_contract_value_gbp=EXCLUDED.min_contract_value_gbp,
              max_contract_value_gbp=EXCLUDED.max_contract_value_gbp,updated_at_utc=NOW()
        """,(p["slug"],p["name"],json.dumps(p["geography"]),json.dumps(p["sectors"]),
             json.dumps(p["capabilities"]),json.dumps(p["preferred_buyers"]),json.dumps(p["excluded_scopes"]),
             p["min_contract_value_gbp"],p["max_contract_value_gbp"]))
print("Demo customer seeded.")
