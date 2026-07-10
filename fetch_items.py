#!/usr/bin/env python3
"""
Step 1 of the pipeline: deterministic fetch + dedupe. No AI anywhere in this file.

What it does:
  1. Fetches competitor blog feeds (RSS/Atom, with autodiscovery and an HTML fallback)
  2. Searches Hacker News (Algolia API) and Reddit (public JSON) for brand/category mentions
  3. Dedupes against state.json so each run only emits genuinely NEW items
  4. Attaches a SUGGESTED priority from the keyword rules in config.json
     (a hint only; the AI review step makes the final call)
  5. Writes work/new_items_<timestamp>.json plus an audit log of every fetch, skip, and failure

Design rule: one broken source must never kill the run. Every source is wrapped
in its own try/except and lands in fetch_report, which becomes the digest's
pipeline-health footer.

No third-party dependencies. Python 3.10+.
"""

import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONFIG = json.loads((HERE / "config.json").read_text())
SETTINGS = CONFIG["settings"]

NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=SETTINGS["lookback_days"])
RUN_ID = NOW.strftime("%Y%m%d_%H%M")

WORK_DIR = HERE / SETTINGS["work_dir"]
WORK_DIR.mkdir(exist_ok=True)
LOG_PATH = WORK_DIR / f"fetch_log_{RUN_ID}.txt"


# ---------------------------------------------------------------- utilities

def log(msg: str) -> None:
    """Audit trail: everything notable goes to stdout AND a per-run log file,
    so 'why is this item in the digest?' is always answerable."""
    line = f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    with LOG_PATH.open("a") as f:
        f.write(line + "\n")


# Offline cache mode (env var DIGEST_HTTP_CACHE=path/to/cache_dir):
# serves every request from a local manifest.json + body files instead of the
# network. Exists for reproducible tests, and for sandboxed environments where
# this process can't reach the internet but an operator can fetch the sources
# through another channel and drop the responses in. Digest health entries are
# tagged "[offline cache]" whenever this mode is on, so provenance is never hidden.
CACHE_DIR = os.environ.get("DIGEST_HTTP_CACHE")
_MANIFEST = (json.loads((Path(CACHE_DIR) / "manifest.json").read_text())
             if CACHE_DIR else {})


def _cache_key(url: str) -> str:
    """The HN time filter changes every run, so normalize it for cache lookups."""
    return re.sub(r"created_at_i%3E\d+", "created_at_i%3E%2A", url)


def http_get(url: str) -> tuple[int, bytes]:
    """GET a URL with a proper User-Agent. The single network chokepoint for
    the whole pipeline, which is what keeps cache mode a 15-line feature."""
    if CACHE_DIR:
        entry = _MANIFEST.get(_cache_key(url))
        if entry is None:
            raise RuntimeError("not in offline cache")
        if entry.get("status", 200) != 200:
            raise RuntimeError(f"HTTP {entry['status']}")
        return 200, (Path(CACHE_DIR) / entry["file"]).read_bytes()
    req = urllib.request.Request(url, headers={"User-Agent": SETTINGS["user_agent"]})
    with urllib.request.urlopen(req, timeout=SETTINGS["request_timeout_seconds"]) as resp:
        return resp.status, resp.read()


def strip_html(text: str, limit: int = 400) -> str:
    """Turn an HTML fragment into a plain-text snippet."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def normalize_url(url: str) -> str:
    """Canonical form for dedupe. Tracking params get stripped so the same post
    shared with different utm tags doesn't reappear as 'new'."""
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query)
             if not k.lower().startswith(("utm_", "ref", "fbclid", "gclid"))]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc.lower(), parts.path.rstrip("/"),
         urllib.parse.urlencode(query), "")
    )


def parse_date(raw: str):
    """Feeds disagree on date formats: RSS uses RFC-822, Atom uses ISO-8601.
    Try both; return an aware datetime or None."""
    if not raw:
        return None
    for parser in (parsedate_to_datetime,
                   lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))):
        try:
            dt = parser(raw.strip())
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
    return None


# ---------------------------------------------------- deterministic pre-classifier

def suggest_priority(title: str, snippet: str) -> tuple[str, str]:
    """Apply the keyword rules from config.json. First match wins.
    This is only a HINT for the AI review step: cheap, transparent, editable.
    Misfires are expected and fine, because a reviewer sees every ranking."""
    haystack = f" {title} {snippet} ".lower()
    for level in ("high", "medium"):
        for rule in CONFIG["classification_rules"].get(level, []):
            for kw in rule["keywords"]:
                if kw.lower() in haystack:
                    return level.upper(), f"{rule['reason']} (matched '{kw.strip()}')"
    return CONFIG["classification_rules"].get("default", "LOW"), "No rule matched"


# ------------------------------------------------------------- RSS/Atom feeds

def _local(tag: str) -> str:
    """Element tag without its XML namespace: '{http://...}entry' -> 'entry'.
    Cheaper and more forgiving than registering every namespace variant."""
    return tag.rsplit("}", 1)[-1]


def _child_text(el, name: str) -> str:
    for c in el:
        if _local(c.tag) == name:
            return (c.text or "").strip()
    return ""


def parse_feed(body: bytes) -> list[dict]:
    """Parse RSS 2.0 or Atom into a uniform list of {title, url, date, snippet}."""
    root = ET.fromstring(body)
    items = []

    if _local(root.tag) == "rss":                     # ---- RSS 2.0
        channel = next((c for c in root if _local(c.tag) == "channel"), None)
        for item in (channel if channel is not None else []):
            if _local(item.tag) != "item":
                continue
            items.append({
                "title": _child_text(item, "title"),
                "url": _child_text(item, "link") or _child_text(item, "guid"),
                "date": parse_date(_child_text(item, "pubDate") or _child_text(item, "date")),
                "snippet": strip_html(_child_text(item, "description") or _child_text(item, "encoded")),
            })

    elif _local(root.tag) == "feed":                  # ---- Atom
        for entry in root:
            if _local(entry.tag) != "entry":
                continue
            links = [c for c in entry if _local(c.tag) == "link"]
            href = ""
            for l in links:                            # prefer rel="alternate"
                if l.get("rel", "alternate") == "alternate":
                    href = l.get("href", "")
                    break
            if not href and links:
                href = links[0].get("href", "")
            items.append({
                "title": _child_text(entry, "title"),
                "url": href,
                "date": parse_date(_child_text(entry, "published") or _child_text(entry, "updated")),
                "snippet": strip_html(_child_text(entry, "summary") or _child_text(entry, "content")),
            })
    return items


def looks_like_feed(body: bytes) -> bool:
    head = body[:2000].lower()
    return b"<rss" in head or b"<feed" in head or b"<rdf" in head


def discover_and_fetch_feed(source: dict) -> tuple[list[dict], str]:
    """Three escalating strategies, because real competitor blogs are messy:
    1. candidate feed URLs from config
    2. <link rel="alternate"> autodiscovery on the blog index page
    3. last resort: scrape article links off the index (items carry no dates)
    Returns (items, how_we_got_them) so the digest can show provenance."""
    tried = []

    # 1. Explicit candidates from config
    for url in source["feed_candidates"]:
        try:
            status, body = http_get(url)
            if status == 200 and looks_like_feed(body):
                return parse_feed(body), f"feed: {url}"
            tried.append(f"{url} (not a feed, HTTP {status})")
        except Exception as e:
            tried.append(f"{url} ({type(e).__name__})")

    # 2. Autodiscovery on the index page
    index_html = ""
    try:
        status, body = http_get(source["blog_index"])
        index_html = body.decode("utf-8", errors="replace")
        for link_tag in re.findall(r"<link[^>]+>", index_html, re.I):
            if re.search(r"application/(rss|atom)\+xml", link_tag, re.I):
                m = re.search(r'href=["\']([^"\']+)["\']', link_tag)
                if not m:
                    continue
                feed_url = urllib.parse.urljoin(source["blog_index"], m.group(1))
                try:
                    status, body = http_get(feed_url)
                    if status == 200 and looks_like_feed(body):
                        return parse_feed(body), f"autodiscovered feed: {feed_url}"
                except Exception as e:
                    tried.append(f"{feed_url} ({type(e).__name__})")
    except Exception as e:
        tried.append(f"index {source['blog_index']} ({type(e).__name__})")

    # 3. Fallback: scrape article links from the blog index (best-effort)
    if index_html:
        seen, items = set(), []
        for href, inner in re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                                      index_html, re.S | re.I):
            if "/blog/" not in href or href.rstrip("/").endswith("/blog"):
                continue
            url = urllib.parse.urljoin(source["blog_index"], href)
            title = strip_html(inner, 150)
            # short anchor text is nav junk ("Read more"), not an article title
            if url in seen or len(title) < 15:
                continue
            seen.add(url)
            items.append({"title": title, "url": url, "date": None,
                          "snippet": "(scraped from blog index — no feed available, no date)"})
            if len(items) >= 10:
                break
        if items:
            return items, "HTML-scrape fallback (no feed found)"

    raise RuntimeError("all strategies failed: " + "; ".join(tried[:6]))


# ------------------------------------------------------------------ Hacker News

def fetch_hackernews(query_cfg: dict) -> list[dict]:
    hn = CONFIG["hackernews"]
    params = urllib.parse.urlencode({
        "query": query_cfg["query"],
        "tags": "(story,comment)",
        "numericFilters": f"created_at_i>{int(CUTOFF.timestamp())}",
        "hitsPerPage": hn["hits_per_page"],
    })
    _, body = http_get(f"{hn['api']}?{params}")
    hits = json.loads(body).get("hits", [])

    items, words = [], query_cfg["query"].lower().split()
    context = [t.lower() for t in query_cfg.get("context_terms", [])]
    for hit in hits:
        text_blob = " ".join(filter(None, [
            hit.get("title"), hit.get("story_title"),
            strip_html(hit.get("comment_text") or "", 2000),
            strip_html(hit.get("story_text") or "", 2000), hit.get("url"),
        ])).lower()
        # Algolia matches fuzzily ('mirrord' returns 'mirrored'), so require the
        # literal words client-side, plus a context term for ambiguous queries.
        if not all(w in text_blob for w in words):
            continue
        if context and not any(t in text_blob for t in context):
            continue
        is_comment = "comment" in (hit.get("_tags") or [])
        items.append({
            "title": hit.get("title") or hit.get("story_title") or "(HN comment)",
            "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit['objectID']}",
            "hn_link": f"https://news.ycombinator.com/item?id={hit['objectID']}",
            "date": parse_date(hit.get("created_at")),
            "snippet": strip_html(hit.get("comment_text") or hit.get("story_text") or "", 400),
            "kind": "comment" if is_comment else "story",
            "_id": f"hn:{hit['objectID']}",
        })
    return items


# ---------------------------------------------------------------------- Reddit

def fetch_reddit(subreddit: str) -> list[dict]:
    r = CONFIG["reddit"]
    params = urllib.parse.urlencode({
        "q": r["search_query"], "restrict_sr": "on",
        "sort": "new", "t": r["time_window"], "limit": 25,
    })
    _, body = http_get(f"https://www.reddit.com/r/{subreddit}/search.json?{params}")
    posts = json.loads(body).get("data", {}).get("children", [])

    items = []
    for post in posts:
        d = post.get("data", {})
        created = datetime.fromtimestamp(d.get("created_utc", 0), tz=timezone.utc)
        if created < CUTOFF:
            continue
        items.append({
            "title": d.get("title", "(untitled)"),
            "url": "https://www.reddit.com" + d.get("permalink", ""),
            "date": created,
            "snippet": strip_html(d.get("selftext", ""), 400),
            "_id": f"reddit:{d.get('name', d.get('id', ''))}",
        })
    return items


# ------------------------------------------------------------------- main run

def main() -> None:
    state_path = HERE / SETTINGS["state_file"]
    state = json.loads(state_path.read_text()) if state_path.exists() else {"seen": {}}
    seen = state["seen"]

    new_items, report = [], []
    dup_count = 0
    max_items = SETTINGS["max_items_per_source"]

    def admit(raw_items: list[dict], source_name: str, source_type: str) -> int:
        """Shared gate for every source: sort newest first, apply the lookback
        window, dedupe against state, cap volume, attach the keyword hint."""
        nonlocal dup_count
        kept = 0
        # undated items (HTML fallback) sort last rather than being dropped
        raw_items.sort(key=lambda i: i["date"] or datetime.min.replace(tzinfo=timezone.utc),
                       reverse=True)
        for it in raw_items[: max_items]:
            if it["date"] and it["date"] < CUTOFF:
                continue                                # too old
            item_id = it.get("_id") or normalize_url(it["url"])
            if item_id in seen:
                dup_count += 1
                continue                                # already digested in a past run
            seen[item_id] = {"first_seen": NOW.isoformat(), "source": source_name}
            prio, why = suggest_priority(it["title"], it["snippet"])
            new_items.append({
                "id": item_id,
                "source": source_name,
                "source_type": source_type,
                "title": it["title"],
                "url": it["url"],
                "hn_link": it.get("hn_link"),
                "kind": it.get("kind"),
                "date": it["date"].date().isoformat() if it["date"] else None,
                "snippet": it["snippet"],
                "suggested_priority": prio,
                "suggested_reason": why,
            })
            kept += 1
        return kept

    # --- 1. Competitor blogs -------------------------------------------------
    for source in CONFIG["competitor_feeds"]:
        try:
            raw, method = discover_and_fetch_feed(source)
            kept = admit(raw, source["name"], "blog")
            report.append({"source": source["name"], "status": "ok",
                           "detail": method, "fetched": len(raw), "new": kept})
            log(f"OK   {source['name']}: {len(raw)} fetched, {kept} new ({method})")
        except Exception as e:
            report.append({"source": source["name"], "status": "failed",
                           "detail": str(e)[:300], "fetched": 0, "new": 0})
            log(f"FAIL {source['name']}: {e}")

    # --- 2. Hacker News ------------------------------------------------------
    for qcfg in CONFIG["hackernews"]["queries"]:
        label = f"HN: \"{qcfg['query']}\""
        try:
            raw = fetch_hackernews(qcfg)
            kept = admit(raw, label, "hackernews")
            report.append({"source": label, "status": "ok",
                           "detail": "Algolia API", "fetched": len(raw), "new": kept})
            log(f"OK   {label}: {len(raw)} relevant, {kept} new")
        except Exception as e:
            report.append({"source": label, "status": "failed",
                           "detail": str(e)[:300], "fetched": 0, "new": 0})
            log(f"FAIL {label}: {e}")

    # --- 3. Reddit (often blocks unauthenticated clients — degrade, don't die)
    for sub in CONFIG["reddit"]["subreddits"]:
        label = f"Reddit: r/{sub}"
        try:
            raw = fetch_reddit(sub)
            kept = admit(raw, label, "reddit")
            report.append({"source": label, "status": "ok",
                           "detail": "public JSON endpoint", "fetched": len(raw), "new": kept})
            log(f"OK   {label}: {len(raw)} fetched, {kept} new")
        except urllib.error.HTTPError as e:
            detail = (f"HTTP {e.code} — Reddit blocks unauthenticated requests from this "
                      f"network; digest degrades gracefully (add OAuth creds to fix)")
            report.append({"source": label, "status": "blocked", "detail": detail,
                           "fetched": 0, "new": 0})
            log(f"BLOCKED {label}: {detail}")
        except Exception as e:
            report.append({"source": label, "status": "failed",
                           "detail": str(e)[:300], "fetched": 0, "new": 0})
            log(f"FAIL {label}: {e}")
        time.sleep(CONFIG["reddit"]["seconds_between_requests"])

    # --- 4. Persist ----------------------------------------------------------
    if CACHE_DIR:  # full transparency in the digest's health footer
        for r in report:
            r["detail"] += " [offline cache]"

    out = {
        "run_at": NOW.isoformat(),
        "lookback_days": SETTINGS["lookback_days"],
        "duplicates_skipped": dup_count,
        "fetch_report": report,
        "items": new_items,
    }
    out_path = WORK_DIR / f"new_items_{RUN_ID}.json"
    out_path.write_text(json.dumps(out, indent=2))
    state_path.write_text(json.dumps(state, indent=2))

    ok = sum(1 for r in report if r["status"] == "ok")
    log(f"DONE {len(new_items)} new items | {dup_count} duplicates skipped | "
        f"{ok}/{len(report)} sources healthy")
    log(f"Wrote {out_path.name} — next step: AI review writes classified_{RUN_ID}.json, "
        f"then run make_digest.py")
    print(str(out_path))  # machine-readable last line for schedulers/orchestrators


if __name__ == "__main__":
    sys.exit(main())
