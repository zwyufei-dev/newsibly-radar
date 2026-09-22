#!/usr/bin/env python3

import argparse
import json
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse


URL_DEFAULT = "https://newsibly.nz/?mode=latest"
HOST = "newsibly.nz"


def clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def valid_url(url):
    if not url:
        return False

    p = urlparse(url)

    return (
        p.scheme in ("http", "https")
        and bool(p.netloc)
    )


def extract_from_html(html, base_url, limit):
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    results = []
    seen = set()

    ignored_titles = {
        "latest",
        "top stories",
        "my region",
        "search",
        "apply filter",
        "clear filters",
        "raw feed",
        "home",
        "about",
        "login",
    }

    for a in soup.find_all("a", href=True):

        href = urljoin(
            base_url,
            a.get("href", "")
        )

        title = clean(
            a.get_text(" ", strip=True)
        )

        if not valid_url(href):
            continue

        if not title:
            continue

        if href in seen:
            continue

        if title.lower() in ignored_titles:
            continue

        if len(title) < 18:
            continue

        parsed = urlparse(href)

        same_host = parsed.netloc.endswith(HOST)

        looks_article = (
            not same_host
            or any(
                token in parsed.path.lower()
                for token in (
                    "/article",
                    "/news",
                    "/story",
                    "/stories",
                )
            )
        )

        if not looks_article:
            continue

        node = a
        container = None

        for _ in range(5):

            node = getattr(
                node,
                "parent",
                None
            )

            if node is None:
                break

            txt = clean(
                node.get_text(
                    " ",
                    strip=True
                )
            )

            if 40 <= len(txt) <= 1200:
                container = node
                break

        results.append(
            {
                "rank": len(results) + 1,
                "title": title,
                "url": href,
                "source": (
                    ""
                    if same_host
                    else parsed.netloc
                ),
                "card_text": (
                    clean(
                        container.get_text(
                            " ",
                            strip=True
                        )
                    )
                    if container
                    else title
                ),
            }
        )

        seen.add(href)

        if len(results) >= limit:
            break

    return results


def read_with_playwright(
    url,
    limit,
    headed=False
):

    from playwright.sync_api import sync_playwright

    json_endpoints = []

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=not headed
        )

        page = browser.new_page(
            viewport={
                "width": 1440,
                "height": 1800,
            },
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/140.0 Safari/537.36"
            ),
        )

        def on_response(resp):

            ctype = (
                resp.headers
                .get("content-type", "")
                .lower()
            )

            if "json" in ctype:
                json_endpoints.append(
                    resp.url
                )

        page.on(
            "response",
            on_response
        )

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=60000,
        )

        page.wait_for_timeout(8000)

        # 多滚几次，让 Latest 动态加载更多新闻。
        for _ in range(10):

            page.mouse.wheel(
                0,
                1800
            )

            page.wait_for_timeout(800)

        html = page.content()

        items = extract_from_html(
            html,
            url,
            limit
        )

        browser.close()

    if not items:
        raise RuntimeError(
            "Newsibly page loaded but no article items were extracted."
        )

    return (
        items,
        sorted(
            set(json_endpoints)
        ),
    )


def load_json(
    path,
    default
):

    p = Path(path)

    if not p.exists():
        return default

    try:

        with p.open(
            "r",
            encoding="utf-8"
        ) as f:

            return json.load(f)

    except Exception:

        return default


def save_json(
    path,
    payload
):

    tmp = Path(
        str(path) + ".tmp"
    )

    with tmp.open(
        "w",
        encoding="utf-8"
    ) as f:

        json.dump(
            payload,
            f,
            ensure_ascii=False,
            indent=2
        )

    tmp.replace(path)


def update_archive(
    archive_path,
    new_items,
    now,
    hours=24
):

    existing_payload = load_json(
        archive_path,
        {
            "items": []
        }
    )

    existing = existing_payload.get(
        "items",
        []
    )

    cutoff = (
        now
        - timedelta(hours=hours)
    )

    by_url = {}

    # 保留过去24小时内的数据
    for item in existing:

        url = item.get("url")

        ts = item.get(
            "discovered_at_utc"
        )

        if not url or not ts:
            continue

        try:

            discovered = datetime.fromisoformat(
                ts.replace(
                    "Z",
                    "+00:00"
                )
            )

        except Exception:

            continue

        if discovered >= cutoff:

            by_url[url] = item

    # 加入本次抓到的新新闻
    for item in new_items:

        record = dict(item)

        record["discovered_at_utc"] = (
            now.isoformat()
        )

        by_url[
            record["url"]
        ] = record

    items = sorted(
        by_url.values(),
        key=lambda x: x.get(
            "discovered_at_utc",
            ""
        ),
        reverse=True,
    )

    payload = {
        "source": "Newsibly",
        "window": "rolling_24_hours",
        "updated_at_utc": (
            now.isoformat()
        ),
        "count": len(items),
        "items": items,
    }

    save_json(
        archive_path,
        payload
    )

    return len(items)


def main():

    ap = argparse.ArgumentParser()

    ap.add_argument(
        "--url",
        default=URL_DEFAULT
    )

    ap.add_argument(
        "--limit",
        type=int,
        default=50
    )

    ap.add_argument(
        "--save",
        default="newsibly_latest50.json"
    )

    ap.add_argument(
        "--archive",
        default="newsibly_archive24h.json"
    )

    ap.add_argument(
        "--headed",
        action="store_true"
    )

    args = ap.parse_args()

    now = datetime.now(
        timezone.utc
    )

    try:

        items, endpoints = (
            read_with_playwright(
                args.url,
                args.limit,
                args.headed
            )
        )

    except Exception as exc:

        print(
            "Newsibly read failed; "
            "preserving previous files: "
            f"{exc}",
            file=sys.stderr
        )

        sys.exit(1)

    payload = {
        "source": "Newsibly",
        "feed_url": args.url,
        "mode": "latest",
        "retrieved_at_utc": (
            now.isoformat()
        ),
        "count": len(items),
        "items": items,
        "json_endpoints_observed": (
            endpoints
        ),
    }

    # 最新50条
    save_json(
        args.save,
        payload
    )

    # 更新24小时滚动池
    archive_count = update_archive(
        args.archive,
        items,
        now,
        hours=24
    )

    print(
        json.dumps(
            {
                "status": "success",
                "latest_count": len(items),
                "archive24h_count": archive_count,
                "latest_file": args.save,
                "archive_file": args.archive,
                "retrieved_at_utc": (
                    now.isoformat()
                ),
            },
            ensure_ascii=False,
            indent=2
        )
    )


if __name__ == "__main__":
    main()
