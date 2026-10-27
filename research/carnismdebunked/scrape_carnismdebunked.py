"""Fetch carnismdebunked.com (sitemap pages) and convert each page to markdown.

Collection and parsing are separate steps:
    python scrape_carnismdebunked.py fetch   # sitemap -> raw/<slug>.html (skips pages already saved)
    python scrape_carnismdebunked.py parse   # raw/*.html -> pages/<slug>.md
    python scrape_carnismdebunked.py all     # both

Direct connection (Josh's call, 2026-10-27, personal project). Polite: robots.txt checked,
one request every DELAY_S seconds, retries with backoff, and a circuit breaker that aborts
after MAX_CONSECUTIVE_FAILS. Resumable: raw files act as the checkpoint. Log: logs/scrape.log.
"""
import json
import logging
import re
import sys
import time
import urllib.robotparser
from datetime import datetime, timezone
from pathlib import Path

import requests
import trafilatura

BASE = "https://www.carnismdebunked.com"
SITEMAP = f"{BASE}/sitemap.xml"
USER_AGENT = "Mozilla/5.0 (personal study; low-rate, robots.txt respected)"
DELAY_S = 3.0
RETRIES = 3
TIMEOUT_S = 20
MAX_CONSECUTIVE_FAILS = 3
SKIP_SLUGS = {"contact", "services", "portraiture", "support-carnism-debunked", "hero-image-1", "new-page"}

ROOT = Path(__file__).parent
RAW_DIR = ROOT / "raw"
PAGES_DIR = ROOT / "pages"
LOG_DIR = ROOT / "logs"
EVENTS = LOG_DIR / "events.jsonl"

log = logging.getLogger("carnismdebunked")


def setup_logging():
    LOG_DIR.mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler(LOG_DIR / "scrape.log", encoding="utf-8"),
                                  logging.StreamHandler()])


def event(kind, **fields):
    rec = {"ts": datetime.now(timezone.utc).isoformat(timespec="seconds"), "event": kind, **fields}
    with EVENTS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def slug_of(url):
    path = url.replace(BASE, "").strip("/")
    return re.sub(r"[^a-z0-9-]+", "-", path.lower()) or "home"


def get(session, url):
    """GET with retries and backoff. Returns text or raises the last error."""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            r = session.get(url, timeout=TIMEOUT_S)
            if r.status_code == 200:
                return r.text
            last = RuntimeError(f"HTTP {r.status_code}")
            if r.status_code in (403, 404):
                break
        except requests.RequestException as e:
            last = e
        time.sleep(DELAY_S * attempt * 2)
    raise last or RuntimeError("no attempts made")


def robots(session):
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(get(session, f"{BASE}/robots.txt").splitlines())
    return rp


def sitemap_urls(session):
    return re.findall(r"<loc>([^<]+)</loc>", get(session, SITEMAP))


def fetch():
    RAW_DIR.mkdir(exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    rp = robots(session)
    time.sleep(DELAY_S)
    urls = sitemap_urls(session)
    log.info("sitemap: %d urls", len(urls))
    event("sitemap", count=len(urls))
    fetched = skipped = failed = consecutive = 0
    for url in urls:
        slug = slug_of(url)
        out = RAW_DIR / f"{slug}.html"
        if slug in SKIP_SLUGS or out.exists():
            skipped += 1
            continue
        if not rp.can_fetch(USER_AGENT, url):
            log.info("robots disallows %s", url)
            event("robots_skip", url=url)
            skipped += 1
            continue
        time.sleep(DELAY_S)
        try:
            html = get(session, url)
        except Exception as e:
            failed += 1
            consecutive += 1
            log.warning("failed %s: %s", url, e)
            event("fetch_fail", url=url, error=str(e))
            if consecutive >= MAX_CONSECUTIVE_FAILS:
                log.error("circuit breaker: %d consecutive failures, aborting", consecutive)
                event("abort", reason="circuit_breaker")
                break
            continue
        consecutive = 0
        out.write_text(html, encoding="utf-8")
        fetched += 1
        log.info("fetched %s (%d bytes)", slug, len(html))
        event("fetch_ok", url=url, slug=slug, bytes=len(html))
    log.info("fetch done: fetched=%d skipped=%d failed=%d", fetched, skipped, failed)
    event("fetch_done", fetched=fetched, skipped=skipped, failed=failed)
    return failed == 0


def parse_page(html, url):
    """Main content as markdown with a source header. Returns (markdown, word_count)."""
    body = trafilatura.extract(html, output_format="markdown", include_links=True,
                               include_formatting=True, favor_recall=True) or ""
    meta = trafilatura.extract_metadata(html)
    title = (meta.title if meta and meta.title else url).strip()
    md = f"# {title}\n\nSource: {url}\nRetrieved: {datetime.now().date().isoformat()}\n\n{body.strip()}\n"
    return md, len(body.split())


def parse():
    PAGES_DIR.mkdir(exist_ok=True)
    rows = []
    for raw in sorted(RAW_DIR.glob("*.html")):
        slug = raw.stem
        url = BASE if slug == "home" else f"{BASE}/{slug}"
        md, words = parse_page(raw.read_text(encoding="utf-8"), url)
        if words < 30:
            log.warning("thin page %s: %d words", slug, words)
        (PAGES_DIR / f"{slug}.md").write_text(md, encoding="utf-8")
        rows.append((slug, words))
        event("parse_ok", slug=slug, words=words)
    log.info("parsed %d pages, %d words total", len(rows), sum(w for _, w in rows))
    for slug, words in rows:
        log.info("  %-28s %6d words", slug, words)


def main(step):
    setup_logging()
    event("run_start", step=step)
    if step in ("fetch", "all"):
        fetch()
    if step in ("parse", "all"):
        parse()
    event("run_end", step=step)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "all")
