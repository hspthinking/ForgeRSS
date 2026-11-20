#!/usr/bin/env python3
"""Browser-free IT early-news adapter for ForgeRSS and ChatGPT cloud tasks."""
from __future__ import annotations

import argparse
import logging
from datetime import datetime
from pathlib import Path
import xml.etree.ElementTree as ET

import pytz
import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from generators.base import Article, BaseFeedGenerator
from run_ithome import clean_body

ROOT = Path(__file__).resolve().parent
FEED_LINK = "https://cdn.jsdelivr.net/gh/hspthinking/ForgeRSS@main/feeds/feed_ithome_account_10233.xml"
ACCOUNT_API = "https://mp.ithome.com/static/api/news/itaccount/accountinfo/10233"
SHANGHAI = pytz.timezone("Asia/Shanghai")


def news_url(news_id):
    digits = str(int(news_id)).zfill(7)
    groups = []
    while digits:
        groups.insert(0, digits[-3:])
        digits = digits[:-3]
    return "https://www.ithome.com/" + "/".join(groups) + ".htm"


def published_date(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = SHANGHAI.localize(stamp)
    return stamp.astimezone(pytz.UTC)


def make_session():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 ForgeRSS personal feed reader", "Referer": "https://mp.ithome.com/account/"})
    retry = Retry(total=2, backoff_factor=2, status_forcelist=(429, 500, 502, 503, 504), allowed_methods=("GET",))
    session.mount("https://", HTTPAdapter(max_retries=retry))
    return session


class ITHomeHTTPGenerator(BaseFeedGenerator):
    FEED_NAME = "ithome_account_10233"
    FEED_TITLE = "IT早报 - IT之家"
    FEED_URL = "https://mp.ithome.com/account/#/10233"
    FEED_DESCRIPTION = "IT早报账号文章订阅；由 ForgeRSS 生成。"
    FEED_LANGUAGE = "zh-CN"

    def fetch_articles(self):
        cached = {a.url: a for a in self.load_cache()}
        with make_session() as session:
            response = session.get(ACCOUNT_API, timeout=(10, 40))
            response.raise_for_status()
            data = response.json()
            account = data.get("content", {}).get("account", {})
            if data.get("success") is not True or int(account.get("accountid", 0)) != 10233:
                raise RuntimeError("Account API returned an invalid or unexpected account.")
            rows = account.get("newslist", [])
            if not rows:
                raise RuntimeError("Account API has no articles; keeping the old RSS.")
            articles = []
            for row in rows[:self.limit]:
                title = str(row["newstitle"]).strip()
                if not title.startswith("IT早报"):
                    raise RuntimeError("Unexpected title in IT early-news account.")
                url = news_url(row["newsid"])
                body = cached[url].content if url in cached else None
                if not body:
                    detail = session.get(url, timeout=(10, 40))
                    detail.raise_for_status()
                    soup = BeautifulSoup(detail.content, "html.parser")
                    node = soup.select_one("#paragraph")
                    if node is None or len(node.get_text(strip=True)) < 100:
                        raise RuntimeError("Article body is missing: " + url)
                    body = clean_body(node.decode_contents(), url)
                articles.append(Article(url=url, title=title, published_at=published_date(row["addtime"]),
                                        content=body, author="IT早报", category="科技早报"))
                self.logger.info("Ready: %s %s", title[:30], url)
            return articles


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--max", type=int, choices=range(1, 21), default=3, metavar="1-20")
    args = parser.parse_args()
    generator = ITHomeHTTPGenerator(base_dir=ROOT)
    generator.limit = args.max
    try:
        fresh = generator.fetch_articles()
        previous = {a.url: a for a in generator.load_cache()}
        previous.update({a.url: a for a in fresh})
        articles = sorted(previous.values(), key=lambda a: a.published_at or datetime.min.replace(tzinfo=pytz.UTC), reverse=True)[:20]
        output = generator.save_feed_streaming(articles)
        xml = output.read_text(encoding="utf-8").replace(
            '<atom:link href="' + generator.FEED_URL + '" rel="self"',
            '<atom:link href="' + FEED_LINK + '" rel="self"')
        output.write_text(xml, encoding="utf-8")
        items = ET.parse(output).findall("./channel/item")
        if not items or any(not i.findtext("title") or not i.findtext("link") or not i.findtext("description") for i in items):
            raise RuntimeError("Generated RSS failed validation")
        generator.save_cache(articles)
        newest = articles[0]
        print("SUCCESS:", output)
        print("Latest title:", newest.title)
        print("Latest date (Beijing):", newest.published_at.astimezone(SHANGHAI).isoformat())
        print("RSS items:", len(items))
        return 0
    except Exception:
        generator.logger.exception("HTTP RSS generation failed; do not publish this run")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
