"""Meaningful resume/routing/coverage tests with a fake student; no model load."""
import copy
from contextlib import closing
import json
import itertools
from pathlib import Path
import shutil
import sqlite3
import sys
import unittest
import uuid
from unittest.mock import patch

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import kg_cascade_bulk as bulk
import kg_cascade_routing as routing
import knowledge_graph_news_preview as preview
import knowledge_graph_reselection_worker as worker


class FakeStudent:
    def __init__(self, checkpoint, *, device, owner):
        self.manifest_sha256 = preview.file_sha256(Path(checkpoint) / "training_manifest.json")
        self.owner = owner
        owner.loads += 1

    def predict(self, rows):
        self.owner.calls += 1
        if self.owner.fail_call == self.owner.calls:
            raise KeyboardInterrupt("crash before chunk commit")
        values = []
        for row in rows:
            index = int(row["article_id"][1:])
            winner = ("include", "exclude", "review")[index % 3]
            probabilities = {key: .005 for key in routing.LABELS}
            probabilities[winner] = .99
            coverage = {"full_coverage": True, "encoding_completed": True, "truncated": False}
            if index == 4:
                coverage["full_coverage"] = False
            values.append({"article_id": row["article_id"], "ticker": row["ticker"],
                "input_sha256": preview.canonical_input_sha256(row), "student_manifest_sha256": self.manifest_sha256,
                "probabilities": probabilities, "coverage": coverage,
                "requires_fallback": False, "fallback_reasons": [], "evidence": None})
        if self.owner.change_identity:
            values[0]["ticker"] = "WRONG"
        return values

    def close(self):
        self.owner.closes += 1


class Factory:
    def __init__(self, fail_call=None, change_identity=False):
        self.loads = self.calls = self.closes = 0
        self.fail_call, self.change_identity = fail_call, change_identity

    def __call__(self, checkpoint, *, device):
        return FakeStudent(checkpoint, device=device, owner=self)


class BulkTests(unittest.TestCase):
    def setUp(self):
        self.root = HERE / "artifacts" / ("kg_bulk_test_" + uuid.uuid4().hex)
        self.root.mkdir()
        self.source = self.root / "source"
        self.source.mkdir()
        self.checkpoint = self.root / "model"
        self.checkpoint.mkdir()
        self.out = self.root / "output"
        self.rows = [dict(article_id="a" + str(i), ticker="A", company_name="기업", published_at=None,
                          title="기업 계약", body="계약에 관한 기사 " + str(i), source_url=None) for i in range(6)]
        worker.atomic_json(self.checkpoint / "training_manifest.json", {"complete": True, "fake": True})
        self.model_sha = preview.file_sha256(self.checkpoint / "training_manifest.json")
        self.policy_path, self.receipt_path = self.root / "policy.json", self.root / "release.json"
        # Genuine confirmation with enough independent groups; no weakened bound.
        labels = ["include"] * 220 + ["exclude"] * 220
        scores = [{label: .99 if label == winner else .005 for label in routing.LABELS} for winner in labels]
        candidate = routing.tune_policy(labels, scores, dev_ids=["d" + str(i) for i in range(440)],
                                       dev_group_ids=["dg" + str(i) for i in range(440)])
        self.policy = routing.confirm_policy(labels, scores, candidate, cal_ids=["c" + str(i) for i in range(440)],
                                             cal_group_ids=["cg" + str(i) for i in range(440)])
        worker.atomic_json(self.policy_path, self.policy)
        self.write_source(self.rows)
        self.write_release()

    def tearDown(self):
        require_root = (HERE / "artifacts").resolve()
        if not self.root.resolve().is_relative_to(require_root):
            raise ValueError("Cleanup must stay in artifacts")
        shutil.rmtree(self.root)

    def write_source(self, rows, *, expected=None, expected_articles=None):
        inputs = self.source / "inputs.jsonl"
        inputs.write_text("".join(preview.canonical(row) + "\n" for row in rows), encoding="utf8")
        protocol = {"model_receipt_sha256": preview.file_sha256(HERE / "local_models/relevance_model_receipt.json"),
            "code_sha256": {name: preview.file_sha256(HERE / name) for name in
                            ("knowledge_graph_relevance_policy.py", "relevance_sentence_evidence.py")}}
        worker.atomic_json(self.source / "protocol.json", protocol)
        manifest = {"schema": worker.MANIFEST_SCHEMA, "complete": True,
            "protocol_sha256": preview.file_sha256(self.source / "protocol.json"),
            "files": [{"path": inputs.name, "bytes": inputs.stat().st_size,
                       "sha256": preview.file_sha256(inputs), "rows": len(rows)}],
            "expected_unique_pairs": expected if expected is not None else len(rows),
            "counts": {"mention_unique_articles": expected_articles if expected_articles is not None else len({r["article_id"] for r in rows})}}
        worker.atomic_json(self.source / "manifest.json", manifest)
        worker.atomic_json(self.source / "_VERIFIED", {"manifest_sha256": preview.file_sha256(self.source / "manifest.json")})

    def write_release(self, *, gates=True, seconds=.001, setup_seconds=0, pairs=100):
        report = {"pilot_gates": dict.fromkeys(("all_pass", "decision_parity_pass", "coverage_pass", "measured_speed_pass"), gates),
            "student_manifest_sha256": self.model_sha, "policy_sha256": self.policy["policy_sha256"],
            "total_pairs": pairs, "timing_comparison": {"student_total_seconds": seconds + setup_seconds},
            "student_bulk_timing": {"pairs": pairs, "inference_seconds": seconds, "setup_seconds": setup_seconds}}
        worker.atomic_json(self.root / "validation.json", report)
        self.receipt = {"schema": bulk.RELEASE_SCHEMA, "complete": True, "full_fast_pass_allowed": True,
            "source_manifest_sha256": preview.file_sha256(self.source / "manifest.json"),
            "student_module": "kg_cascade_student", "student_manifest_sha256": self.model_sha,
            "routing_policy_file_sha256": preview.file_sha256(self.policy_path),
            "routing_policy_sha256": self.policy["policy_sha256"],
            "validation_report": {"path": "validation.json", "sha256": preview.file_sha256(self.root / "validation.json")}}
        worker.atomic_json(self.receipt_path, self.receipt)

    def run_pass(self, factory=None, **kwargs):
        return bulk.run_bulk(self.source / "manifest.json", self.checkpoint, self.policy_path,
            self.receipt_path, self.out, student_module="kg_cascade_student", device="cpu",
            predictor_factory=factory or Factory(), chunk_size=2, shard_size=2, **kwargs)

    def test_complete_fast_pass_retains_unknown_and_unsupported_not_excluded(self):
        factory = Factory()
        result = self.run_pass(factory)
        self.assertTrue(result["fast_pass_complete"])
        self.assertFalse(result["fully_adjudicated"])
        self.assertEqual(result["teacher_calls"], 0)
        self.assertEqual(result["decisions"], {"include": 2, "exclude": 1, "needs_review": 3})
        self.assertEqual(result["observed_unique_articles"], 6)
        self.assertEqual((factory.loads, factory.closes), (1, 1))
        pending = self.out / result["outputs"]["pending_manifest"]
        manifest = bulk.read(pending)
        self.assertEqual(manifest["schema"], bulk.PENDING_SCHEMA)
        self.assertEqual(manifest["expected_unique_pairs"], 3)
        items = [row for part in manifest["files"] for row in preview.read_jsonl(pending.parent / part["path"])]
        self.assertEqual({row["article_id"] for row in items}, {"a2", "a4", "a5"})
        with self.assertRaises(ValueError):
            worker.inventory(pending)  # Cannot accidentally launch an unbounded teacher.

    def test_pause_limit_resume_does_not_repeat_committed_rows(self):
        first = Factory()
        result = self.run_pass(first, max_pairs=2)
        self.assertEqual(result["state"], "paused_limit")
        self.assertEqual(result["processed_unique_pairs"], 2)
        self.assertFalse((self.out / "fast_pass_receipt.json").exists())
        second = Factory()
        result = self.run_pass(second, resume=True)
        self.assertTrue(result["fast_pass_complete"])
        self.assertEqual(second.calls, 2)

    def test_crash_before_commit_retries_only_uncommitted_chunk(self):
        with self.assertRaises(KeyboardInterrupt):
            self.run_pass(Factory(fail_call=2))
        with closing(sqlite3.connect(self.out / "state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0], 2)
        good = Factory()
        result = self.run_pass(good, resume=True)
        self.assertTrue(result["fast_pass_complete"])
        self.assertEqual(good.calls, 2)

    def test_failed_quality_gate_prevents_any_predictor_load(self):
        self.write_release(gates=False)
        factory = Factory()
        with self.assertRaises(ValueError):
            self.run_pass(factory)
        self.assertEqual(factory.loads, 0)

    def test_release_source_or_policy_binding_is_required(self):
        for key in ("source_manifest_sha256", "student_manifest_sha256", "routing_policy_file_sha256"):
            bad = copy.deepcopy(self.receipt)
            bad[key] = "0" * 64
            worker.atomic_json(self.receipt_path, bad)
            with self.assertRaises(ValueError):
                self.run_pass()

    def test_preflight_pauses_without_loading_any_model(self):
        self.write_release(seconds=10000000)
        factory = Factory()
        result = self.run_pass(factory)
        self.assertEqual(result["state"], "paused_preflight_budget")
        self.assertEqual(factory.loads, 0)
        self.assertFalse((self.out / "fast_pass_receipt.json").exists())

    def test_preflight_scales_inference_but_not_one_time_setup(self):
        # A small one-pair measurement includes a large one-time load. Scaling
        # the whole 18s CLI would incorrectly reject this <60s six-pair run.
        self.write_release(seconds=5.27, setup_seconds=12.73, pairs=1)
        factory = Factory()
        result = self.run_pass(factory, max_pairs=2, max_fast_pass_hours=60 / 3600)
        self.assertEqual(result["state"], "paused_limit")
        self.assertEqual(factory.loads, 1)
        preflight = bulk.read(self.out / "preflight.json")
        self.assertAlmostEqual(preflight["estimated_fast_pass_seconds"], 44.35)
        self.assertTrue(preflight["within_budget"])

    def test_invalid_or_mismatched_bulk_timing_cannot_authorize_loading(self):
        base = bulk.read(self.root / "validation.json")
        bad_values = [dict(pairs=99, inference_seconds=.001, setup_seconds=0),
                      dict(pairs=True, inference_seconds=.001, setup_seconds=0),
                      dict(pairs=100, inference_seconds=0, setup_seconds=0),
                      dict(pairs=100, inference_seconds=.001, setup_seconds=-1),
                      dict(pairs=100, inference_seconds=.001, setup_seconds=True), [], None]
        for timing in bad_values:
            with self.subTest(timing=timing):
                changed = dict(base)
                if timing is None:
                    changed.pop("student_bulk_timing")
                else:
                    changed["student_bulk_timing"] = timing
                worker.atomic_json(self.root / "validation.json", changed)
                receipt = copy.deepcopy(self.receipt)
                receipt["validation_report"]["sha256"] = preview.file_sha256(self.root / "validation.json")
                worker.atomic_json(self.receipt_path, receipt)
                factory = Factory()
                with self.assertRaises(ValueError):
                    self.run_pass(factory)
                self.assertEqual(factory.loads, 0)

    def test_observed_setup_is_not_multiplied_at_64_pair_budget_check(self):
        rows = [dict(self.rows[0], article_id="a" + str(i), body="기사 " + str(i)) for i in range(70)]
        self.write_source(rows)
        self.write_release()
        # One 10s model load plus 35 1ms chunks fits the 10.3s budget. Treating
        # load as per-row work would falsely pause at 64 pairs (~10.97s).
        clock = [0, 10]
        for index in range(35):
            clock.extend((10 + index * .001, 10 + (index + 1) * .001))
        with patch.object(bulk.time, "perf_counter", side_effect=clock):
            result = self.run_pass(max_fast_pass_hours=10.3 / 3600)
        self.assertTrue(result["fast_pass_complete"])
        self.assertAlmostEqual(result["student_setup_seconds"], 10)
        self.assertAlmostEqual(result["student_inference_seconds"], .035)
        self.assertAlmostEqual(result["projected_total_student_seconds"], 10.035)

    def test_wrong_prediction_identity_rolls_back_whole_chunk(self):
        with self.assertRaises(ValueError):
            self.run_pass(Factory(change_identity=True))
        with closing(sqlite3.connect(self.out / "state.sqlite3")) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM pairs").fetchone()[0], 0)

    def test_full_source_article_count_is_audited(self):
        self.write_source(self.rows, expected_articles=7)
        self.write_release()
        with self.assertRaises(ValueError):
            self.run_pass()
        self.assertFalse((self.out / "fast_pass_receipt.json").exists())

    def test_source_change_on_resume_is_rejected(self):
        self.run_pass(max_pairs=2)
        with (self.source / "inputs.jsonl").open("a", encoding="utf8") as stream:
            stream.write("{}\n")
        with self.assertRaises(ValueError):
            self.run_pass(resume=True)

    def test_conflicting_duplicate_pair_cannot_be_reused(self):
        changed = dict(self.rows[0], body="different source")
        self.write_source([self.rows[0], changed], expected=1)
        self.write_release()
        with self.assertRaises(ValueError):
            self.run_pass()
        self.assertFalse((self.out / "fast_pass_receipt.json").exists())

    def test_invalid_scores_and_ties_are_pending_not_auto_exclusion(self):
        factory = Factory()
        predictor = factory(self.checkpoint, device="cpu")
        result = predictor.predict([self.rows[0]])[0]
        for scores in (None, {"include": .5, "exclude": .5, "review": 0},
                       {"include": float("nan"), "exclude": 0, "review": 0}):
            altered = dict(result, probabilities=scores)
            value = bulk.interpret(self.rows[0], altered, self.policy, self.model_sha)
            self.assertEqual(value["decision"], "needs_review")
            preview.canonical(value)  # Invalid numeric input still has a durable audit.

    def test_observed_eta_guard_pauses_after_bounded_probe_and_stays_paused(self):
        rows = [dict(self.rows[0], article_id="a" + str(i), body="기사 " + str(i)) for i in range(70)]
        self.write_source(rows)
        self.write_release()
        first = Factory()
        with patch.object(bulk.time, "perf_counter", side_effect=itertools.count()):
            result = self.run_pass(first, max_fast_pass_hours=.0001)
        self.assertEqual(result["state"], "paused_observed_budget")
        self.assertEqual(result["processed_unique_pairs"], 64)
        self.assertFalse((self.out / "fast_pass_receipt.json").exists())
        second = Factory()
        again = self.run_pass(second, resume=True, max_fast_pass_hours=.0001)
        self.assertEqual(again["state"], "paused_observed_budget")
        self.assertEqual(second.loads, 0)


if __name__ == "__main__":
    unittest.main()
