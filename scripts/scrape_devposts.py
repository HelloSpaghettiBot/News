#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import json, re, time
from pathlib import Path
from datetime import datetime
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parents[1]
NEWS_PATH = ROOT / "news.json"

INDEX_URL = "https://www.endless-online.com/devposts.html"
DEVPOST_HREF_RE = re.compile(r"/devpost/(\d{4})\.html$")

MONTH_MAP = {
    "Jan":1,"January":1,"Feb":2,"February":2,"Mar":3,"March":3,"Apr":4,"April":4,"May":5,
    "Jun":6,"June":6,"Jul":7,"July":7,"Aug":8,"August":8,"Sep":9,"Sept":9,"September":9,
    "Oct":10,"Okt":10,"October":10,"Nov":11,"November":11,"Dec":12,"December":12,
}

HEADERS = {"User-Agent": "HelloSpaghettiBot-DevPostSync/1.1 (+github actions)"}

def fetch(url: str, retries: int = 4, backoff: float = 1.5) -> BeautifulSoup:
    last = None
    for i in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            print(f"[fetch] {url} -> {r.status_code}")
            r.raise_for_status()
            return BeautifulSoup(r.text, "html.parser")
        except Exception as e:
            last = e
            wait = backoff**i
            print(f"[fetch] retry {i+1}/{retries} after error: {e} (sleep {wait:.1f}s)")
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch {url}: {last}")

def parse_index():
    soup = fetch(INDEX_URL)
    results = []
    for a in soup.find_all("a", href=True):
        m = DEVPOST_HREF_RE.search(a["href"])
        if not m: 
            continue
        devpost_url = requests.compat.urljoin(INDEX_URL, a["href"])
        digits = m.group(1)              # e.g. 0447
        version = f"0.{int(digits[0])}.{int(digits[1:])}"  # -> 0.4.47

        parent_text = " ".join((a.parent.get_text(" ", strip=True) if a.parent else a.get_text(" ", strip=True)).split())
        date_iso = None
        date_match = re.search(r"(\d{1,2})\s([A-Za-z]+)\s(\d{4})", parent_text)
        if date_match:
            d = int(date_match.group(1)); mon_token = date_match.group(2); y = int(date_match.group(3))
            mon = MONTH_MAP.get(mon_token) or MONTH_MAP.get(mon_token[:3].title())
            if mon: date_iso = f"{y:04d}-{mon:02d}-{d:02d}"

        results.append({"url": devpost_url, "version": version, "date_iso": date_iso})

    def keyfn(e):
        return int(re.search(r"/devpost/(\d{4})\.html", e["url"]).group(1))
    results.sort(key=keyfn, reverse=True)
    print(f"[parse_index] found {len(results)} devpost links")
    return results

def first_nonempty_paragraphs(soup: BeautifulSoup, max_chars=600) -> str:
    h1 = soup.find(["h1","h2"], string=re.compile(r"Endless Online .* Dev Post", re.I))
    start = h1.find_next() if h1 else soup.body
    text_chunks, chars, node = [], 0, start
    while node and chars < max_chars:
        if getattr(node,"name",None) in ("p","div","section"):
            txt = node.get_text(" ", strip=True)
            if txt and len(txt) > 40 and "Privacy" not in txt and "Cookie" not in txt:
                text_chunks.append(txt); chars += len(txt)
        node = node.find_next() if hasattr(node,"find_next") else None
    snippet = "\n".join(text_chunks).strip()
    return snippet[:max_chars].rstrip()

def find_zip_links(soup: BeautifulSoup):
    out, seen = [], set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.lower().endswith(".zip"):
            url = requests.compat.urljoin(INDEX_URL, href)
            label = " ".join(a.get_text(" ", strip=True).split()) or url
            if url not in seen:
                seen.add(url)
                out.append((label, url))
    return out

def load_news():
    if NEWS_PATH.exists():
        try:
            with NEWS_PATH.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"[load_news] invalid JSON, starting fresh: {e}")
    return {"news": []}

def save_news(doc):
    with NEWS_PATH.open("w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")

def already_has(devpost_url: str, title: str, news_list):
    for item in news_list:
        if item.get("title") == title:
            return True
        if devpost_url and devpost_url in item.get("content",""):
            return True
    return False

def build_content(page_url: str, soup: BeautifulSoup) -> str:
    page_title_el = soup.find(["h1","h2"], string=re.compile(r"Endless Online .* Dev Post", re.I))
    page_title = page_title_el.get_text(" ", strip=True) if page_title_el else "Dev Post"
    intro = first_nonempty_paragraphs(soup, max_chars=600)
    zips = find_zip_links(soup)
    lines = [page_title]
    if intro: lines.append(intro)
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
    if not isinstance(news, list): news = []
    doc["news"] = news

    posts = parse_index()
    new_items = []
    for p in posts:
        url, version = p["url"], p["version"]
        title = f"Dev Post {version}"
        if already_has(url, title, news) or any(i.get("title")==title for i in new_items):
            continue
        try:
            page = fetch(url)
        except Exception as e:
            print(f"[main] skip {url}: {e}")
            continue  # do not fail whole job

        content = build_content(url, page)
        date_iso = p["date_iso"] or datetime.utcnow().strftime("%Y-%m-%d")
        new_items.append({"title": title, "content": content, "date": date_iso, "type": "dev post"})

    if not new_items:
        print("[main] no new items; exiting clean")
        return

    merged = new_items + news
    def dt_key(item):
        try: return datetime.strptime(item.get("date",""), "%Y-%m-%d")
        except: return datetime.min
    merged.sort(key=dt_key, reverse=True)
    doc["news"] = merged
    save_news(doc)
    print(f"[main] wrote news.json with {len(doc['news'])} total entries; added {len(new_items)} new")

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        # Print full error and still exit 1 so you can see the stack in Actions
        import traceback; traceback.print_exc()
        raise
