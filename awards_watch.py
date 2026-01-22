import json
import os
import re
import time
import hashlib
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup

UA = "Mozilla/5.0 (compatible; AwardsWatchAI/1.0; +github-actions)"
TIMEOUT = 20

OUT_DIR = "docs"
OUT_HTML = os.path.join(OUT_DIR, "index.html")
STATE_FILE = "state_seen.json"  # committed so Pages always matches what you've seen

# --- Lightweight classifier (your “AI”) ---
AWARD_TERMS = [
    "oscar", "oscars", "academy award", "academy awards",
    "bafta", "emmy", "golden globe", "golden globes",
    "sag", "dga", "pga", "wga", "guild",
    "nominee", "nominees", "nomination", "nominations",
    "shortlist", "longlist", "winner", "winners", "wins",
    "for your consideration", "fyc", "campaign", "contender", "awards season",
    "critics choice", "cannes", "venice", "berlin", "sundance", "telluride", "tiff"
]

NEGATIVE_HINTS = ["trailer", "teaser", "box office", "first look", "poster"]

def score_item(text: str) -> tuple[float, list[str]]:
    t = (text or "").lower()
    score = 0.0
    hits = []

    for kw in AWARD_TERMS:
        if kw in t:
            hits.append(kw)
            score += 1.0

    strong = ["nominations", "nominees", "shortlist", "longlist", "winners", "wins", "eligibility", "rules", "deadline"]
    for kw in strong:
        if kw in t:
            score += 0.6

    for bad in NEGATIVE_HINTS:
        if bad in t:
            score -= 0.8

    score = max(0.0, min(score, 10.0))
    hits = list(dict.fromkeys(hits))[:10]
    return score, hits

def stable_id(url: str, title: str) -> str:
    return hashlib.sha256((url.strip() + "|" + (title or "").strip()).encode("utf-8")).hexdigest()[:24]

def load_sources():
    with open("sources.json", "r", encoding="utf-8") as f:
        return json.load(f)

def load_seen():
    if not os.path.exists(STATE_FILE):
        return set()
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return set(json.load(f))

def save_seen(seen: set[str]):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(seen), f, indent=2)

def fetch(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def parse_rss(rss_url: str, limit: int = 40):
    feed = feedparser.parse(rss_url)
    items = []
    for e in feed.entries[:limit]:
        title = getattr(e, "title", "").strip()
        link = getattr(e, "link", "").strip()
        summary_html = getattr(e, "summary", "") or getattr(e, "description", "") or ""
        summary = BeautifulSoup(summary_html, "html.parser").get_text(" ", strip=True)

        published = ""
        if getattr(e, "published_parsed", None):
            published = datetime.fromtimestamp(time.mktime(e.published_parsed), tz=timezone.utc).isoformat()

        if title and link:
            items.append({"title": title, "url": link, "summary": summary, "published": published})
    return items

def parse_listing(page_url: str, limit: int = 50):
    html = fetch(page_url)
    soup = BeautifulSoup(html, "html.parser")
    site_host = urlparse(page_url).netloc

    found = []
    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        title = " ".join(a.get_text(" ", strip=True).split())
        if len(title) < 18:
            continue

        full = href if href.startswith("http") else urljoin(page_url, href)
        if not full.startswith("http"):
            continue

        # keep same-domain links
        if urlparse(full).netloc != site_host:
            continue

        # avoid obvious non-article links
        bad = ["privacy", "terms", "account", "login", "subscribe", "newsletter", "contact", "about"]
        if any(b in full.lower() for b in bad):
            continue

        # attempt blurb from nearby <p>
        blurb = ""
        p = a.find_parent().find("p") if a.find_parent() else None
        if p:
            blurb = " ".join(p.get_text(" ", strip=True).split())

        found.append({"title": title, "url": full, "summary": blurb, "published": ""})

    # dedupe by url
    dedup = []
    seen = set()
    for it in found:
        if it["url"] in seen:
            continue
        seen.add(it["url"])
        dedup.append(it)

    return dedup[:limit]

def html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def build_html(items, generated_at, min_score):
    rows = []
    for it in items:
        score = it["score"]
        badge = f"{score:.1f}"
        hits = ", ".join(it["hits"])
        rows.append(f"""
        <div class="card">
          <div class="top">
            <div class="title"><a href="{html_escape(it['url'])}" target="_blank" rel="noreferrer">{html_escape(it['title'])}</a></div>
            <div class="score">{badge}</div>
          </div>
          <div class="meta">
            <span class="source">{html_escape(it['source'])}</span> ·
            <span>{html_escape(it['type'])}</span> ·
            <span>Nom: {('Y' if it['affects_nom'] else 'N')}</span> ·
            <span>Win: {('Y' if it['affects_win'] else 'N')}</span>
            {" · <span class='pub'>" + html_escape(it['published']) + "</span>" if it['published'] else ""}
          </div>
          {"<div class='summary'>" + html_escape(it['summary']) + "</div>" if it['summary'] else ""}
          {"<div class='hits'>Signals: " + html_escape(hits) + "</div>" if hits else ""}
        </div>
        """)

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Awards Watch AI Digest</title>
  <style>
    body {{ font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial; margin: 24px; background:#0b0c10; color:#e5e7eb; }}
    a {{ color: #93c5fd; text-decoration: none; }}
    a:hover {{ text-decoration: underline; }}
    .header {{ display:flex; justify-content:space-between; align-items:baseline; gap:16px; flex-wrap:wrap; }}
    .muted {{ color:#9ca3af; }}
    .grid {{ display:grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap:16px; margin-top:18px; }}
    .card {{ background:#111827; border:1px solid #1f2937; border-radius:14px; padding:14px; }}
    .top {{ display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }}
    .title {{ font-weight:700; line-height:1.25; }}
    .score {{ font-weight:800; background:#0f172a; border:1px solid #1f2937; padding:4px 10px; border-radius:999px; }}
    .meta {{ font-size:12px; margin-top:8px; color:#cbd5e1; }}
    .summary {{ margin-top:10px; color:#e5e7eb; font-size:14px; }}
    .hits {{ margin-top:10px; font-size:12px; color:#a7f3d0; }}
    .footer {{ margin-top:24px; font-size:12px; color:#9ca3af; }}
    .pill {{ display:inline-block; padding:4px 10px; border-radius:999px; border:1px solid #1f2937; background:#0f172a; }}
  </style>
</head>
<body>
  <div class="header">
    <h1 style="margin:0;">Awards Watch AI Digest</h1>
    <div class="muted">Generated: {html_escape(generated_at)}</div>
  </div>
  <div class="muted">
    <span class="pill">Min score: {min_score:.1f}</span>
    <span class="pill">Items: {len(items)}</span>
  </div>

  <div class="grid">
    {''.join(rows) if rows else "<div class='muted'>No items matched your threshold.</div>"}
  </div>

  <div class="footer">
    Tip: raise min score to focus on nominations/winners; lower it for campaign/trade chatter.
  </div>
</body>
</html>
"""

def main():
    sources = load_sources()
    seen = load_seen()

    min_score = float(os.environ.get("MIN_SCORE", "3.5"))
    mark_seen = os.environ.get("MARK_SEEN", "0") == "1"  # optional: auto-mark shown items as seen

    all_items = []
    errors = []

    for s in sources:
        try:
            if s.get("rss"):
                items = parse_rss(s["rss"])
            else:
                items = parse_listing(s["url"])
        except Exception as e:
            errors.append(f"{s['name']}: {e}")
            continue

        for it in items:
            sid = stable_id(it["url"], it["title"])
            if sid in seen:
                continue

            text = f"{it['title']} {it.get('summary','')}"
            score, hits = score_item(text)

            if score < min_score:
                continue

            all_items.append({
                **it,
                "id": sid,
                "score": score,
                "hits": hits,
                "source": s["name"],
                "type": s["type"],
                "affects_nom": bool(s["affects_nom"]),
                "affects_win": bool(s["affects_win"]),
            })

            if mark_seen:
                seen.add(sid)

    # sort: highest score, then published
    def pub_key(p):
        try:
            return datetime.fromisoformat(p.replace("Z", "+00:00"))
        except Exception:
            return datetime(1970,1,1,tzinfo=timezone.utc)

    all_items.sort(key=lambda x: (x["score"], pub_key(x.get("published",""))), reverse=True)

    os.makedirs(OUT_DIR, exist_ok=True)
    generated_at = datetime.now(timezone.utc).isoformat()
    html = build_html(all_items, generated_at, min_score)
    with open(OUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)

    # save state
    save_seen(seen)

    if errors:
        print("Some sources failed:\n" + "\n".join(errors))

if __name__ == "__main__":
    main()
