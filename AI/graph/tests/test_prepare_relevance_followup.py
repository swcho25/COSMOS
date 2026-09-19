"""Fresh deterministic text sampling excludes prior exposure and held-out identity."""
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_relevance_followup as followup
import prepare_local_relevance_pilot as pilot


class FollowupPreparationTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.root = self.parent / (".followup-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.source = self.root / "training_candidates.jsonl"
        self.companies = self.root / "companies.csv"
        self.companies.write_text("ticker,market,name_official\n005930,KOSPI,삼성전자\n000660,KOSPI,SK하이닉스\nAAPL,NASDAQ,Apple\nMSFT,NASDAQ,Microsoft\n", encoding="utf8")
        self.reviews = self.root / "known_reviews.jsonl"
        self.legacy = self.root / "legacy.jsonl"
        self.metadata = self.root / "legacy_metadata.jsonl"
        self.out = self.root / "prepared"
        self.source_rows = [self.article(region + str(index), region) for region in followup.REGIONS for index in range(40)]
        prior = self.article("prior")
        heldout = {**self.article("held-out"), "split": "test"}
        indexed = {**self.article("numeric-review"), "split": "train"}
        self.write_rows(self.legacy, [indexed, heldout])
        self.write_rows(self.metadata, [{"record_id": "numeric-review", "url": indexed["url"]},
                                        {"record_id": "held-out", "url": heldout["url"], "body_sha256": "f" * 64}])
        # Verdict text is present in source contracts but never accessed as a selector.
        self.write_rows(self.reviews, [{"record_id": "prior", "expected_label": "forbidden-reference-value"}, {"article": 0}])
        self.source_rows += [
            prior, indexed,
            {**self.article("review-body-copy"), "body": prior["body"]},
            {**self.article("review-clean-copy"), "body": prior["body"] + "\n관련 뉴스\n버려질 꼬리 내용"},
            {**self.article("review-url-copy"), "url": prior["url"] + "?utm_source=tracking"},
            {**self.article("holdout-body-copy"), "body": heldout["body"]},
            {**self.article("holdout-raw-hash-copy"), "body_sha256": "f" * 64},
            {**self.article("body-199"), "body": "가" * 199},
            {**self.article("body-200"), "body": "나" * 200},
            {**self.article("body-6000"), "body": "다" * 6000},
            {**self.article("body-6001"), "body": "라" * 6001},
            {**self.article("later-year"), "published_date": "2023-01-01"},
        ]
        self.save_source()

    def tearDown(self):
        if self.root.resolve().parent != self.parent:
            raise RuntimeError("Cleanup escaped test directory")
        shutil.rmtree(self.root)

    @staticmethod
    def article(identity, region="domestic"):
        return {"record_id": identity, "title": "자료 기사 " + identity,
                "body": ("고유 자료 " + identity + "에 관한 본문이며 당시 기사 원문을 담았다. ") * 12,
                "url": "https://example.invalid/news/" + identity,
                "published_date": "2021-03-02", "region": region, "source_dataset": "fixture",
                "companies": [{"ticker": ticker} for ticker in (("005930", "000660") if region == "domestic" else ("AAPL", "MSFT"))],
                "metadata_json": {"future_return": "never-copy"}, "expected_label": "never-copy", "y_1d": "never-copy"}

    @staticmethod
    def write_rows(path, rows):
        Path(path).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf8")

    def save_source(self):
        self.write_rows(self.source, self.source_rows)
        self.source.with_name("manifest.json").write_text(json.dumps({"complete": True, "training_candidates": len(self.source_rows),
                                                                    "training_candidates_sha256": pilot.sha(self.source)}), encoding="utf8")

    def prepare(self, out=None):
        return followup.prepare(source=self.source, companies=self.companies, review_paths=[self.reviews],
                                legacy_articles=self.legacy, legacy_metadata=self.metadata, out=out or self.out)

    def test_full_cohort_exclusions_original_text_and_balanced_review_packets(self):
        dependencies = [self.source, self.source.with_name("manifest.json"), self.companies, self.reviews, self.legacy, self.metadata]
        before = {path: path.read_bytes() for path in dependencies}
        manifest = self.prepare()
        inputs = list(pilot.rows(self.out / "new_inputs.jsonl"))
        self.assertEqual((manifest["articles"], manifest["regions"]), (32, {"domestic": 16, "overseas": 16}))
        self.assertEqual(manifest["eligible_by_region"], {"domestic": 42, "overseas": 40})
        self.assertEqual(manifest["first_exclusion_reason_counts"], {"known_review_and_or_holdout_overlap": 7,
                         "outside_clean_body_length_cohort_200_6000": 2, "outside_2020_2022": 1})
        self.assertGreater(manifest["overlap_match_counts_nonexclusive"]["holdout_stored_source_body_hash"], 0)
        originals = {row["record_id"]: row for row in self.source_rows}
        for row in inputs:
            self.assertEqual(tuple(row), pilot.INPUT_FIELDS)
            self.assertEqual(row["body"], pilot.minimal_clean(originals[row["article_id"]])["body"])
            self.assertIsNone(row["company_context"])
            self.assertEqual(row["source_split"], "train")
            self.assertIn(row["ticker"], {c["ticker"] for c in originals[row["article_id"]]["companies"]})
            self.assertTrue(200 <= len(row["body"]) <= 6000)
        self.assertEqual(len({row["article_id"] for row in inputs}), 32)
        self.assertNotIn("never-copy", (self.out / "new_inputs.jsonl").read_text(encoding="utf8"))
        self.assertEqual(list(pilot.rows(self.out / "review_inputs_01.jsonl")), inputs[:16])
        self.assertEqual(list(pilot.rows(self.out / "review_inputs_02.jsonl")), inputs[16:])
        for packet in (inputs[:16], inputs[16:]):
            self.assertEqual(sum(originals[row["article_id"]]["region"] == "domestic" for row in packet), 8)
        self.assertEqual({path: path.read_bytes() for path in dependencies}, before)
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_selection_is_invariant_to_source_and_candidate_order(self):
        self.prepare()
        before = (self.out / "new_inputs.jsonl").read_bytes()
        self.source_rows.reverse()
        for row in self.source_rows:
            row["companies"].reverse()
            row["expected_label"] = "another-unused-value"
        self.save_source()
        second = self.root / "second"
        self.prepare(second)
        self.assertEqual((second / "new_inputs.jsonl").read_bytes(), before)

    def test_missing_review_body_and_invalid_manifest_fail_before_output(self):
        self.write_rows(self.reviews, [{"record_id": "absent-prior-identity"}])
        with self.assertRaisesRegex(ValueError, "bodies could not all be recovered"):
            self.prepare()
        self.assertFalse(self.out.exists())
        manifest_path = self.source.with_name("manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf8"))
        manifest["training_candidates_sha256"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf8")
        with self.assertRaisesRegex(ValueError, "checksum mismatch"):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_source_hash_namespaces_and_review_annotation_are_not_body_text(self):
        index = followup.identity_index()
        followup.add_identity(index, {"record_id": "x", "body_clean": True, "excerpt": "partial only",
                                      "body_sha256": "a" * 64})
        self.assertEqual(index["bodies"], set())
        self.assertEqual(followup.match_kinds("new", set(), set(), index, {("body_dedup_sha256", "a" * 64)}), [])
        self.assertEqual(followup.match_kinds("new", set(), set(), index, {("body_sha256", "a" * 64)}), ["stored_source_body_hash"])

    def test_duplicate_identity_is_not_silently_resampled(self):
        self.source_rows.append(copy.deepcopy(self.source_rows[0]))
        self.save_source()
        with self.assertRaisesRegex(ValueError, "repeated source record identity"):
            self.prepare()
        self.assertFalse(self.out.exists())


if __name__ == "__main__":
    unittest.main()
