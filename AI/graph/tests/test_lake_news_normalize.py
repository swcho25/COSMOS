"""Source-time, dedup-candidate and literal-evidence regressions for lake rows."""
import datetime as dt
import hashlib
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lake_news_normalize import analyze_article, canonicalize_url, normalize_article
from matcher import Alias, CompanyMatcher


class NormalizeTests(unittest.TestCase):
    def row(self, **changes):
        value = {"record_id": "source-1", "title": "기업 소식", "body": "실적이 늘었다.",
                 "published_date": "2026-09-15"}
        value.update(changes)
        return value

    def test_explicit_offset_wins_and_converts_utc(self):
        value = normalize_article(self.row(published_at="2026-09-15T01:30:00+09:00"))
        self.assertEqual(value["published_at"], "2026-09-14T16:30:00Z")
        self.assertEqual(value["published_date"], "2026-09-14")
        self.assertEqual(value["published_at_precision"], "timestamp")
        self.assertEqual(value["published_at_source"], "published_at")

    def test_date_only_conservative_and_no_country_timezone_guess(self):
        value = normalize_article(self.row(published_at="2026-09-15T13:00:00", region="domestic"))
        self.assertEqual(value["published_at"], "2026-09-15T23:59:59.999999Z")
        self.assertEqual(value["published_at_precision"], "date")
        self.assertIn("publication_time_without_valid_timezone", value["quality_flags"])

    def test_raw_date_cannot_be_upgraded_by_normalized_midnight(self):
        value = normalize_article(self.row(published_at_raw="2026-09-15", published_at="2026-09-15T00:00:00Z"))
        self.assertEqual(value["published_at_precision"], "date")
        self.assertEqual(value["published_at"], "2026-09-15T23:59:59.999999Z")

    def test_missing_date_falls_back_to_naive_clock_date(self):
        value = normalize_article(self.row(published_date=None, published_at="2026-09-15 13:20:00"))
        self.assertEqual(value["published_date"], "2026-09-15")
        self.assertEqual(value["published_at_precision"], "date")

    def test_aware_datetime_input(self):
        value = normalize_article(self.row(published_at=dt.datetime(2026, 9, 15, 4, tzinfo=dt.timezone.utc)))
        self.assertEqual(value["published_at"], "2026-09-15T04:00:00Z")

    def test_invalid_text_or_dates_rejected_but_old_news_not_filtered(self):
        self.assertIsNone(normalize_article(self.row(title=" ", body=None)))
        self.assertIsNone(normalize_article(self.row(published_date="2026-02-30")))
        self.assertIsNone(normalize_article(self.row(published_date=None)))
        self.assertIsNotNone(normalize_article(self.row(published_date="2010-01-01", text_eligible=False)))

    def test_url_preserves_article_query_and_encoding(self):
        url = "HTTPS://Example.COM:443/read?id=42&x=a%2Fb&UTM_source=feed&id=43&sig=one+two#comments"
        self.assertEqual(canonicalize_url(url), "https://example.com/read?id=42&x=a%2Fb&id=43&sig=one+two")
        self.assertNotEqual(canonicalize_url("https://example.com/?id=1"), canonicalize_url("https://example.com/?id=2"))
        self.assertIsNone(canonicalize_url("javascript:alert(1)"))
        self.assertIsNone(canonicalize_url("https://secret@example.com/"))
        self.assertIsNone(canonicalize_url("https://example.com:bad/"))

    def test_clean_hash_identity_no_document_assignment_and_input_unchanged(self):
        row = self.row(body="삼성전자(251,250원 ▼9,750 -3.74%)는 실적이 늘었다.", url="https://EXAMPLE.com/a?utm_medium=email")
        original = dict(row)
        clean = normalize_article(row)
        self.assertEqual(row, original)
        self.assertNotIn("document_id", clean)
        self.assertNotIn("251,250", clean["body"])
        self.assertEqual(clean["canonical_url"], "https://example.com/a")
        self.assertEqual(clean["identity_candidate"], "url:" + hashlib.sha256(b"https://example.com/a").hexdigest())
        self.assertEqual(clean["content_hash"], hashlib.sha256("삼성전자는 실적이 늘었다.".encode()).hexdigest())

    def test_content_identity_collapses_whitespace_and_normalization_is_stable(self):
        a = normalize_article(self.row(body="실적이  늘었다.\n 매출 증가"))
        b = normalize_article(self.row(body="실적이 늘었다. 매출 증가"))
        self.assertEqual(a["identity_candidate"], b["identity_candidate"])
        self.assertEqual(a["identity_candidate"], "content:" + a["content_hash"])
        again = normalize_article(a)
        self.assertEqual(again["published_at"], a["published_at"])
        self.assertEqual(again["published_at_precision"], "date")


class AnalysisTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.matcher = CompanyMatcher([
            Alias("삼성전자", "005930", "삼성전자", "official", "unambiguous", ""),
            Alias("엔비디아", "NVDA", "엔비디아", "official", "unambiguous", ""),
            Alias("애플", "AAPL", "애플", "official", "unambiguous", ""),
            Alias("LG이노텍", "011070", "LG이노텍", "official", "unambiguous", ""),
            Alias("Apple", "AAPL", "Apple", "official", "unambiguous", ""),
            Alias("Microsoft", "MSFT", "Microsoft", "official", "unambiguous", ""),
        ])

    def analyze(self, body, title="기업 소식", **options):
        row = normalize_article({"record_id": "test-1", "title": title, "body": body, "published_date": "2026-09-15"})
        return row, analyze_article(row, self.matcher, **options)

    def assert_literal(self, row, result):
        text = row["title"] + "\n" + row["body"]
        for mention in result["mentions"]:
            for span in mention["spans"]:
                self.assertEqual(span["quote"], text[span["start"]:span["end"]])
        for relation in result["relationships"]:
            self.assertEqual(relation["sentence"], text[relation["sentence_start"]:relation["sentence_end"]])

    def test_clean_text_and_whitespace_adjusted_sentence_offsets(self):
        row, result = self.analyze("   삼성전자(251,250원 ▼9,750 -3.74%)는 엔비디아에 메모리를 공급한다.\n후속 소식이다.")
        self.assert_literal(row, result)
        self.assertEqual(len(result["relationships"]), 1)
        relation = result["relationships"][0]
        self.assertEqual((relation["src_ticker"], relation["dst_ticker"], relation["rel_type"]), ("005930", "NVDA", "SUPPLY"))
        self.assertEqual(relation["sentence_start"], len(row["title"]) + 4)
        self.assertTrue(relation["sentence"].endswith("."))

    def test_reverse_supply_preserves_supplier_direction(self):
        row, result = self.analyze("애플에 카메라를 공급하는 LG이노텍은 실적을 발표했다.")
        self.assert_literal(row, result)
        relation = result["relationships"][0]
        self.assertEqual((relation["src_ticker"], relation["dst_ticker"]), ("011070", "AAPL"))
        text = row["title"] + "\n" + row["body"]
        self.assertEqual(text[relation["source_start"]:relation["source_end"]], "LG이노텍")

    def test_undirected_sort_and_article_company_aggregation(self):
        row, result = self.analyze("엔비디아와 삼성전자는 협력을 체결했다. 삼성전자는 실적도 발표했다.")
        self.assert_literal(row, result)
        self.assertEqual(len(result["mentions"]), 2)
        self.assertEqual(next(r for r in result["mentions"] if r["ticker"] == "005930")["n_mentions"], 2)
        relation = result["relationships"][0]
        self.assertEqual((relation["src_ticker"], relation["dst_ticker"]), ("005930", "NVDA"))

    def test_rejection_rules_and_cross_sentence_isolation(self):
        for body in (
            "삼성전자와 엔비디아의 협력은 무산됐다.",
            "삼성전자와 엔비디아에 메모리를 공급한다.",
            "삼성전자는 공급한다.엔비디아는 실적을 발표했다.",
            "삼성전자와 엔비디아, 애플은 협력 계약을 체결했다.",
            "정부는 삼성전자와 엔비디아의 협력 심사를 진행했다.",
        ):
            with self.subTest(body=body):
                self.assertEqual(self.analyze(body)[1]["relationships"], [])

    def test_affiliate_competition_is_rejected(self):
        body = "삼성전자와 엔비디아는 시장 경쟁을 이어갔다."
        self.assertEqual(len(self.analyze(body)[1]["relationships"]), 1)
        result = self.analyze(body, affiliate_pairs=[("NVDA", "005930")])[1]
        self.assertEqual(result["relationships"], [])

    def test_english_mentions_retained_and_relation_limit_explicit(self):
        row, result = self.analyze("Apple and Microsoft signed a partnership agreement.", title="Business news")
        self.assert_literal(row, result)
        self.assertEqual({r["ticker"] for r in result["mentions"]}, {"AAPL", "MSFT"})
        self.assertEqual(result["relationships"], [])
        self.assertIn("relationship_language_unsupported", result["analysis_flags"])
        self.assertEqual(result["relationship_language_support"], "ko_only")


if __name__ == "__main__":
    unittest.main()
