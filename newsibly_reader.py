#!/usr/bin/env python3
"""
Newsibly feed reader
====================

Reads Newsibly's National / Category page and extracts the latest article cards.
Because Newsibly appears to render its feed dynamically, this tool uses Playwright
(headless Chromium) first, then falls back to requests/BeautifulSoup.

Install:
    pip install playwright requests beautifulsoup4
    playwright install chromium

Usage:
    python newsibly_reader.py
    python newsibly_reader.py --url "https://newsibly.nz/?region=national&mode=category" --limit 10
    python newsibly_reader.py --headed   # useful for debugging

Output:
    JSON to stdout by default.

Notes:
- This does not invent article URLs. It only reports URLs actually exposed by the page.
- It records the page URL and timestamp so the result can be audited.
- If Newsibly changes its DOM/API, use --headed and inspect the saved HTML.
"""

import argparse
import json
import re
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

URL_DEFAULT = "https://newsibly.nz/?region=national&mode=category"
HOST = "newsibly.nz"

def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()

def valid_url(url):
    if not url:
        return False
    p = urlparse(url)
    return p.scheme in ("http", "https") and bool(p.netloc)

def extract_from_html(html, base_url, limit):
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return []

    soup = BeautifulSoup(html, "html.parser")
    results = []
    seen = set()

    # Prefer links that leave Newsibly for an original publisher.
    # Also accept Newsibly article links as a fallback.
    links = soup.find_all("a", href=True)

    for a in links:
        href = urljoin(base_url, a.get("href", ""))
        title = clean(a.get_text(" ", strip=True))
        if not valid_url(href) or not title:
            continue
        if href in seen:
            continue

        parsed = urlparse(href)
        same_host = parsed.netloc.endswith(HOST)

        # Ignore navigation/UI links.
        if title.lower() in {
            "latest", "top stories", "my region", "search", "apply filter",
            "clear filters", "raw feed", "home", "about", "login"
        }:
            continue

        # Avoid tiny UI labels.
        if len(title) < 18:
            continue

        # A news article link is usually either external or a Newsibly article path.
        looks_article = (not same_host) or any(
            token in parsed.path.lower()
            for token in ("/article", "/news", "/story", "/stories")
        )
        if not looks_article:
            continue

        # Find nearby metadata from the card/container.
        node = a
        container = None
        for _ in range(5):
            node = getattr(node, "parent", None)
            if node is None:
                break
            txt = clean(node.get_text(" ", strip=True))
            if 40 <= len(txt) <= 1200:
                container = node
                break

        card_text = clean(container.get_text(" ", strip=True)) if container else title

        results.append({
            "title": title,
            "url": href,
            "source": "" if same_host else parsed.netloc,
            "card_text": card_text,
        })
        seen.add(href)

        if len(results) >= limit:
            break

    return results

def playwright_read(url, limit, headed=False, wait_ms=5000):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, "Playwright is not installed."

    captured = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not headed, executable_path="/usr/bin/chromium")
        page = browser.new_page(
            viewport={"width": 1440, "height": 1600},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            ),
        )

        # Capture JSON responses; this can reveal the dynamic feed without guessing.
        def on_response(resp):
            ctype = (resp.headers.get("content-type") or "").lower()
            if "json" in ctype:
                captured.append(resp.url)

        page.on("response", on_response)
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(wait_ms)

        # Give lazy-loaded cards a chance to appear.
        for _ in range(4):
            page.mouse.wheel(0, 1800)
            page.wait_for_timeout(1000)

        html = page.content()
        page.screenshot(path="newsibly_debug.png", full_page=True)
        with open("newsibly_page.html", "w", encoding="utf-8") as f:
            f.write(html)

        browser.close()

    items = extract_from_html(html, url, limit)
    return {
        "items": items,
        "json_endpoints_observed": sorted(set(captured)),
        "html_file": "newsibly_page.html",
        "screenshot_file": "newsibly_debug.png",
    }, None

def requests_read(url, limit):
    try:
        import requests
    except ImportError:
        return None, "requests is not installed."

    try:
        r = requests.get(
            url,
            timeout=30,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/140.0 Safari/537.36"
                )
            },
        )
        r.raise_for_status()
        items = extract_from_html(r.text, url, limit)
        return {"items": items, "status_code": r.status_code}, None
    except Exception as e:
        return None, str(e)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default=URL_DEFAULT)
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    result = {
        "requested_url": args.url,
        "retrieved_at_utc": datetime.now(timezone.utc).isoformat(),
        "limit": args.limit,
        "method": None,
        "items": [],
        "json_endpoints_observed": [],
        "warnings": [],
    }

    pw, err = playwright_read(args.url, args.limit, args.headed)
    if pw and pw.get("items"):
        result["method"] = "playwright"
        result["items"] = pw["items"][:args.limit]
        result["json_endpoints_observed"] = pw.get("json_endpoints_observed", [])
        result["debug_files"] = {
            "html": pw["html_file"],
            "screenshot": pw["screenshot_file"],
        }
    else:
        if err:
            result["warnings"].append("Playwright: " + err)

        req, err2 = requests_read(args.url, args.limit)
        if req and req.get("items"):
            result["method"] = "requests"
            result["items"] = req["items"][:args.limit]
        else:
            if err2:
                result["warnings"].append("Requests: " + err2)
            result["warnings"].append(
                "No article cards were exposed. Newsibly may be rendering the feed "
                "through a client-side API. Run with --headed and inspect "
                "json_endpoints_observed/newsibly_page.html."
            )

    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
