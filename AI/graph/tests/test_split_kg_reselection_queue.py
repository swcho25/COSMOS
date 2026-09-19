"""CPU-only verified shard partition contracts; no real candidate data copied."""
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import split_kg_reselection_queue as splitter
import knowledge_graph_reselection_worker as worker
import local_news_relevance as base


class Split(unittest.TestCase):
    def setUp(self):
        artifact_root = (worker.HERE / "artifacts").resolve()
        self.root = artifact_root / ("kg_split_test_" + uuid.uuid4().hex)
        if not self.root.resolve().is_relative_to(artifact_root):
            raise ValueError("Test cleanup must stay in artifacts")
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        self.parent, self.out = self.root / "parent", self.root / "queues"
        (self.parent / "inputs").mkdir(parents=True)
        files = []
        for i, count in enumerate((8, 5, 3, 2, 1)):
            path = self.parent / "inputs" / f"part-{i}.txt"
            path.write_bytes((b'{"synthetic":true}\n') * count)
            files.append({"path": path.relative_to(self.parent).as_posix(), "rows": count,
                          "bytes": path.stat().st_size, "sha256": base.sha(path)})
        self.protocol = {"source_inventory_fingerprint": "source-fingerprint", "source_inventory": {"news": ["source"]},
            "model_receipt_sha256": base.sha(worker.HERE / "local_models/relevance_model_receipt.json"),
            "code_sha256": {name: base.sha(worker.HERE / name) for name in
                            ("knowledge_graph_relevance_policy.py", "relevance_sentence_evidence.py")}}
        worker.atomic_json(self.parent / "protocol.json", self.protocol)
        self.manifest = {"schema": worker.MANIFEST_SCHEMA, "complete": True, "files": files,
            "expected_unique_pairs": sum(x["rows"] for x in files),
            "source_inventory_fingerprint": "source-fingerprint",
            "protocol_sha256": base.sha(self.parent / "protocol.json")}
        self.save_manifest()

    def save_manifest(self):
        worker.atomic_json(self.parent / "manifest.json", self.manifest)
        worker.atomic_json(self.parent / "_VERIFIED", {"manifest_sha256": base.sha(self.parent / "manifest.json")})

    def split(self, **kwargs):
        return splitter.split_queue(self.parent / "manifest.json", self.out, **kwargs)

    def test_disjoint_full_union_and_parent_provenance_preserved(self):
        plan = self.split(weights=(1, 2))
        paths = [x["paths"] for x in plan["partitions"]]
        self.assertFalse(set(paths[0]) & set(paths[1]))
        self.assertEqual(set(paths[0] + paths[1]), {x["path"] for x in self.manifest["files"]})
        self.assertEqual(sum(x["expected_unique_pairs"] for x in plan["partitions"]), 19)
        self.assertFalse(plan["global_selection_complete"])
        self.assertFalse(plan["inference_performed"])
        for index in (1, 2):
            root = self.out / f"pc{index}"
            verified = worker.inventory(root / "manifest.json")
            subset = json.loads((root / "manifest.json").read_text())
            self.assertEqual(subset["scope"], "parent_shard_subset")
            self.assertEqual(subset["expected_unique_pairs"], verified["expected_unique_pairs"])
            self.assertEqual(base.sha(root / "parent/manifest.json"), base.sha(self.parent / "manifest.json"))
            self.assertEqual(base.sha(root / "parent/protocol.json"), base.sha(self.parent / "protocol.json"))
            protocol = json.loads((root / "protocol.json").read_text())
            self.assertEqual(protocol["model_receipt_sha256"], self.protocol["model_receipt_sha256"])
            self.assertEqual(protocol["code_sha256"], self.protocol["code_sha256"])
            self.assertEqual(protocol["parent_source_inventory"], self.protocol["source_inventory"])

    def test_row_metric_weights_and_default_independent_copy(self):
        plan = self.split(weights=(1, 3), metric="rows")
        self.assertGreater(plan["partitions"][1]["load"], plan["partitions"][0]["load"])
        first = plan["partitions"][0]["paths"][0]
        copied = self.out / "pc1" / first
        copied.write_bytes(b"changed-copy")
        source = self.parent / first
        self.assertNotEqual(source.read_bytes(), copied.read_bytes())

    def test_part_hash_failure_does_not_create_queue(self):
        (self.parent / self.manifest["files"][0]["path"]).write_bytes(b"changed")
        with self.assertRaisesRegex(ValueError, "bytes/hash differ"):
            self.split()
        self.assertFalse(self.out.exists())

    def test_unfinished_parent_and_bad_protocol_hash_rejected(self):
        self.manifest["complete"] = False
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.split()
        self.manifest["complete"] = True
        self.manifest["protocol_sha256"] = "x" * 64
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "protocol (hash|raw-file SHA)"):
            self.split()

    def test_path_escape_rejected_before_copy(self):
        self.manifest["files"][0]["path"] = "../outside.txt"
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.split()

    def test_nonunique_parent_denominator_and_bad_weights_rejected(self):
        self.manifest["expected_unique_pairs"] -= 1
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, "unique pairs equal"):
            self.split()
        for weights in ((0, 1), (True, 1), (1,), (1, 2, 3)):
            with self.assertRaises(ValueError):
                self.split(weights=weights)

    def test_existing_output_and_nested_output_are_not_overwritten(self):
        self.out.mkdir()
        with self.assertRaisesRegex(ValueError, "new output"):
            self.split()
        with self.assertRaisesRegex(ValueError, "outside"):
            splitter.split_queue(self.parent / "manifest.json", self.parent / "nested")


if __name__ == "__main__":
    unittest.main()
