"""
Project Scope - NSTA Energy Pathfinder collector
Version: 0.6.7

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
from intelligence import (
    classify_award_intelligence,
    match_downstream_scopes_to_customer,
)

COLLECTOR_VERSION = "0.6.7"
SOURCE = "nsta_energy_pathfinder"
BASE = "https://energypathfinder.nstauthority.co.uk"
PUBLIC_DATA_URL = urljoin(BASE, "/data/public-data.json")

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

NSTA_NUXT_PROBE_CACHE = None

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



def context_snippets(body, needles, radius=280, max_per_needle=6):
    out = {}
    lower = body.lower()

    for needle in needles:
        n = needle.lower()
        hits = []
        start = 0

        while len(hits) < max_per_needle:
            idx = lower.find(n, start)
            if idx < 0:
                break

            lo = max(0, idx - radius)
            hi = min(len(body), idx + len(needle) + radius)
            snippet = clean_text(body[lo:hi])
            hits.append(snippet[:1200])
            start = idx + len(needle)

        if hits:
            out[needle] = hits

    return out


def quoted_strings(body, keywords=None, max_items=160):
    pattern = r"[\"']([^\"'\\]{2,900})[\"']"
    items = []
    seen = set()

    for item in re.findall(pattern, body):
        cleaned = clean_text(item)
        if not cleaned or cleaned in seen:
            continue

        if keywords and not any(
            k.lower() in cleaned.lower()
            for k in keywords
        ):
            continue

        seen.add(cleaned)
        items.append(cleaned[:900])

        if len(items) >= max_items:
            break

    return items


def domain_candidates(body):
    pattern = (
        r"(?:https?://)?"
        r"([A-Za-z0-9.-]+\.(?:co\.uk|gov\.uk|com|net|io|azurewebsites\.net))"
    )
    domains = sorted(set(re.findall(pattern, body, flags=re.I)))

    return [
        d for d in domains
        if any(
            token in d.lower()
            for token in (
                "nsta",
                "pathfinder",
                "authority",
                "azure",
                "api",
            )
        )
    ][:80]


def source_map_url(body, bundle_url):
    tail = body[-5000:]
    matches = re.findall(
        r"sourceMappingURL=([^\s*]+)",
        tail,
        flags=re.I,
    )
    if not matches:
        return None
    return urljoin(bundle_url, matches[-1].strip())


def probe_json_candidate(session, url):
    try:
        response = session.get(
            url,
            timeout=min(TIMEOUT, 30),
            verify=certifi.where(),
            headers={
                "User-Agent": BROWSER_USER_AGENT,
                "Accept": "application/json,text/plain,*/*",
            },
        )

        result = {
            "url": url,
            "status": response.status_code,
            "content_type": response.headers.get("content-type"),
            "bytes": len(response.content),
            "preview": clean_text(response.text[:1000])[:700],
        }

        try:
            payload = response.json()
            result["json_type"] = type(payload).__name__
            if isinstance(payload, dict):
                result["json_keys"] = list(payload.keys())[:40]
            elif isinstance(payload, list):
                result["json_length"] = len(payload)
        except Exception:
            pass

        return result

    except Exception as exc:
        return {
            "url": url,
            "error": f"{type(exc).__name__}: {exc}",
        }



TARGET_NUXT_ROUTES = {
    "/",
    "/projects",
    "/upcoming-tenders",
    "/awarded-contracts",
    "/collaboration-opportunities",
    "/forward-work-plan-upcoming-tenders",
    "/forward-work-plan-awarded-contracts",
    "/forward-work-plan-collaboration-opportunities",
    "/forward-work-plans",
}


def extract_route_chunk_map(main_bundle, page_url):
    """
    Extract Nuxt/Vite route -> dynamic JS chunk mappings from the generated
    router table in the main bundle.
    """
    mappings = []

    # Works against Nuxt's generated route table:
    # name:"...",path:"/...",component:()=>...import("./Chunk.js")
    pattern = re.compile(
        r'name:"(?P<name>[^"]+)"'
        r',path:"(?P<path>[^"]+)"'
        r'.{0,500}?import\("\./(?P<chunk>[A-Za-z0-9_-]+\.js)"\)',
        flags=re.S,
    )

    for match in pattern.finditer(main_bundle):
        route_path = match.group("path")
        if route_path not in TARGET_NUXT_ROUTES:
            continue

        mappings.append({
            "name": match.group("name"),
            "path": route_path,
            "chunk": match.group("chunk"),
            "chunk_url": urljoin(
                page_url,
                f"/_nuxt/{match.group('chunk')}",
            ),
        })

    # De-duplicate while retaining deterministic order.
    unique = []
    seen = set()
    for item in mappings:
        key = (item["path"], item["chunk"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)

    return unique


def local_js_imports(body, page_url):
    refs = set()

    for match in re.findall(
        r'(?:import\(|from\s*)["\']\./([A-Za-z0-9_-]+\.js)["\']',
        body,
    ):
        refs.add(
            urljoin(
                page_url,
                f"/_nuxt/{match}",
            )
        )

    return sorted(refs)


def compact_data_call_contexts(body):
    needles = (
        "Pathfinder data API call returned error",
        "$fetch",
        "useFetch",
        "useAsyncData",
        "fetch(",
        "baseURL",
        "/api/",
        "api/",
        "download",
        "spreadsheet",
        "export",
    )

    result = {}
    lower = body.lower()

    for needle in needles:
        idx = lower.find(needle.lower())
        if idx < 0:
            continue

        lo = max(0, idx - 550)
        hi = min(len(body), idx + len(needle) + 1200)

        result[needle] = clean_text(
            body[lo:hi]
        )[:1800]

    return result


def compact_endpoint_candidates(body, page_url):
    candidates = set()

    # Strings likely to be actual app data calls rather than library URLs.
    quoted_pattern = r'["\']([^"\']{2,600})["\']'

    for value in re.findall(quoted_pattern, body):
        low = value.lower()

        if not any(
            token in low
            for token in (
                "api",
                "tender",
                "contract",
                "project",
                "collaboration",
                "pathfinder",
                "spreadsheet",
                "download",
                "export",
            )
        ):
            continue

        if value.startswith("http"):
            candidate = value
        elif value.startswith("/"):
            candidate = urljoin(page_url, value)
        else:
            continue

        # Remove known frontend routes and third-party library noise.
        c_low = candidate.lower()
        if any(
            noise in c_low
            for noise in (
                "opengis.net",
                "google.com",
                "github.com",
                "stadiamaps",
                "openstreetmap",
                "microsoft.com",
                "iiif",
                "stanford",
                "virtualearth",
            )
        ):
            continue

        candidates.add(candidate[:700])

    return sorted(candidates)[:40]


def inspect_route_chunks(
    session,
    main_bundle,
    page_url,
):
    mappings = extract_route_chunk_map(
        main_bundle,
        page_url,
    )

    chunk_cache = {}
    route_results = []

    for mapping in mappings:
        chunk_url = mapping["chunk_url"]

        if chunk_url not in chunk_cache:
            try:
                response = session.get(
                    chunk_url,
                    timeout=TIMEOUT,
                    verify=certifi.where(),
                    headers={
                        "User-Agent": BROWSER_USER_AGENT,
                    },
                )
                response.raise_for_status()

                body = response.text
                chunk_cache[chunk_url] = {
                    "status": response.status_code,
                    "bytes": len(response.content),
                    "body": body,
                    "imports": local_js_imports(
                        body,
                        page_url,
                    ),
                }
            except Exception as exc:
                chunk_cache[chunk_url] = {
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    ),
                    "body": "",
                    "imports": [],
                }

        chunk = chunk_cache[chunk_url]
        body = chunk.get("body") or ""

        route_results.append({
            "route": mapping["path"],
            "route_name": mapping["name"],
            "chunk": mapping["chunk"],
            "chunk_url": chunk_url,
            "status": chunk.get("status"),
            "bytes": chunk.get("bytes"),
            "error": chunk.get("error"),
            "local_imports": (
                chunk.get("imports") or []
            )[:12],
            "endpoint_candidates": (
                compact_endpoint_candidates(
                    body,
                    page_url,
                )
            ),
            "data_call_contexts": (
                compact_data_call_contexts(body)
            ),
        })

    # Inspect one layer of shared local imports used by the target list chunks.
    shared_urls = []
    for item in route_results:
        for url in item.get("local_imports") or []:
            if url not in shared_urls:
                shared_urls.append(url)

    shared_results = []
    for url in shared_urls[:12]:
        try:
            response = session.get(
                url,
                timeout=TIMEOUT,
                verify=certifi.where(),
                headers={
                    "User-Agent": BROWSER_USER_AGENT,
                },
            )
            response.raise_for_status()
            body = response.text

            contexts = compact_data_call_contexts(
                body
            )
            endpoints = compact_endpoint_candidates(
                body,
                page_url,
            )

            # Only surface shared chunks that contain useful data-call clues.
            if contexts or endpoints:
                shared_results.append({
                    "url": url,
                    "status": response.status_code,
                    "bytes": len(response.content),
                    "endpoint_candidates": endpoints,
                    "data_call_contexts": contexts,
                })

        except Exception as exc:
            shared_results.append({
                "url": url,
                "error": (
                    f"{type(exc).__name__}: {exc}"
                ),
            })

    return {
        "route_mapping_count": len(mappings),
        "routes": route_results,
        "shared_chunks_with_data_clues": (
            shared_results[:12]
        ),
    }



def deep_probe_nuxt_bundle(session, bundle_url, page_url):
    global NSTA_NUXT_PROBE_CACHE

    if NSTA_NUXT_PROBE_CACHE is not None:
        return NSTA_NUXT_PROBE_CACHE

    response = session.get(
        bundle_url,
        timeout=TIMEOUT,
        verify=certifi.where(),
        headers={"User-Agent": BROWSER_USER_AGENT},
    )
    response.raise_for_status()
    body = response.text

    route_keywords = (
        "upcoming-tenders",
        "awarded-contracts",
        "collaboration-opportunities",
        "forward-work-plan",
        "projects",
        "spreadsheet",
        "download",
        "export",
    )

    transport_keywords = (
        "$fetch",
        "useFetch",
        "useAsyncData",
        "fetch(",
        "baseURL",
        "baseUrl",
        "/api/",
        "api/",
        "graphql",
        "axios",
        "endpoint",
        "runtimeConfig",
    )

    route_strings = quoted_strings(
        body,
        keywords=route_keywords,
        max_items=180,
    )

    transport_strings = quoted_strings(
        body,
        keywords=(
            "api",
            "fetch",
            "graphql",
            "endpoint",
            "baseurl",
            "spreadsheet",
            "download",
            "export",
        ),
        max_items=180,
    )

    contexts = context_snippets(
        body,
        list(route_keywords) + list(transport_keywords),
        radius=360,
        max_per_needle=5,
    )

    endpoint_candidates = set()

    for item in route_strings + transport_strings:
        low = item.lower()

        if item.startswith("http"):
            endpoint_candidates.add(item)
        elif item.startswith("/") and any(
            token in low
            for token in (
                "api",
                "project",
                "tender",
                "contract",
                "collaboration",
                "download",
                "spreadsheet",
                "export",
            )
        ):
            endpoint_candidates.add(urljoin(page_url, item))

    broad_pattern = (
        r"[\"']("
        r"(?:https?://[^\"']+|/"
        r"[^\"']*(?:api|tender|contract|project|collaboration|download|spreadsheet|export)"
        r"[^\"']*)"
        r")[\"']"
    )
    for match in re.findall(broad_pattern, body, flags=re.I):
        endpoint_candidates.add(
            match if match.startswith("http")
            else urljoin(page_url, match)
        )

    frontend_suffixes = (
        "/projects",
        "/upcoming-tenders",
        "/awarded-contracts",
        "/collaboration-opportunities",
        "/forward-work-plan-upcoming-tenders",
    )

    filtered_candidates = []
    for candidate in sorted(endpoint_candidates):
        low = candidate.lower()

        if len(candidate) > 900:
            continue
        if candidate.startswith("data:"):
            continue
        if "iiif" in low or "stanford" in low:
            continue
        if candidate.endswith(frontend_suffixes):
            continue

        filtered_candidates.append(candidate)

    candidate_probes = [
        probe_json_candidate(session, url)
        for url in filtered_candidates[:15]
    ]

    sm_url = source_map_url(body, bundle_url)
    sm_probe = None

    if sm_url:
        try:
            sm_resp = session.get(
                sm_url,
                timeout=TIMEOUT,
                verify=certifi.where(),
                headers={"User-Agent": BROWSER_USER_AGENT},
            )
            sm_probe = {
                "url": sm_url,
                "status": sm_resp.status_code,
                "content_type": sm_resp.headers.get("content-type"),
                "bytes": len(sm_resp.content),
            }

            if sm_resp.ok:
                try:
                    sm_json = sm_resp.json()
                    sm_probe["sources_count"] = len(
                        sm_json.get("sources") or []
                    )
                    sm_probe["sources_sample"] = (
                        sm_json.get("sources") or []
                    )[:30]

                    source_contents = (
                        sm_json.get("sourcesContent") or []
                    )
                    joined = "\n".join(
                        x for x in source_contents
                        if isinstance(x, str)
                    )
                    if joined:
                        sm_probe["source_contexts"] = context_snippets(
                            joined,
                            list(route_keywords)
                            + list(transport_keywords),
                            radius=300,
                            max_per_needle=3,
                        )
                except Exception as exc:
                    sm_probe["parse_error"] = (
                        f"{type(exc).__name__}: {exc}"
                    )
        except Exception as exc:
            sm_probe = {
                "url": sm_url,
                "error": f"{type(exc).__name__}: {exc}",
            }

    route_chunk_probe = inspect_route_chunks(
        session,
        body,
        page_url,
    )

    NSTA_NUXT_PROBE_CACHE = {
        "framework": "Nuxt",
        "bundle_url": bundle_url,
        "bundle_status": response.status_code,
        "bundle_bytes": len(response.content),
        "domains": domain_candidates(body),
        "route_strings": route_strings[:120],
        "transport_strings": transport_strings[:120],
        "contexts": contexts,
        "endpoint_candidates": filtered_candidates[:80],
        "endpoint_probes": candidate_probes,
        "source_map": sm_probe,
        "route_chunk_probe": route_chunk_probe,
    }

    return NSTA_NUXT_PROBE_CACHE



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
    nuxt_deep_probe = None

    if not browser_rows:
        nuxt_bundle = next(
            (
                src
                for src in (
                    browser_diag.get("script_srcs") or []
                )
                if "/_nuxt/" in src
                and src.lower().endswith(".js")
            ),
            None,
        )

        if nuxt_bundle:
            nuxt_deep_probe = deep_probe_nuxt_bundle(
                session,
                nuxt_bundle,
                browser.url,
            )
        else:
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
        "nuxt_deep_probe": nuxt_deep_probe,
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

        # Keep the customer-independent research record regardless of whether
        # any current customer profile can act on it.
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
        downstream_match = {
            "matched_scopes": [],
            "matches": [],
            "match_count": 0,
        }

        inferred_capabilities = None

        if (
            award_intel
            and award_intel["kind"] == "DOWNSTREAM"
        ):
            downstream_match = (
                match_downstream_scopes_to_customer(
                    award_intel[
                        "likely_downstream_scopes"
                    ],
                    customer.get(
                        "capabilities"
                    ) or [],
                )
            )

            inferred_capabilities = downstream_match[
                "matched_scopes"
            ]

            # Package-level downstream potential is useful research, but it
            # is not customer-facing unless the inferred scopes overlap the
            # customer's actual capability profile.
            if not inferred_capabilities:
                score, reasons = (
                    score_procurement_for_customer(
                        procurement,
                        customer,
                    )
                )
                reasons["intelligence"] = {
                    **award_intel,
                    "customer_downstream_match": (
                        downstream_match
                    ),
                }

                deactivate_signal(
                    cur,
                    customer["id"],
                    procurement["id"],
                    signal_type,
                    score,
                    reasons,
                )
                continue

        score, reasons = (
            score_procurement_for_customer(
                procurement,
                customer,
                inferred_capabilities=(
                    inferred_capabilities
                    if award_intel
                    and award_intel[
                        "kind"
                    ] == "DOWNSTREAM"
                    else None
                ),
            )
        )

        reasons["source_intelligence"] = {
            "source": "NSTA Energy Pathfinder",
            "authoritative_energy_source": True,
        }

        if award_intel:
            reasons["intelligence"] = {
                **award_intel,
                "customer_downstream_match": (
                    downstream_match
                ),
            }

        fit_tier = (
            reasons.get(
                "customer_fit",
                {},
            ).get(
                "tier",
                "NONE",
            )
        )

        min_signal_score = (
            45
            if fit_tier == "INFERRED_DOWNSTREAM"
            else 35
        )

        if (
            fit_tier == "NONE"
            or score < min_signal_score
        ):
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
            timing = (
                "Pre-tender / early engagement"
            )
            action = (
                "Review the Energy Pathfinder opportunity, "
                "verify the buyer route-to-market/access position "
                "and consider early engagement only where the "
                "customer capability match is confirmed."
            )
        else:
            if fit_tier == "INFERRED_DOWNSTREAM":
                timing = (
                    "Downstream watch / supplier entry"
                )
                scopes = ", ".join(
                    downstream_match[
                        "matched_scopes"
                    ][:5]
                )
                action = (
                    "Monitor this award for downstream supplier-entry "
                    "opportunities specifically matching the customer's "
                    "capabilities"
                    + (
                        f": {scopes}. "
                        if scopes
                        else ". "
                    )
                    + (
                        "Confirm the actual subcontract package and route "
                        "to market before treating it as actionable."
                    )
                )
            else:
                timing = (
                    "Direct capability review"
                )
                action = (
                    "Review this award because the source text contains "
                    "direct customer-capability evidence. Confirm the "
                    "buyer route-to-market/access position before engagement."
                )

        confidence = (
            award_intel["confidence"]
            if award_intel
            else (
                80
                if score >= 75
                else 65
            )
        )

        if fit_tier == "INFERRED_DOWNSTREAM":
            confidence = min(
                confidence,
                75,
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
                confidence,
                timing,
                dumps(reasons),
                action,
                dumps([{
                    "raw_event_id": raw_event_id,
                    "source": (
                        "NSTA Energy Pathfinder"
                    ),
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



def normalized_key(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def scalar_text(value):
    if value is None:
        return None

    if isinstance(value, str):
        value = clean_text(value)
        return value or None

    if isinstance(value, (int, float, bool)):
        return clean_text(value)

    if isinstance(value, dict):
        preferred = (
            "name",
            "title",
            "label",
            "value",
            "displayName",
            "companyName",
            "organisationName",
            "organizationName",
            "operatorDeveloperName",
        )
        for key in preferred:
            if key in value:
                candidate = scalar_text(value.get(key))
                if candidate:
                    return candidate

        # Last resort for small descriptive dictionaries.
        parts = []
        for item in value.values():
            if isinstance(item, (str, int, float, bool)):
                candidate = scalar_text(item)
                if candidate and candidate not in parts:
                    parts.append(candidate)
        return " | ".join(parts[:6]) or None

    if isinstance(value, (list, tuple)):
        parts = []
        for item in value:
            candidate = scalar_text(item)
            if candidate and candidate not in parts:
                parts.append(candidate)
        return " | ".join(parts[:8]) or None

    return clean_text(value) or None


def deep_value(obj, *aliases):
    """
    Breadth-first lookup by normalised key. This makes the collector resilient
    to NSTA moving a field between a record and a nested `details` object.
    """
    wanted = {
        normalized_key(alias)
        for alias in aliases
        if alias
    }
    queue = [obj]
    seen = set()

    while queue:
        current = queue.pop(0)

        if id(current) in seen:
            continue
        seen.add(id(current))

        if isinstance(current, dict):
            # Check current level first.
            for key, value in current.items():
                if normalized_key(key) in wanted:
                    candidate = scalar_text(value)
                    if candidate not in (None, ""):
                        return candidate

            # Then recurse.
            for value in current.values():
                if isinstance(value, (dict, list, tuple)):
                    queue.append(value)

        elif isinstance(current, (list, tuple)):
            for value in current:
                if isinstance(value, (dict, list, tuple)):
                    queue.append(value)

    return None


def record_id(record):
    return deep_value(
        record,
        "id",
        "projectId",
        "tenderId",
        "contractId",
        "opportunityId",
        "forwardWorkPlanId",
    )


def detail_url_for(kind, record):
    rid = record_id(record)

    route = {
        "project": "projects",
        "upcoming_tender": "upcoming-tenders",
        "award": "awarded-contracts",
        "collaboration": "collaboration-opportunities",
        "fwp_upcoming_tender": "forward-work-plan-upcoming-tenders",
        "fwp_award": "forward-work-plan-awarded-contracts",
        "fwp_collaboration": "forward-work-plan-collaboration-opportunities",
        "forward_work_plan": "forward-work-plans",
    }.get(kind)

    if route and rid:
        return urljoin(BASE, f"/{route}/{rid}")

    if route:
        return urljoin(BASE, f"/{route}")

    return BASE


def project_type_text(project):
    project_type = deep_value(
        project,
        "projectType",
        "energyType",
        "sector",
    )
    subcategory = deep_value(
        project,
        "projectTypeSubCategory",
        "projectSubCategory",
        "subCategory",
    )

    if project_type and subcategory:
        if subcategory.lower() in project_type.lower():
            return project_type
        return f"{project_type} ({subcategory})"

    return project_type or subcategory


def project_row_from_json(project):
    return {
        "Project title": deep_value(
            project,
            "projectTitle",
            "title",
            "projectName",
            "name",
        ),
        "Operator/Developer": deep_value(
            project,
            "operatorDeveloper",
            "operatorDeveloperName",
            "operator",
            "developer",
            "organisation",
            "organization",
        ),
        "Project type (sub category)": project_type_text(project),
        "Field type": deep_value(
            project,
            "fieldType",
            "field",
        ),
        "Area": deep_value(
            project,
            "area",
            "geographicArea",
            "region",
            "locationArea",
        ),
        "Contact details": deep_value(
            project,
            "contactDetails",
            "contacts",
            "contact",
        ),
        "_detail_url": detail_url_for(
            "project",
            project,
        ),
        "_raw": project,
    }


def child_row_from_json(
    record,
    parent=None,
    kind="upcoming_tender",
    lane_label=None,
):
    parent = parent or {}

    operator = (
        deep_value(
            record,
            "operatorDeveloper",
            "operatorDeveloperName",
            "operator",
            "developer",
        )
        or deep_value(
            parent,
            "operatorDeveloper",
            "operatorDeveloperName",
            "operator",
            "developer",
            "organisation",
            "organization",
        )
    )

    project_title = (
        deep_value(
            record,
            "projectTitle",
            "infrastructureProjectTitle",
            "projectName",
        )
        or deep_value(
            parent,
            "projectTitle",
            "title",
            "projectName",
            "name",
        )
    )

    description = deep_value(
        record,
        "descriptionOfWork",
        "description",
        "scopeOfWork",
        "workDescription",
    ) or ""

    duration = deep_value(
        record,
        "contractDuration",
        "duration",
    )
    lane = lane_label or kind

    description_parts = [description]
    if duration:
        description_parts.append(
            f"Contract duration: {duration}"
        )
    description_parts.append(
        f"Energy Pathfinder lane: {lane}"
    )

    return {
        "Operator/Developer": operator,
        "Function": deep_value(
            record,
            "function",
            "functionName",
            "workFunction",
            "category",
        ),
        "Description of work": clean_text(
            " ".join(
                p for p in description_parts
                if p
            )
        ),
        "Project title": project_title,
        "Contractor": deep_value(
            record,
            "contractor",
            "contractorName",
            "supplier",
            "supplierName",
        ),
        "Area": (
            deep_value(
                record,
                "area",
                "geographicArea",
                "region",
            )
            or deep_value(
                parent,
                "area",
                "geographicArea",
                "region",
                "locationArea",
            )
        ),
        "Contract band": deep_value(
            record,
            "contractBand",
            "contractValueBand",
            "valueBand",
        ),
        "Estimated tender date": deep_value(
            record,
            "estimatedTenderDate",
            "tenderDate",
            "estimatedTenderQuarter",
            "estimatedTenderDateQuarter",
        ),
        "Date awarded": deep_value(
            record,
            "dateAwarded",
            "awardDate",
            "awardedDate",
        ),
        "_detail_url": detail_url_for(
            kind,
            record,
        ),
        "_raw": record,
        "_parent_raw": parent,
    }


def list_field(record, *aliases):
    wanted = {
        normalized_key(alias)
        for alias in aliases
    }

    if not isinstance(record, dict):
        return []

    for key, value in record.items():
        if normalized_key(key) in wanted:
            return value if isinstance(value, list) else []

    # NSTA currently keeps these lists at the record top level. Retain a
    # shallow nested fallback for future schema changes.
    for value in record.values():
        if not isinstance(value, dict):
            continue
        for key, nested in value.items():
            if normalized_key(key) in wanted:
                return nested if isinstance(nested, list) else []

    return []


def fetch_public_data(session):
    response = session.get(
        PUBLIC_DATA_URL,
        timeout=TIMEOUT,
        verify=certifi.where(),
        headers={
            "User-Agent": BROWSER_USER_AGENT,
            "Accept": "application/json",
            "Cache-Control": "no-cache",
        },
    )
    response.raise_for_status()

    payload = response.json()

    if not isinstance(payload, dict):
        raise ValueError(
            "Energy Pathfinder public-data.json did not return a JSON object"
        )

    projects = payload.get("infrastructureProjects")
    forward_plans = payload.get("forwardWorkPlans")

    if not isinstance(projects, list):
        raise ValueError(
            "Energy Pathfinder JSON missing infrastructureProjects[]"
        )

    if not isinstance(forward_plans, list):
        raise ValueError(
            "Energy Pathfinder JSON missing forwardWorkPlans[]"
        )

    return response, payload, projects, forward_plans


def process_json_feed(conn, session):
    response, payload, projects, forward_plans = fetch_public_data(
        session
    )

    counters = {
        "projects": 0,
        "project_upcoming_tenders": 0,
        "project_awarded_contracts": 0,
        "project_collaboration_opportunities": 0,
        "forward_work_plans": 0,
        "fwp_upcoming_tenders": 0,
        "fwp_awarded_contracts": 0,
        "fwp_collaboration_opportunities": 0,
    }

    fetched = 0
    processed = 0
    errors = 0
    messages = []

    def process_one(row, kind, label):
        nonlocal fetched, processed, errors
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

        except Exception as exc:
            conn.rollback()
            errors += 1
            messages.append(
                f"{label}: {type(exc).__name__}: {exc}"
            )

    for project in projects:
        counters["projects"] += 1
        process_one(
            project_row_from_json(project),
            "project",
            "Projects",
        )

        for tender in list_field(
            project,
            "upcomingTenders",
        ):
            counters[
                "project_upcoming_tenders"
            ] += 1
            process_one(
                child_row_from_json(
                    tender,
                    project,
                    "upcoming_tender",
                    "Upcoming tenders",
                ),
                "emerging",
                "Upcoming tenders",
            )

        for award in list_field(
            project,
            "awardedContracts",
        ):
            counters[
                "project_awarded_contracts"
            ] += 1
            process_one(
                child_row_from_json(
                    award,
                    project,
                    "award",
                    "Awarded contracts",
                ),
                "award",
                "Awarded contracts",
            )

        for opportunity in list_field(
            project,
            "collaborationOpportunities",
        ):
            counters[
                "project_collaboration_opportunities"
            ] += 1
            process_one(
                child_row_from_json(
                    opportunity,
                    project,
                    "collaboration",
                    "Collaboration opportunities",
                ),
                "emerging",
                "Collaboration opportunities",
            )

    for plan in forward_plans:
        counters["forward_work_plans"] += 1

        for tender in list_field(
            plan,
            "upcomingTenders",
        ):
            counters["fwp_upcoming_tenders"] += 1
            process_one(
                child_row_from_json(
                    tender,
                    plan,
                    "fwp_upcoming_tender",
                    "Forward work plan upcoming tenders",
                ),
                "emerging",
                "Forward work plan upcoming tenders",
            )

        for award in list_field(
            plan,
            "awardedContracts",
        ):
            counters["fwp_awarded_contracts"] += 1
            process_one(
                child_row_from_json(
                    award,
                    plan,
                    "fwp_award",
                    "Forward work plan awarded contracts",
                ),
                "award",
                "Forward work plan awarded contracts",
            )

        for opportunity in list_field(
            plan,
            "collaborationOpportunities",
        ):
            counters[
                "fwp_collaboration_opportunities"
            ] += 1
            process_one(
                child_row_from_json(
                    opportunity,
                    plan,
                    "fwp_collaboration",
                    "Forward work plan collaboration opportunities",
                ),
                "emerging",
                "Forward work plan collaboration opportunities",
            )

    diagnostics = {
        "public_data_url": PUBLIC_DATA_URL,
        "http_status": response.status_code,
        "content_type": response.headers.get(
            "content-type"
        ),
        "bytes": len(response.content),
        "top_level_keys": list(payload.keys())[:30],
        "source_counts": counters,
        "sample_project_keys": (
            list(projects[0].keys())[:40]
            if projects and isinstance(projects[0], dict)
            else []
        ),
        "sample_forward_work_plan_keys": (
            list(forward_plans[0].keys())[:40]
            if forward_plans
            and isinstance(forward_plans[0], dict)
            else []
        ),
    }

    return (
        fetched,
        processed,
        errors,
        messages,
        diagnostics,
    )


def run_html_diagnostics_only(session):
    """
    Retained fallback from v0.6.1-v0.6.5. If NSTA ever removes the public
    JSON endpoint, this still tells us what the website is returning.
    """
    summaries = []

    for spec in PAGES:
        url = urljoin(BASE, spec["path"])

        try:
            _response, rows, diag = (
                fetch_page_with_fallback(
                    session,
                    url,
                )
            )

            summaries.append({
                "label": spec["label"],
                "url": url,
                "rows": len(rows),
                "mode": diag.get("mode"),
            })

        except Exception as exc:
            summaries.append({
                "label": spec["label"],
                "url": url,
                "error": (
                    f"{type(exc).__name__}: {exc}"
                ),
            })

    return summaries



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

        try:
            (
                fetched,
                processed,
                errors,
                messages,
                diagnostics,
            ) = process_json_feed(
                conn,
                session,
            )

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

            print(
                "NSTA public-data diagnostics:",
                json.dumps(
                    diagnostics,
                    default=str,
                ),
                flush=True,
            )

            summary = {
                "collector": SOURCE,
                "collector_version": COLLECTOR_VERSION,
                "classifier_version": CLASSIFIER_VERSION,
                "status": status,
                "mode": "direct_public_json",
                "public_data_url": PUBLIC_DATA_URL,
                "fetched": fetched,
                "processed": processed,
                "errors": errors,
                "source_counts": diagnostics[
                    "source_counts"
                ],
                "sector_policy": (
                    "NSTA Energy Pathfinder treated as "
                    "authoritative energy-sector source"
                ),
                "http_retry_policy": (
                    "4 attempts with exponential backoff"
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

        except Exception as exc:
            conn.rollback()

            fallback = run_html_diagnostics_only(
                session
            )

            error_text = (
                f"{type(exc).__name__}: {exc}"
            )

            finish_run(
                conn,
                run_id,
                "error",
                0,
                0,
                1,
                error_text,
            )
            conn.commit()

            print(
                "NSTA public JSON fetch failed:",
                error_text,
                flush=True,
            )

            print(
                "NSTA HTML fallback diagnostics:",
                json.dumps(
                    fallback,
                    default=str,
                ),
                flush=True,
            )

            print(
                "collector:",
                json.dumps({
                    "collector": SOURCE,
                    "collector_version": COLLECTOR_VERSION,
                    "classifier_version": CLASSIFIER_VERSION,
                    "status": "error",
                    "mode": "direct_public_json_failed",
                    "public_data_url": PUBLIC_DATA_URL,
                    "errors": 1,
                }),
                flush=True,
            )


if __name__ == "__main__":
    main()
