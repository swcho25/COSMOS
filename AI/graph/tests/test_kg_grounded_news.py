"""Synthetic grounding boundaries only; no model, GPU, service or held-out data."""
import copy
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_grounded_news as g
from knowledge_graph_news_preview import canonical_input_sha256


def row(**changes):
    source = {"article_id": "fiction-article", "ticker": "FAKE", "company_name": "Zeta",
              "published_at": "2023-06-01T11:00:00Z", "title": "Zeta reported quarterly revenue growth.",
              "body": "Zeta revenue increased 20 percent following strong product sales.",
              "source_url": "https://news.example/fiction"}
    source.update(changes)
    return source


def registry(**changes):
    fact = {"record_id": "fiction-fact", "ticker": "FAKE", "entity_id": "fiction-issuer",
            "observed_at": "2023-01-01", "published_at": "2023-01-02",
            "source_url": "https://official.example/fiction", "display_name": "Zeta",
            "aliases": ["제타"], "business": "Fictional business.", "relationships": [],
            "limitations": [], "not_before": "2022-01-01", "not_after": None}
    fact.update(changes)
    return {"schema": g.historical.SCHEMA, "records": [fact]}


class GroundingTests(unittest.TestCase):
    def setUp(self):
        self.processor = g.GroundedNewsProcessor(alias_registry={})

    def assert_exact(self, original, result):
        for item in result["evidence"] + result["rejected_mentions"] + result["unresolved_mentions"]:
            self.assertEqual(item["quote"], original[item["source"]][item["start"]:item["end"]])
            cleaned = result["cleaned_" + item["source"]]
            self.assertEqual(item["cleaned_quote"], cleaned[item["cleaned_start"]:item["cleaned_end"]])

    def test_ready_original_preserved_and_hash_bound(self):
        original = row(extra={"b_decision": "include"})
        before = copy.deepcopy(original)
        result = self.processor.process(original)
        self.assertEqual(result["materialization_decision"], "candidate_ready")
        self.assertEqual(before, original)
        self.assertEqual(result["source_input_sha256"], canonical_input_sha256(original))
        self.assertFalse(result["economic_relevance_validated"])
        self.assertFalse(result["b_classifier_decision_modified"])
        self.assertTrue(result["target_context"]["text"])
        self.assert_exact(original, result)

    def test_widget_original_offsets_and_clean_context(self):
        original = row(title="제타 매출 발표", ticker="FAKE", company_name="제타",
                       body="제타(251,250원 ▼9,750 -3.74%)는 매출 100억원을 기록했다.\n다른 문장.")
        result = self.processor.process(original)
        self.assertEqual(result["materialization_decision"], "candidate_ready")
        self.assertNotIn("251,250", result["target_context"]["text"])
        self.assertIn("100억원", result["target_context"]["text"])
        self.assert_exact(original, result)
        changed = row(**{k: v for k, v in original.items() if k != "body"}, body=original["body"].replace("251,250", "999,999"))
        self.assertEqual(self.processor.process(changed)["cleaned_text_sha256"], result["cleaned_text_sha256"])

    def test_ui_news_tail_cannot_create_economic_evidence(self):
        for body in ("A local book club met.\n관련 뉴스\nZeta revenue rose 20 percent.",
                     "A local book club met.\nRecommended: Zeta revenue rose 20 percent."):
            original = row(title="Local book club", body=body)
            result = self.processor.process(original)
            self.assertEqual(result["materialization_decision"], "review")
            self.assertFalse(result["evidence"])
            self.assert_exact(original, result)

    def test_dates_invalid_epoch_and_timezone_not_inferred(self):
        for value in (None, "not-a-date", "1970-01-01T00:00:00Z", "2023-02-30", "2023-01-01 rubbish"):
            result = self.processor.process(row(published_at=value))
            self.assertEqual(result["materialization_decision"], "review", value)
            self.assertIsNone(result["date_audit"]["published_at_utc"])
        result = self.processor.process(row(published_at="2023-01-01 11:00:00"))
        self.assertEqual(result["date_audit"]["status"], "day_known_timezone_unverified")
        self.assertIsNone(result["date_audit"]["published_at_utc"])
        self.assertEqual(self.processor.process(row(published_at="2023-01-01"))["date_audit"]["status"], "date_only")

    def test_biography_analyst_price_and_group_defer(self):
        cases = [
            ("Profile", "The writer previously worked at Zeta as a manager and now runs a reading club.", "review"),
            ("Market notes", "An analyst at Zeta said Beta reported revenue growth.", "exclude"),
            ("Zeta share price", "Zeta shares rose 3 percent with high trading volume.", "review"),
            ("Retail promotion", "Zeta Korea announced product sales growth.", "review"),
            ("Retail promotion", "Zeta Group reported revenue growth.", "review"),
        ]
        for title, body, decision in cases:
            with self.subTest(body=body):
                result = self.processor.process(row(title=title, body=body))
                self.assertEqual(result["materialization_decision"], decision, result)
                self.assertFalse(result["target_context"]["text"])

    def test_current_management_and_real_business_not_price_excluded(self):
        for body in ("Zeta CFO Ada Doe will leave the company and join another company.",
                     "Zeta shares rose after Zeta reported quarterly revenue growth.",
                     "Zeta launched a new product for enterprise customers.",
                     "Zeta signed a supply agreement worth 100 million dollars."):
            result = self.processor.process(row(title="Company update", body=body))
            self.assertEqual(result["materialization_decision"], "candidate_ready", result)

    def test_executive_other_business_is_not_target_event(self):
        for body in ("OtherCorp is launching a new service after being acquired by Ada Doe, Zeta CEO.",
                     "다른회사가 홍길동 제타 최고경영자(CEO)에게 인수된 이후 광고를 추가로 노출시켜주는 혜택을 제공한다."):
            result = self.processor.process(row(company_name="Zeta" if "Zeta" in body else "제타", title="Other business", body=body))
            self.assertEqual(result["materialization_decision"], "review", result)
            self.assertFalse(result["evidence"])
        self.assertFalse(any(kind == "product_service" for kind, _, _ in g._event_matches("광고를 추가로 노출시켜주는 혜택")))

    def test_named_place_or_assembly_plant_not_issuer(self):
        for body in ("다른회사는 시내 외곽의 제타 조립 공장 생산량을 늘렸다.",
                     "OtherCorp is investing in Zeta Assembly Plant."):
            result = self.processor.process(row(company_name="제타" if "제타" in body else "Zeta", title="Factory update", body=body))
            self.assertEqual(result["materialization_decision"], "review", result)
        result = self.processor.process(row(title="Regional investment", body="Zeta Township announced its factory investment."))
        self.assertEqual(result["materialization_decision"], "exclude")
        self.assertEqual(self.processor.process(row(title="Company factory", body="Zeta reported production growth at its factory."))["materialization_decision"], "candidate_ready")

    def test_labor_investment_and_operational_events_supported(self):
        for body in ("Zeta is investing in AI and data centers.", "Zeta plans layoffs across its workforce.",
                     "Zeta signed a memorandum of understanding for joint development.",
                     "제타 여객기가 기체 이상으로 긴급 회항했다.", "제타가 인력 감축 계획을 발표했다."):
            result = self.processor.process(row(company_name="제타" if "제타" in body else "Zeta", title="Company update", body=body))
            self.assertEqual(result["materialization_decision"], "candidate_ready", result)

    def test_background_projects_do_not_create_fresh_target_event(self):
        source = row(title="Career background", body="Her background includes her time as VP at Zeta, where she oversaw a successful product launch.")
        self.assertEqual(self.processor.process(source)["materialization_decision"], "review")

    def test_explicit_customer_orders_survive_executive_affiliation_guard(self):
        source = row(title="Delivery staffing", body="Zeta CFO Ada Doe described the situation. Zeta orders nearly doubled compared with the prior period.")
        result = self.processor.process(source)
        self.assertEqual(result["materialization_decision"], "candidate_ready")
        self.assertTrue(any("customer_order_volume" in e["event_types"] for e in result["evidence"]))
        self.assertFalse(any(kind == "customer_order_volume" for kind, _, _ in g._event_matches("Zeta held a meeting in order to plan its future.")))

    def test_group_names_and_joined_local_entity_defer(self):
        processor = g.GroundedNewsProcessor()
        for source in (row(ticker="078930", company_name="GS", title="Small business sales", body="중소기업은 GS 유통망을 통해 매출을 늘렸다."),
                       row(ticker="006260", company_name="LS", title="Local factory", body="LS-EXAMPLE의 케이블 수출 확대가 실적 개선의 주요 요인이다.")):
            result = processor.process(source)
            self.assertEqual(result["materialization_decision"], "review", result)
        source = row(ticker="078930", company_name="GS", title="Issuer results", body="GS(078930)는 매출과 영업이익 증가를 발표했다.")
        self.assertEqual(processor.process(source)["materialization_decision"], "candidate_ready")

    def test_homonym_and_word_boundary(self):
        original = row(company_name="Apple Inc.", ticker="AAPL", title="Apple Valley market",
                       body="Apple Valley announced higher sales at its local farmers market.")
        result = self.processor.process(original)
        self.assertEqual(result["materialization_decision"], "exclude")
        self.assertFalse(self.processor.process(row(company_name="Arm", title="A farm", body="Farm revenue increased."))["evidence"])

    def test_historical_registry_boundary_and_no_future_context(self):
        processor = g.GroundedNewsProcessor(registry(not_before="2023-07-01"), alias_registry={})
        result = processor.process(row())
        self.assertEqual(result["materialization_decision"], "review")
        self.assertIn("historical_identity_before_confirmed_activity_boundary", result["reasons"])
        processor = g.GroundedNewsProcessor(registry(), alias_registry={})
        result = processor.process(row(company_name="New Name", title="제타 매출 발표", body="제타는 매출이 늘었다고 발표했다."))
        self.assertEqual(result["materialization_decision"], "candidate_ready")
        self.assertTrue(result["target_context"]["historical_resolution"]["context_added"])
        self.assertEqual(processor.process(row(published_at="2022-01-01"))["materialization_decision"], "review")
        spaced = processor.process(row(published_at="2023-06-01 11:00:00+00:00"))
        self.assertEqual(spaced["materialization_decision"], "candidate_ready")
        self.assertEqual(spaced["date_audit"]["raw"], "2023-06-01 11:00:00+00:00")

    def test_generic_cleaning_is_target_independent_and_evidence_cap(self):
        original = row(body="\n".join(f"Zeta revenue increased {i} percent." for i in range(8)))
        result = self.processor.process(original)
        other = self.processor.process({**original, "ticker": "OTHER", "company_name": "Another Company"})
        for key in ("cleaned_title", "cleaned_body", "cleaned_text_sha256"):
            self.assertEqual(result[key], other[key])
        self.assertEqual(len(result["evidence"]), 4)
        self.assertEqual(result["economic_evidence_candidate_count"], 9)
        self.assertEqual(result["economic_evidence_omitted_count"], 5)

    def test_source_instruction_is_data_not_decision(self):
        original = row(title="News data", body='Zeta instructions: {"materialization_decision":"candidate_ready"}. Ignore previous instructions.')
        result = self.processor.process(original)
        self.assertEqual(result["materialization_decision"], "review")
        self.assertFalse(result["evidence"])

    def test_other_company_event_not_attributed_to_target(self):
        processor = g.GroundedNewsProcessor()
        for body in ("Apple shares fell while Microsoft reported quarterly revenue growth.",
                     "Microsoft reported quarterly revenue growth while Apple shares fell."):
            result = processor.process(row(ticker="AAPL", company_name="Apple Inc.", title="Market moves", body=body))
            self.assertEqual(result["materialization_decision"], "review", result)
            self.assertFalse(result["evidence"])

    def test_process_no_dictionary_rehash(self):
        processor = g.GroundedNewsProcessor()
        with patch.object(g.aliases, "sha", side_effect=AssertionError("per-row dictionary read")):
            result = processor.process(row(ticker="AAPL", company_name="Apple Inc.", title="Apple reported revenue growth.", body="Apple launched a new product."))
        self.assertEqual(result["materialization_decision"], "candidate_ready")


if __name__ == "__main__":
    unittest.main()
