"""Fiction-only tests for development-scoped historical company context."""
import copy
from datetime import date, timedelta
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_relevance_context as subject


class PrepareContextTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.root = self.parent / (".relevance-context-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.rows = [{"article_id": f"fiction-{i}", "ticker": f"T{i}", "published_at": "2021-04-27",
                      "title": "가상 기사", "body": "가상 원문 전체. 변형하지 않는다.",
                      "company_context": None, "company_name": "가상 회사", "case_kind": "synthetic",
                      "source_split": "train", "source_url": "https://example.invalid/fiction"}
                     for i in range(13)]
        self.ledger = [{"article_id": r["article_id"], "ticker": r["ticker"],
                        "input_sha256": subject.canonical_sha(r), "human_selection": "do not copy",
                        "ai_reason": "do not copy"} for r in self.rows]
        self.source = {"source_id": "fiction-source", "primary_source": True,
                       "source_url": "https://www.sec.gov/fiction", "publication_date_source_url": "https://www.sec.gov/fiction-index",
                       "source_published_at": "2021-03-03", "source_published_at_precision": "date",
                       "observed_at": "2020-12-31", "available_from": "2021-03-04T00:00:00Z",
                       "excerpts": ["Fiction only"], "limitations": ["이후 소유 변동은 미확인이다."]}
        self.contexts = [{"article_id": r["article_id"], "ticker": r["ticker"],
                          "input_sha256": subject.canonical_sha(r), "article_published_at": r["published_at"],
                          "source_ids": ["fiction-source"], "use_scope": "development_13_only",
                          "continuous_ownership_through_article_verified": False, "is_gold": False,
                          "source_publication_lte_article": True, "all_sources_available_by_article_day_start": True,
                          "source_published_at": "2021-03-03", "available_from": "2021-03-04T00:00:00Z",
                          "observed_at": "2020-12-31", "valid_from": "2021-04-27T00:00:00Z",
                          "valid_to": "2021-04-28T00:00:00Z", "valid_to_exclusive": True,
                          "valid_bounds_meaning": "개발 기사일 사용 범위이며 소유 존속기간이 아님.",
                          "context_text": "2020-12-31 관측 관계이며 이후 변동 여부는 확인하지 않았다.",
                          "limitations": ["실제 주가 효과를 뜻하지 않는다."]} for r in self.rows[:5]]
        self.research = {"schema_version": "point_in_time_context_research_v1", "development_case_count": 13,
                         "supported_development_context_count": 5, "sources": [self.source], "contexts": self.contexts}
        self.inputs = self.root / "development_inputs.jsonl"
        self.research_path = self.root / "context_research.json"
        self.write_rows(self.inputs, self.rows)
        paths = [self.root / "differences_from_ai.jsonl", self.root / "human_inputs.jsonl"]
        self.write_rows(paths[0], self.ledger)
        self.write_rows(paths[1], self.rows)
        self.research["input_sources"] = [{"path": str(p), "sha256": subject.digest(p.read_bytes())} for p in paths]

    def tearDown(self):
        if self.root.resolve().parent != self.parent:
            raise RuntimeError("Cleanup outside test workspace")
        shutil.rmtree(self.root)

    @staticmethod
    def write_rows(path, rows):
        path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf8")

    def run_prepare(self):
        self.research_path.write_text(json.dumps(self.research, ensure_ascii=False), encoding="utf8")
        return subject.prepare(inputs=self.inputs, research=self.research_path, out_dir=self.root / "out",
                               expected_research_sha256=subject.digest(self.research_path.read_bytes()))

    def test_exact_five_contexts_only_and_manifest_preserves_originals(self):
        before = self.inputs.read_bytes()
        manifest = self.run_prepare()
        result = subject.parse_rows(Path(manifest["output"]["path"]).read_bytes())
        self.assertEqual(self.inputs.read_bytes(), before)
        self.assertEqual(len(result), 13)
        self.assertEqual(sum(r["company_context"] is not None for r in result), 5)
        for i, (old, new, audit) in enumerate(zip(self.rows, result, manifest["rows"])):
            restored = copy.deepcopy(new)
            restored["company_context"] = None
            self.assertEqual(restored, old)
            self.assertEqual(audit["original_row_sha256"], subject.canonical_sha(old))
            self.assertEqual(audit["context_row_sha256"], subject.canonical_sha(new))
            if i < 5:
                context = new["company_context"]
                self.assertEqual(context["as_of"], "2021-03-04T00:00:00Z")
                self.assertEqual(context["source"], self.source["source_url"])
                self.assertEqual(context["relationships"][0], self.contexts[i]["context_text"])
                self.assertIn(self.source["limitations"][0], context["relationships"])
            else:
                self.assertEqual(new, old)
        self.assertNotIn("human_selection", json.dumps(result))
        self.assertEqual(subject.digest(Path(manifest["output"]["path"]).read_bytes()), manifest["output"]["sha256"])
        self.assertTrue(json.loads((self.root / "out/context_manifest.json").read_text(encoding="utf8"))["complete"])
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            self.run_prepare()

    def test_changed_original_any_of_thirteen_is_rejected_before_output(self):
        self.rows[12]["body"] += " 변형"
        self.write_rows(self.inputs, self.rows)
        with self.assertRaisesRegex(ValueError, "canonical row hash"):
            self.run_prepare()
        self.assertFalse((self.root / "out").exists())

    def test_foreign_article_missing_or_duplicate_rejected(self):
        for mode in ("other", "missing", "duplicate", "context-other"):
            with self.subTest(mode=mode):
                rows, research = copy.deepcopy(self.rows), copy.deepcopy(self.research)
                if mode == "other": rows[12]["article_id"] = "fresh-outside-development"
                if mode == "missing": rows.pop()
                if mode == "duplicate": rows[12] = copy.deepcopy(rows[11])
                if mode == "context-other": research["contexts"][0]["article_id"] = "fresh-outside-development"
                with self.assertRaises(ValueError):
                    subject.build_context_rows(rows, research, self.ledger)

    def test_same_day_and_future_publication_rejected_even_if_metadata_agrees(self):
        for publication in ("2021-04-27", "2021-04-28"):
            with self.subTest(publication=publication):
                research = copy.deepcopy(self.research)
                available = subject.midnight(date.fromisoformat(publication) + timedelta(days=1))
                research["sources"][0].update(source_published_at=publication, available_from=available)
                for context in research["contexts"]:
                    context.update(source_published_at=publication, available_from=available)
                with self.assertRaisesRegex(ValueError, "Future context"):
                    subject.build_context_rows(self.rows, research, self.ledger)

    def test_temporal_identity_scope_and_source_corruption_are_rejected(self):
        mutations = [
            ("source", "primary_source", False), ("source", "source_url", "https://not-official.example/"),
            ("source", "source_published_at_precision", "timestamp"),
            ("source", "observed_at", "2021-04-01"), ("source", "available_from", "2021-03-03T00:00:00Z"),
            ("context", "ticker", "OTHER"), ("context", "input_sha256", "0" * 64),
            ("context", "article_published_at", "2021-04-26"),
            ("context", "use_scope", "all_articles"), ("context", "source_ids", ["missing"]),
            ("context", "source_ids", ["fiction-source", "fiction-source"]),
            ("context", "source_published_at", "2021-03-02"),
            ("context", "available_from", "2021-03-05T00:00:00Z"),
            ("context", "observed_at", "2021-01-01"),
            ("context", "valid_from", "2020-01-01T00:00:00Z"),
            ("context", "valid_to", "2099-01-01T00:00:00Z"),
            ("context", "valid_to_exclusive", False),
            ("context", "continuous_ownership_through_article_verified", True),
            ("context", "is_gold", True),
        ]
        for target, key, value in mutations:
            with self.subTest(target=target, key=key):
                research = copy.deepcopy(self.research)
                research["sources" if target == "source" else "contexts"][0][key] = value
                with self.assertRaises(ValueError):
                    subject.build_context_rows(self.rows, research, self.ledger)

    def test_research_and_provenance_hash_mismatches_are_rejected(self):
        self.research_path.write_text(json.dumps(self.research), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "Research file hash"):
            subject.prepare(inputs=self.inputs, research=self.research_path, out_dir=self.root / "out",
                            expected_research_sha256="0" * 64)
        (self.root / "differences_from_ai.jsonl").write_text("{}\n", encoding="utf8")
        with self.assertRaisesRegex(ValueError, "provenance file changed"):
            self.run_prepare()
        self.assertFalse((self.root / "out").exists())


if __name__ == "__main__":
    unittest.main()
