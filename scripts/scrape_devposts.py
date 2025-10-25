#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Scrapes https://www.endless-online.com/devposts.html, fetches each linked
/devpost/NNNN.html page, and prepends new "dev post" entries into news.json
in the exact HTML-rich format you’ve been using.

Rules:
- Title: "Dev Post {major.minor.patch}"
- Date: ISO "YYYY-MM-DD" parsed from the index's "Last Updated" column
- Type: "dev post"
- Content:
    "{page_h1}\n{intro_snippet}\nDownloads:\n- {zip_1} — <a href='URL' style='color:#ffcc66;'>URL</a>\nDev Post URL: <a href='DEVPOST_URL' style='color:#ffcc66;'>DEVPOST_URL</a>"
  If no zips are found, the Downloads block is omitted.

Idempotent:
- We dedupe by Dev Post URL (preferred) and by Title.
"""

import json
import re
from pathlib import Path
from datetime import datetime

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
NEWS_PATH = ROOT / "news.json"

INDEX_URL = "https://www.endless-online.com/devposts.html"
DEVPOST_HREF_RE = re.compile(r"/devpost/(\d{4})\.html$")

# Map non-English month abbreviations seen on the index to month numbers
# Examples on index: "4 Okt 2025", "20 Sept 2025" etc.
MONTH_MAP = {
    # English + common variants
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

HEADERS = {"User-Agent": "HelloSpaghettiBot-DevPostSync/1.0 (+github actions)"}


def fetch(url: str) -> BeautifulSoup:
    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    return BeautifulSoup(r.text, "html.parser")


def parse_index():
    """
    Returns a list of dicts:
    [{
      'url': 'https://www.endless-online.com/devpost/0447.html',
      'version': '0.4.47',
      'date_iso': '2025-10-04'
    }, ...] newest-first.
    """
    soup = fetch(INDEX_URL)

    results = []
    for a in soup.find_all("a", href=True):
        m = DEVPOST_HREF_RE.search(a["href"])
        if not m:
            continue

        # Build absolute URL
        devpost_url = requests.compat.urljoin(INDEX_URL, a["href"])

        # Version can be pulled from link text or from href digits
        # href "0447.html" -> "0.4.47"
        digits = m.group(1)
        version = f"0.{int(digits[0])}.{int(digits[1:])}"

        # Try to find a readable date near the link text (within same line / parent)
        date_iso = None
        parent_text = " ".join(a.parent.get_text(" ", strip=True).split()) if a.parent else a.get_text(" ", strip=True)
        # Looks like "... 0.4.47  4 Okt 2025"
        date_match = re.search(r"(\d{1,2})\s([A-Za-z]+)\s(\d{4})", parent_text)
        if date_match:
            d = int(date_match.group(1))
            mon_token = date_match.group(2)
            y = int(date_match.group(3))
            mon = MONTH_MAP.get(mon_token, None)
            if mon is None:
                # Fallback: try first 3 letters capitalized
                mon = MONTH_MAP.get(mon_token[:3].title(), None)
            if mon:
                date_iso = f"{y:04d}-{mon:02d}-{d:02d}"

        # If we couldn't parse a date, leave None (the writer will skip/keep empty)
        results.append({
            "url": devpost_url,
            "version": version,
            "date_iso": date_iso,
        })

    # latest ones are typically higher numbers; sort by URL numeric code descending
    def keyfn(entry):
        code = int(re.search(r"/devpost/(\d{4})\.html", entry["url"]).group(1))
        return code

    results.sort(key=keyfn, reverse=True)
    return results


def first_nonempty_paragraphs(soup: BeautifulSoup, max_chars=500) -> str:
    """
    Grab a short intro snippet from the dev post page.
    Strategy: accumulate text from the first few non-empty blocks (<p>, bare text nodes),
    skipping nav/footer and stopping once ~max_chars is reached.
    """
    h1 = soup.find(["h1", "h2"], string=re.compile(r"Endless Online .* Dev Post", re.I))
    # Start scanning after the heading if possible
    start = h1.find_next() if h1 else soup.body

    text_chunks = []
    chars = 0
    node = start

    while node and chars < max_chars:
        # pick textlike elements
        if getattr(node, "name", None) in ("p", "div", "section"):
            txt = node.get_text(" ", strip=True)
            # heuristics: skip super short or navigation-y lines
            if txt and len(txt) > 40 and "© by" not in txt and "Privacy Policy" not in txt:
                text_chunks.append(txt)
                chars += len(txt)
        node = node.find_next() if hasattr(node, "find_next") else None

    snippet = "\n".join(text_chunks)
    snippet = re.sub(r"\s+\n", "\n", snippet).strip()
    return snippet[:max_chars].rstrip()


def find_zip_links(soup: BeautifulSoup):
    """
    Find any .zip links present on the page and return list of (label, url).
    """
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".zip"):
            url = requests.compat.urljoin(INDEX_URL, href)
            label = " ".join(a.get_text(" ", strip=True).split()) or url
            out.append((label, url))
    # de-dupe preserving order
    seen = set()
    uniq = []
    for label, url in out:
        if url in seen:
            continue
        seen.add(url)
        uniq.append((label, url))
    return uniq


def load_news():
    if NEWS_PATH.exists():
        try:
            with NEWS_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {"news": []}


def save_news(doc):
    # pretty & stable keys
    with NEWS_PATH.open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")


def already_has(devpost_url: str, title: str, news_list):
    for item in news_list:
        if item.get("title") == title:
            return True
        content = item.get("content", "")
        if devpost_url in content:
            return True
    return False


def build_content(page_url: str, soup: BeautifulSoup) -> str:
    # Title line as printed on the page (e.g., "Endless Online 0.4.47 : Dev Post")
    page_title_el = soup.find(["h1", "h2"], string=re.compile(r"Endless Online .* Dev Post", re.I))
    page_title = page_title_el.get_text(" ", strip=True) if page_title_el else "Dev Post"

    intro = first_nonempty_paragraphs(soup, max_chars=600)

    zips = find_zip_links(soup)
    content_lines = [page_title]
    if intro:
        content_lines.append(intro)

    if zips:
        content_lines.append("Downloads:")
        for label, url in zips:
            # keep their style color like your example
            a = f"<a href='{url}' style='color:#ffcc66;'>{url}</a>"
            # include the label if present
            bullet = f"- {label} — {a}" if label and label != url else f"- {a}"
            content_lines.append(bullet)

    # always append the source link
    content_lines.append(
        f"Dev Post URL: <a href='{page_url}' style='color:#ffcc66;'>{page_url}</a>"
    )
    return "\n".join(content_lines)


def main():
    # 1) Load current news.json
    doc = load_news()
    news = doc.get("news", [])
    if not isinstance(news, list):
        news = []
    doc["news"] = news

    # 2) Parse index for post list
    posts = parse_index()

    # 3) For each dev post, if not present, fetch & add
    new_items = []
    for p in posts:
        url = p["url"]
        version = p["version"]
        title = f"Dev Post {version}"

        if already_has(url, title, news) or any(it.get("title") == title for it in new_items):
            continue

        page = fetch(url)
        content = build_content(url, page)
        date_iso = p["date_iso"] or datetime.utcnow().strftime("%Y-%m-%d")

        new_items.append({
            "title": title,
            "content": content,
            "date": date_iso,
            "type": "dev post",
        })

    if not new_items:
        # nothing new
        return

    # 4) Prepend new dev posts, keep others, then sort all "news" by date desc where possible
    merged = new_items + news

    def dt_key(item):
        try:
            return datetime.strptime(item.get("date", ""), "%Y-%m-%d")
        except Exception:
            # push undated items to bottom
            return datetime.min

    merged.sort(key=dt_key, reverse=True)
    doc["news"] = merged

    # 5) Write file
    save_news(doc)


if __name__ == "__main__":
    main()
