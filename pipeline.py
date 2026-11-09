#!/usr/bin/env python3
"""Daily Hacker News scan -> local-LLM classification -> Markdown digest.

A small, dependency-light automation demo:

    scrape HN front page -> SQLite (URL dedup) -> local LLM classify -> Markdown digest

Only `requests` and `beautifulsoup4` are required. All classification runs
against a local OpenAI-compatible endpoint (no cloud API involved).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sqlite3
import sys
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HN_URL = "https://news.ycombinator.com"
LLM_BASE_URL = "http://127.0.0.1:8731/v1"
LLM_MODEL = "halogen-qwen3.8-flash-next"
LLM_API_KEY = "halo-local-123"
USER_AGENT = "ai-digest-pipeline/1.0 (portfolio demo)"

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "digest.db"
OUTPUT_DIR = BASE_DIR / "output"

CLASSIFY_PROMPT = """\
You are a tech-news triage assistant for an AI automation developer.
Classify the article below and respond with ONLY a JSON object (no prose, no code fences):
{{"ai_related": true or false, "summary": "<one factual sentence about the article>", "relevance_score": <integer 1-5: how interesting this is for someone building AI-powered automation tools>}}

Title: {title}
URL: {url}
"""


def init_db(db_path: Path) -> sqlite3.Connection:
    """Open (and create if needed) the SQLite database with the items table."""
    conn = sqlite3.connect(db_path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS items (
            url             TEXT PRIMARY KEY,
            title           TEXT NOT NULL,
            points          INTEGER NOT NULL DEFAULT 0,
            rank            INTEGER NOT NULL DEFAULT 0,
            scraped_at      TEXT NOT NULL,
            ai_related      INTEGER,
            summary         TEXT,
            relevance_score INTEGER
        )
        """
    )
    conn.commit()
    return conn


def scrape_hn(limit: int) -> list[dict]:
    """Fetch the Hacker News front page and return structured story items.

    Each item is a dict with keys: rank, title, url, points.
    """
    resp = requests.get(HN_URL, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    items: list[dict] = []
    for row in soup.select("tr.athing")[:limit]:
        link = row.select_one("span.titleline > a")
        if link is None:
            continue
        url = link.get("href", "").strip()
        if url.startswith("item?id="):  # job-style relative links
            url = f"{HN_URL}/{url}"

        rank_el = row.select_one("span.rank")
        rank_match = re.search(r"\d+", rank_el.get_text()) if rank_el else None
        rank = int(rank_match.group()) if rank_match else len(items) + 1

        # Points live in the subtext row that follows each story row.
        subtext = row.find_next_sibling("tr")
        score_el = subtext.select_one("span.score") if subtext else None
        points_match = re.search(r"\d+", score_el.get_text()) if score_el else None
        points = int(points_match.group()) if points_match else 0

        items.append(
            {"rank": rank, "title": link.get_text(strip=True), "url": url, "points": points}
        )
    return items


def store_items(conn: sqlite3.Connection, items: list[dict]) -> list[dict]:
    """Insert scraped items, skipping URLs already seen. Returns only NEW items."""
    new_items: list[dict] = []
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for item in items:
        cur = conn.execute(
            "INSERT OR IGNORE INTO items (url, title, points, rank, scraped_at) VALUES (?, ?, ?, ?, ?)",
            (item["url"], item["title"], item["points"], item["rank"], now),
        )
        if cur.rowcount:
            new_items.append(item)
    conn.commit()
    return new_items


def parse_classification(text: str) -> dict:
    """Extract the JSON classification object from an LLM reply.

    Tolerates surrounding prose or code fences. Raises ValueError if no
    parseable JSON object is found.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object in LLM reply: {text[:120]!r}")
    data = json.loads(match.group())
    score = max(1, min(5, int(data.get("relevance_score", 3))))
    return {
        "ai_related": bool(data.get("ai_related", False)),
        "summary": str(data.get("summary", "")).strip(),
        "relevance_score": score,
    }


def classify_item(item: dict) -> dict:
    """Ask the local LLM to classify one story. Returns a classification dict.

    On any LLM/parse failure, returns a graceful fallback (score 1, marked
    summary) so one bad reply never kills the run.
    """
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "user", "content": CLASSIFY_PROMPT.format(title=item["title"], url=item["url"])}
        ],
        "max_tokens": 400,
        "temperature": 0.2,
    }
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post(f"{LLM_BASE_URL}/chat/completions", headers=headers, json=payload, timeout=120)
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"]
        return parse_classification(reply)
    except (requests.RequestException, KeyError, ValueError) as exc:
        print(f"  ! classification failed ({exc}); using fallback", file=sys.stderr)
        return {"ai_related": False, "summary": "(classification failed)", "relevance_score": 1}


def save_classification(conn: sqlite3.Connection, item: dict) -> None:
    """Persist a classification result back onto the stored item."""
    conn.execute(
        "UPDATE items SET ai_related = ?, summary = ?, relevance_score = ? WHERE url = ?",
        (int(item["ai_related"]), item["summary"], item["relevance_score"], item["url"]),
    )
    conn.commit()


def _render_group(lines: list[str], heading: str, group: list[dict]) -> None:
    """Append one digest section, sub-grouped by descending relevance score."""
    lines.append(f"## {heading}")
    lines.append("")
    for score in sorted({i["relevance_score"] for i in group}, reverse=True):
        lines.append(f"### Relevance {score}")
        lines.append("")
        for it in sorted((x for x in group if x["relevance_score"] == score), key=lambda x: x["rank"]):
            lines.append(
                f"- [{it['title']}]({it['url']}) — {it['summary']} "
                f"({it['points']} points, HN rank #{it['rank']})"
            )
        lines.append("")


def render_digest(items: list[dict], day: dt.date, llm_used: bool) -> str:
    """Build the Markdown digest: AI-related stories first, then the rest."""
    lines = [f"# Hacker News Digest — {day.isoformat()}", ""]
    if llm_used:
        lines.append(
            f"{len(items)} new stories scraped from Hacker News, classified locally "
            f"by {LLM_MODEL}."
        )
    else:
        lines.append(f"{len(items)} new stories scraped from Hacker News (LLM classification skipped).")
    lines.append("")

    if not llm_used:
        for it in sorted(items, key=lambda x: x["rank"]):
            lines.append(f"- [{it['title']}]({it['url']}) ({it['points']} points, HN rank #{it['rank']})")
        lines.append("")
        return "\n".join(lines)

    ai_items = [i for i in items if i["ai_related"]]
    other_items = [i for i in items if not i["ai_related"]]
    if ai_items:
        _render_group(lines, "AI-Related", ai_items)
    if other_items:
        _render_group(lines, "General Tech", other_items)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """Run the full pipeline: scrape -> dedup -> classify -> write digest."""
    parser = argparse.ArgumentParser(description="Scrape Hacker News and build a local-LLM Markdown digest.")
    parser.add_argument("--limit", type=int, default=15, help="Number of front-page items to scrape (default: 15)")
    parser.add_argument("--skip-llm", action="store_true", help="Skip LLM classification (offline mode)")
    args = parser.parse_args(argv)

    conn = init_db(DB_PATH)

    print(f"Scraping {HN_URL} (limit {args.limit})...")
    scraped = scrape_hn(args.limit)
    print(f"  scraped {len(scraped)} items")

    new_items = store_items(conn, scraped)
    print(f"  {len(new_items)} new after URL dedup")
    if not new_items:
        print("Nothing new to process — dedup working. No digest written.")
        return 0

    if args.skip_llm:
        print("LLM classification skipped (--skip-llm).")
    else:
        for idx, item in enumerate(new_items, 1):
            print(f"  [{idx}/{len(new_items)}] classifying: {item['title'][:70]}")
            item.update(classify_item(item))
            save_classification(conn, item)

    day = dt.date.today()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    digest_path = OUTPUT_DIR / f"digest-{day.isoformat()}.md"
    digest_path.write_text(render_digest(new_items, day, llm_used=not args.skip_llm), encoding="utf-8")
    print(f"Digest written to {digest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
