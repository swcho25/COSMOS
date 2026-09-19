"""A local relevance gate must filter aggregates without inventing relations."""
from copy import deepcopy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import knowledge_graph_lake_gate as gate
import knowledge_graph_news_preview as preview


def fixture():
    title = "공급 계약 발표"
    body = "가온이 나래와 공급 계약을 발표했다."
    text = title + "\n" + body
    inputs, choices, mentions = [], [], []
    for ticker, company in (("A", "가온"), ("B", "나래")):
        row = {
            "article_id": "raw-article", "ticker": ticker, "company_name": company,
            "published_at": "2021-01-01", "title": title, "body": body,
            "source_url": "https://example.test/article",
        }
        inputs.append(row)
        choices.append({
            "article_id": row["article_id"], "ticker": ticker,
            "input_sha256": preview.canonical_input_sha256(row),
            "decision": "include", "relation": "direct",
            "evidence": [{"source": "body", "quote": body}], "reason": "공급 계약의 당사자다.",
            "policy_version": "synthetic-1", "model_provenance": {"model": "synthetic"},
        })
        start = text.index(company)
        mentions.append({
            "record_id": "raw-article", "document_id": "separate-document-id",
            "ticker": ticker, "name_official": company, "confidence": 1.0, "n_mentions": 1,
            "offset_basis": gate.OFFSET_BASIS,
            "spans": [{"start": start, "end": start + len(company), "quote": company}],
        })
    a, b = [row["spans"][0] for row in mentions]
    analysis = {
        "mentions": mentions, "analysis_flags": ["upstream_flag"],
        "relationship_language_support": "ko_only",
        "relationships": [{
            "record_id": "raw-article", "document_id": "separate-document-id",
            "src_ticker": "A", "dst_ticker": "B", "rel_type": "PARTNER", "confidence": 0.8,
            "pattern": "upstream_pattern", "sentence": body,
            "sentence_start": len(title) + 1, "sentence_end": len(text),
            "source_start": a["start"], "source_end": a["end"],
            "target_start": b["start"], "target_end": b["end"],
            "offset_basis": gate.OFFSET_BASIS,
        }],
    }
    return analysis, inputs, choices


class LakeGate(unittest.TestCase):
    def test_both_included_retains_only_existing_relation(self):
        analysis, inputs, choices = fixture()
        result = gate.apply_selection(analysis, inputs, choices)
        self.assertEqual(result["mentions"], analysis["mentions"])
        self.assertEqual(result["relationships"], analysis["relationships"])
        audit = result["knowledge_graph_selection"]
        self.assertEqual(audit["article_id"], "raw-article")
        self.assertEqual(audit["included_tickers"], ["A", "B"])
        self.assertFalse(audit["company_relationship_semantics_verified"])
        self.assertFalse(audit["analysis_source_full_hash_verified"])
        self.assertFalse(audit["production_approved"])
        self.assertTrue(audit["input_selection_hashes_verified"])
        self.assertIn("upstream_flag", result["analysis_flags"])

    def test_exclude_or_review_endpoint_cannot_feed_aggregates(self):
        for decision, relation in (("exclude", "incidental"), ("review", "unclear")):
            analysis, inputs, choices = fixture()
            choices[1].update(decision=decision, relation=relation)
            if decision == "review":
                choices[1]["evidence"] = []
            result = gate.apply_selection(analysis, inputs, choices)
            self.assertEqual([row["ticker"] for row in result["mentions"]], ["A"])
            self.assertEqual(result["relationships"], [])
            audit = result["knowledge_graph_selection"]
            self.assertEqual(audit["dropped_mentions"], 1)
            self.assertEqual(audit["dropped_relationships"], 1)
            self.assertEqual(len(audit["selection_audit"]), 2)
            self.assertEqual(audit["decision_counts"][decision], 1)

    def test_missing_duplicate_or_extra_pairs_fail_closed(self):
        analysis, inputs, choices = fixture()
        for rows, selected in ((inputs, choices[:1]), (inputs[:1], choices[:1]),
                               (inputs, choices + choices[:1]), (inputs + inputs[:1], choices)):
            with self.assertRaises(ValueError):
                gate.apply_selection(analysis, rows, selected)
        extra_input = dict(inputs[0], ticker="C", company_name="다온")
        extra_selection = dict(choices[0], ticker="C", input_sha256=preview.canonical_input_sha256(extra_input))
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            gate.apply_selection(analysis, inputs + [extra_input], choices + [extra_selection])

    def test_wrong_record_ids_or_relationship_endpoints_rejected(self):
        analysis, inputs, choices = fixture()
        for key in ("mentions", "relationships"):
            wrong = deepcopy(analysis)
            wrong[key][0]["record_id"] = "separate-document-id"
            with self.assertRaisesRegex(ValueError, "record_id differs"):
                gate.apply_selection(wrong, inputs, choices)
        for endpoint in ("C", "A"):
            wrong = deepcopy(analysis)
            wrong["relationships"][0]["dst_ticker"] = endpoint
            with self.assertRaisesRegex(ValueError, "endpoint"):
                gate.apply_selection(wrong, inputs, choices)

    def test_source_hash_and_original_literal_offsets_are_checked(self):
        analysis, inputs, choices = fixture()
        wrong = deepcopy(choices)
        wrong[0]["input_sha256"] = "not-the-source-hash"
        with self.assertRaisesRegex(ValueError, "hash"):
            gate.apply_selection(analysis, inputs, wrong)
        for list_name, field, value in (("relationships", "sentence", "없는 원문"),
                                        ("relationships", "target_start", 0),
                                        ("mentions", "offset_basis", "raw_text")):
            wrong = deepcopy(analysis)
            wrong[list_name][0][field] = value
            with self.assertRaises(ValueError):
                gate.apply_selection(wrong, inputs, choices)
        wrong = deepcopy(analysis)
        wrong["mentions"][0]["spans"][0]["quote"] = "없는 기업"
        with self.assertRaisesRegex(ValueError, "quote differs"):
            gate.apply_selection(wrong, inputs, choices)

    def test_deep_copy_preserves_original_and_reapplication_is_rejected(self):
        analysis, inputs, choices = fixture()
        originals = deepcopy((analysis, inputs, choices))
        result = gate.apply_selection(analysis, inputs, choices)
        result["mentions"][0]["spans"][0]["quote"] = "changed"
        result["knowledge_graph_selection"]["selection_audit"][0]["model_provenance"]["model"] = "changed"
        self.assertEqual((analysis, inputs, choices), originals)
        with self.assertRaisesRegex(ValueError, "already applied"):
            gate.apply_selection(result, inputs, choices)
        empty = {"mentions": [], "relationships": [], "analysis_flags": []}
        self.assertEqual(gate.apply_selection(empty, [], [])["knowledge_graph_selection"]["status"], "no_company_candidates")
        with self.assertRaises(ValueError):
            gate.apply_selection(empty, inputs, choices)


if __name__ == "__main__":
    unittest.main()
