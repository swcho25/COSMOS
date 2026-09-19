"""CPU role/negative/offset regressions for the additive v5 grammar."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_grounded_relations_v4 as old
import kg_grounded_relations_v5 as new

ENTITIES = [
    {"ticker": "A", "company_name": "삼성전자", "aliases": ["Samsung"]},
    {"ticker": "B", "company_name": "애플", "aliases": ["Apple", "Apple Inc."]},
    {"ticker": "C", "company_name": "엔비디아", "aliases": ["Nvidia"]},
    {"ticker": "D", "company_name": "카카오", "aliases": ["Kakao"]},
]

# Nonrandom development examples independently read in their original parent
# articles. These are regression fixtures, not a held-out recall estimate.
REAL_FIXTURE_IDS = {
    "postposed_supplier": "02f1c66127c978ac15b5ad0c73ab69744b7308d939064709f68dc466de991f88",
    "announced_partnership": "0385f74a5b12591f4949c2dc76c24058c134e3db83f08fde63bd68a88471c783",
    "expanded_partnership": "3a9f209515656e99d8cc5d3ecdd5eba6cca78e8ed993deed6c3df969f1f205aa",
    "completed_supply_contract": "061b1fbe0138ae93322874344e25646218eeb85c932ae17e6326598916a45412",
}


class V5Tests(unittest.TestCase):
    def extract(self, body, title="🙂 원문", entities=ENTITIES, **kwargs):
        result = new.extract_grounded_relations(title, body, entities, **kwargs)
        text = title + "\n" + body
        for edge in result["relationships"]:
            self.assertEqual(text[edge["sentence_start"]:edge["sentence_end"]], edge["sentence"])
            for prefix in ("source", "target"):
                start, end = edge[prefix + "_start"], edge[prefix + "_end"]
                self.assertEqual(text[start:end], edge[prefix + "_quote"])
                self.assertTrue(edge["sentence_start"] <= start < end <= edge["sentence_end"])
            if edge.get("expansion_mode"):
                self.assertTrue(edge["contextual_review_required"])
                self.assertFalse(edge["economic_effect_verified"])
                for span in [edge["relation_clause_span"], *edge["anchor_spans"]]:
                    self.assertEqual(text[span["start"]:span["end"]], span["quote"])
        return result

    def signatures(self, result):
        return {(e["src_ticker"], e["dst_ticker"], e["rel_type"]) for e in result["relationships"]}

    def no_edges(self, body, reason=None, **kwargs):
        result = self.extract(body, **kwargs)
        self.assertFalse(result["relationships"], (body, result))
        if reason:
            self.assertIn(reason, result["flags"], (body, result))
        return result

    def test_old_edges_are_byte_structure_unchanged(self):
        body = "삼성전자는 애플에 반도체를 공급했다."
        self.assertEqual(self.extract(body)["relationships"], old.extract_grounded_relations("🙂 원문", body, ENTITIES)["relationships"])
        self.assertEqual(self.extract(body)["new_candidate_count"], 0)

    def test_two_independent_clauses_only_correct_roles(self):
        body = "삼성전자는 애플에 반도체를 공급했으며, 엔비디아는 카카오에 100억원을 투자했다."
        result = self.extract(body)
        self.assertEqual(self.signatures(result), {("A", "B", "SUPPLY"), ("C", "D", "INVEST")})
        self.assertTrue(all(e["expansion_mode"] == "bounded_local_clause" for e in result["relationships"]))

    def test_semicolon_and_comma_preserve_unicode_crlf_offsets(self):
        body = "🙂 Samsung supplies chips to Apple; Nvidia invested in Kakao.\r\n끝."
        result = self.extract(body)
        # Leading arbitrary text does not turn into an unsupported subject.
        self.assertEqual(self.signatures(result), {("C", "D", "INVEST")})
        clean = self.extract("Samsung supplies chips to Apple; Nvidia invested in Kakao.")
        self.assertEqual(self.signatures(clean), {("A", "B", "SUPPLY"), ("C", "D", "INVEST")})

    def test_local_clause_does_not_bypass_negation_or_plans(self):
        for body in ("계약은 취소됐다며, 삼성전자는 애플에 반도체를 공급했다.",
                     "삼성전자는 애플에 반도체를 공급했으며, 해당 계약은 없다고 밝혔다.",
                     "삼성전자는 애플에 반도체를 공급했으며, 엔비디아는 카카오에 투자할 계획이다."):
            self.no_edges(body)

    def test_supply_customer_lists_are_not_customer_cliques(self):
        for body in ("삼성전자는 애플 및 엔비디아에 반도체를 공급했다.",
                     "삼성전자는 애플, 엔비디아에 반도체를 공급했다.",
                     "Samsung supplies chips to Apple and Nvidia."):
            result = self.extract(body)
            self.assertEqual(self.signatures(result), {("A", "B", "SUPPLY"), ("A", "C", "SUPPLY")})
            self.assertTrue(all(e["collective_role_participants"] == ["A", "B", "C"] for e in result["relationships"]))

    def test_investor_lists_only_point_to_named_target(self):
        for body in ("삼성전자와 애플은 엔비디아에 100억원을 투자했다.",
                     "Samsung and Apple jointly invested in Nvidia."):
            self.assertEqual(self.signatures(self.extract(body)), {("A", "C", "INVEST"), ("B", "C", "INVEST")})

    def test_partner_counterparty_list_does_not_connect_peers(self):
        for body in ("삼성전자는 애플 및 엔비디아와 업무협약을 체결했다.",
                     "Samsung signed a partnership agreement with Apple and Nvidia."):
            result = self.extract(body)
            self.assertEqual(self.signatures(result), {("A", "B", "PARTNER"), ("A", "C", "PARTNER")})
            self.assertTrue(all(e["relation_subtype"] == "explicit_collective_counterparties" for e in result["relationships"]))

    def test_lists_shared_industry_customers_and_membership_no_edges(self):
        for body in ("삼성전자와 애플은 엔비디아의 고객이다.",
                     "Samsung and Apple are customers of Nvidia.",
                     "삼성전자와 애플은 카카오와 함께 컨소시엄에 참여했다.",
                     "삼성전자와 애플, 엔비디아는 반도체 기업이다.",
                     "삼성전자와 애플은 카카오에 공동 투자한 회사다."):
            self.no_edges(body)

    def test_four_way_subject_object_list_not_assumed_cartesian(self):
        self.no_edges("삼성전자와 애플은 엔비디아와 카카오에 100억원을 투자했다.")

    def test_pair_new_completed_phrasing(self):
        cases = [
            ("삼성전자는 애플과 서비스 개발을 위한 업무협약을 체결했다.", ("A", "B", "PARTNER")),
            ("삼성전자 애플에 반도체 공급 계약 체결", ("A", "B", "SUPPLY")),
            ("Samsung signed a contract to supply chips to Apple.", ("A", "B", "SUPPLY")),
            ("Samsung entered into a strategic partnership agreement with Apple.", ("A", "B", "PARTNER")),
            ("Apple is supplied with chips by Samsung.", ("A", "B", "SUPPLY")),
        ]
        for body, expected in cases:
            with self.subTest(body=body): self.assertEqual(self.signatures(self.extract(body)), {expected})

    def test_supply_contract_with_does_not_invent_direction(self):
        self.no_edges("삼성전자 애플과 반도체 공급 계약 체결")

    def test_unknown_actor_inside_purpose_is_deferred(self):
        self.no_edges("삼성전자는 애플과 구글과 업무협약을 체결했다.")
        self.no_edges("삼성전자는 애플과 제삼자가 참여하는 업무협약을 체결했다.")

    def test_single_adjacent_coreference_exact_anchor(self):
        for body in ("삼성전자는 신제품을 출시했다. 이 회사는 애플에 반도체를 공급했다.",
                     "Samsung reported earnings. It supplies chips to Apple.",
                     "Samsung released a new product. This company signed a partnership agreement with Apple."):
            result = self.extract(body)
            self.assertEqual(len(result["relationships"]), 1)
            edge = result["relationships"][0]
            self.assertEqual(edge["expansion_mode"], "adjacent_coreference")
            self.assertEqual(len(edge["anchor_spans"]), 1)
            self.assertLess(edge["source_start"], edge["relation_clause_span"]["start"])

    def test_plural_adjacent_coreference(self):
        for body in ("삼성전자와 애플은 실적을 발표했다. 양사는 업무협약을 체결했다.",
                     "Samsung and Apple reported earnings. The two companies signed a partnership agreement."):
            result = self.extract(body)
            self.assertEqual(self.signatures(result), {("A", "B", "PARTNER" )})
            self.assertEqual(len(result["relationships"][0]["anchor_spans"]), 2)

    def test_coreference_not_across_paragraph_title_or_missing_anchor(self):
        self.no_edges("삼성전자는 신제품을 출시했다.\n이 회사는 애플에 반도체를 공급했다.", "coreference_outside_same_body_paragraph")
        self.no_edges("이 회사는 애플에 반도체를 공급했다.", "coreference_outside_same_body_paragraph", title="삼성전자는 신제품을 출시했다.")
        self.no_edges("이 회사는 애플에 반도체를 공급했다.", "coreference_missing_adjacent_anchor", title="")

    def test_coreference_ambiguous_third_actor_or_employee_is_deferred(self):
        for body in ("삼성전자는 애플과 실적을 발표했다. 이 회사는 엔비디아에 반도체를 공급했다.",
                     "Samsung and Apple employees attended a meeting. They signed a partnership agreement.",
                     "삼성전자와 애플은 엔비디아와 행사에 참석했다. 양사는 업무협약을 체결했다.",
                     "삼성전자는 직원이 신제품을 출시했다고 밝혔다. 이 회사는 애플에 반도체를 공급했다."):
            self.no_edges(body)

    def test_unregistered_antecedents_never_resolve_to_known_company(self):
        for body in ("Apple reported results for a company it acquired. The company supplied chips to Nvidia.",
                     "Apple reported revenue from an unnamed manufacturer. The company supplied chips to Nvidia.",
                     "삼성전자는 매출을 공개한 자회사에 투자했다. 이 회사는 애플에 반도체를 공급했다.",
                     "삼성전자와 애플은 두 기업을 인수했다. 두 기업은 업무협약을 체결했다."):
            self.no_edges(body)

    def test_false_or_disputed_statement_not_rescued_by_clause_split(self):
        for body in ("삼성전자는 애플에 반도체를 공급했다, 라는 주장은 거짓이다.",
                     "Apple supplied chips to Nvidia, a false claim.",
                     "Apple supplied chips to Nvidia, the claim was disputed."):
            self.no_edges(body, "contested_or_false_assertion")

    def test_literal_internal_markers_are_untrusted_source_data(self):
        self.no_edges("Samsung and Apple discussed ⟦99⟧ and Nvidia.", "reserved_matcher_marker_in_source")

    def test_list_announced_future_contract_is_not_completed(self):
        self.no_edges("삼성전자는 애플과 엔비디아와 업무협약을 체결한다.")

    def test_coreference_guard_plans_negation_biography(self):
        for body in ("삼성전자는 신제품을 출시했다. 이 회사는 애플에 공급할 계획이다.",
                     "삼성전자는 신제품을 출시하지 않았다. 이 회사는 애플에 반도체를 공급했다.",
                     "삼성전자는 신제품을 출시했다. 이 회사는 애플에 반도체를 공급하지 않았다.",
                     "삼성전자 출신 임원이 신제품을 출시했다. 이 회사는 애플에 반도체를 공급했다."):
            self.no_edges(body)

    def test_group_subsidiary_and_ambiguous_aliases(self):
        self.no_edges("삼성전자는 애플 및 엔비디아의 자회사에 반도체를 공급했다.")
        self.no_edges("삼성전자는 애플 및 엔비디아서비스에 반도체를 공급했다.")
        duplicate = [*ENTITIES, {"ticker": "E", "company_name": "애플"}]
        self.no_edges("삼성전자는 애플 및 엔비디아에 반도체를 공급했다.", entities=duplicate)

    def test_no_price_industry_false_relationship(self):
        for body in ("삼성전자와 애플의 주가는 엔비디아보다 상승했다.",
                     "Samsung and Apple outperformed Nvidia.",
                     "삼성전자는 애플 및 엔비디아와 같은 업종이다."):
            self.no_edges(body)

    def test_affiliate_guard_on_coreference(self):
        self.no_edges("삼성전자와 애플은 실적을 발표했다. 양사는 경쟁사다.",
                      "competition_conflicts_with_affiliate_evidence", affiliate_pairs=[("A", "B")])

    def test_real_postposed_supplier_does_not_reverse(self):
        entities = [{"ticker": "005930", "company_name": "삼성전자"}, {"ticker": "005380", "company_name": "현대차"}]
        body = "현대차 제네시스 G60의 경우 주변을 모두 볼 수 있는 서라운드뷰 카메라와 후방카메라용 이미지센서(CIS) 아이오셀 오토4C를 삼성전자가 공급하고 있다."
        result = self.extract(body, entities=entities)
        self.assertEqual(self.signatures(result), {("005930", "005380", "SUPPLY")})
        self.assertEqual(result["relationships"][0]["relation_subtype"], "component_supply_for_named_product")
        self.no_edges(body.replace("공급하고 있다", "공급할 예정이다"), entities=entities)

    def test_real_announced_partnership_preserves_company_comma_and_platform_repeat(self):
        entities = [{"ticker": "PAYX", "company_name": "Paychex, Inc.", "aliases": ["Paychex"]},
                    {"ticker": "PYPL", "company_name": "PayPal Holdings, Inc."}]
        body = "In a different update, on January 8, Paychex, Inc. announced a new partnership with PayPal Holdings, Inc. (NASDAQ:PYPL) within the Paychex Flex® Perks platform."
        result = self.extract(body, entities=entities)
        self.assertEqual(self.signatures(result), {("PAYX", "PYPL", "PARTNER")})
        self.no_edges(body.replace("announced", "denied"), entities=entities)
        self.no_edges(body.replace("Paychex Flex", "PayPal Holdings, Inc. Flex"), entities=entities)

    def test_real_expanded_partnership_has_only_explicit_two_partners(self):
        entities = [{"ticker": "PLTR", "company_name": "Palantir Technologies"},
                    {"ticker": "064400", "company_name": "LG CNS"},
                    {"ticker": "003550", "company_name": "LG"}]
        body = "Palantir Technologies (NasdaqGS:PLTR) and LG CNS expanded their partnership to roll out AI and data platforms across the wider LG Group."
        result = self.extract(body, entities=entities)
        self.assertEqual(self.signatures(result), {("064400", "PLTR", "PARTNER")})
        self.no_edges(body.replace("expanded", "will expand"), entities=entities)

    def test_real_completed_supply_contract_is_contract_not_future_delivery(self):
        entities = [{"ticker": "003670", "company_name": "포스코퓨처엠"},
                    {"ticker": "373220", "company_name": "LG에너지솔루션"}]
        body = "포스코퓨처엠은 올해부터 2029년까지 7년간 LG에너지솔루션에 약 30조2595억원 규모의 양극재를 공급하는 계약을 26일 체결했다고 밝혔다."
        result = self.extract(body, entities=entities)
        self.assertEqual(self.signatures(result), {("003670", "373220", "SUPPLY")})
        self.assertEqual(result["relationships"][0]["relation_subtype"], "supply_contract")
        self.no_edges(body.replace("체결했다고 밝혔다", "체결할 계획이다"), entities=entities)


if __name__ == "__main__": unittest.main()
