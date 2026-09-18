"""Investing.com Nasdaq-100 collector tests with fake network and outbox."""

import tempfile
from types import SimpleNamespace
import unittest

from services.news_pipeline.investing import (
    COMPONENTS_URL,
    InvestingNasdaqCollector,
    parse_nasdaq100_feeds,
)


def components_html(count=101):
    rows = "".join(
        f'<tr><td><a href="/equities/company-{index:03d}">Company {index}</a></td></tr>'
        for index in range(count)
    )
    return (
        '<table><a href="/equities/sidebar-stock">Sidebar</a></table>'
        f'<h2>Nasdaq 100 Companies</h2><table>{rows}</table>'
    )


class FakeClient:
    def __init__(self, pages=None, errors=None):
        self.pages = pages or {}
        self.errors = errors or {}
        self.urls = []

    def assert_robots_allowed(self, url):
        return None

    def get_text(self, url):
        self.urls.append(url)
        if url in self.errors:
            raise self.errors[url]
        return self.pages[url]


class FakeSource:
    minimum_content_chars = 120

    def parse_listing(self, html):
        if html == "empty":
            return []
        number = "2" if "page two" in html else "1"
        return [SimpleNamespace(
            source="investing_com",
            title=f"Company story {number}",
            url=f"https://www.investing.com/news/company-story-{number}",
            organization="Reuters",
        )]

    def parse_article(self, html, candidate):
        return SimpleNamespace(
            source="investing_com", organization=candidate.organization,
            title=candidate.title, url=candidate.url, content="A" * 300,
            published_at="2026-09-18T01:00:00+00:00",
            crawled_at="2026-09-18T01:01:00+00:00",
        )


class FakeOutbox:
    def __init__(self):
        self.events = []
        self.urls = set()
        self.fail = False

    def seen_url(self, url):
        return url in self.urls

    def enqueue(self, event):
        if self.fail:
            raise OSError("outbox unavailable")
        self.events.append(event)
        self.urls.add(event["url"])
        return True


class InvestingCollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.outbox = FakeOutbox()

    def collector(self, client, **kwargs):
        instance = InvestingNasdaqCollector(
            SimpleNamespace(), self.temp.name, self.outbox,
            client=client, source=FakeSource(), cooldown_seconds=60, **kwargs,
        )
        self.addCleanup(instance.close)
        return instance

    def test_component_parser_uses_main_table_and_builds_news_urls(self):
        feeds = parse_nasdaq100_feeds(components_html())
        self.assertEqual(len(feeds), 101)
        self.assertEqual(feeds[0].slug, "company-000")
        self.assertEqual(
            feeds[0].news_url,
            "https://www.investing.com/equities/company-000-news",
        )
        self.assertNotIn("sidebar-stock", {feed.slug for feed in feeds})

    def test_component_parser_rejects_partial_or_challenge_pages(self):
        with self.assertRaisesRegex(ValueError, "Unexpected.*count"):
            parse_nasdaq100_feeds(components_html(20))
        with self.assertRaisesRegex(ValueError, "table was not found"):
            parse_nasdaq100_feeds("<html>Just a moment...</html>")

    def test_collect_discovers_feed_fetches_article_and_keeps_ticker_metadata(self):
        feed = "https://www.investing.com/equities/company-000-news"
        article = "https://www.investing.com/news/company-story-1"
        client = FakeClient({
            COMPONENTS_URL: components_html(100),
            feed: "<h1>Company Zero (CMP0)</h1> feed page one",
            article: "article body",
        })
        collector = self.collector(client)
        result = collector.collect(limit=1)

        self.assertEqual(result["feed_count"], 100)
        self.assertEqual(result["discovery"]["ticker"], "CMP0")
        self.assertEqual(result["enqueued"], 1)
        self.assertEqual(
            self.outbox.events[0]["metadata"],
            {
                "discovery_method": "investing_nasdaq100_equity_news",
                "discovery_feeds": ["company-000"],
                "discovery_tickers": ["CMP0"],
            },
        )

    def test_backfill_advances_one_page_and_survives_restart(self):
        feed = "https://www.investing.com/equities/company-000-news"
        page_two = f"{feed}/2"
        client = FakeClient({
            COMPONENTS_URL: components_html(100),
            feed: "<h1>Company Zero (CMP0)</h1> feed page one",
            page_two: "<h1>Company Zero (CMP0)</h1> feed page two",
            "https://www.investing.com/news/company-story-1": "article one",
            "https://www.investing.com/news/company-story-2": "article two",
        })
        first = self.collector(client)
        first.collect(limit=1)
        first.close()
        restarted = InvestingNasdaqCollector(
            SimpleNamespace(), self.temp.name, self.outbox,
            client=client, source=FakeSource(), cooldown_seconds=60,
        )
        self.addCleanup(restarted.close)
        result = restarted.collect(limit=1)
        row = restarted.db.execute(
            "SELECT backfill_page FROM investing_feeds WHERE slug='company-000'"
        ).fetchone()

        self.assertEqual(result["discovery"]["mode"], "backfill")
        self.assertEqual(result["discovery"]["page"], 2)
        self.assertEqual(row[0], 3)
        self.assertEqual(result["enqueued"], 1)

    def test_http_429_sets_durable_cooldown_without_busy_retry(self):
        client = FakeClient(errors={
            COMPONENTS_URL: RuntimeError("HTTP 429 Too Many Requests")
        })
        collector = self.collector(client)
        first = collector.collect(limit=1)
        second = collector.collect(limit=1)

        self.assertIn("rate_limited_until", first)
        self.assertIn("rate_limited_until", second)
        self.assertEqual(client.urls, [COMPONENTS_URL])

    def test_outbox_failure_leaves_article_pending_without_consuming_retry(self):
        feed = "https://www.investing.com/equities/company-000-news"
        article = "https://www.investing.com/news/company-story-1"
        client = FakeClient({
            COMPONENTS_URL: components_html(100),
            feed: "<h1>Company Zero (CMP0)</h1> feed page one",
            article: "article body",
        })
        collector = self.collector(client)
        self.outbox.fail = True
        with self.assertRaisesRegex(OSError, "outbox unavailable"):
            collector.collect(limit=1)
        row = collector.db.execute(
            "SELECT status,attempts FROM investing_candidates WHERE url=?", (article,)
        ).fetchone()
        self.assertEqual(tuple(row), ("pending", 0))


if __name__ == "__main__":
    unittest.main()
