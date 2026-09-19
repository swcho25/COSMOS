"""Tiny synthetic CPU workers only; never merge or inspect real full-run data."""
from contextlib import closing
import copy
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_provisional_bulk as bulk
import merge_kg_provisional_partitions as merger
import split_kg_provisional_remaining as splitter
import test_kg_provisional_bulk as fixtures


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.ProvisionalTests()
        self.fixture.setUp()
        self.root, self.source = self.fixture.root, self.fixture.source
        self.parent = self.source / "manifest.json"
        self.rows = [{**self.fixture.rows[0], "article_id": "a" + str(i), "body": "기업 계약 원문 " + str(i)} for i in range(9)]
        protocol = bulk.read(self.source / "protocol.json")
        protocol.update(source_inventory_fingerprint="synthetic-parent", source_inventory={"fixture": "nine synthetic rows"})
        bulk.atomic_json(self.source / "protocol.json", protocol)
        parts = []
        for index in range(3):
            path = self.source / ("part-" + str(index) + ".jsonl")
            path.write_text("".join(bulk.preview.canonical(row) + "\n" for row in self.rows[index*3:index*3+3]), encoding="utf8")
            parts.append({"path": path.name, "rows": 3, "bytes": path.stat().st_size, "sha256": bulk.preview.file_sha256(path)})
        manifest = bulk.read(self.parent)
        manifest.update(files=parts, expected_unique_pairs=9, counts={"mention_unique_articles": 9},
                        source_inventory_fingerprint="synthetic-parent", protocol_sha256=bulk.preview.file_sha256(self.source / "protocol.json"))
        bulk.atomic_json(self.parent, manifest)
        bulk.atomic_json(self.source / "_VERIFIED", {"manifest_sha256": bulk.preview.file_sha256(self.parent)})
        self.model_patch = patch.object(bulk, "model_contract", side_effect=self.fixture.contract)
        self.model_patch.start()
        self.no_llm = patch.object(fixtures.teacher_runtime, "_load_runtime", side_effect=AssertionError("No LLM allowed"))
        self.no_llm.start()
        bulk.run_bulk(self.parent, self.fixture.checkpoint, self.fixture.out, device="cpu", max_pairs=2,
                      predictor_factory=fixtures.Factory(), chunk_size=2, shard_size=2)
        self.split = self.root / "split"
        splitter.split_remaining(self.parent, self.fixture.out, self.split)
        self.plan_path = self.split / "split_plan.json"
        self.plan = bulk.read(self.plan_path)
        self.workers = {}
        for part in self.plan["partitions"]:
            out = self.root / (part["id"] + "_output")
            checkpoint = self.fixture.checkpoint
            if part["id"] == "pc2":
                checkpoint = self.root / "copied_model_on_pc2"
                shutil.copytree(self.fixture.checkpoint, checkpoint)
            bulk.run_bulk(self.split / part["input_manifest"], checkpoint, out, device="cpu",
                          predictor_factory=fixtures.Factory(), chunk_size=2, shard_size=2)
            self.workers[part["id"]] = out

    def tearDown(self):
        self.no_llm.stop()
        self.model_patch.stop()
        self.fixture.tearDown()

    def rewrite_plan(self, value):
        bulk.atomic_json(self.plan_path, value)
        receipt = bulk.read(self.split / "split_plan_receipt.json")
        receipt["plan_sha256"] = bulk.preview.file_sha256(self.plan_path)
        bulk.atomic_json(self.split / "split_plan_receipt.json", receipt)

    def run_merge(self, name="merged", workers=None):
        return merger.merge(self.plan_path, self.workers if workers is None else workers, self.root / name, shard_size=2)

    def test_merge_exactly_covers_parent_preserves_predictions_and_partial_offsets(self):
        before = {str(path): bulk.preview.file_sha256(path) for path in
                  [self.split / "base/state.sqlite3", *(out / "state.sqlite3" for out in self.workers.values())]}
        result = self.run_merge()
        self.assertEqual(result["processed_unique_pairs"], 9)
        self.assertEqual(result["observed_unique_articles"], 9)
        self.assertEqual(result["decisions"], {"include": 3, "exclude": 3, "needs_review": 3})
        self.assertFalse(result["validated_auto_selection"])
        self.assertEqual(result["teacher_calls"], 0)
        self.assertEqual(result["state_sqlite_sha256"], bulk.preview.file_sha256(self.root / "merged/state.sqlite3"))
        with closing(sqlite3.connect(self.root / "merged/state.sqlite3")) as db:
            values = [json.loads(row[0]) for row in db.execute("SELECT result_json FROM pairs ORDER BY article_id")]
        for value, source in zip(values, self.rows):
            self.assertEqual(value["input_sha256"], bulk.preview.canonical_input_sha256(source))
            component = value["component_ref"]["component"]
            path = self.split / "base/state.sqlite3" if component == "base" else self.workers[component] / "state.sqlite3"
            with closing(sqlite3.connect(path)) as original:
                prior = json.loads(original.execute("SELECT result_json FROM pairs WHERE article_id=?", (value["article_id"],)).fetchone()[0])
            self.assertEqual(value["raw_prediction"], prior["raw_prediction"])
            self.assertEqual(value["component_ref"]["source_ref"], prior["source_ref"])
            self.assertEqual(value["source_ref"]["manifest_sha256"], self.plan["parent"]["manifest_sha256"])
        partial = next(x for x in values if x["article_id"] == "a2")
        self.assertEqual(partial["component_ref"]["source_ref"]["byte_offset"], 0)
        self.assertGreater(partial["source_ref"]["byte_offset"], 0)
        for path, digest in before.items():
            self.assertEqual(bulk.preview.file_sha256(Path(path)), digest)

    def test_wrong_partition_and_missing_completion_are_fatal(self):
        with self.assertRaisesRegex(ValueError, "source protocol differs"):
            self.run_merge("wrong_worker", {"pc1": self.workers["pc1"], "pc2": self.workers["pc1"]})
        receipt_path = self.workers["pc1"] / "provisional_receipt.json"
        value = bulk.read(receipt_path)
        value["complete"] = False
        bulk.atomic_json(receipt_path, value)
        with self.assertRaisesRegex(ValueError, "incomplete|receipt"):
            self.run_merge("incomplete")
        self.assertFalse((self.root / "incomplete/provisional_receipt.json").exists())

    def test_wrong_model_or_result_checksum_is_fatal(self):
        path = self.workers["pc1"] / "protocol.json"
        before = path.read_bytes()
        value = bulk.read(path)
        value["model"]["student_manifest_sha256"] = "0" * 64
        bulk.atomic_json(path, value)
        with self.assertRaisesRegex(ValueError, "model differs"):
            self.run_merge("wrong_model")
        path.write_bytes(before)
        with closing(sqlite3.connect(self.workers["pc1"] / "state.sqlite3")) as db:
            db.execute("UPDATE pairs SET result_sha256=? WHERE rowid=(SELECT MIN(rowid) FROM pairs)", ("0" * 64,))
            db.commit()
        with self.assertRaisesRegex(ValueError, "checksum differs"):
            self.run_merge("corrupt_result")

    def test_wrong_offset_and_plan_receipt_are_fatal(self):
        plan = copy.deepcopy(self.plan)
        part = next(p for p in plan["partitions"] if any(m["parent_start_byte"] > 0 for m in p["mappings"]))
        mapping = next(m for m in part["mappings"] if m["parent_start_byte"] > 0)
        mapping["parent_start_byte"] += 1
        self.rewrite_plan(plan)
        with self.assertRaisesRegex(ValueError, "base source cursor|line boundary|source identity"):
            self.run_merge("wrong_offset")
        self.plan_path.write_text(self.plan_path.read_text(encoding="utf8") + " ", encoding="utf8")
        with self.assertRaisesRegex(ValueError, "Split plan receipt"):
            merger.check_plan(self.plan_path)

    def test_missing_committed_row_is_fatal(self):
        with closing(sqlite3.connect(self.workers["pc1"] / "state.sqlite3")) as db:
            db.execute("DELETE FROM pairs WHERE rowid=(SELECT MIN(rowid) FROM pairs)")
            db.commit()
        with self.assertRaisesRegex(ValueError, "incomplete|counters differ"):
            self.run_merge("missing_row")
        self.assertFalse((self.root / "missing_row/provisional_receipt.json").exists())

    def test_duplicate_export_is_fatal(self):
        # Keep each published file's manifest SHA internally consistent to prove
        # that row uniqueness/coverage checks add protection beyond file hashes.
        worker = self.workers["pc1"]
        receipt_path = worker / "provisional_receipt.json"
        receipt = bulk.read(receipt_path)
        exported = worker / receipt["outputs"]["root"]
        manifest_path = worker / receipt["outputs"]["manifest"]
        manifest = bulk.read(manifest_path)
        item = next(item for items in receipt["outputs"]["files"].values() for item in items if item["rows"] >= 1)
        path = exported / item["path"]
        lines = path.read_text(encoding="utf8").splitlines(keepends=True)
        path.write_text(lines[0] + "".join(lines), encoding="utf8")
        for container in (manifest["files"], receipt["outputs"]["files"]):
            changed = next(x for items in container.values() for x in items if x["path"] == item["path"])
            changed["bytes"], changed["sha256"] = path.stat().st_size, bulk.preview.file_sha256(path)
        bulk.atomic_json(manifest_path, manifest)
        receipt["outputs"]["manifest_sha256"] = bulk.preview.file_sha256(manifest_path)
        bulk.atomic_json(receipt_path, receipt)
        with closing(sqlite3.connect(worker / "state.sqlite3")) as db:
            bulk.durable.save_meta(db, "provisional_receipt_sha256", bulk.preview.file_sha256(receipt_path))
            db.commit()
        with self.assertRaisesRegex(ValueError, "Duplicate pair|row count"):
            self.run_merge("duplicate_export")


if __name__ == "__main__":
    unittest.main()
