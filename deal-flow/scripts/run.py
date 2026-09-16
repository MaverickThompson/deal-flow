"""
Deal Flow pipeline.

Runs Exa searches from queries.yml and writes new rows into Notion.
Deduplicates against every Source URL already in the target database, so
re-running is safe and only ever adds what is genuinely new.

Usage:
    python scripts/run.py signals
    python scripts/run.py companies

Environment variables required:
    EXA_API_KEY
    NOTION_TOKEN
    NOTION_SIGNALS_DB
    NOTION_COMPANIES_DB
"""

import os
import re
import sys
import time
from datetime import date, datetime
from urllib.parse import urlparse

import yaml
from exa_py import Exa
from notion_client import Client

# Countries that appear in Exa company records and are not the US.
# Used only to FLAG rows for review — nothing is auto-deleted.
NON_US_HINTS = [
    "United Kingdom", "Canada", "Germany", "France", "Switzerland", "Nepal",
    "India", "Singapore", "Australia", "Netherlands", "Sweden", "Spain",
    "Italy", "Ireland", "Israel", "Brazil", "Japan", "China", "Poland",
    "Portugal", "Denmark", "Norway", "Finland", "Belgium", "Austria",
    "Mexico", "Argentina", "South Korea", "Nigeria", "Kenya", "Estonia",
]

# Exa's category:company search returns plenty of articles ABOUT companies.
# Those are signals, not companies, and must never become rows in Companies.
ARTICLE_DOMAINS = {
    "substack.com", "medium.com", "wordpress.com", "blogspot.com",
    "techcrunch.com", "forbes.com", "businessinsider.com", "bloomberg.com",
    "reuters.com", "cnbc.com", "wsj.com", "nytimes.com", "ft.com",
    "theinformation.com", "axios.com", "venturebeat.com", "theverge.com",
    "wired.com", "fastcompany.com", "inc.com", "entrepreneur.com",
    "sifted.eu", "protocol.com", "fortune.com", "businesswire.com",
    "prnewswire.com", "globenewswire.com", "yahoo.com", "msn.com",
    "linkedin.com", "reddit.com", "youtube.com", "x.com", "twitter.com",
    "facebook.com", "instagram.com", "tiktok.com", "quora.com",
    "news.ycombinator.com", "producthunt.com", "crunchbase.com",
    "pitchbook.com", "wikipedia.org", "glassdoor.com", "indeed.com",
}

# On code hosts the repo path is the identity, not the platform domain.
REPO_HOSTS = {"github.com", "gitlab.com", "huggingface.co", "bitbucket.org"}

# Exa asks callers to retry with exponential backoff on 5xx. Seconds.
RETRY_DELAYS = (2, 4, 8)


def env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


def load_queries(mode: str):
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "queries.yml")) as f:
        config = yaml.safe_load(f)
    queries = config.get(mode)
    if not queries:
        sys.exit(f"No queries defined for mode '{mode}' in queries.yml")
    return queries


def existing_keys(notion: Client, database_id: str, title_prop: str):
    """Every Source URL and every title already in the database.

    The URL is the primary dedupe key. The name is a second key that catches
    the same company arriving at two different URLs.
    """
    urls, names, cursor = set(), set(), None
    while True:
        response = notion.databases.query(
            database_id=database_id,
            start_cursor=cursor,
            page_size=100,
        )
        for page in response["results"]:
            props = page["properties"]
            url_prop = props.get("Source URL", {})
            if url_prop.get("url"):
                urls.add(url_prop["url"].rstrip("/"))
            title = props.get(title_prop, {}).get("title") or []
            text = "".join(t.get("plain_text", "") for t in title).strip().lower()
            if text:
                names.add(text)
        if not response.get("has_more"):
            return urls, names
        cursor = response["next_cursor"]


def is_transient(error) -> bool:
    """True for errors worth retrying: Exa over capacity, rate limits, 5xx."""
    text = str(error).lower()
    if "service_overloaded" in text or "over capacity" in text:
        return True
    if "rate limit" in text or "timed out" in text or "timeout" in text:
        return True
    status = getattr(error, "status_code", None)
    if status is None:
        status = getattr(getattr(error, "response", None), "status_code", None)
    if isinstance(status, int):
        return status == 429 or 500 <= status < 600
    return bool(re.search(r"status code (?:429|5\d\d)", text))


def search_with_retry(exa, query: str, count: int):
    """Exa returns 503 SERVICE_OVERLOADED under load and tells callers to back
    off. Without this the entire job exits 1 on a transient capacity blip."""
    attempts = 1 + len(RETRY_DELAYS)
    for i in range(attempts):
        try:
            return exa.search(
                query,
                type="auto",
                num_results=count,
                contents={"highlights": True},
            )
        except Exception as error:
            if i == attempts - 1 or not is_transient(error):
                raise
            delay = RETRY_DELAYS[i]
            print(f"  Exa unavailable — retry {i + 1}/{len(RETRY_DELAYS)} "
                  f"in {delay}s | {query[:50]}")
            time.sleep(delay)


def host_of(url: str) -> str:
    try:
        host = urlparse(url or "").netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_article(url: str) -> bool:
    host = host_of(url)
    if not host:
        return False
    return any(host == d or host.endswith("." + d) for d in ARTICLE_DOMAINS)


def company_name(result) -> str:
    """The company's own name, from its domain — never the page headline.

    Headlines make three stories about Fizz look like three companies. The
    domain is the stable identity. The original title is kept in Unverified.
    """
    url = result.url or ""
    host = host_of(url)
    if not host:
        return (getattr(result, "title", None) or url)[:200]

    if host in REPO_HOSTS:
        parts = [p for p in urlparse(url).path.split("/") if p][:2]
        if parts:
            return "/".join(parts)[:200]

    pieces = host.split(".")
    label = pieces[0]
    if label in ("app", "get", "try", "hq", "about", "home", "go") and len(pieces) > 1:
        label = pieces[1]
    return (label.replace("-", " ").title() or host)[:200]


def published_date(result) -> str:
    """Exa's published date, normalized to YYYY-MM-DD. Falls back to today."""
    raw = getattr(result, "published_date", None)
    if raw:
        match = re.match(r"(\d{4}-\d{2}-\d{2})", str(raw))
        if match:
            return match.group(1)
    return date.today().isoformat()


def excerpt(result, limit: int = 1800) -> str:
    highlights = getattr(result, "highlights", None) or []
    text = " ".join(h.strip() for h in highlights)
    if not text:
        text = (getattr(result, "text", "") or "")[:limit]
    return text[:limit].strip()


def geo_flag(text: str) -> str:
    hits = [c for c in NON_US_HINTS if c in text]
    return f" [GEO CHECK: mentions {', '.join(hits[:3])}]" if hits else ""


def write_signal(notion, database_id, result, sector):
    body = excerpt(result)
    notion.pages.create(
        parent={"database_id": database_id},
        properties={
            "Name": {"title": [{"text": {"content": (result.title or result.url)[:200]}}]},
            "Type": {"select": {"name": "founder"}},
            "Source URL": {"url": result.url},
            "Date": {"date": {"start": published_date(result)}},
            "Sector": {"select": {"name": sector}},
            "Status": {"select": {"name": "new"}},
            "Found by": {"select": {"name": "Founder Scout"}},
            "Inferred": {"checkbox": True},  # pipeline rows are unconfirmed by default
            "Notes": {"rich_text": [{"text": {"content": (body[:1900] + geo_flag(body))[:2000]}}]},
        },
    )


def write_company(notion, database_id, result, sector):
    body = excerpt(result)
    title = (getattr(result, "title", None) or "").strip()
    unverified = ("Auto-imported from Exa. Nothing here is verified. "
                  f"Name derived from domain. Page title: {title or 'none'}."
                  + geo_flag(body))
    notion.pages.create(
        parent={"database_id": database_id},
        properties={
            "Company": {"title": [{"text": {"content": company_name(result)}}]},
            "One-liner": {"rich_text": [{"text": {"content": body[:400]}}]},
            "Source URL": {"url": result.url},
            "Sector": {"select": {"name": sector}},
            "Diligence status": {"select": {"name": "not started"}},
            "Funding": {"rich_text": [{"text": {"content": "not checked by pipeline"}}]},
            "Unverified": {"rich_text": [{"text": {"content": unverified[:2000]}}]},
        },
    )


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("signals", "companies"):
        sys.exit("Usage: python scripts/run.py [signals|companies]")
    mode = sys.argv[1]

    exa = Exa(api_key=env("EXA_API_KEY"))
    notion = Client(auth=env("NOTION_TOKEN"))
    database_id = env("NOTION_SIGNALS_DB" if mode == "signals" else "NOTION_COMPANIES_DB")
    writer = write_signal if mode == "signals" else write_company
    title_prop = "Name" if mode == "signals" else "Company"

    seen, seen_names = existing_keys(notion, database_id, title_prop)
    print(f"{datetime.now().isoformat(timespec='seconds')} | mode={mode} | "
          f"{len(seen)} rows already in Notion")

    added = skipped = failed = articles = 0

    for entry in load_queries(mode):
        query = entry["query"]
        sector = entry.get("sector", "other")
        count = entry.get("num_results", 10)

        try:
            response = search_with_retry(exa, query, count)
        except Exception as e:
            print(f"  SEARCH FAILED | {query[:60]}... | {e}")
            failed += 1
            continue

        new_this_query = 0
        for result in response.results:
            key = (result.url or "").rstrip("/")
            if not key or key in seen:
                skipped += 1
                continue

            if mode == "companies":
                if is_article(result.url):
                    articles += 1
                    print(f"       not a company (article) | {result.url}")
                    continue
                name = company_name(result).lower()
                if name in seen_names:
                    skipped += 1
                    continue

            try:
                writer(notion, database_id, result, sector)
                seen.add(key)
                if mode == "companies":
                    seen_names.add(company_name(result).lower())
                added += 1
                new_this_query += 1
            except Exception as e:
                print(f"  WRITE FAILED | {result.url} | {e}")
                failed += 1

        print(f"  {new_this_query:>3} new | {sector:<18} | {query[:70]}")

    print(f"\nDONE | added={added} skipped_as_duplicate={skipped} "
          f"skipped_as_article={articles} failed={failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
