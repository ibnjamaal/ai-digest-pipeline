# AI Digest Pipeline

A small, working automation demo: scrape the Hacker News front page, deduplicate
against a local SQLite database, classify each new story with a **100% local LLM**,
and emit a Markdown digest — the same shape as a real daily job/tech-scan automation.

## Architecture

```
+--------------------+     +--------------------------+     +----------------------+     +------------------------+
|  Hacker News       | --> |  SQLite (digest.db)      | --> |  Local LLM           | --> |  Markdown digest       |
|  front-page scrape |     |  URL dedup: only NEW     |     |  classify each item  |     |  output/digest-        |
|  (requests + BS4)  |     |  items pass through      |     |  (OpenAI-compatible  |     |  YYYY-MM-DD.md         |
+--------------------+     +--------------------------+     |  API, plain requests)|     +------------------------+
                                                           +----------------------+
```

1. **Scrape** — `requests` + `BeautifulSoup` parse the HN front page into structured items (rank, title, url, points).
2. **Store & dedup** — items are inserted into SQLite with the URL as primary key; `INSERT OR IGNORE` means re-runs only process genuinely new stories.
3. **Classify** — each new item is sent to a local OpenAI-compatible endpoint (`/chat/completions`, plain `requests`, no SDK) which returns JSON: `ai_related`, a one-line summary, and a 1–5 relevance score.
4. **Digest** — a Markdown file is generated with AI-related stories first, grouped by descending relevance.

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Dependencies: `requests` and `beautifulsoup4` only.

A local LLM server must be listening at `http://127.0.0.1:8731/v1`
(model `halogen-qwen3.8-flash-next`). All inference runs locally — no cloud API calls.

## Usage

```bash
# Full pipeline: scrape 10 items, classify with the local LLM, write the digest
.venv/bin/python pipeline.py --limit 10

# Offline mode: scrape + dedup + digest, no LLM calls
.venv/bin/python pipeline.py --limit 10 --skip-llm

# Re-run: everything already seen -> 0 new items, no work done
.venv/bin/python pipeline.py --limit 10
```

The digest lands at `output/digest-YYYY-MM-DD.md`. A committed example lives at
[`examples/digest-sample.md`](examples/digest-sample.md).

## Why this exists

This mirrors a real daily automation: every morning you want the signal from a noisy
firehose (HN, RSS, job boards) without re-reading what you already saw, and you want
each item triaged — is this relevant to me, and why? The three hard parts of that job
are scraping reliably, deduplicating statefully, and getting structured output from an
LLM. This repo does all three in ~250 lines with two dependencies and fully local
inference, so it runs anywhere a model server runs.

## Notes

- `digest.db` and `output/` are gitignored — the database is runtime state, not source.
- LLM replies are parsed defensively (regex-extracted JSON, clamped scores); a bad
  reply degrades to a fallback instead of failing the run.
- Single module (`pipeline.py`), argparse CLI, docstrings throughout — deliberately
  small so the whole flow is readable in one sitting.
