# deal-flow

Automated sourcing for early-stage deal flow. Runs Exa searches on a schedule
and writes new rows into Notion. Free to operate: Exa's free tier plus GitHub
Actions.

**What it does not do:** verify anything, or form an opinion. Everything it
writes lands as unverified with `Status = new` for human triage. That is
deliberate.

---

## Setup

### 1. Notion integration

1. Go to **notion.so/my-integrations** → **New integration**
2. Name it `deal-flow`, internal integration, pick your workspace
3. Copy the **Internal Integration Secret** — this is `NOTION_TOKEN`
4. Open your **Deal Flow** page in Notion → **⋯** → **Connections** →
   add `deal-flow`. The integration cannot see anything you do not share
   with it, and sharing the parent page covers the databases inside it.

### 2. GitHub secrets

In the repo: **Settings → Secrets and variables → Actions → New repository secret**

| Name | Value |
|---|---|
| `EXA_API_KEY` | from dashboard.exa.ai/api-keys |
| `NOTION_TOKEN` | the integration secret from step 1 |
| `NOTION_SIGNALS_DB` | `91dbe887f9de46ea98724d76d9c792da` |
| `NOTION_COMPANIES_DB` | `e31bb9a739cd4b449f1865b42166baf3` |

### 3. First run

**Actions** tab → **Deal Flow — daily** → **Run workflow**. Do this manually
before trusting the schedule. Read the log. Then check Notion.

---

## Running it locally

```bash
pip install -r requirements.txt
cp .env.example .env      # then fill in your keys
set -a && source .env && set +a
python scripts/run.py signals
python scripts/run.py companies
```

---

## Changing what it hunts for

Edit `queries.yml`. No code changes needed.

Write each query as a **description of the ideal page**, not keywords — Exa is
neural search, and keyword-style queries make results worse.

- Good: `post from a founder announcing they left their job to build an AI agent company`
- Bad: `quit job AI agent startup`

Prefix a query with `category:company` to get structured company records —
headcount, year-over-year growth, founders, founding year — instead of articles.
That is the highest-value query type in the system.

---

## How deduplication works

Before writing anything, the script pulls every `Source URL` already in the
target database and holds them in a set. Anything already present is skipped.

This means re-running is always safe, and the daily job only ever adds what is
genuinely new. It also means **never edit the Source URL field by hand** — that
is the dedupe key.

---

## Cost

| | |
|---|---|
| Exa search | $0.007/request, first 10 results included, $0.001 each after |
| GitHub Actions | free (2,000 min/month private, unlimited public) |
| Notion | free tier |

At eight queries a day this runs to a few cents a month, covered by Exa's
$10/month free credits.

---

## Schedule

`0 11 * * *` — 6:00am America/Chicago during CDT. GitHub cron is always UTC and
does not follow daylight saving, so in winter this fires at 5:00am local. Change
to `0 12 * * *` after the November clock change if that matters.

GitHub also delays scheduled runs under load, sometimes by an hour. It is not a
precise scheduler and does not need to be.

---

## What this deliberately does not do

- **No verification.** Every row is unconfirmed. `Inferred` is checked on all
  pipeline-written signals.
- **No opinions.** `Verdict` is never written to. That field is yours.
- **No geography filtering.** Non-US mentions get a `[GEO CHECK]` note in the
  row rather than being dropped, so you decide.
- **No memos.** The pipeline produces candidates. The judgment is the product.
