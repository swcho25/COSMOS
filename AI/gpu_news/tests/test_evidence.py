from __future__ import annotations

from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
for path in (HERE, ROOT / "AI" / "graph", ROOT / "AI" / "ner"):
    sys.path.insert(0, str(path))

from evidence import EvidenceError, article_evidence, sentence_language


def company(stock_code, name, *aliases, market="KOSPI"):
    return {"market": market, "stock_code": stock_code, "ticker": f"{stock_code}.KS",
            "name": name, "aliases": list(aliases), "confidence": 0.9}


class AttributionTest(unittest.TestCase):
    def test_sentence_naming_two_companies_is_evidence_for_neither(self):
        """The ambiguous-sentence rule is what keeps one company's bad news off
        the other, so it is checked before anything else."""
        result = article_evidence(
            "삼성전자, SK하이닉스와 경쟁 심화",
            "삼성전자는 영업이익이 늘었다고 밝혔다. SK하이닉스는 증설을 예고했다.",
            [company("005930", "삼성전자"), company("000660", "SK하이닉스")])
        texts = {item["name"]: [e["text"] for e in item["evidence"]] for item in result}
        self.assertEqual(len(texts["삼성전자"]), 1)
        self.assertEqual(len(texts["SK하이닉스"]), 1)
        self.assertNotIn("경쟁", texts["삼성전자"][0])
        self.assertNotIn("경쟁", texts["SK하이닉스"][0])

    def test_other_company_set_is_this_article_only(self):
        """A universe name that never appears in this article must not be able to
        reject a sentence: the same text keeps its evidence when the unrelated
        company is not among the article's matches."""
        body = "삼성전자는 영업이익이 늘었다고 밝혔다."
        alone = article_evidence("제목", body, [company("005930", "삼성전자")])
        self.assertEqual(alone[0]["n_sentences"], 1)
        with_unrelated = article_evidence(
            "제목", body, [company("005930", "삼성전자"), company("035420", "NAVER")])
        by_name = {item["name"]: item for item in with_unrelated}
        self.assertEqual(by_name["삼성전자"]["n_sentences"], 1)
        self.assertEqual(by_name["NAVER"]["status"], "no_attributed_sentence")

    def test_english_word_boundary_rejects_longer_word(self):
        result = article_evidence(
            "Appleton Papers files", "Appleton Papers reported a loss.",
            [company("AAPL", "Apple", market="NASDAQ")])
        self.assertEqual(result[0]["n_sentences"], 0)
        self.assertEqual(result[0]["status"], "no_attributed_sentence")

    def test_alias_counts_as_the_target_name(self):
        result = article_evidence(
            "제목", "네이버는 신규 서비스를 공개했다.",
            [company("035420", "NAVER", "네이버")])
        self.assertEqual(result[0]["n_sentences"], 1)

    def test_shared_alias_does_not_make_a_sentence_ambiguous(self):
        """A name the target itself goes by is removed from the other-company set,
        otherwise a company would reject its own evidence."""
        result = article_evidence(
            "제목", "카카오는 실적을 발표했다.",
            [company("035720", "카카오", "카카오"), company("323410", "카카오뱅크")])
        by_name = {item["name"]: item for item in result}
        self.assertEqual(by_name["카카오뱅크"]["n_sentences"], 0)


class SplitNameTest(unittest.TestCase):
    """Names containing a period are cut in half by the segmenter, which makes
    the company invisible in its own article."""

    def test_company_named_across_a_period_still_gets_its_sentence(self):
        result = article_evidence(
            "Results", "Warner Bros. Discovery cut its guidance today.",
            [company("WBD", "Warner Bros. Discovery", market="NASDAQ")])
        self.assertEqual(result[0]["n_sentences"], 1)
        self.assertIn("Warner Bros. Discovery", result[0]["evidence"][0]["text"])

    def test_dotted_domain_name_is_not_cut(self):
        result = article_evidence(
            "Results", "Booking.com raised its full-year outlook.",
            [company("BKNG", "Booking.com", market="NASDAQ")])
        self.assertEqual(result[0]["n_sentences"], 1)

    def test_rejoining_does_not_merge_ordinary_sentences(self):
        result = article_evidence(
            "제목", "삼성전자는 이익이 늘었다. 삼성전자는 증설도 검토한다.",
            [company("005930", "삼성전자")])
        self.assertEqual(result[0]["n_sentences"], 2)

    def test_a_split_name_still_blocks_attribution_for_the_other_company(self):
        """The rejoined sentence names both companies, so it is evidence for
        neither - the guard must see the whole name to reject it."""
        result = article_evidence(
            "Results", "Apple and Warner Bros. Discovery announced a deal.",
            [company("AAPL", "Apple", market="NASDAQ"),
             company("WBD", "Warner Bros. Discovery", market="NASDAQ")])
        self.assertEqual([item["n_sentences"] for item in result], [0, 0])


class ScannableRegionTest(unittest.TestCase):
    """Evidence must stay inside the region CompanyMatcher was willing to scan."""

    def test_sidebar_after_the_boilerplate_cut_is_not_evidence(self):
        body = ("[인사] 새만금개발청은 인사를 단행했다.\n"
                "주요 뉴스\n삼성전자는 실적이 좋았다.\nSK하이닉스가 증설한다.")
        result = article_evidence("인사 공고", body, [company("005930", "삼성전자")])
        for item in result[0]["evidence"]:
            self.assertNotIn("주요 뉴스", item["text"])
        self.assertEqual(result[0]["n_sentences"], 0)

    def test_ordinary_article_is_untouched_by_the_cut(self):
        result = article_evidence(
            "삼성전자 실적", "삼성전자는 영업이익이 늘었다고 밝혔다.",
            [company("005930", "삼성전자")])
        self.assertEqual(result[0]["n_sentences"], 2)

    def test_stored_sentence_matches_its_own_offsets(self):
        body = "삼성전자는 이익이 늘었다.\n관련 기사\n삼성전자 주가 급등"
        result = article_evidence("제목", body, [company("005930", "삼성전자")])
        field = {"title": "제목", "body": body}
        for item in result[0]["evidence"]:
            self.assertEqual(item["text"], field[item["source"]][item["start"]:item["end"]])


class CatalogTest(unittest.TestCase):
    def test_offsets_address_their_own_field(self):
        """Offsets are per field with an explicit source, never into a joined
        title + body string, which is where the shifted-evidence bug came from."""
        title = "삼성전자 실적 발표"
        body = "삼성전자는 영업이익이 늘었다고 밝혔다."
        result = article_evidence(title, body, [company("005930", "삼성전자")])
        field = {"title": title, "body": body}
        for item in result[0]["evidence"]:
            self.assertEqual(item["text"], field[item["source"]][item["start"]:item["end"]])

    def test_reading_order_is_title_then_body(self):
        result = article_evidence(
            "삼성전자 실적 발표", "삼성전자는 이익이 늘었다.", [company("005930", "삼성전자")])
        evidence = result[0]["evidence"]
        self.assertEqual([item["source"] for item in evidence], ["title", "body"])
        self.assertLess(evidence[0]["sentence_order"], evidence[1]["sentence_order"])
        self.assertGreaterEqual(min(item["sentence_order"] for item in evidence), 0)


class LanguageTest(unittest.TestCase):
    def test_language_is_decided_per_sentence(self):
        self.assertEqual(sentence_language("삼성전자는 이익이 늘었다."), "ko")
        self.assertEqual(sentence_language("Revenue improved."), "en")
        self.assertEqual(sentence_language("Apple 은 실적을 냈다."), "ko")


class ValidationTest(unittest.TestCase):
    def test_company_without_market_or_code_is_rejected(self):
        with self.assertRaises(EvidenceError):
            article_evidence("제목", "본문", [{"name": "삼성전자", "aliases": []}])

    def test_company_without_any_name_is_rejected(self):
        with self.assertRaises(EvidenceError):
            article_evidence("제목", "본문",
                             [{"market": "KOSPI", "stock_code": "005930", "aliases": []}])

    def test_article_with_no_companies_returns_nothing(self):
        self.assertEqual(article_evidence("제목", "본문", []), [])


if __name__ == "__main__":
    unittest.main()
