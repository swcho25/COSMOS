"""Prevent silently dropped fields, unbound provenance and incomplete graph runs."""
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "spark"))
import publish_grounded_lake as grounded
import publish_lake_backfill as publisher


class GroundedPublisherTests(unittest.TestCase):
    def setUp(self):
        self.workspace = (Path(__file__).resolve().parents[1] / "artifacts").resolve()
        self.root = self.workspace / ("grounded-publisher-tests-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        (self.root / "parts").mkdir()
        self.schema = {"type": "struct", "fields": [
            {"name": "id", "type": "string", "nullable": True, "metadata": {}},
            {"name": "score", "type": "double", "nullable": True, "metadata": {}}]}
        self.datasets = []
        for index, (target, subpath) in enumerate(sorted(grounded.REQUIRED_DESTINATIONS)):
            part = self.root / "parts" / f"data_{index}.jsonl"
            part.write_text('{"id":"a","score":null}\n', encoding="utf-8")
            self.datasets.append({"name": f"dataset_{index}", "target_root": target, "dataset_path": subpath,
                                  "schema": copy.deepcopy(self.schema), "key_columns": ["id"], "row_count": 1,
                                  "files": [{"local_file": f"parts/data_{index}.jsonl", "row_count": 1}]})
        self.source = {"grounded_manifest_sha256": "a" * 64, "integrated_manifest_sha256": "b" * 64,
                       "previous_identity_map_sha256": "c" * 64}
        self.validation = {"status": "PASS", "checks": {key: True for key in grounded.REQUIRED_CHECKS}}
        self.publication = {"schema_version": "lake-backfill-publication-1", "run_id": "test-grounded",
                            "as_of": "2026-09-16T00:00:00Z", "snapshot_id": str(uuid.uuid4()),
                            "datasets": self.datasets, "grounded_contract": {
                                "schema_version": "grounded-lake-1", "validation_file": "grounded_validation.json"}}

    def tearDown(self):
        path = self.root.resolve()
        if not path.is_relative_to(self.workspace) or not path.name.startswith("grounded-publisher-tests-"):
            raise AssertionError("Refusing cleanup outside generated workspace")
        shutil.rmtree(path)

    def write(self, filename, value):
        path = self.root / filename
        path.write_text(json.dumps(value, allow_nan=False), encoding="utf-8")
        return publisher.file_sha256(path)

    def manifest(self):
        source_sha = self.write("source_manifest.json", self.source)
        self.publication["source_manifest_sha256"] = source_sha
        self.validation["source_manifest_sha256"] = source_sha
        self.publication["grounded_contract"]["validation_sha256"] = self.write("grounded_validation.json", self.validation)
        for dataset in self.datasets:
            for part in dataset["files"]:
                source = self.root / part["local_file"]
                part.update(sha256=publisher.file_sha256(source), bytes=source.stat().st_size)
        digest = self.write("backfill_publication.json", self.publication)
        self.write("finish.done.json", {"publication_sha256": digest})

    def test_valid_complete_run_schema_and_nulls(self):
        self.manifest()
        publication, report = grounded.validate_grounded(self.root, "test-grounded")
        self.assertEqual(len(report["datasets"]), 16)
        self.assertEqual(report["status"], "PASS")
        self.assertEqual(publication, self.publication)

    def test_sidecar_extension_does_not_relax_default_publisher(self):
        self.manifest()
        with self.assertRaisesRegex(ValueError, "Invalid dataset_path"):
            publisher.validate_publication(self.root, "test-grounded")
        publisher.validate_publication(self.root, "test-grounded", extra_graph_paths=grounded.EXTRA_GRAPH_PATHS)

    def test_required_graph_sidecar_cannot_be_omitted(self):
        self.datasets.pop()
        self.manifest()
        with self.assertRaisesRegex(ValueError, "destination set mismatch"):
            grounded.validate_grounded(self.root, "test-grounded")

    def test_unknown_missing_duplicate_and_coerced_fields_rejected(self):
        part = self.root / self.datasets[0]["files"][0]["local_file"]
        for line in ['{"id":"a","score":null,"unpublished":"lost"}', '{"id":"a"}',
                     '{"id":"a","score":null,"id":"b"}', '{"id":"a","score":"0.5"}',
                     '{"id":"a","score":NaN}', '{"id":"a","score":Infinity}']:
            with self.subTest(line=line):
                part.write_text(line + "\n", encoding="utf-8")
                self.manifest()
                with self.assertRaises(ValueError):
                    grounded.validate_grounded(self.root, "test-grounded")

    def test_blank_and_null_keys_rejected(self):
        part = self.root / self.datasets[0]["files"][0]["local_file"]
        for line in ['{"id":"","score":null}', '{"id":null,"score":null}', '']:
            part.write_text(line + "\n", encoding="utf-8")
            self.manifest()
            with self.assertRaises(ValueError):
                grounded.validate_grounded(self.root, "test-grounded")

    def test_false_missing_or_other_source_semantic_checks_rejected(self):
        self.validation["checks"]["evidence_quotes"] = False
        self.manifest()
        with self.assertRaisesRegex(ValueError, "required successful check"):
            grounded.validate_grounded(self.root, "test-grounded")
        self.validation["checks"]["evidence_quotes"] = True
        self.manifest()
        self.write("grounded_validation.json", {**self.validation, "source_manifest_sha256": "d" * 64})
        with self.assertRaisesRegex(ValueError, "SHA mismatch"):
            grounded.validate_grounded(self.root, "test-grounded")

    def test_changed_shard_cannot_pass_preflight(self):
        self.manifest()
        (self.root / self.datasets[0]["files"][0]["local_file"]).write_text('{"id":"b","score":null}\n', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "SHA changed"):
            grounded.validate_grounded(self.root, "test-grounded")

    def test_integer_boolean_and_nested_schema_are_exact(self):
        for value in [True, 1.5, 2 ** 63]:
            with self.assertRaises(ValueError):
                grounded.exact_value(value, "long", False, "x")
        array_schema = {"type": "array", "containsNull": False, "elementType": "string"}
        grounded.exact_value(["x"], array_schema, False, "x")
        with self.assertRaises(ValueError):
            grounded.exact_value([None], array_schema, False, "x")
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            grounded._schema({"type": "map", "keyType": "string", "valueType": "string"}, "x")

    def test_commit_metadata_confines_files_and_binds_sha(self):
        digest = self.write("source_manifest.json", self.source)
        entries = [{"name": "source_manifest.json", "local_file": "source_manifest.json", "sha256": digest}]
        self.assertEqual(publisher.checked_commit_metadata(self.root, entries), entries)
        for change in [{"name": "receipt.json"}, {"name": "../leak.json"},
                       {"local_file": "../outside.json"}, {"sha256": "e" * 64}]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                publisher.checked_commit_metadata(self.root, [{**entries[0], **change}])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            publisher.checked_commit_metadata(self.root, entries + entries)


if __name__ == "__main__":
    unittest.main()
