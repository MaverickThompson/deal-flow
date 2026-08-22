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
from datetime import date, datetime

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


def existing_source_urls(notion: Client, database_id: str) -> set:
    """Every Source URL already in the database. This is the dedupe key."""
    urls, cursor = set(), None
    while True:
        response = notion.databases.query(
            database_id=database_id,
            start_cursor=cursor,
            page_size=100,
        )
        for page in response["results"]:
            prop = page["properties"].get("Source URL", {})
            if prop.get("url"):
                urls.add(prop["url"].rstrip("/"))
        if not response.get("has_more"):
            return urls
        cursor = response["next_cursor"]


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
    notion.pages.create(
        parent={"database_id": database_id},
        properties={
            "Company": {"title": [{"text": {"content": (result.title or result.url)[:200]}}]},
            "One-liner": {"rich_text": [{"text": {"content": body[:400]}}]},
            "Source URL": {"url": result.url},
            "Sector": {"select": {"name": sector}},
            "Diligence status": {"select": {"name": "not started"}},
            "Funding": {"rich_text": [{"text": {"content": "not checked by pipeline"}}]},
            "Unverified": {
                "rich_text": [
                    {"text": {"content": ("Auto-imported from Exa. Nothing here is verified."
                                          + geo_flag(body))[:2000]}}
                ]
            },
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

    seen = existing_source_urls(notion, database_id)
    print(f"{datetime.now().isoformat(timespec='seconds')} | mode={mode} | {len(seen)} rows already in Notion")

    added = skipped = failed = 0

    for entry in load_queries(mode):
        query = entry["query"]
        sector = entry.get("sector", "other")
        count = entry.get("num_results", 10)

        try:
            response = exa.search(
                query,
                type="auto",
                num_results=count,
                contents={"highlights": True},
            )
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
            try:
                writer(notion, database_id, result, sector)
                seen.add(key)
                added += 1
                new_this_query += 1
            except Exception as e:
                print(f"  WRITE FAILED | {result.url} | {e}")
                failed += 1

        print(f"  {new_this_query:>3} new | {sector:<18} | {query[:70]}")

    print(f"\nDONE | added={added} skipped_as_duplicate={skipped} failed={failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
