"""Pure CPU/stdlib fact-contract tests using fictional articles only."""
import copy
import json
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import relevance_grounded_policy as policy
import relevance_sentence_evidence as evidence


class GroundedPolicyTests(unittest.TestCase):
    def test_none_channel_may_quote_the_reason_for_absence_without_adoption(self):
        row = self.row(body="대표는 5년 전 가상기계에서 퇴사했다.")
        prediction, errors = policy.validate_prediction(self.raw(role="former_affiliation", newness="past",
            economic_channel="none", event_summary="대표의 과거 직장을 설명했다."), row)
        self.assertEqual(errors, [])
        self.assertEqual(prediction["selection"], "제외")

    def test_industry_fact_without_target_or_context_cannot_be_adopted(self):
        row = self.row(title="전기요금 인상", body="오늘 국내 산업용 전기요금 인상이 발표됐다.")
        self.assert_abstention(self.raw(role="current_affected", economic_channel="cost"), row,
                              error="unanchored_target_attribution")
        row["company_context"] = {"source": "fiction-source", "as_of": "2020-01-01",
                                  "business": "가상의 국내 제조 기업"}
        prediction, errors = policy.validate_prediction(self.raw(role="current_affected", economic_channel="cost"), row)
        self.assertEqual(errors, [])
        self.assertEqual(prediction["selection"], "채택")

    def test_legal_suffix_removal_does_not_invent_alias_or_parent(self):
        row = self.row(title="Example 계약", body="Today Example announced a supply contract.")
        row["company_name"] = "Example, Inc."
        self.assertEqual(policy.validate_prediction(self.raw(), row)[1], [])
        row["company_name"] = "Example Holdings Inc."
        self.assert_abstention(self.raw(), row, error="unanchored_target_attribution")

    def row(self, body="오늘 가상기계가 부품 공급 계약을 체결했다. 비용 조건은 공개하지 않았다.", title="가상기계 계약 발표"):
        return {"article_id": "fiction-grounded", "ticker": "FICTION-01", "company_name": "가상기계",
                "published_at": "2021-05-20T10:00:00+09:00", "title": title, "body": body,
                "company_context": None, "case_kind": "synthetic", "source_split": "diagnostic"}

    def facts(self, **changes):
        return {"role": "current_actor", "newness": "new", "economic_channel": "supply",
                "attribution_evidence_ids": [2], "time_evidence_ids": [2], "economic_evidence_ids": [2],
                "event_summary": "가상기계가 오늘 부품 공급 계약을 발표했다.", **changes}

    def raw(self, **changes):
        return json.dumps(self.facts(**changes), ensure_ascii=False)

    def assert_abstention(self, raw, row=None, error=None, **kwargs):
        prediction, errors = policy.validate_prediction(raw, self.row() if row is None else row, **kwargs)
        self.assertTrue(errors)
        self.assertEqual(prediction["selection"], "보류")
        self.assertEqual(prediction["label"], "판단보류")
        self.assertEqual(prediction["evidence_sentences"], [])
        self.assertEqual(prediction["economic_path"], "")
        if error:
            self.assertIn(error, errors)

    def test_current_actor_requires_three_grounded_facts_and_restores_exact_quote(self):
        row = self.row(body="  오늘 가상기계가 부품 공급 계약을 체결했다.\r\n비용 조건은 공개하지 않았다. ")
        prediction, errors = policy.validate_prediction(self.raw(), row)
        self.assertEqual(errors, [])
        self.assertEqual((prediction["label"], prediction["time_status"], prediction["selection"]),
                         ("직접관련", "신규", "채택"))
        self.assertEqual(prediction["evidence_sentences"], ["  오늘 가상기계가 부품 공급 계약을 체결했다.\r\n"])
        self.assertIn("공급", prediction["economic_path"])
        self.assertIn(prediction["evidence_sentences"][0], prediction["economic_path"])
        self.assertEqual(set(prediction), {"label", "event_summary", "evidence_sentences", "economic_path", "time_status", "selection"})

    def test_current_affected_can_be_selected_without_being_primary_actor(self):
        row = self.row(body="오늘 항만이 폐쇄돼 가상기계의 부품 운송이 중단됐다.")
        prediction, errors = policy.validate_prediction(self.raw(role="current_affected"), row)
        self.assertEqual(errors, [])
        self.assertEqual((prediction["label"], prediction["selection"]), ("간접관련", "채택"))

    def test_former_affiliation_is_excluded_and_not_transferred_to_current_business(self):
        row = self.row(body="다른 회사 대표는 5년 전 가상기계에서 퇴사했다.")
        prediction, errors = policy.validate_prediction(self.raw(role="former_affiliation", newness="past",
            economic_channel="none", economic_evidence_ids=[], event_summary="대표의 과거 직장을 설명했다."), row)
        self.assertEqual(errors, [])
        self.assertEqual((prediction["label"], prediction["selection"]), ("단순언급", "제외"))
        self.assertEqual(prediction["economic_path"], "")

    def test_incidental_comparison_excluded_even_if_other_company_event_is_new(self):
        row = self.row(body="새 업체는 오늘 가상기계처럼 유명해지고 싶다고 말했다.")
        prediction, errors = policy.validate_prediction(self.raw(role="incidental", economic_channel="none",
            economic_evidence_ids=[], event_summary="새 업체가 가상기계를 비교 대상으로 언급했다."), row)
        self.assertEqual(errors, [])
        self.assertEqual(prediction["selection"], "제외")

    def test_past_event_and_undated_product_are_held(self):
        for newness, time_ids in (("past", [2]), ("unclear", [])):
            with self.subTest(newness=newness):
                prediction, errors = policy.validate_prediction(self.raw(newness=newness, time_evidence_ids=time_ids), self.row())
                self.assertEqual(errors, [])
                self.assertEqual(prediction["selection"], "보류")

    def test_new_lawsuit_about_past_supply_can_be_new_event(self):
        row = self.row(body="오늘 가상기계는 3년 전 납품 건에 관한 새로운 소송을 제기당했다.")
        prediction, errors = policy.validate_prediction(self.raw(economic_channel="litigation",
            event_summary="가상기계의 과거 납품 건에 관한 새 소송이 오늘 제기됐다."), row)
        self.assertEqual(errors, [])
        self.assertEqual((prediction["selection"], prediction["time_status"]), ("채택", "신규"))
        self.assertIn("소송", prediction["economic_path"])

    def test_new_disclosure_of_past_data_can_be_new_event(self):
        row = self.row(body="오늘 감독기관은 가상기계의 과거 품질 위반 자료를 새로 공개했다.")
        prediction, errors = policy.validate_prediction(self.raw(economic_channel="regulation",
            event_summary="감독기관이 오늘 가상기계의 과거 위반 자료를 공개했다."), row)
        self.assertEqual(errors, [])
        self.assertEqual(prediction["selection"], "채택")

    def test_unknown_holding_company_identity_cannot_be_selected(self):
        row = self.row(body="가상은행이 새 대출 상품을 발표했다.")
        row["company_name"] = "가상금융지주"
        prediction, errors = policy.validate_prediction(self.raw(role="unclear", attribution_evidence_ids=[],
            economic_channel="financing", event_summary="가상은행이 대출 상품을 발표했으나 대상 지주와의 관계는 불명이다."), row)
        self.assertEqual(errors, [])
        self.assertEqual((prediction["label"], prediction["selection"]), ("판단보류", "보류"))
        self.assertIn("추가 확인", prediction["economic_path"])

    def test_no_or_unclear_economic_channel_is_not_selected(self):
        for channel in ("none", "unclear"):
            with self.subTest(channel=channel):
                prediction, errors = policy.validate_prediction(self.raw(economic_channel=channel, economic_evidence_ids=[]), self.row())
                self.assertEqual(errors, [])
                self.assertEqual(prediction["selection"], "보류")

    def test_all_unclear_empty_evidence_is_valid_abstention(self):
        prediction, errors = policy.validate_prediction(self.raw(role="unclear", newness="unclear", economic_channel="unclear",
            attribution_evidence_ids=[], time_evidence_ids=[], economic_evidence_ids=[]), self.row())
        self.assertEqual(errors, [])
        self.assertEqual(prediction["selection"], "보류")
        self.assertEqual(prediction["evidence_sentences"], [])

    def test_asserted_facts_with_missing_evidence_fail_closed(self):
        for field, error in (("attribution_evidence_ids", "asserted_role_without_evidence"),
                             ("time_evidence_ids", "asserted_newness_without_evidence"),
                             ("economic_evidence_ids", "asserted_channel_without_evidence")):
            with self.subTest(field=field):
                self.assert_abstention(self.raw(**{field: []}), error=error)

    def test_explicit_contradictions_fail_closed(self):
        for role in ("former_affiliation", "incidental"):
            with self.subTest(role=role):
                self.assert_abstention(self.raw(role=role), error="noncurrent_role_with_new_economic_event")

    def test_id_types_unknown_duplicate_and_limits_fail_closed(self):
        for ids, error in (([True], "invalid_evidence_id_types"), ([2.0], "invalid_evidence_id_types"),
                           (["2"], "invalid_evidence_id_types"), (None, "invalid_evidence_id_types"),
                           ([0], "unknown_evidence_id"), ([9999], "unknown_evidence_id"),
                           ([2, 2], "duplicate_evidence_ids"), ([1, 2, 3, 4], "too_many_evidence_ids")):
            for field in policy.ID_FIELDS:
                with self.subTest(ids=ids, field=field):
                    self.assert_abstention(self.raw(**{field: ids}), error=error)

    def test_category_overlap_valid_three_ids_each_and_final_quotes_deduplicated(self):
        row = self.row(body="오늘 가상기계가 첫 계약을 발표했다. 오늘 두 번째 계약을 발표했다. 오늘 세 번째 계약을 발표했다.")
        prediction, errors = policy.validate_prediction(self.raw(attribution_evidence_ids=[2, 3, 4],
            time_evidence_ids=[4, 2, 3], economic_evidence_ids=[3, 4, 2]), row)
        self.assertEqual(errors, [])
        self.assertEqual(len(prediction["evidence_sentences"]), 3)
        self.assertEqual(prediction["evidence_sentences"], [entry["text"] for entry in evidence.build_sentence_catalog(row)[1:]])

    def test_json_keys_enums_and_summary_contract_fail_closed(self):
        for raw in ("{}", "null", "[]", "```json\n{}\n```", self.raw(selection="채택"),
                    self.raw(economic_path="근거 없는 미래 이익 증가"),
                    self.raw(role="invented"), self.raw(newness="today"), self.raw(economic_channel=[]),
                    self.raw(event_summary=""), self.raw(event_summary="가" * 241),
                    self.raw(event_summary="첫 문장. 둘째 문장."), self.raw(event_summary="한 줄\n두 줄"),
                    self.raw().replace('"current_actor"', 'NaN'),
                    self.raw()[:-1] + ',"role":"unclear"}'):
            with self.subTest(raw=raw):
                self.assert_abstention(raw)

    def test_catalog_mutation_or_source_change_cannot_restore_partial_evidence(self):
        row = self.row()
        catalog = evidence.build_sentence_catalog(row)
        bad = copy.deepcopy(catalog)
        bad[-1]["text"] += "없는 내용"
        self.assert_abstention(self.raw(), row, catalog=bad)
        changed = dict(row, body=row["body"] + " 새 문장.")
        self.assert_abstention(self.raw(), changed, catalog=catalog)

    def test_audit_separates_categories_and_does_not_claim_semantic_truth(self):
        audit, errors = policy.parse_audit(self.raw(), self.row())
        self.assertEqual(errors, [])
        self.assertEqual(set(audit["facts"]), policy.RAW_KEYS)
        self.assertFalse(audit["semantic_truth_verified"])
        for field in policy.ID_FIELDS:
            self.assertEqual(audit["evidence_by_category"][field][0]["id"], 2)
        prediction, _ = policy.validate_prediction(self.raw(), self.row())
        self.assertEqual(audit["prediction"], prediction)

    def test_messages_preserve_full_original_prompt_and_text_without_reference_leak(self):
        row = self.row(body=" \r\n" + "아주 긴 무구두점 원문 " * 3000 + "끝", title="  원제목\r\n")
        row.update(expected_selection="REFERENCE_SECRET", realized_return="FUTURE_SECRET", review="REVIEW_SECRET")
        prompt = "원문 기준\n기업과 산업 경제 관련성 판단\n"
        messages = policy.messages(row, prompt)
        self.assertEqual(messages[0]["content"], prompt + policy.FORMAT_CONTRACT)
        payload = json.loads(messages[1]["content"])
        for field in ("title", "body"):
            self.assertEqual("".join(item["text"] for item in payload["evidence_catalog"] if item["source"] == field), row[field])
        self.assertNotIn("SECRET", messages[1]["content"])
        self.assertEqual(payload["company_context"], None)

    def test_future_context_and_nested_label_context_never_enter_messages(self):
        for context in ({"source": "fixture", "as_of": "2023-01-01", "business": "부품"},
                        {"source": "fixture", "as_of": "2020-01-01", "business": {"expected_label": "leak"}}):
            row = self.row(); row["company_context"] = context
            with self.subTest(context=context), self.assertRaises(ValueError):
                policy.messages(row, "원문 기준")


if __name__ == "__main__":
    unittest.main()
