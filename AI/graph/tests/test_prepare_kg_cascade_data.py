"""CPU-only full-source grouping/exclusion/sealing regression checks."""
import gzip
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_kg_cascade_data as data


def row(identity, *, ticker="A", body=None, url=None):
    return {"article_id": str(identity), "ticker": ticker, "company_name": "Company " + ticker,
            "published_at": "1999-01-01", "title": "Title " + str(identity),
            "body": body if body is not None else "Unique economic article body " + str(identity),
            "source_url": url}


class CascadeData(unittest.TestCase):
    def setUp(self):
        real_here = data.worker.HERE
        test_root = real_here / "artifacts"
        self.root = test_root / ("cascade_data_test_" + uuid.uuid4().hex)
        self.assertTrue(self.root.resolve().is_relative_to(test_root.resolve()))
        self.root.mkdir()
        self.addCleanup(shutil.rmtree, self.root)
        # Inventory verifies real policy hashes without requiring downloaded weights.
        fake = self.root / "code/graph"
        (fake / "local_models").mkdir(parents=True)
        (fake.parent / "ner").mkdir()
        for name in ("knowledge_graph_relevance_policy.py", "relevance_sentence_evidence.py",
                     "lake_news_normalize.py", "news_content.py"):
            shutil.copyfile(real_here / name, fake / name)
        shutil.copyfile(real_here.parent / "ner/matcher.py", fake.parent / "ner/matcher.py")
        (fake / "local_models/relevance_model_receipt.json").write_text('{"repo":"test","revision":"test"}', encoding="utf8")
        patcher = mock.patch.object(data.worker, "HERE", fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.sizes = dict.fromkeys(data.SIZES, 1)
        self.exclude_path = self.root / "exclusions.jsonl"
        self.exclusions(ids=["not-in-source"], bodies=[data.prior.text_hash("not-in-source body")])

    def exclusions(self, *, ids, bodies=(), urls=()):
        self.exclude_path.write_text(data.base.canonical({"ids": list(ids), "bodies": list(bodies),
            "urls": list(urls), "source": "known_review"}) + "\n", encoding="utf8")

    def source(self, shards, *, folder="source", compressed=False, expected=None):
        target = self.root / folder
        target.mkdir()
        parts = []
        for i, rows in enumerate(shards):
            path = target / (f"part-{i}.txt.gz" if compressed else f"part-{i}.txt")
            payload = ("".join(data.base.canonical(x) + "\n" for x in rows)).encode("utf8")
            if compressed:
                with gzip.open(path, "wb") as stream:
                    stream.write(payload)
            else:
                path.write_bytes(payload)
            parts.append({"path": path.name, "bytes": path.stat().st_size,
                          "sha256": data.base.sha(path), "rows": len(rows)})
        protocol = {"model_receipt_sha256": data.base.sha(data.worker.HERE / "local_models/relevance_model_receipt.json"),
            "code_sha256": {name: data.base.sha(data.worker.HERE / name) for name in
                ("knowledge_graph_relevance_policy.py", "relevance_sentence_evidence.py")},
            "scope": "full_original_test_fixture", "source_inventory_fingerprint": "fixture"}
        data.worker.atomic_json(target / "protocol.json", protocol)
        value = {"schema": data.worker.MANIFEST_SCHEMA, "complete": True, "files": parts,
            "protocol_sha256": data.base.sha(target / "protocol.json"),
            "expected_unique_pairs": expected if expected is not None else len({
                (x["article_id"], x["ticker"]) for rows in shards for x in rows})}
        data.worker.atomic_json(target / "manifest.json", value)
        data.worker.atomic_json(target / "_VERIFIED", {"manifest_sha256": data.base.sha(target / "manifest.json")})
        return target / "manifest.json"

    def prepare(self, source, name="prepared", **kwargs):
        return data.prepare(source, self.exclude_path, self.root / name,
                            sizes=self.sizes, progress_every=2, **kwargs)

    def outputs(self, name="prepared"):
        return {fold: [json.loads(line) for line in (self.root / name / fold / "inputs.jsonl").read_text(
            encoding="utf8").splitlines()] for fold in data.SIZES}

    def test_exact_four_worker_manifests_and_test_sealed(self):
        source = self.source([[row(i) for i in range(8)]])
        result = self.prepare(source)
        self.assertTrue(result["complete"])
        self.assertEqual(result["counts"]["source_rows"], 8)
        self.assertEqual(result["cross_split_group_overlap"], 0)
        self.assertFalse(result["labels_read"])
        self.assertFalse(result["population_accuracy_claim"])
        self.assertTrue(result["test_sealed_before_teacher_inference"])
        for fold in data.SIZES:
            inventory = data.worker.inventory(self.root / "prepared" / fold / "manifest.json")
            self.assertEqual(inventory["expected_unique_pairs"], 1)
            p = json.loads((self.root / "prepared" / fold / "protocol.json").read_text(encoding="utf8"))
            self.assertEqual(p["cascade_sampling"]["sealed_test"], fold == "test")
            self.assertEqual(p["source_inventory_fingerprint"], "fixture")
            self.assertIn("parent_export_protocol_sha256", p)

    def test_late_bridge_propagates_exclusion_through_entire_component(self):
        a = row("a", body="First specific economic body", url="https://example.test/shared?utm_source=x")
        b = row("b", body="Second specific economic body", url="https://example.test/shared")
        c = row("c", body=b["body"], url="https://example.test/third")
        self.exclusions(ids=["c"], bodies=[data.prior.text_hash("unrelated")])
        source = self.source([[a, *[row(i) for i in range(4)], c], [b]])
        result = self.prepare(source)
        selected = [r["article_id"] for rows in self.outputs().values() for r in rows]
        self.assertEqual(set(selected), {"0", "1", "2", "3"})
        self.assertEqual(result["counts"]["excluded_groups"], 1)
        self.assertEqual(result["counts"]["excluded_unique_pairs"], 3)

    def test_multitarget_and_whitespace_duplicate_are_one_component(self):
        a = row("a", ticker="A", body="Economic   information about 회사")
        b = row("a", ticker="B", body=a["body"])
        c = row("alternate", ticker="C", body="Economic\ninformation about 회사")
        source = self.source([[a, b, c, *[row(i) for i in range(3)]]])
        result = self.prepare(source)
        selected = [r for rows in self.outputs().values() for r in rows]
        self.assertEqual(result["counts"]["all_groups"], 4)
        self.assertEqual(sum(r["article_id"] in {"a", "alternate"} for r in selected), 1)

    def test_deterministic_across_shard_and_row_order(self):
        rows = [row(i) for i in range(9)] + [row(4, ticker="B")]
        a = self.source([rows[:3], rows[3:]], folder="source-a")
        b = self.source([list(reversed(rows))], folder="source-b")
        self.prepare(a, "prepared-a")
        self.prepare(b, "prepared-b")
        self.assertEqual(self.outputs("prepared-a"), self.outputs("prepared-b"))

    def test_identical_duplicate_is_counted_without_extra_candidate(self):
        source = self.source([[row(0), row(0), *[row(i) for i in range(1, 4)]]])
        result = self.prepare(source)
        self.assertEqual(result["counts"]["unique_pairs"], 4)
        self.assertEqual(result["counts"]["identical_duplicate_pairs"], 1)

    def test_conflicting_duplicate_fails_without_completion_marker(self):
        source = self.source([[row(0), row(0, body="Changed economic body"), *[row(i) for i in range(1, 5)]]])
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.prepare(source)
        self.assertFalse((self.root / "prepared/_VERIFIED").exists())
        self.assertFalse((self.root / "prepared/manifest.json").exists())

    def test_gzip_offset_extraction_and_large_unknown_date_are_preserved(self):
        long_row = row("long", body="긴 본문 entirely retained. " * 6000)
        long_row["published_at"] = None
        source = self.source([[row(i) for i in range(3)], [long_row]], compressed=True)
        self.prepare(source)
        selected = [r for rows in self.outputs().values() for r in rows]
        self.assertIn(long_row, selected)

    def test_exclusion_body_hash_and_url_are_normalized(self):
        bad_body = row("bad-body", body="한국 경제\n 정보")
        bad_url = row("bad-url", url="https://example.test/news?utm_source=tracker")
        self.exclusions(ids=["unused"], bodies=[data.prior.text_hash("한국 경제 정보")],
                        urls=["https://example.test/news"])
        result = self.prepare(self.source([[bad_body, bad_url, *[row(i) for i in range(4)]]]))
        self.assertEqual(result["counts"]["excluded_groups"], 2)
        self.assertEqual(result["counts"]["eligible_groups"], 4)

    def test_reject_labels_extra_keys_and_empty_exclusion_inventory(self):
        contaminated = row(0)
        contaminated["decision"] = "include"
        source = self.source([[contaminated, *[row(i) for i in range(1, 4)]]])
        with self.assertRaisesRegex(ValueError, "seven input fields"):
            self.prepare(source)
        self.exclude_path.write_text("", encoding="utf8")
        with self.assertRaisesRegex(ValueError, "Hydrated"):
            data.load_exclusions(self.exclude_path)

    def test_exclusion_schema_refuses_label_bearing_documents(self):
        self.exclude_path.write_text('{"ids":["a"],"decision":"exclude"}\n', encoding="utf8")
        with self.assertRaisesRegex(ValueError, "schema"):
            data.load_exclusions(self.exclude_path)

    def test_key_rows_supported(self):
        values = [{"kind": "id", "value": "unused", "source": "known_review"},
                  {"kind": "body_sha256", "value": data.prior.text_hash("unused"), "source": "price_holdout"}]
        self.exclude_path.write_text("".join(data.base.canonical(v) + "\n" for v in values), encoding="utf8")
        keys, summary = data.load_exclusions(self.exclude_path)
        self.assertEqual(keys["id"], {"unused"})
        self.assertEqual(summary["declared_keys_by_source"]["price_holdout"]["body_sha256"], 1)

    def test_manifest_row_and_distinct_counts_enforced(self):
        source = self.source([[row(i) for i in range(4)]], expected=3)
        with self.assertRaisesRegex(ValueError, "denominator"):
            self.prepare(source)
        source2 = self.source([[row(i) for i in range(4)]], folder="source2")
        value = json.loads(source2.read_text(encoding="utf8"))
        value["files"][0]["rows"] += 1
        data.worker.atomic_json(source2, value)
        data.worker.atomic_json(source2.parent / "_VERIFIED", {"manifest_sha256": data.base.sha(source2)})
        with self.assertRaisesRegex(ValueError, "row count"):
            self.prepare(source2, "prepared2")

    def test_source_hash_and_origin_protocol_are_enforced(self):
        source = self.source([[row(i) for i in range(4)]])
        part = source.parent / "part-0.txt"
        part.write_bytes(part.read_bytes().replace(b"Title 0", b"Title X"))
        with self.assertRaisesRegex(ValueError, "bytes/hash differ"):
            self.prepare(source)
        source2 = self.source([[row(i) for i in range(4)]], folder="source2")
        protocol_path = source2.parent / "protocol.json"
        protocol = json.loads(protocol_path.read_text(encoding="utf8"))
        protocol["model_receipt_sha256"] = "0" * 64
        data.worker.atomic_json(protocol_path, protocol)
        manifest = json.loads(source2.read_text(encoding="utf8"))
        manifest["protocol_sha256"] = data.base.sha(protocol_path)
        data.worker.atomic_json(source2, manifest)
        data.worker.atomic_json(source2.parent / "_VERIFIED", {"manifest_sha256": data.base.sha(source2)})
        with self.assertRaisesRegex(ValueError, "model receipt"):
            self.prepare(source2, "prepared2")

    def test_exclusions_modified_after_scan_prevent_publication(self):
        source = self.source([[row(i) for i in range(4)]])
        original = data.identity_keys
        changed = False
        def mutate(row):
            nonlocal changed
            result = original(row)
            if not changed:
                self.exclude_path.write_text(self.exclude_path.read_text(encoding="utf8") + "\n", encoding="utf8")
                changed = True
            return result
        with mock.patch.object(data, "identity_keys", side_effect=mutate):
            with self.assertRaisesRegex(ValueError, "changed during sampling"):
                self.prepare(source)
        self.assertFalse((self.root / "prepared/_VERIFIED").exists())
        self.assertFalse((self.root / "prepared/train/_VERIFIED").exists())

    def test_output_reuse_and_insufficient_groups_fail(self):
        source = self.source([[row(i) for i in range(4)]])
        self.prepare(source)
        with self.assertRaises(FileExistsError):
            self.prepare(source)
        self.exclusions(ids=["0"], bodies=[data.prior.text_hash(row(0)["body"])])
        with self.assertRaisesRegex(ValueError, "Fewer eligible"):
            self.prepare(source, "too-small")
        self.assertFalse((self.root / "too-small/_VERIFIED").exists())


if __name__ == "__main__":
    unittest.main()
