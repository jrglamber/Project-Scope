from pathlib import Path
from db import connection

sql = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
with connection() as conn:
    with conn.cursor() as cur:
        cur.execute(sql)
print("Opportunity Radar schema initialised.")
