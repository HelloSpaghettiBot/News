#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrapes https://www.endless-online.com/devposts.html (index of dev posts),
opens each /devpost/NNNN.html, and prepends new entries to news.json.

Updated to match actual structure:
- Index rows are simple "lines" with an <a href="/devpost/NNNN.html"> and
  trailing "Last Updated" date tokens like "4 Okt 2025" or "20 Sept 2025".
- Each post page has a heading like "Endless Online 0.4.47 : Dev Post",
  paragraphs of content, and optional .zip download links.

Idempotent:
- Dedupe by Dev Post URL and Title ("Dev Post X.Y.Z").
- Only commits if news.json changes.

Output "content" preserves your link style color (#ffcc66).
"""

from __future__ import annotations
import json
import re
from datetime import datetime
from pathlib import Path
import time

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
NEWS_PATH = ROOT / "news.json"

INDEX_URL = "https://www.endless-online.com/devposts.html"
DEVPOST_HREF_RE = re.compile(r"^/devpost/(\d{4})\.html$")

# Month tokens as seen on the index ("Okt", "Sept") plus normal English
MONTH_MAP = {
    "Jan": 1, "January": 1,
    "Feb": 2, "February": 2,
    "Mar": 3, "March": 3,
    "Apr": 4, "April": 4,
    "May": 5,
    "Jun": 6, "June": 6,
    "Jul": 7, "July": 7,
    "Aug": 8, "August": 8,
    "Sep": 9, "Sept": 9, "September": 9,
    "Oct": 10, "Okt": 10, "October": 10,
    "Nov": 11, "November": 11,
    "Dec": 12, "December": 12,
}

HEADERS = {"User-Agent": "HelloSpaghettiBot-DevPostSync/1.2 (+github actions)"}


def log(msg: str):
    print(f"[devposts] {msg}", flush=True)


def fetch_html(url: str, retries: int = 4, backoff: float = 1.6) -> BeautifulSoup:
    last_err = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            log(f"GET {url} -> {r.status_code}")
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            last_err = e
            wait = backoff ** i
            log(f"retry {i+1}/{retries} after error: {e} (sleep {wait:.1f}s)")
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def parse_date_from_line(text: str) -> str | None:
    """
    Parse '4 Okt 2025' / '20 Sept 2025' / '18 August 2025' etc. -> 'YYYY-MM-DD'
    """
    m = re.search(r"(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", text)
    if not m:
        return None
    d = int(m.group(1))
    token = m.group(2)
    y = int(m.group(3))
    mon = MONTH_MAP.get(token) or MONTH_MAP.get(token[:3].title())
    if not mon:
        return None
    return f"{y:04d}-{mon:02d}-{d:02d}"


def version_from_digits(digits: str) -> str:
    # '0447' -> '0.4.47'
    return f"0.{int(digits[0])}.{int(digits[1:])}"


def parse_index() -> list[dict]:
    soup = fetch_html(INDEX_URL)

    posts = []
    for a in soup.select("a[href^='/devpost/'][href$='.html']"):
        href = a.get("href", "")
        m = DEVPOST_HREF_RE.match(href)
        if not m:
            continue

        # Absolute URL
        url = requests.compat.urljoin(INDEX_URL, href)

        # Version from digits in href
        digits = m.group(1)  # e.g., 0447
        version = version_from_digits(digits)

        # The date is usually in the same "line"/parent's text
        line_text = a.parent.get_text(" ", strip=True) if a.parent else a.get_text(" ", strip=True)
        line_text = " ".join(line_text.split())
        date_iso = parse_date_from_line(line_text)

        posts.append({
            "url": url,
            "version": version,
            "date_iso": date_iso,
            "line_text": line_text,
        })

    # Newest first by numeric code
    def keyfn(entry):
        return int(re.search(r"/devpost/(\d{4})\.html", entry["url"]).group(1))

    posts.sort(key=keyfn, reverse=True)
    log(f"index: found {len(posts)} devpost links")
    return posts


def collect_intro(soup: BeautifulSoup, max_chars: int = 600) -> str:
    """
    Take first few substantial text blocks after the main heading.
    """
    h = soup.find(["h1", "h2"], string=re.compile(r"Endless Online .* Dev Post", re.I))
    node = h.find_next() if h else soup.body
    chunks, n = [], 0

    def ok(txt: str) -> bool:
        if not txt or len(txt) < 40:
            return False
        low = txt.lower()
        for bad in ("privacy", "cookie", "©", "terms"):
            if bad in low:
                return False
        return True

    while node and n < max_chars:
        if getattr(node, "name", None) in ("p", "div", "section"):
            txt = node.get_text(" ", strip=True)
            if ok(txt):
                chunks.append(txt)
                n += len(txt)
        node = node.find_next() if hasattr(node, "find_next") else None

    out = "\n".join(chunks).strip()
    return out[:max_chars].rstrip()


def find_zip_links(soup: BeautifulSoup) -> list[tuple[str, str]]:
    """
    Return list of (label, absolute_url) for .zip anchors.
    """
    seen = set()
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if not href.lower().endswith(".zip"):
            continue
        url = requests.compat.urljoin(INDEX_URL, href)
        if url in seen:
            continue
        seen.add(url)
        label = " ".join(a.get_text(" ", strip=True).split()) or url
        out.append((label, url))
    return out


def load_news() -> dict:
    if NEWS_PATH.exists():
        try:
            with NEWS_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log(f"news.json invalid; starting fresh: {e}")
    return {"news": []}


def save_news(doc: dict) -> None:
    with NEWS_PATH.open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")


def already_has(devpost_url: str, title: str, news_list: list[dict]) -> bool:
    for item in news_list:
        if item.get("title") == title:
            return True
        if devpost_url and devpost_url in item.get("content", ""):
            return True
    return False


def build_content(page_url: str, soup: BeautifulSoup) -> str:
    title_el = soup.find(["h1", "h2"], string=re.compile(r"Endless Online .* Dev Post", re.I))
    title_txt = title_el.get_text(" ", strip=True) if title_el else "Dev Post"
    intro = collect_intro(soup, max_chars=600)
    zips = find_zip_links(soup)

    lines = [title_txt]
    if intro:
        lines.append(intro)

    if zips:
        lines.append("Downloads:")
        for label, url in zips:
            a = f"<a href='{url}' style='color:#ffcc66;'>{url}</a>"
            bullet = f"- {label} — {a}" if label and label != url else f"- {a}"
            lines.append(bullet)

    lines.append(f"Dev Post URL: <a href='{page_url}' style='color:#ffcc66;'>{page_url}</a>")
    return "\n".join(lines)


def main():
    doc = load_news()
    news = doc.get("news", [])
    if not isinstance(news, list):
        news = []
    doc["news"] = news

    posts = parse_index()
    new_items = []

    for p in posts:
        url = p["url"]
        version = p["version"]
        title = f"Dev Post {version}"

        if already_has(url, title, news) or any(n.get("title") == title for n in new_items):
            continue

        try:
            page = fetch_html(url)
        except Exception as e:
            log(f"skip {url}: {e}")
            continue

        content = build_content(url, page)
        date_iso = p["date_iso"] or datetime.utcnow().strftime("%Y-%m-%d")

        new_items.append({
            "title": title,
            "content": content,
            "date": date_iso,
            "type": "dev post",
        })
        log(f"queued: {title} ({date_iso})")

    if not new_items:
        log("no new items; exit clean")
        return

    merged = new_items + news

    def dt_key(item: dict):
        try:
            return datetime.strptime(item.get("date", ""), "%Y-%m-%d")
        except Exception:
            return datetime.min

    merged.sort(key=dt_key, reverse=True)
    doc["news"] = merged

    save_news(doc)
    log(f"wrote news.json with {len(doc['news'])} total; added {len(new_items)} new")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise
