"""Generic source and graph safety tests, independent of real article judgments."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sys
import shutil
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import knowledge_graph_news_preview as p


@contextmanager
def temporary_workspace():
    # Some managed Windows runtimes create inaccessible tempfile directories.
    # Keep test files under the workspace and validate the cleanup boundary.
    parent = (Path(__file__).resolve().parents[1] / "artifacts").resolve()
    root = parent / ("test_kg_preview_" + uuid.uuid4().hex)
    root.mkdir(parents=True, exist_ok=False)
    try:
        yield root
    finally:
        resolved = root.resolve()
        if resolved.parent != parent or not resolved.name.startswith("test_kg_preview_"):
            raise ValueError("Test cleanup escaped the artifact directory")
        shutil.rmtree(resolved)


def fixture(ticker="FICTION", company="가온"):
    row = {
        "article_id": "story-1", "ticker": ticker, "company_name": company,
        "published_at": "2021-01-01", "title": "가온의 부품 공급 계약",
        "body": "가온은 지난해 공급 계약을 발표했다. 인터뷰한 인물은 과거 나래에서 근무했다.",
        "source_url": "https://example.test/story-1",
    }
    selection = {
        "article_id": row["article_id"], "ticker": ticker,
        "input_sha256": p.canonical_input_sha256(row), "decision": "include", "relation": "direct",
        "evidence": [{"source": "body", "quote": "가온은 지난해 공급 계약을 발표했다."}],
        "reason": "과거 공급 계약도 기업의 경제적 활동에 대한 정보다.",
        "policy_version": "synthetic-policy-1", "model_provenance": {"model": "synthetic"},
    }
    return row, selection


class Preview(unittest.TestCase):
    def test_same_article_decisions_are_company_specific(self):
        first, include = fixture()
        second, exclude = fixture("NARAE", "나래")
        exclude.update(decision="exclude", relation="incidental", reason="인물의 과거 직장만 언급했다.",
                       evidence=[{"source": "body", "quote": "인터뷰한 인물은 과거 나래에서 근무했다."}])
        payloads, summary = p.prepare_preview([first, second], [include, exclude])
        self.assertEqual(summary["input_pairs"], 2)
        self.assertEqual(summary["input_articles"], 1)
        self.assertEqual(summary["decision_counts"], {"exclude": 1, "include": 1, "review": 0})
        self.assertEqual(len(payloads["news_nodes.jsonl"]), 1)
        self.assertEqual([x["ticker"] for x in payloads["company_nodes.jsonl"]], ["FICTION"])
        self.assertEqual([x["ticker"] for x in payloads["selected_links.jsonl"]], ["FICTION"])

    def test_cooccurrence_never_creates_company_company_edges(self):
        first, a = fixture()
        second, b = fixture("OTHER", "다른기업")
        payloads, summary = p.prepare_preview([first, second], [a, b])
        self.assertEqual(summary["company_company_edges"], 0)
        for edge in payloads["selected_links.jsonl"]:
            self.assertTrue(edge["source_node"].startswith("news:"))
            self.assertTrue(edge["target_node"].startswith("company:"))
            self.assertEqual(edge["predicate"], p.PREDICATE)

    def test_past_event_can_be_included_without_becoming_training_gold(self):
        row, selection = fixture()
        payloads, summary = p.prepare_preview([row], [selection])
        edge = payloads["selected_links.jsonl"][0]
        self.assertEqual(edge["published_at"], "2021-01-01")
        self.assertIsNone(edge["event_date"])
        self.assertEqual(edge["event_date_status"], "not_extracted")
        self.assertFalse(edge["human_gold"])
        self.assertFalse(edge["ready_for_gnn_training"])
        self.assertFalse(summary["production_approved"])
        self.assertEqual(summary["metrics_are"], "descriptive_counts_not_accuracy")

    def test_exact_seven_field_hash_is_order_independent(self):
        row, _ = fixture()
        expected = hashlib.sha256(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf8")).hexdigest()
        self.assertEqual(p.canonical_input_sha256(row), expected)
        reversed_row = dict(reversed(list(row.items())))
        reversed_row["ignored_metadata"] = "different metadata is explicitly outside the hash contract"
        self.assertEqual(p.canonical_input_sha256(reversed_row), expected)

    def test_source_content_and_company_changes_break_hash(self):
        row, selection = fixture()
        for field in p.INPUT_FIELDS:
            changed = dict(row, **{field: row[field] + " changed"})
            with self.assertRaises(ValueError, msg=field):
                p.validate_selection(selection, changed)

    def test_invented_or_wrong_source_quotes_are_rejected(self):
        row, selection = fixture()
        for evidence in (
            [{"source": "body", "quote": "주가가 반드시 오른다."}],
            [{"source": "title", "quote": selection["evidence"][0]["quote"]}],
            [{"source": "body", "quote": " "}],
            selection["evidence"] * 2,
        ):
            with self.assertRaises(ValueError):
                p.validate_selection(dict(selection, evidence=evidence), row)

    def test_review_without_evidence_creates_no_nodes(self):
        row, selection = fixture()
        selection.update(decision="review", relation="unclear", evidence=[], reason="관련성 근거가 불충분하다.")
        payloads, summary = p.prepare_preview([row], [selection])
        self.assertEqual(summary["selected_edges"], 0)
        self.assertEqual(payloads["news_nodes.jsonl"], [])
        self.assertEqual(payloads["company_nodes.jsonl"], [])
        self.assertEqual(len(payloads["review.jsonl"]), 1)

    def test_include_and_exclude_require_compatible_relation_and_evidence(self):
        row, selection = fixture()
        for update in (
            {"relation": "incidental"}, {"relation": "unclear"}, {"evidence": []},
            {"decision": "exclude", "relation": "direct"},
            {"decision": "exclude", "relation": "incidental", "evidence": []},
        ):
            with self.assertRaises(ValueError):
                p.validate_selection(dict(selection, **update), row)

    def test_duplicate_missing_extra_decisions_and_inputs_rejected(self):
        row, selection = fixture()
        another, another_selection = fixture("NARAE", "나래")
        for rows, choices in (
            ([row], []), ([row], [selection, selection]), ([row, row], [selection]),
            ([row], [selection, another_selection]), ([row, another], [selection]),
        ):
            with self.assertRaises(ValueError):
                p.prepare_preview(rows, choices)

    def test_same_article_id_cannot_hide_different_content(self):
        row, selection = fixture()
        another, another_selection = fixture("NARAE", "나래")
        another["body"] += " 별도로 수정된 기사다."
        another_selection["input_sha256"] = p.canonical_input_sha256(another)
        with self.assertRaisesRegex(ValueError, "Conflicting source content"):
            p.prepare_preview([row, another], [selection, another_selection])

    def test_english_text_and_unknown_publication_are_preserved(self):
        row, selection = fixture()
        row.update(title="Company announces a supply agreement", body="The company signed a supply agreement.", published_at=None, source_url=None)
        selection.update(input_sha256=p.canonical_input_sha256(row), evidence=[{"source": "body", "quote": row["body"]}])
        payloads, _ = p.prepare_preview([row], [selection])
        self.assertEqual(payloads["news_nodes.jsonl"][0]["body"], row["body"])
        self.assertIsNone(payloads["selected_links.jsonl"][0]["published_at"])

    def test_selection_schema_cannot_claim_human_gold(self):
        row, selection = fixture()
        for update in ({"human_gold": True}, {"model_provenance": {}}, {"policy_version": ""}):
            with self.assertRaises(ValueError):
                p.validate_selection(dict(selection, **update), row)

    def test_complete_manifest_counts_and_hashes_match_payloads(self):
        row, selection = fixture()
        with temporary_workspace() as temporary:
            root = Path(temporary)
            inputs = root / "inputs.jsonl"
            choices = root / "selections.jsonl"
            inputs.write_text(p.canonical(row) + "\n", encoding="utf8")
            choices.write_text(p.canonical(selection) + "\n", encoding="utf8")
            output = root / "preview"
            manifest = p.build_preview(inputs, choices, output)
            self.assertTrue(manifest["complete"])
            self.assertEqual(manifest, json.loads((output / "manifest.json").read_text(encoding="utf8")))
            self.assertFalse((output / "manifest.json.pending").exists())
            for name, expected in manifest["files"].items():
                self.assertEqual(p.file_sha256(output / name), expected["sha256"])
                self.assertEqual(len(p.read_jsonl(output / name)), expected["records"])
            with self.assertRaisesRegex(ValueError, "refusing overwrite"):
                p.build_preview(inputs, choices, output)

    def test_invalid_selection_leaves_no_output_directory(self):
        row, selection = fixture()
        selection["input_sha256"] = "wrong"
        with temporary_workspace() as temporary:
            root = Path(temporary)
            inputs, choices = root / "inputs.jsonl", root / "selections.jsonl"
            inputs.write_text(p.canonical(row) + "\n", encoding="utf8")
            choices.write_text(p.canonical(selection) + "\n", encoding="utf8")
            with self.assertRaises(ValueError):
                p.build_preview(inputs, choices, root / "preview")
            self.assertFalse((root / "preview").exists())

    def test_jsonl_duplicate_keys_nonfinite_and_blank_lines_rejected(self):
        with temporary_workspace() as temporary:
            path = Path(temporary) / "bad.jsonl"
            for content in ('{"a":1,"a":2}\n', '{"value":NaN}\n', '\n', '[]\n'):
                path.write_text(content, encoding="utf8")
                with self.assertRaises(ValueError):
                    p.read_jsonl(path)


if __name__ == "__main__":
    unittest.main()
