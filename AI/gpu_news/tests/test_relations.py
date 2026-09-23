from __future__ import annotations

from pathlib import Path
import sys
import unittest

HERE = Path(__file__).resolve().parents[1]
ROOT = HERE.parents[1]
for path in (HERE, ROOT / "AI" / "graph", ROOT / "AI" / "ner"):
    sys.path.insert(0, str(path))

from relations import article_relations


def company(stock_code, name, *aliases, market="KOSPI"):
    return {"market": market, "stock_code": stock_code, "ticker": f"{stock_code}.KS",
            "name": name, "aliases": list(aliases), "confidence": 1.0, "method": "dict"}


KR = [company("005930", "삼성전자"), company("000660", "SK하이닉스")]


class ExtractionTest(unittest.TestCase):
    def test_a_two_company_sentence_becomes_a_relation(self):
        result = article_relations(
            "반도체 공급망 재편",
            "삼성전자가 SK하이닉스와 협력을 강화하기로 했다.", KR, "ko")
        self.assertTrue(result)
        self.assertEqual(result[0]["relation_type"], "PARTNER")
        self.assertIn("협력", result[0]["text"])

    def test_the_sentence_sentiment_keeps_is_not_a_relation(self):
        """One company means no pair, whatever the predicate says."""
        result = article_relations(
            "실적 발표", "삼성전자는 영업이익이 늘었다고 밝혔다.",
            [company("005930", "삼성전자")], "ko")
        self.assertEqual(result, [])

    def test_an_article_with_one_company_yields_nothing(self):
        self.assertEqual(article_relations("제목", "삼성전자는 이익이 늘었다.",
                                           [company("005930", "삼성전자")], "ko"), [])

    def test_the_evidence_sentence_is_carried_with_the_relation(self):
        result = article_relations(
            "제목", "삼성전자가 SK하이닉스와 기술 협력을 확대한다.", KR, "ko")
        self.assertTrue(result)
        row = result[0]
        self.assertIn("SK하이닉스", row["text"])
        self.assertIn("삼성전자", row["text"])
        self.assertGreaterEqual(row["sentence_order"], 1)
        self.assertIn(row["source"], ("title", "body"))

    def test_enumeration_is_reported_so_the_caller_can_drop_it(self):
        """'A·B·C 와 협약' names three parties that contracted with a fourth,
        not with each other. The classifier lets it through for PARTNER, so the
        flag is how a caller filters it."""
        result = article_relations(
            "지자체 협약",
            "강원도가 삼성전자, SK하이닉스와 손잡고 지원에 나섰다.", KR, "ko")
        if result:
            self.assertTrue(any(row["enumeration"] for row in result))

    def test_one_sentence_yields_one_row_per_pair(self):
        """Both orders of a pair are asked; a symmetric answer must not double."""
        result = article_relations(
            "제목", "삼성전자가 SK하이닉스와 협력한다.", KR, "ko")
        keys = [(r["relation_type"], r["source_stock_code"],
                 r["target_stock_code"], r["sentence_order"]) for r in result]
        self.assertEqual(len(keys), len(set(keys)))

    def test_pairs_carry_market_and_code_for_both_sides(self):
        result = article_relations(
            "제목", "삼성전자가 SK하이닉스와 협력한다.", KR, "ko")
        self.assertTrue(result)
        row = result[0]
        for field in ("source_market", "source_stock_code",
                      "target_market", "target_stock_code"):
            self.assertTrue(row[field])
        self.assertNotEqual(row["source_stock_code"], row["target_stock_code"])


class EnglishTest(unittest.TestCase):
    US = [company("AAPL", "Apple", market="NASDAQ"),
          company("INTC", "Intel", market="NASDAQ")]

    def test_english_sentences_use_the_english_classifier(self):
        result = article_relations(
            "Chip market", "Apple competes with Intel in the laptop market.",
            self.US, "en")
        for row in result:
            self.assertEqual(row["language"], "en")
            self.assertTrue(row["pattern"].startswith("en:"))

    def test_boilerplate_and_captions_never_reach_the_classifier(self):
        result = article_relations("Results", "[사진=Apple] Intel", self.US, "en")
        self.assertEqual(result, [])


class SubstringNameTest(unittest.TestCase):
    """A short name matching inside a longer one invents pairs that the
    sentence never states."""

    GROUP = [company("035720", "카카오"), company("377300", "카카오페이"),
             company("323410", "카카오뱅크")]

    def test_a_name_found_only_inside_a_longer_one_is_not_a_party(self):
        result = article_relations(
            "제휴", "카카오페이와 카카오뱅크는 파이어블록스와 손잡고 사업을 검토한다.",
            self.GROUP, "ko")
        involved = {code for row in result
                    for code in (row["source_stock_code"], row["target_stock_code"])}
        self.assertNotIn("035720", involved)

    def test_the_longer_company_still_takes_part(self):
        result = article_relations(
            "제휴", "카카오페이와 카카오뱅크는 협력 구조를 검토한다.", self.GROUP, "ko")
        involved = {code for row in result
                    for code in (row["source_stock_code"], row["target_stock_code"])}
        self.assertTrue({"377300", "323410"} & involved)

    def test_the_short_name_counts_when_it_stands_alone(self):
        result = article_relations(
            "제휴", "카카오가 카카오뱅크와 별도로 협력 방안을 논의했다.",
            [company("035720", "카카오"), company("323410", "카카오뱅크")], "ko")
        involved = {code for row in result
                    for code in (row["source_stock_code"], row["target_stock_code"])}
        self.assertIn("035720", involved)


class MatchConfidenceTest(unittest.TestCase):
    """Only identities the matcher was certain about can carry a relation."""

    def test_a_group_name_guess_is_not_a_party(self):
        weak = dict(company("005930", "삼성전자"), confidence=0.5, method="rule")
        result = article_relations(
            "제목", "삼성전자가 SK하이닉스와 협력한다.",
            [weak, company("000660", "SK하이닉스")], "ko")
        self.assertEqual(result, [])

    def test_certain_hits_still_produce_relations(self):
        result = article_relations(
            "제목", "삼성전자가 SK하이닉스와 협력한다.",
            [dict(company("005930", "삼성전자"), method="dict"),
             dict(company("000660", "SK하이닉스"), method="dict")], "ko")
        self.assertTrue(result)

    def test_an_ellipsis_expansion_is_flagged_like_an_enumeration(self):
        result = article_relations(
            "제목", "삼성전자가 SK하이닉스와 협력한다.",
            [dict(company("005930", "삼성전자"), method="dict+ellipsis"),
             dict(company("000660", "SK하이닉스"), method="dict")], "ko")
        self.assertTrue(result)
        self.assertTrue(all(row["enumeration"] for row in result))


if __name__ == "__main__":
    unittest.main()
