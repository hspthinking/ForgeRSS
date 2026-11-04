#!/usr/bin/env python3
r"""IT home account adapter for ForgeRSS. Place beside requirements.txt.

Run: .\.venv\Scripts\python.exe run_ithome.py --max 3
Uses a separate temporary Chrome session; no personal profile or cookies.
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))


def article_url(value: str) -> bool:
    parsed = urlparse(value)
    return (parsed.scheme == "https" and parsed.hostname == "www.ithome.com"
            and re.fullmatch(r"/\d+/\d+/\d+\.htm", parsed.path) is not None)


def open_article(page, row, timeout=30):
    """Click the whole article card, allowing either a new tab or same-tab navigation."""
    previous_tabs = set(page.tab_ids)
    if row.click(timeout=5) is False:
        raise RuntimeError("Article card could not be clicked.")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for tab_id in set(page.tab_ids) - previous_tabs:
            tab = page.get_tab(tab_id)
            if article_url(tab.url):
                return tab, True
        if article_url(page.url):
            return page, False
        time.sleep(0.25)
    raise RuntimeError(
        f"Click did not reach an IT article within {timeout}s. "
        f"Current page: {page.url}; open tabs: {len(page.tab_ids)}."
    )


def clean_body(markup: str, url: str) -> str:
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(markup, "html.parser")
    for node in soup.select("script, style, iframe, form, input, button, object, embed"):
        node.decompose()
    for node in soup.find_all(True):
        for key in list(node.attrs):
            if key.lower().startswith("on") or key.lower() in {"srcdoc", "style"}:
                del node.attrs[key]
        for key in ("src", "href"):
            value = node.get(key)
            if value:
                resolved = urljoin(url, value.strip())
                if urlparse(resolved).scheme not in {"http", "https"}:
                    del node.attrs[key]
                else:
                    node[key] = resolved
        if node.name == "img":
            original = node.get("data-original") or node.get("data-src")
            if original:
                resolved = urljoin(url, original)
                if urlparse(resolved).scheme in {"http", "https"}:
                    node["src"] = resolved
            for key in ("data-original", "data-src", "srcset"):
                node.attrs.pop(key, None)
    return str(soup)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate the IT early-news RSS with ForgeRSS")
    parser.add_argument("--max", type=int, default=3, choices=range(1, 21), metavar="1-20")
    parser.add_argument("--headless", action="store_true", help="Use headless Chrome, for CI testing")
    parser.add_argument("--chrome", help="Optional full path to chrome.exe")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log = logging.getLogger("ithome")
    if not (ROOT / "generators" / "base.py").is_file():
        print("ERROR: Place run_ithome.py in the ForgeRSS root, beside requirements.txt.")
        return 1
    try:
        import pytz
        from DrissionPage import ChromiumOptions, ChromiumPage
        from generators.base import Article, BaseFeedGenerator
    except ImportError as exc:
        print(f"ERROR: Missing dependency: {exc}. Run pip install -r requirements.txt using .venv Python.")
        return 1

    class ITHomeAccountGenerator(BaseFeedGenerator):
        FEED_NAME = "ithome_account_10233"
        FEED_TITLE = "IT早报 - IT之家"
        FEED_URL = "https://mp.ithome.com/account/#/10233"
        FEED_DESCRIPTION = "IT早报账号文章订阅；由 ForgeRSS 生成。"
        FEED_LANGUAGE = "zh-CN"

        def fetch_articles(self):
            options = ChromiumOptions().auto_port()
            options.set_timeouts(base=30, page_load=60)
            # The account website opens articles with window.open().
            options.set_argument("--disable-popup-blocking")
            if args.headless:
                options.headless()
            if args.chrome:
                options.set_browser_path(args.chrome)
            # Direct Chrome control; no Selenium Manager or ChromeDriver download.
            # auto_port() uses a separate temporary browser profile.
            page = ChromiumPage(options)
            articles = []
            try:
                # The account frontend formats dates in the browser timezone.
                page.run_cdp("Emulation.setTimezoneOverride", timezoneId="Asia/Shanghai")
                log.info("Loading IT early-news account page...")
                page.get(self.FEED_URL)
                if not page.ele("css:.post.single .title", timeout=30):
                    raise RuntimeError("Account list did not load within 30 seconds.")
                total = min(args.max, len(page.eles("css:.post.single")))
                if total == 0:
                    raise RuntimeError("No articles found on account page; feed was not updated.")
                for index in range(total):
                    detail = None
                    new_tab = False
                    try:
                        rows = page.eles("css:.post.single")
                        row = rows[index]
                        title_ele = row.ele("css:.title")
                        title = title_ele.text.strip()
                        date_text = row.ele("css:.time").text.strip()
                        published = pytz.timezone("Asia/Shanghai").localize(
                            datetime.strptime(date_text, "%Y-%m-%d %H:%M")
                        ).astimezone(pytz.UTC)
                        log.info("Opening article %s/%s...", index + 1, total)
                        detail, new_tab = open_article(page, row)
                        heading = detail.ele("css:h1", timeout=30)
                        if not heading:
                            raise RuntimeError("Article heading did not load.")
                        url = detail.url.split("#", 1)[0].split("?", 1)[0]
                        if not article_url(url):
                            raise RuntimeError(f"Unexpected article URL: {url}")
                        full_title = heading.text.strip() or title
                        body = detail.ele("css:#paragraph", timeout=30)
                        if not body or len(body.text.strip()) <= 30:
                            raise RuntimeError("Article body is missing or unexpectedly short.")
                        content = clean_body(body.inner_html, url)
                        articles.append(Article(url=url, title=full_title, published_at=published,
                                                content=content, author="IT早报", category="科技早报"))
                        log.info("Fetched %s/%s: %s", index + 1, total, url)
                    except Exception:
                        # Fail the run rather than presenting a partial scrape as success.
                        log.exception("Could not fetch article %s; existing feed is preserved", index + 1)
                        raise
                    finally:
                        if detail and new_tab:
                            detail.close()
                        elif detail:
                            page.get(self.FEED_URL)
                            if not page.ele("css:.post.single .title", timeout=30):
                                raise RuntimeError("Account list did not reload after same-tab navigation.")
                    if index + 1 < total:
                        time.sleep(2)
            finally:
                page.quit()
            return articles

    log.info("IT early-news adapter v4: Beijing timestamps and new/same-tab navigation.")
    log.info("Starting Chrome with DrissionPage (no ChromeDriver required).")
    generator = ITHomeAccountGenerator(base_dir=ROOT)
    # Call directly so failures do not silently reuse a stale cache as success.
    try:
        articles = generator.fetch_articles()
        if not articles:
            raise RuntimeError("No articles fetched")
        existing = generator.load_cache()
        # Refresh existing entries too, so corrected timestamps/content take effect.
        by_url = {article.url: article for article in existing}
        by_url.update({article.url: article for article in articles})
        merged = sorted(by_url.values(), key=lambda article: article.published_at or datetime.min.replace(tzinfo=pytz.UTC), reverse=True)[:20]
        generator.save_cache(merged)
        output = generator.save_feed_streaming(merged)
        # Basic structural validation of the generated artifact.
        import xml.etree.ElementTree as ET
        xml = output.read_text(encoding="utf-8")
        xml = xml.replace('<atom:link href="' + generator.FEED_URL + '" rel="self"', '<atom:link href="https://cdn.jsdelivr.net/gh/hspthinking/ForgeRSS@main/feeds/feed_ithome_account_10233.xml" rel="self"')
        output.write_text(xml, encoding="utf-8")
        items = ET.parse(output).findall("./channel/item")
        if not items or any(not item.findtext("link") or not item.findtext("title") for item in items):
            raise RuntimeError("RSS validation failed")
        print(f"\nSUCCESS: {output.resolve()}")
        print(f"Fetched {len(articles)} articles this run; RSS contains {len(items)} items.")
        print("This is a local file. A hosted URL will be configured after verification.")
        return 0
    except Exception:
        log.exception("FAILED. Copy the final error lines for troubleshooting.")
        print("Copy the final error lines and describe which page was visible before Chrome closed.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())