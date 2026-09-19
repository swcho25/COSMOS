"""The100 is fresh and blinded; evaluation32 is an unchanged balanced subset."""
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_relevance_two_stage as sample


class TwoStageDataTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.root = self.parent / (".two-stage-data-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.source = self.root / "training_candidates.jsonl"
        self.companies = self.root / "companies.csv"
        self.companies.write_text("ticker,market,name_official\nK1,KOSPI,가상회사1\nK2,KOSPI,가상회사2\nU1,NASDAQ,Fiction One\nU2,NASDAQ,Fiction Two\n", encoding="utf8")
        self.rows = [self.article(region + str(index), region) for region in sample.REGIONS for index in range(96)]
        prior_dir = self.root / "previous32"
        prior_dir.mkdir()
        self.prior = prior_dir / "new_inputs.jsonl"
        names = sample.previous.company_names(self.companies)
        self.prior_rows = [sample.previous.make_input(row, sample.pilot.minimal_clean(row), row["companies"][0]["ticker"], names)[0]
                           for row in self.rows[:16] + self.rows[96:112]]
        self.write_rows(self.prior, self.prior_rows)
        self.save_prior_manifest()
        self.review = self.root / "prior_review.jsonl"
        known = self.article("known")
        self.write_rows(self.review, [{"record_id": "known", "reason": "unused prior AI decision"}])
        self.legacy, self.metadata = self.root / "legacy.jsonl", self.root / "metadata.jsonl"
        held = {**self.article("held"), "split": "test"}
        self.write_rows(self.legacy, [held])
        self.write_rows(self.metadata, [{"record_id": "held", "url": held["url"], "body_sha256": held["body_sha256"]}])
        self.rows.extend([known,
            {**self.article("prior-body-copy"), "body": self.rows[0]["body"]},
            {**self.article("prior-clean-copy"), "body": self.rows[0]["body"] + "\n관련 뉴스\n버려질 꼬리 내용"},
            {**self.article("prior-url-copy"), "url": self.rows[0]["url"] + "?utm_source=tracking"},
            {**self.article("prior-hash-copy"), "body_sha256": self.rows[0]["body_sha256"]},
            {**self.article("held-copy"), "body": held["body"]},
            {**self.article("short"), "body": "가" * 199}, {**self.article("long"), "body": "나" * 6001},
            {**self.article("min-inclusive"), "body": "다" * 200}, {**self.article("max-inclusive"), "body": "라" * 6000},
            {**self.article("future"), "published_date": "2023-01-01"}])
        self.save_source()
        self.out = self.root / "output"

    def tearDown(self):
        if self.root.resolve().parent != self.parent:
            raise RuntimeError("Cleanup escaped workspace tests")
        shutil.rmtree(self.root)

    @staticmethod
    def article(identity, region="domestic"):
        body = ("이 본문은 가상의 중립적인 자료이고 식별자는 " + identity + " 이다. ") * 10
        return {"record_id": identity, "region": region, "published_date": "2021-06-15", "title": "가상자료 " + identity,
                "body": body, "body_sha256": sample.previous.digest(body), "url": "https://example.invalid/" + identity,
                "companies": [{"ticker": ticker} for ticker in (("K1", "K2") if region == "domestic" else ("U1", "U2"))],
                "expected_selection": "DO_NOT_COPY", "future_return": "DO_NOT_COPY", "source_dataset": "fiction"}

    @staticmethod
    def write_rows(path, rows):
        Path(path).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf8")

    def save_source(self):
        self.write_rows(self.source, self.rows)
        self.source.with_name("manifest.json").write_text(json.dumps({"complete": True,
            "training_candidates": len(self.rows), "training_candidates_sha256": sample.pilot.sha(self.source)}), encoding="utf8")

    def save_prior_manifest(self):
        self.prior.with_name("manifest.json").write_text(json.dumps({"pairs": 32,
            "files": {self.prior.name: {"sha256": sample.pilot.sha(self.prior)}}}), encoding="utf8")

    def prepare(self, out=None):
        return sample.prepare(source=self.source, companies=self.companies, review_paths=[self.review], prior_followup=self.prior,
                              legacy_articles=self.legacy, legacy_metadata=self.metadata, out=out or self.out)

    def test_human100_is_blank_and_eval32_has_exact_matching_inputs_and_hashes(self):
        protected = [self.prior, self.prior.with_name("manifest.json"), self.source, self.review, self.legacy, self.metadata]
        before = {path: path.read_bytes() for path in protected}
        result = self.prepare()
        self.assertEqual(result["human_sample_regions"], {"domestic": 50, "overseas": 50})
        self.assertEqual(result["evaluation_regions"], {"domestic": 16, "overseas": 16})
        self.assertEqual(result["first_exclusion_reason_counts"]["outside_clean_body_length_cohort_200_6000"], 2)
        self.assertEqual(result["exclusion_identity_coverage"]["known_reviews_including_previous_followup"]["ids"], 33)
        original = {row["record_id"]: row for row in self.rows}
        humans = list(sample.pilot.rows(self.out / "human_inputs.jsonl"))
        review = list(sample.pilot.rows(self.out / "human_review_packet.jsonl"))
        evaluation = list(sample.pilot.rows(self.out / "eval_inputs.jsonl"))
        self.assertEqual(len(humans), 100)
        self.assertEqual(len({row["article_id"] for row in humans}), 100)
        self.assertFalse({row["article_id"] for row in humans} & {row["article_id"] for row in self.prior_rows})
        indexed = {row["article_id"]: row for row in humans}
        for row, packet in zip(humans, review):
            self.assertEqual(tuple(row), sample.pilot.INPUT_FIELDS)
            self.assertEqual(row["body"], sample.pilot.minimal_clean(original[row["article_id"]])["body"])
            self.assertTrue(200 <= len(row["body"]) <= 6000)
            self.assertEqual({field: packet[field] for field in sample.HUMAN_FIELDS}, sample.HUMAN_FIELDS)
            self.assertFalse(set(packet) & {"prediction", "ai_reference", "expected_selection", "fact_audit"})
            self.assertEqual(packet["input_sha256"], sample.previous.digest(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))))
        for row in evaluation:
            self.assertEqual(row, indexed[row["article_id"]])
        self.assertEqual(evaluation, list(sample.pilot.rows(self.out / "review_inputs_01.jsonl")) + list(sample.pilot.rows(self.out / "review_inputs_02.jsonl")))
        for packet in (evaluation[:16], evaluation[16:]):
            self.assertEqual(sum(original[row["article_id"]]["region"] == "domestic" for row in packet), 8)
        for name, info in result["files"].items():
            self.assertEqual(sample.pilot.sha(self.out / name), info["sha256"])
        self.assertNotIn("DO_NOT_COPY", (self.out / "human_review_packet.jsonl").read_text(encoding="utf8"))
        self.assertEqual({path: path.read_bytes() for path in protected}, before)
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_source_order_target_order_and_unused_labels_cannot_change_sample_or_subset(self):
        self.prepare()
        expected = {name: (self.out / name).read_bytes() for name in
                    ("human_inputs.jsonl", "human_review_packet.jsonl", "eval_inputs.jsonl", "review_inputs_01.jsonl", "review_inputs_02.jsonl")}
        self.rows.reverse()
        for row in self.rows:
            row["companies"].reverse()
            row["expected_selection"] = "DIFFERENT_UNUSED_LABEL"
        self.save_source()
        second = self.root / "repeat"
        self.prepare(second)
        self.assertEqual({name: (second / name).read_bytes() for name in expected}, expected)

    def test_previous_followup_manifest_tamper_fails_before_output(self):
        with self.prior.open("a", encoding="utf8") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "Previous follow-up32 differs"):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_not_enough_unique_bodies_does_not_silently_reduce_human_quota(self):
        candidates = [{"article_id": str(n), "region": region, "rank": str(n).zfill(3),
                       "bodies": {region}, "urls": set(), "source_hashes": set()}
                      for region in sample.REGIONS for n in range(100)]
        with self.assertRaisesRegex(ValueError, "Fewer than 50 unique"):
            sample.select_unique(candidates)

    def test_subset_is_exact_lowest_evaluation_hash_per_region(self):
        selected = [{"article_id": region + str(n), "region": region} for region in sample.REGIONS for n in range(50)]
        actual = sample.evaluation_subset(selected)
        expected = {row["article_id"] for region in sample.REGIONS for row in
                    sorted((x for x in selected if x["region"] == region), key=lambda x: sample.rank(x["article_id"], "evaluation"))[:16]}
        self.assertEqual({row["article_id"] for row in actual}, expected)
        self.assertEqual(actual, sample.evaluation_subset(list(reversed(selected))))


if __name__ == "__main__":
    unittest.main()
