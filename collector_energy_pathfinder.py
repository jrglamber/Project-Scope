"""
Project Scope - NSTA Energy Pathfinder collector
Version: 0.6.3

Purpose:
- Collect energy-specific market intelligence from the official NSTA Energy
  Pathfinder.
- Keep the existing PCS and Find a Tender collectors unchanged.
- Treat Energy Pathfinder itself as authoritative sector evidence.
- Projects -> project/company memory.
- Upcoming tenders / forward work plan tenders / collaboration opportunities
  -> EMERGING candidate signals.
- Awarded contracts -> INTELLIGENCE / downstream research.

This collector only reads public first-party NSTA pages.
"""

import os
import json
import hashlib
import math
import re
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import certifi
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from db import connection
from classification import (
    classify_energy,
    sector_gate_passed,
    CLASSIFIER_VERSION,
)
from scoring import score_procurement_for_customer
from intelligence import classify_award_intelligence

COLLECTOR_VERSION = "0.6.3"
SOURCE = "nsta_energy_pathfinder"
BASE = "https://energypathfinder.nstauthority.co.uk"

USER_AGENT = os.environ.get(
    "NSTA_USER_AGENT",
    "Project-Scope/0.6 (+private commercial opportunity research)",
)

BROWSER_USER_AGENT = os.environ.get(
    "NSTA_BROWSER_USER_AGENT",
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0 Safari/537.36"
    ),
)

TIMEOUT = max(10, int(os.environ.get("NSTA_TIMEOUT_SECONDS", "60")))
MAX_ROWS_PER_PAGE = max(
    1,
    int(os.environ.get("NSTA_MAX_ROWS_PER_PAGE", "500")),
)
DETAIL_FETCH = os.environ.get("NSTA_FETCH_DETAILS", "0").strip() in {
    "1", "true", "TRUE", "yes", "YES"
}
DETAIL_SLEEP_SECONDS = max(
    0.0,
    float(os.environ.get("NSTA_DETAIL_SLEEP_SECONDS", "0.15")),
)
ENERGY_MIN_SCORE = max(
    0,
    int(os.environ.get("ENERGY_MIN_SCORE", "2")),
)

PAGES = [
    {
        "kind": "project",
        "path": "/",
        "label": "Projects",
    },
    {
        "kind": "emerging",
        "path": "/upcoming-tenders",
        "label": "Upcoming tenders",
    },
    {
        "kind": "emerging",
        "path": "/forward-work-plan-upcoming-tenders",
        "label": "Forward work plan upcoming tenders",
    },
    {
        "kind": "emerging",
        "path": "/collaboration-opportunities",
        "label": "Collaboration opportunities",
    },
    {
        "kind": "award",
        "path": "/awarded-contracts",
        "label": "Awarded contracts",
    },
]


def utcnow():
    return datetime.now(timezone.utc)


def build_session():
    session = requests.Session()
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        status=4,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(
        max_retries=retry,
        pool_connections=4,
        pool_maxsize=8,
    )
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
    })
    return session


def clean_text(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def json_safe(value):
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, dict):
        return {
            str(k): json_safe(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value


def dumps(value):
    return json.dumps(
        json_safe(value),
        default=str,
        allow_nan=False,
        ensure_ascii=False,
    )


def stable_hash(value):
    return hashlib.sha256(
        json.dumps(
            json_safe(value),
            sort_keys=True,
            separators=(",", ":"),
            default=str,
            allow_nan=False,
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def parse_date(value):
    text = clean_text(value)
    if not text:
        return None

    # Quarter-only values should stay descriptive rather than pretending to
    # have exact precision.
    if re.fullmatch(r"Q[1-4]\s+\d{4}", text, re.I):
        return None

    try:
        dt = date_parser.parse(
            text,
            dayfirst=True,
            fuzzy=False,
        )
        if not dt.tzinfo:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def fetch_html(session, url, user_agent=None):
    headers = {}
    if user_agent:
        headers["User-Agent"] = user_agent

    response = session.get(
        url,
        timeout=TIMEOUT,
        verify=certifi.where(),
        headers=headers,
    )
    response.raise_for_status()
    return response



def page_diagnostics(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    lower = html.lower()

    detail_patterns = (
        "/projects/",
        "/upcoming-tenders/",
        "/forward-work-plan-upcoming-tenders/",
        "/collaboration-opportunities/",
        "/awarded-contracts/",
    )

    detail_links = []
    export_links = []
    form_actions = []
    script_srcs = []

    for a in soup.find_all("a", href=True):
        href = urljoin(page_url, a.get("href"))
        label = clean_text(a.get_text(" ", strip=True))

        if any(token in href for token in detail_patterns):
            detail_links.append({
                "text": label[:120],
                "url": href,
            })

        probe = f"{label} {href}".lower()
        if any(
            token in probe
            for token in (
                "download",
                "spreadsheet",
                "export",
                ".csv",
                ".xlsx",
            )
        ):
            export_links.append({
                "text": label[:120],
                "url": href,
            })

    for form in soup.find_all("form"):
        action = form.get("action")
        if action:
            form_actions.append(urljoin(page_url, action))

    for script in soup.find_all("script", src=True):
        script_srcs.append(urljoin(page_url, script.get("src")))

    urlish = sorted(set(
        item
        for item in re.findall(r'["\']([^"\']{3,500})["\']', html)
        if any(
            token in item.lower()
            for token in (
                "api",
                "download",
                "export",
                "spreadsheet",
                ".csv",
                ".xlsx",
            )
        )
    ))

    return {
        "html_bytes": len(html.encode("utf-8", errors="ignore")),
        "title": (
            clean_text(soup.title.get_text(" ", strip=True))
            if soup.title else None
        ),
        "tables": len(soup.find_all("table")),
        "trs": len(soup.find_all("tr")),
        "lis": len(soup.find_all("li")),
        "articles": len(soup.find_all("article")),
        "links": len(soup.find_all("a")),
        "scripts": len(soup.find_all("script")),
        "forms": len(soup.find_all("form")),
        "has_results_text": "results list" in lower,
        "has_download_text": "download as spreadsheet" in lower,
        "has_failed_data_text": "failed to load pathfinder data" in lower,
        "detail_link_count": len(detail_links),
        "detail_links_sample": detail_links[:8],
        "export_links": export_links[:12],
        "form_actions": form_actions[:12],
        "script_srcs": script_srcs[:12],
        "api_export_urlish": urlish[:20],
    }




def extract_route_candidates(text_value, page_url):
    candidates = set()

    keywords = (
        "api",
        "pathfinder",
        "project",
        "tender",
        "contract",
        "collaboration",
        "forward-work",
        "spreadsheet",
        "download",
        "export",
        "azure",
        "graphql",
    )

    absolute_pattern = (
        r"https?://[A-Za-z0-9._~:/?#\[\]@!$&()*+,;=%-]+"
    )
    for match in re.findall(absolute_pattern, text_value):
        cleaned = match.rstrip("'\"),;]}")
        if any(k in cleaned.lower() for k in keywords):
            candidates.add(cleaned[:700])

    quoted_pattern = r"[\"']([^\"']{2,700})[\"']"
    for match in re.findall(quoted_pattern, text_value):
        low = match.lower()

        if not any(k in low for k in keywords):
            continue

        if (
            match.startswith("/")
            or match.startswith("api/")
            or match.startswith("http")
        ):
            candidates.add(urljoin(page_url, match)[:700])

    fetch_pattern = (
        r"(?:fetch|axios\.(?:get|post))"
        r"\s*\(\s*[\"']([^\"']+)[\"']"
    )
    for match in re.findall(
        fetch_pattern,
        text_value,
        flags=re.I,
    ):
        candidates.add(urljoin(page_url, match)[:700])

    return candidates


def extract_next_chunk_urls(html, page_url):
    """
    Next App Router pages commonly list route-specific JS chunks inside
    inline `self.__next_f.push(...)` hydration scripts rather than <script src>.
    """
    soup = BeautifulSoup(html, "html.parser")
    refs = set()

    patterns = (
        r"(?:/_next/)?static/chunks/[A-Za-z0-9_./-]+\.js",
        r"_next/static/chunks/[A-Za-z0-9_./-]+\.js",
    )

    for script in soup.find_all("script"):
        body = script.string or script.get_text() or ""
        for pattern in patterns:
            for ref in re.findall(pattern, body):
                if not ref.startswith("/"):
                    if ref.startswith("_next/"):
                        ref = "/" + ref
                    else:
                        ref = "/_next/" + ref
                refs.add(urljoin(page_url, ref))

    return sorted(refs)


def inspect_inline_next_scripts(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    candidates = set()
    samples = []
    next_f_scripts = 0
    inline_scripts = 0

    for script in soup.find_all("script"):
        if script.get("src"):
            continue

        body = script.string or script.get_text() or ""
        if not clean_text(body):
            continue

        inline_scripts += 1

        if "__next_f" in body or "self.__next_f" in body:
            next_f_scripts += 1

        candidates.update(
            extract_route_candidates(
                body,
                page_url,
            )
        )

        if len(samples) < 5:
            samples.append(
                clean_text(body)[:900]
            )

    return {
        "inline_script_count": inline_scripts,
        "next_f_script_count": next_f_scripts,
        "candidate_count": len(candidates),
        "candidates": sorted(candidates)[:80],
        "samples": samples,
        "chunk_urls": extract_next_chunk_urls(
            html,
            page_url,
        )[:80],
    }



def inspect_javascript_bundles(
    session,
    script_srcs,
    page_url,
    extra_chunk_urls=None,
):
    """
    Inspect first-party Next.js bundles, including route chunks discovered
    from inline App Router hydration data.
    """
    page_host = urlparse(page_url).netloc.lower()
    candidates = set()
    inspected = []
    failures = []
    discovered_chunks = set(extra_chunk_urls or [])

    queue = []
    seen = set()

    for src in list(script_srcs or []) + list(extra_chunk_urls or []):
        if src not in queue:
            queue.append(src)

    # One recursive discovery level is enough for diagnosis while keeping
    # the collector lightweight.
    max_bundles = 30

    while queue and len(seen) < max_bundles:
        src = queue.pop(0)

        if src in seen:
            continue
        seen.add(src)

        parsed = urlparse(src)
        if parsed.netloc and parsed.netloc.lower() != page_host:
            continue

        try:
            response = session.get(
                src,
                timeout=TIMEOUT,
                verify=certifi.where(),
                headers={"User-Agent": BROWSER_USER_AGENT},
            )
            response.raise_for_status()
            body = response.text

            inspected.append({
                "url": src,
                "status": response.status_code,
                "bytes": len(
                    body.encode(
                        "utf-8",
                        errors="ignore",
                    )
                ),
            })

            candidates.update(
                extract_route_candidates(
                    body,
                    page_url,
                )
            )

            # Route chunks can reference further webpack chunks.
            for ref in re.findall(
                r"(?:/_next/)?static/chunks/"
                r"[A-Za-z0-9_./-]+\.js",
                body,
            ):
                if not ref.startswith("/"):
                    if ref.startswith("_next/"):
                        ref = "/" + ref
                    else:
                        ref = "/_next/" + ref

                full = urljoin(
                    page_url,
                    ref,
                )
                discovered_chunks.add(full)

                if (
                    full not in seen
                    and full not in queue
                    and len(queue) + len(seen) < max_bundles
                ):
                    queue.append(full)

        except Exception as exc:
            failures.append({
                "url": src,
                "error": (
                    f"{type(exc).__name__}: {exc}"
                ),
            })

    return {
        "inspected_count": len(inspected),
        "inspected": inspected[:30],
        "candidate_count": len(candidates),
        "candidates": sorted(candidates)[:100],
        "discovered_chunk_count": len(discovered_chunks),
        "discovered_chunks": sorted(
            discovered_chunks
        )[:100],
        "failures": failures[:20],
    }


def fetch_page_with_fallback(session, url):
    primary = fetch_html(session, url)
    primary_rows = table_rows(primary.text, primary.url)

    if primary_rows:
        return primary, primary_rows, {
            "mode": "default_user_agent",
        }

    primary_diag = page_diagnostics(primary.text, primary.url)

    browser = fetch_html(
        session,
        url,
        user_agent=BROWSER_USER_AGENT,
    )
    browser_rows = table_rows(browser.text, browser.url)
    browser_diag = page_diagnostics(browser.text, browser.url)

    inline_next = None
    js_diagnostics = None

    if not browser_rows:
        inline_next = inspect_inline_next_scripts(
            browser.text,
            browser.url,
        )

        js_diagnostics = inspect_javascript_bundles(
            session,
            browser_diag.get("script_srcs") or [],
            browser.url,
            extra_chunk_urls=(
                inline_next.get("chunk_urls") or []
            ),
        )

    diagnostics = {
        "mode": (
            "browser_user_agent"
            if browser_rows
            else "zero_rows_after_browser_retry"
        ),
        "primary": {
            "status": primary.status_code,
            "final_url": primary.url,
            "content_type": primary.headers.get("content-type"),
            **primary_diag,
        },
        "browser": {
            "status": browser.status_code,
            "final_url": browser.url,
            "content_type": browser.headers.get("content-type"),
            **browser_diag,
        },
        "inline_next_probe": inline_next,
        "javascript_bundle_probe": js_diagnostics,
    }

    if browser_rows:
        return browser, browser_rows, diagnostics

    return primary, [], diagnostics


def table_rows(html, page_url):
    soup = BeautifulSoup(html, "html.parser")
    candidates = []

    for table in soup.find_all("table"):
        headers = [
            clean_text(x.get_text(" ", strip=True))
            for x in table.find_all("th")
        ]
        body_rows = table.find_all("tr")

        rows = []
        for tr in body_rows:
            cells = tr.find_all("td")
            if not cells:
                continue

            values = [
                clean_text(cell.get_text(" ", strip=True))
                for cell in cells
            ]

            if headers:
                # Some tables include a final action/contact column with no
                # header. Preserve it under a generic key.
                keys = list(headers)
                while len(keys) < len(values):
                    keys.append(f"_extra_{len(keys)+1}")
                row = dict(zip(keys, values))
            else:
                row = {
                    f"column_{idx+1}": value
                    for idx, value in enumerate(values)
                }

            links = []
            for a in tr.find_all("a", href=True):
                href = urljoin(page_url, a["href"])
                text_value = clean_text(a.get_text(" ", strip=True))
                links.append({
                    "text": text_value,
                    "url": href,
                })

            detail_url = None
            for item in links:
                u = item["url"]
                if any(
                    token in u
                    for token in (
                        "/upcoming-tenders/",
                        "/awarded-contracts/",
                        "/collaboration-opportunities/",
                        "/projects/",
                    )
                ):
                    detail_url = u
                    break

            row["_links"] = links
            row["_detail_url"] = detail_url
            rows.append(row)

        if rows:
            candidates.append((len(rows), rows))

    if not candidates:
        return []

    candidates.sort(
        key=lambda item: item[0],
        reverse=True,
    )
    return candidates[0][1]


def normalize_key_map(row):
    return {
        clean_text(k).lower(): v
        for k, v in row.items()
        if not str(k).startswith("_")
    }


def pick(row, *names):
    norm = normalize_key_map(row)
    for name in names:
        value = norm.get(name.lower())
        if value not in (None, ""):
            return clean_text(value)
    return None


def detail_text(session, row):
    url = row.get("_detail_url")
    if not DETAIL_FETCH or not url:
        return ""

    try:
        response = fetch_html(session, url)
        soup = BeautifulSoup(response.text, "html.parser")
        main = soup.find("main") or soup.body or soup
        text = clean_text(main.get_text(" ", strip=True))
        if DETAIL_SLEEP_SECONDS:
            time.sleep(DETAIL_SLEEP_SECONDS)
        return text[:50000]
    except Exception:
        return ""


def upsert_company(cur, name, kind):
    name = clean_text(name)
    if not name:
        return None

    cur.execute(
        """
        INSERT INTO companies(
            canonical_name,
            company_type
        )
        VALUES(%s,%s)
        ON CONFLICT(canonical_name)
        DO UPDATE SET
            updated_at_utc=NOW(),
            company_type=COALESCE(
                companies.company_type,
                EXCLUDED.company_type
            )
        RETURNING id
        """,
        (name, kind),
    )
    return cur.fetchone()["id"]


def active_customers(cur):
    cur.execute(
        "SELECT * FROM customer_profiles WHERE active=TRUE"
    )
    return cur.fetchall()


def authoritative_energy_score(
    title,
    description,
    function_text="",
):
    score, reasons = classify_energy(
        title,
        " ".join([
            description or "",
            function_text or "",
        ]),
        "",
    )

    passed = sector_gate_passed(reasons)

    # Energy Pathfinder is itself a curated NSTA energy/supply-chain source.
    # This is stronger evidence than an incidental keyword hit in a general
    # procurement portal.
    if not passed:
        score = max(score, 10)
        reasons.append({
            "term": "NSTA Energy Pathfinder",
            "weight": 10,
            "category": "authoritative_source",
        })
        reasons.append({
            "term": "strict sector gate",
            "weight": 0,
            "category": "decision",
            "decision": "accepted",
            "reason": (
                "Official NSTA Energy Pathfinder is an "
                "authoritative energy-sector source."
            ),
        })
        passed = True

    return min(20, score), reasons, passed


def insert_raw_event(
    cur,
    row,
    source_url,
    event_type,
    title,
    published=None,
):
    raw_payload = {
        "row": row,
        "source_url": source_url,
        "collector_version": COLLECTOR_VERSION,
    }
    content_hash = stable_hash(raw_payload)
    source_event_id = (
        source_url.rstrip("/").split("/")[-1]
        if source_url
        else content_hash[:24]
    )

    cur.execute(
        """
        INSERT INTO raw_events(
            source,
            source_event_id,
            source_url,
            event_type,
            published_at_utc,
            content_hash,
            title,
            raw_json
        )
        VALUES(
            %s,%s,%s,%s,%s,%s,%s,%s::jsonb
        )
        ON CONFLICT(source,content_hash)
        DO UPDATE SET
            collected_at_utc=NOW(),
            source_url=EXCLUDED.source_url
        RETURNING id
        """,
        (
            SOURCE,
            source_event_id,
            source_url,
            event_type,
            published,
            content_hash,
            title,
            dumps(raw_payload),
        ),
    )
    return cur.fetchone()["id"]


def upsert_project(cur, row, session):
    title = pick(
        row,
        "Project title",
    )
    operator = pick(
        row,
        "Operator/Developer",
    )
    project_type = pick(
        row,
        "Project type (sub category)",
        "Project type",
    )
    area = pick(
        row,
        "Area",
    )
    field_type = pick(
        row,
        "Field type",
    )
    contact = pick(
        row,
        "Contact details",
    )

    if not title:
        return False

    operator_id = upsert_company(
        cur,
        operator,
        "Operator/Developer",
    )

    source_url = (
        row.get("_detail_url")
        or urljoin(BASE, "/")
    )

    detail = detail_text(session, row)

    raw_event_id = insert_raw_event(
        cur,
        row,
        source_url,
        "Energy Pathfinder Project",
        title,
    )

    metadata = {
        "source": "NSTA Energy Pathfinder",
        "operator": operator,
        "project_type": project_type,
        "field_type": field_type,
        "area": area,
        "contact": contact,
        "detail_text": detail or None,
        "source_url": source_url,
    }

    sector = project_type or field_type or "Energy"

    cur.execute(
        """
        INSERT INTO projects(
            canonical_name,
            sector,
            location_text,
            project_stage,
            metadata
        )
        VALUES(%s,%s,%s,%s,%s::jsonb)
        ON CONFLICT(canonical_name)
        DO UPDATE SET
            sector=COALESCE(
                EXCLUDED.sector,
                projects.sector
            ),
            location_text=COALESCE(
                EXCLUDED.location_text,
                projects.location_text
            ),
            project_stage=COALESCE(
                EXCLUDED.project_stage,
                projects.project_stage
            ),
            metadata=EXCLUDED.metadata,
            updated_at_utc=NOW()
        RETURNING id
        """,
        (
            title,
            sector,
            area,
            project_type,
            dumps(metadata),
        ),
    )
    project_id = cur.fetchone()["id"]

    if operator_id:
        cur.execute(
            """
            INSERT INTO project_participants(
                project_id,
                company_id,
                role,
                scope,
                confidence,
                evidence_raw_event_id
            )
            VALUES(%s,%s,'Operator/Developer',%s,95,%s)
            ON CONFLICT(
                project_id,
                company_id,
                role,
                scope
            )
            DO UPDATE SET
                confidence=EXCLUDED.confidence,
                evidence_raw_event_id=EXCLUDED.evidence_raw_event_id
            """,
            (
                project_id,
                operator_id,
                project_type,
                raw_event_id,
            ),
        )

    return True


def procurement_identity(kind, row):
    source_url = row.get("_detail_url")
    if source_url:
        token = source_url.rstrip("/").split("/")[-1]
    else:
        token = stable_hash(row)[:24]

    return (
        f"nsta-{kind}-{token}",
        f"{kind}-{token}",
    )


def upsert_procurement(
    cur,
    row,
    session,
    kind,
    label,
):
    operator = pick(
        row,
        "Operator/Developer",
    )
    function_text = pick(
        row,
        "Function",
    )
    description = pick(
        row,
        "Description of work",
        "Description",
    ) or ""
    project_title = pick(
        row,
        "Project title",
    )
    contractor = pick(
        row,
        "Contractor",
    )
    area = pick(
        row,
        "Area",
    )
    contract_band = pick(
        row,
        "Contract band",
    )
    tender_date_text = pick(
        row,
        "Estimated tender date",
    )
    award_date_text = pick(
        row,
        "Date awarded",
    )

    if project_title:
        title = f"{project_title} — {function_text or label}"
    elif contractor and kind == "award":
        title = (
            f"{function_text or 'Contract award'} — "
            f"{operator or 'Unknown operator'} → {contractor}"
        )
    else:
        title = (
            f"{function_text or label} — "
            f"{operator or 'Unknown operator'}"
        )

    detail = detail_text(session, row)
    combined_description = clean_text(
        " ".join([
            description,
            detail,
            (
                f"Contract band: {contract_band}"
                if contract_band else ""
            ),
            (
                f"Estimated tender date: {tender_date_text}"
                if tender_date_text else ""
            ),
        ])
    )

    source_url = (
        row.get("_detail_url")
        or urljoin(
            BASE,
            {
                "emerging": "/upcoming-tenders",
                "award": "/awarded-contracts",
            }.get(kind, "/")
        )
    )

    published = (
        parse_date(award_date_text)
        if kind == "award"
        else None
    )
    deadline = (
        parse_date(tender_date_text)
        if kind == "emerging"
        else None
    )

    buyer_id = upsert_company(
        cur,
        operator,
        "Operator/Developer",
    )

    energy_score, energy_reasons, sector_pass = (
        authoritative_energy_score(
            title,
            combined_description,
            function_text,
        )
    )

    raw_event_id = insert_raw_event(
        cur,
        row,
        source_url,
        (
            "Energy Pathfinder Award"
            if kind == "award"
            else "Energy Pathfinder Emerging"
        ),
        title,
        published,
    )

    ocid, release_id = procurement_identity(
        kind,
        row,
    )

    cur.execute(
        """
        INSERT INTO procurements(
            source,
            ocid,
            release_id,
            notice_type,
            title,
            description,
            buyer_name,
            buyer_company_id,
            published_at_utc,
            deadline_at_utc,
            status,
            procurement_method,
            cpv_codes,
            location_text,
            value_amount,
            value_currency,
            raw_event_id,
            energy_relevance_score,
            energy_relevance_reasons,
            sector_gate_passed,
            classifier_version
        )
        VALUES(
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,'[]'::jsonb,%s,NULL,'GBP',%s,%s,
            %s::jsonb,%s,%s
        )
        ON CONFLICT(
            source,
            ocid,
            release_id
        )
        DO UPDATE SET
            notice_type=EXCLUDED.notice_type,
            title=EXCLUDED.title,
            description=EXCLUDED.description,
            buyer_name=EXCLUDED.buyer_name,
            buyer_company_id=EXCLUDED.buyer_company_id,
            published_at_utc=EXCLUDED.published_at_utc,
            deadline_at_utc=EXCLUDED.deadline_at_utc,
            status=EXCLUDED.status,
            procurement_method=EXCLUDED.procurement_method,
            location_text=EXCLUDED.location_text,
            raw_event_id=EXCLUDED.raw_event_id,
            energy_relevance_score=EXCLUDED.energy_relevance_score,
            energy_relevance_reasons=EXCLUDED.energy_relevance_reasons,
            sector_gate_passed=EXCLUDED.sector_gate_passed,
            classifier_version=EXCLUDED.classifier_version,
            updated_at_utc=NOW()
        RETURNING *
        """,
        (
            SOURCE,
            ocid,
            release_id,
            (
                "NSTA Energy Pathfinder Award"
                if kind == "award"
                else "NSTA Energy Pathfinder Emerging"
            ),
            title,
            combined_description,
            operator,
            buyer_id,
            published,
            deadline,
            (
                "awarded"
                if kind == "award"
                else "planned"
            ),
            "Energy Pathfinder",
            area,
            raw_event_id,
            energy_score,
            dumps(energy_reasons),
            sector_pass,
            CLASSIFIER_VERSION,
        ),
    )
    procurement = cur.fetchone()

    if kind == "award" and contractor:
        supplier_id = upsert_company(
            cur,
            contractor,
            "Supplier",
        )
        award_id = (
            source_url.rstrip("/").split("/")[-1]
            if source_url
            else stable_hash(row)[:24]
        )
        award_date = parse_date(award_date_text)

        cur.execute(
            """
            INSERT INTO contract_awards(
                procurement_id,
                source,
                ocid,
                award_id,
                buyer_name,
                supplier_name,
                supplier_company_id,
                title,
                description,
                award_date,
                value_amount,
                value_currency,
                raw_event_id
            )
            VALUES(
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                NULL,'GBP',%s
            )
            ON CONFLICT(
                source,
                ocid,
                award_id,
                supplier_name
            )
            DO UPDATE SET
                description=EXCLUDED.description,
                supplier_company_id=EXCLUDED.supplier_company_id
            """,
            (
                procurement["id"],
                SOURCE,
                ocid,
                award_id,
                operator,
                contractor,
                supplier_id,
                title,
                combined_description,
                award_date.date() if award_date else None,
                raw_event_id,
            ),
        )

    return (
        procurement,
        buyer_id,
        raw_event_id,
        source_url,
        contractor,
    )


def ensure_research_table(cur):
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS research_intelligence(
            id BIGSERIAL PRIMARY KEY,
            procurement_id BIGINT NOT NULL
                REFERENCES procurements(id) ON DELETE CASCADE,
            project_id BIGINT REFERENCES projects(id),
            buyer_company_id BIGINT REFERENCES companies(id),
            title TEXT NOT NULL,
            intelligence_kind TEXT NOT NULL CHECK(
                intelligence_kind IN(
                    'DIRECT',
                    'DOWNSTREAM',
                    'RESEARCH_ONLY'
                )
            ),
            customer_facing BOOLEAN NOT NULL DEFAULT FALSE,
            confidence INTEGER NOT NULL DEFAULT 50 CHECK(
                confidence BETWEEN 0 AND 100
            ),
            likely_downstream_scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
            reason_json JSONB NOT NULL DEFAULT '{}'::jsonb,
            evidence_json JSONB NOT NULL DEFAULT '[]'::jsonb,
            status TEXT NOT NULL DEFAULT 'ACTIVE',
            first_seen_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            last_updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            UNIQUE(procurement_id)
        )
        """
    )


def upsert_research(
    cur,
    procurement,
    buyer_id,
    raw_event_id,
    source_url,
    intelligence,
):
    ensure_research_table(cur)
    cur.execute(
        """
        INSERT INTO research_intelligence(
            procurement_id,
            project_id,
            buyer_company_id,
            title,
            intelligence_kind,
            customer_facing,
            confidence,
            likely_downstream_scopes,
            reason_json,
            evidence_json
        )
        VALUES(
            %s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s::jsonb
        )
        ON CONFLICT(procurement_id)
        DO UPDATE SET
            intelligence_kind=EXCLUDED.intelligence_kind,
            customer_facing=EXCLUDED.customer_facing,
            confidence=EXCLUDED.confidence,
            likely_downstream_scopes=EXCLUDED.likely_downstream_scopes,
            reason_json=EXCLUDED.reason_json,
            evidence_json=EXCLUDED.evidence_json,
            status='ACTIVE',
            last_updated_at_utc=NOW()
        """,
        (
            procurement["id"],
            procurement.get("project_id"),
            buyer_id,
            procurement["title"],
            intelligence["kind"],
            intelligence["customer_facing"],
            intelligence["confidence"],
            dumps(
                intelligence["likely_downstream_scopes"]
            ),
            dumps(intelligence),
            dumps([{
                "raw_event_id": raw_event_id,
                "source": "NSTA Energy Pathfinder",
                "url": source_url,
            }]),
        ),
    )


def deactivate_signal(
    cur,
    customer_id,
    procurement_id,
    signal_type,
    score=None,
    reasons=None,
):
    if score is None:
        cur.execute(
            """
            UPDATE opportunity_signals
            SET
                status='INACTIVE',
                last_updated_at_utc=NOW()
            WHERE
                customer_profile_id=%s
                AND procurement_id=%s
                AND signal_type=%s
            """,
            (
                customer_id,
                procurement_id,
                signal_type,
            ),
        )
    else:
        cur.execute(
            """
            UPDATE opportunity_signals
            SET
                status='INACTIVE',
                relevance_score=%s,
                reason_json=%s::jsonb,
                last_updated_at_utc=NOW()
            WHERE
                customer_profile_id=%s
                AND procurement_id=%s
                AND signal_type=%s
            """,
            (
                score,
                dumps(reasons or {}),
                customer_id,
                procurement_id,
                signal_type,
            ),
        )


def create_customer_signals(
    cur,
    procurement,
    buyer_id,
    raw_event_id,
    source_url,
    kind,
):
    customers = active_customers(cur)
    signal_type = (
        "INTELLIGENCE"
        if kind == "award"
        else "EMERGING"
    )

    if (
        not procurement.get("sector_gate_passed")
        or int(
            procurement.get(
                "energy_relevance_score"
            ) or 0
        ) < ENERGY_MIN_SCORE
    ):
        for customer in customers:
            deactivate_signal(
                cur,
                customer["id"],
                procurement["id"],
                signal_type,
            )
        return

    award_intel = None
    if kind == "award":
        award_intel = classify_award_intelligence(
            procurement.get("title") or "",
            procurement.get("description") or "",
        )
        upsert_research(
            cur,
            procurement,
            buyer_id,
            raw_event_id,
            source_url,
            award_intel,
        )

        if not award_intel["customer_facing"]:
            for customer in customers:
                deactivate_signal(
                    cur,
                    customer["id"],
                    procurement["id"],
                    "INTELLIGENCE",
                )
            return

    for customer in customers:
        score, reasons = score_procurement_for_customer(
            procurement,
            customer,
        )

        reasons["source_intelligence"] = {
            "source": "NSTA Energy Pathfinder",
            "authoritative_energy_source": True,
        }

        if award_intel:
            reasons["intelligence"] = award_intel
            if (
                award_intel["kind"] == "DOWNSTREAM"
                and score < 45
            ):
                score = min(
                    70,
                    max(
                        45,
                        score
                        + award_intel[
                            "downstream_score"
                        ] * 2,
                    ),
                )

        if score < 35:
            deactivate_signal(
                cur,
                customer["id"],
                procurement["id"],
                signal_type,
                score,
                reasons,
            )
            continue

        if signal_type == "EMERGING":
            timing = "Pre-tender / early engagement"
            action = (
                "Review the Energy Pathfinder opportunity, "
                "confirm the route to market and consider "
                "early engagement with the operator/developer."
            )
        else:
            timing = "Review downstream"
            if (
                award_intel
                and award_intel["kind"] == "DOWNSTREAM"
            ):
                scopes = ", ".join(
                    award_intel[
                        "likely_downstream_scopes"
                    ][:5]
                )
                action = (
                    "Review the award for downstream "
                    "supplier-entry opportunities"
                    + (
                        f" in {scopes}."
                        if scopes else "."
                    )
                )
            else:
                action = (
                    "Review the award for direct or "
                    "downstream supplier-entry opportunities."
                )

        cur.execute(
            """
            INSERT INTO opportunity_signals(
                customer_profile_id,
                signal_type,
                procurement_id,
                buyer_company_id,
                title,
                relevance_score,
                confidence,
                timing_label,
                reason_json,
                recommended_action,
                evidence_json
            )
            VALUES(
                %s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s::jsonb
            )
            ON CONFLICT(
                customer_profile_id,
                signal_type,
                procurement_id
            )
            DO UPDATE SET
                relevance_score=EXCLUDED.relevance_score,
                confidence=EXCLUDED.confidence,
                timing_label=EXCLUDED.timing_label,
                reason_json=EXCLUDED.reason_json,
                recommended_action=EXCLUDED.recommended_action,
                evidence_json=EXCLUDED.evidence_json,
                status='ACTIVE',
                last_updated_at_utc=NOW()
            """,
            (
                customer["id"],
                signal_type,
                procurement["id"],
                buyer_id,
                procurement["title"],
                score,
                (
                    award_intel["confidence"]
                    if award_intel
                    else (75 if score >= 70 else 60)
                ),
                timing,
                dumps(reasons),
                action,
                dumps([{
                    "raw_event_id": raw_event_id,
                    "source": "NSTA Energy Pathfinder",
                    "url": source_url,
                }]),
            ),
        )


def process_market_row(
    cur,
    row,
    session,
    kind,
    label,
):
    if kind == "project":
        return upsert_project(
            cur,
            row,
            session,
        )

    (
        procurement,
        buyer_id,
        raw_event_id,
        source_url,
        _contractor,
    ) = upsert_procurement(
        cur,
        row,
        session,
        kind,
        label,
    )

    create_customer_signals(
        cur,
        procurement,
        buyer_id,
        raw_event_id,
        source_url,
        kind,
    )
    return True


def start_run(conn):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO collector_runs(
                collector,
                status
            )
            VALUES('nsta_energy_pathfinder','running')
            RETURNING id
            """
        )
        return cur.fetchone()["id"]


def finish_run(
    conn,
    run_id,
    status,
    fetched,
    processed,
    errors,
    error_text,
):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE collector_runs
            SET
                finished_at_utc=NOW(),
                status=%s,
                fetched_count=%s,
                processed_count=%s,
                error_count=%s,
                error_text=%s
            WHERE id=%s
            """,
            (
                status,
                fetched,
                processed,
                errors,
                error_text,
                run_id,
            ),
        )


def main():
    session = build_session()

    with connection() as conn:
        run_id = start_run(conn)
        conn.commit()

        fetched = 0
        processed = 0
        errors = 0
        messages = []
        per_page = {}

        for spec in PAGES:
            url = urljoin(
                BASE,
                spec["path"],
            )
            label = spec["label"]
            kind = spec["kind"]

            try:
                response, rows, fetch_diag = fetch_page_with_fallback(
                    session,
                    url,
                )
                rows = rows[:MAX_ROWS_PER_PAGE]

                per_page[label] = {
                    "rows": len(rows),
                    "processed": 0,
                    "errors": 0,
                    "fetch_mode": fetch_diag.get("mode"),
                }

                print(
                    f"NSTA Energy Pathfinder: {label}: "
                    f"{len(rows)} rows",
                    flush=True,
                )

                if not rows:
                    print(
                        "NSTA zero-row diagnostics:",
                        json.dumps({
                            "label": label,
                            "url": url,
                            "diagnostics": fetch_diag,
                        }, default=str),
                        flush=True,
                    )

                for idx, row in enumerate(
                    rows,
                    start=1,
                ):
                    fetched += 1

                    try:
                        with conn.cursor() as cur:
                            ok = process_market_row(
                                cur,
                                row,
                                session,
                                kind,
                                label,
                            )

                        conn.commit()

                        if ok:
                            processed += 1
                            per_page[label][
                                "processed"
                            ] += 1

                    except Exception as exc:
                        conn.rollback()
                        errors += 1
                        per_page[label]["errors"] += 1

                        messages.append(
                            f"{label} item {idx}: "
                            f"{type(exc).__name__}: {exc}"
                        )

            except Exception as exc:
                conn.rollback()
                errors += 1
                per_page[label] = {
                    "rows": 0,
                    "processed": 0,
                    "errors": 1,
                }
                messages.append(
                    f"{label} fetch: "
                    f"{type(exc).__name__}: {exc}"
                )

        if fetched == 0 and errors == 0:
            status = "empty_source_response"
        else:
            status = (
                "ok"
                if errors == 0
                else (
                    "partial"
                    if processed > 0
                    else "error"
                )
            )

        finish_run(
            conn,
            run_id,
            status,
            fetched,
            processed,
            errors,
            "\n".join(messages[:50])
            if messages else None,
        )
        conn.commit()

        summary = {
            "collector": SOURCE,
            "collector_version": COLLECTOR_VERSION,
            "classifier_version": CLASSIFIER_VERSION,
            "status": status,
            "fetched": fetched,
            "processed": processed,
            "errors": errors,
            "pages": per_page,
            "detail_fetch": DETAIL_FETCH,
            "sector_policy": (
                "NSTA Energy Pathfinder treated as "
                "authoritative energy-sector source"
            ),
            "http_retry_policy": (
                "4 attempts with exponential backoff"
            ),
            "zero_row_policy": (
                "browser-UA retry plus DOM/export/API diagnostics"
            ),
            "javascript_probe": (
                "inspect inline Next hydration data plus route-specific JS "
                "chunks for API/export routes"
            ),
        }

        print(
            "collector:",
            json.dumps(
                summary,
                default=str,
            ),
            flush=True,
        )

        if messages:
            print(
                "collector_diagnostics:",
                " | ".join(messages[:20]),
                flush=True,
            )


if __name__ == "__main__":
    main()
