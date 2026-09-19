"""Tiny CPU fixtures: paused-prefix coverage, preserved inputs and partial scope."""
from contextlib import closing
import copy
import json
from pathlib import Path
import sqlite3
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_provisional_bulk as bulk
import merge_kg_provisional_partitions as full
import merge_kg_provisional_partial as partial
import test_merge_kg_provisional_partitions as complete_fixtures
import test_kg_provisional_bulk as fixtures


class PartialMergeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = complete_fixtures.MergeTests()
        self.fixture.setUp()
        self.root, self.plan_path = self.fixture.root, self.fixture.plan_path
        self.plan = self.fixture.plan
        self.workers = dict(self.fixture.workers)
        self.workers["pc1"] = self.root / "paused_pc1"
        part = next(p for p in self.plan["partitions"] if p["id"] == "pc1")
        manifest = self.fixture.split / part["input_manifest"]
        bulk.run_bulk(manifest, self.fixture.fixture.checkpoint, self.workers["pc1"], device="cpu", max_pairs=1,
                      predictor_factory=fixtures.Factory(), chunk_size=2, shard_size=2)
        (self.workers["pc1"] / "STOP").write_text("keep paused", encoding="utf8")
        bulk.run_bulk(manifest, self.fixture.fixture.checkpoint, self.workers["pc1"], device="cpu", resume=True,
                      stop_file=self.workers["pc1"] / "STOP", predictor_factory=fixtures.Factory(), chunk_size=2, shard_size=2)
        pc2 = next(p for p in self.plan["partitions"] if p["id"] == "pc2")
        self.covered = self.plan["base"]["committed_unique_pairs"] + 1 + pc2["expected_unique_pairs"]
        self.missing = self.plan["parent"]["expected_unique_pairs"] - self.covered

    def tearDown(self):
        self.fixture.tearDown()

    def run_merge(self, name="partial", **kwargs):
        return partial.merge_partial(self.plan_path, self.workers, self.root / name, shard_size=2, **kwargs)

    def test_partial_artifact_preserves_exact_data_and_known_missing_ranges(self):
        immutable = [self.fixture.split / "base/state.sqlite3", self.workers["pc1"] / "STOP",
                     *(root / name for root in self.workers.values() for name in ("state.sqlite3", "protocol.json", "status.json"))]
        before = {p: bulk.preview.file_sha256(p) for p in immutable}
        result = self.run_merge(expected_covered=self.covered, expected_unprocessed=self.missing)
        self.assertTrue(result["artifact_complete"])
        self.assertFalse(result["complete"])
        self.assertFalse(result["full_population_complete"])
        self.assertFalse(result["validated_auto_selection"])
        self.assertEqual(result["covered_unique_pairs"], self.covered)
        self.assertEqual(result["unprocessed_unique_pairs"], self.missing)
        self.assertEqual(result["teacher_calls"], 0)
        self.assertFalse((self.root / "partial/provisional_receipt.json").exists())
        self.assertEqual(result["state_sqlite_sha256"], bulk.preview.file_sha256(self.root / "partial/state.sqlite3"))
        coverage = bulk.read(self.root / "partial/coverage.json")
        self.assertEqual(sum(x["unprocessed_rows"] for x in coverage["unprocessed_ranges"]), self.missing)
        self.assertTrue(all(x["component"] == "pc1" and x["byte_end"] > x["byte_start"] for x in coverage["unprocessed_ranges"]))
        with closing(sqlite3.connect(self.root / "partial/state.sqlite3")) as db:
            values = db.execute("SELECT article_id,ticker,decision,component,input_json,result_json FROM pairs").fetchall()
        self.assertEqual(len(values), self.covered)
        for article, ticker, decision, component, input_json, payload in values:
            source, value = json.loads(input_json), json.loads(payload)
            self.assertEqual(source["article_id"], article)
            self.assertEqual(source["ticker"], ticker)
            self.assertEqual(set(source), set(bulk.preview.INPUT_FIELDS))
            self.assertEqual(bulk.preview.canonical_input_sha256(source), value["input_sha256"])
            original_db = self.fixture.split / "base/state.sqlite3" if component == "base" else self.workers[component] / "state.sqlite3"
            with closing(sqlite3.connect(original_db)) as original:
                prior = json.loads(original.execute("SELECT result_json FROM pairs WHERE article_id=? AND ticker=?", (article, ticker)).fetchone()[0])
            self.assertEqual(value["raw_prediction"], prior["raw_prediction"])
            self.assertEqual(value["component_ref"]["source_ref"], prior["source_ref"])
        exported = bulk.read(self.root / "partial/exports/manifest.json")
        self.assertEqual(sum(x["rows"] for x in exported["include_with_source"]), result["decisions"]["include"])
        for part in exported["include_with_source"]:
            path = self.root / "partial/exports" / part["path"]
            for line in path.read_text(encoding="utf8").splitlines():
                source = json.loads(line)
                self.assertFalse(source["full_population_complete"])
                self.assertEqual(bulk.preview.canonical_input_sha256(source), source["input_sha256"])
        for path, digest in before.items():
            self.assertEqual(bulk.preview.file_sha256(path), digest)

    def test_expected_counts_cannot_relax_actual_missing_ranges(self):
        with self.assertRaisesRegex(ValueError, "expected_unprocessed"):
            self.run_merge("wrong_expected", expected_unprocessed=self.missing + 1)
        self.assertFalse((self.root / "wrong_expected/partial_receipt.json").exists())
        plan = copy.deepcopy(self.plan)
        target = next(p for p in plan["partitions"] if p["id"] == "pc1")["mappings"][0]
        target["parent_start_byte"] += 1
        self.fixture.rewrite_plan(plan)
        with self.assertRaisesRegex(ValueError, "Mapping differs"):
            self.run_merge("wrong_mapping")

    def test_unpaused_or_locked_worker_is_refused(self):
        with bulk.durable.WorkerLock(self.workers["pc1"] / "provisional.lock"):
            with self.assertRaisesRegex(RuntimeError, "Another worker"):
                self.run_merge("locked")
        path = self.workers["pc1"] / "status.json"
        value = bulk.read(path)
        value["state"] = "running"
        bulk.atomic_json(path, value)
        with self.assertRaisesRegex(ValueError, "explicitly paused"):
            self.run_merge("running")

    def test_lost_committed_row_is_not_counted_as_allowed_unprocessed(self):
        with closing(sqlite3.connect(self.workers["pc1"] / "state.sqlite3")) as db:
            db.execute("DELETE FROM pairs WHERE rowid=(SELECT MIN(rowid) FROM pairs)")
            db.commit()
        with self.assertRaisesRegex(ValueError, "counters differ"):
            self.run_merge("lost_row")
        self.assertFalse((self.root / "lost_row/partial_receipt.json").exists())

    def test_complete_worker_still_requires_valid_receipt_and_full_merger_stays_strict(self):
        with self.assertRaises((FileNotFoundError, ValueError)):
            full.merge(self.plan_path, self.workers, self.root / "full_must_fail", shard_size=2)
        path = self.workers["pc2"] / "provisional_receipt.json"
        value = bulk.read(path)
        value["complete"] = False
        bulk.atomic_json(path, value)
        with self.assertRaisesRegex(ValueError, "incomplete|receipt"):
            self.run_merge("bad_complete_worker")
        self.assertFalse((self.root / "bad_complete_worker/partial_receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
