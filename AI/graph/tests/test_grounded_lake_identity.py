"""Canonical ID reuse must never change the text behind a citation span."""
import hashlib
import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from grounded_lake_identity import GroundedIdentityIndex, IdentityConflict, build_identity_index
from lake_document_ids import assign_document_ids


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1] / "artifacts" / ("test_identity_" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.prior = self.root / "prior.jsonl"
        self.prior.write_text("", encoding="utf-8")
        self.counter = 0
        self.opened = []

    def tearDown(self):
        for item in self.opened:
            item.close()
        allowed = Path(__file__).resolve().parents[1] / "artifacts"
        self.assertEqual(self.root.resolve().parent, allowed.resolve())
        shutil.rmtree(self.root)

    def row(self, article, body="short", **kw):
        row = {"article_id": article, "title": "Original title", "body": body,
               "cleaned_title": "Original title", "cleaned_body": body,
               "published_at": "2026-09-15T12:00:00Z", "source_url": None}
        row.update(kw)
        return row

    def build(self, rows, **kw):
        self.counter += 1
        result = build_identity_index(iter(rows), self.prior, self.root / f"index{self.counter}.sqlite3", **kw)
        self.opened.append(result)
        return result

    def prior_rows(self, rows):
        self.prior.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")

    def test_url_revision_preserves_distinct_exact_text_hash(self):
        a = self.row("a", "Original news text", source_url="https://example.com/a?utm_source=one#x")
        b = self.row("b", "Changed news text", source_url="https://example.com/a")
        index = self.build([a, b])
        left, right = index.get("a"), index.get("b")
        self.assertEqual(left["document_id"], right["document_id"])
        self.assertNotEqual(left["source_revision_id"], right["source_revision_id"])
        self.assertNotEqual(left["original_text_sha256"], right["original_text_sha256"])
        self.assertEqual(right["original_text_sha256"], hashlib.sha256(b"Original title\nChanged news text").hexdigest())
        self.assertEqual(right["representative_article_id"], "a")

    def test_stable_uuid_matches_existing_lake_algorithm(self):
        rows = [self.row("a", "x" * 80), self.row("b", "x" * 80)]
        old = [{"source_dataset": "grounded-news-v1", "record_id": r["article_id"],
                "body": r["cleaned_body"], "published_at": r["published_at"]} for r in rows]
        expected = assign_document_ids(old)[0][0]["document_id"]
        index = self.build(rows)
        self.assertEqual(index.get("a")["document_id"], expected)
        reversed_index = self.build(reversed(rows))
        self.assertEqual(index.get("a"), reversed_index.get("a"))

    def test_transitive_component_and_base_representative(self):
        a = self.row("a", "x" * 80, source_url="https://example.com/a", _lake_scope="relation_only")
        b = self.row("b", "y" * 80, source_url="https://example.com/a", _lake_scope="relation_only")
        c = self.row("c", "y" * 80)
        index = self.build([a, b, c])
        self.assertEqual(index.summary["representative_count"], 1)
        self.assertEqual(index.get("a")["representative_article_id"], "c")
        self.assertEqual(index.get("c")["duplicate_count"], 3)

    def test_body_normalization_and_eighty_character_threshold(self):
        index = self.build([self.row("a", "x" * 79), self.row("b", "x" * 79)])
        self.assertEqual(index.summary["representative_count"], 2)
        index = self.build([self.row("a", "é" * 80), self.row("b", "e\u0301" * 80)])
        self.assertEqual(index.summary["representative_count"], 1)

    def test_prior_reuse_preserves_all_metadata_and_unseen_aliases(self):
        old_id = str(uuid.uuid4())
        unrelated_id = str(uuid.uuid4())
        key = "url:" + hashlib.sha256(b"https://example.com/a").hexdigest()
        entries = [{"identity_key": key, "document_id": old_id, "available_at": "2020-01-01T00:00:00Z", "extra": {"audit": 3}},
                   {"identity_key": "record:old:unseen", "document_id": unrelated_id, "available_at": None}]
        self.prior_rows(entries)
        index = self.build([self.row("new", "x" * 90, source_url="https://example.com/a")])
        self.assertEqual(index.get("new")["document_id"], old_id)
        self.assertEqual(index.get("new")["available_at"], "2026-09-15T12:00:00Z")
        out = self.root / "map.jsonl"
        index.export_id_map(out)
        exported = {r["identity_key"]: r for r in map(json.loads, out.read_text(encoding="utf-8").splitlines())}
        for entry in entries:
            self.assertEqual(exported[entry["identity_key"]], entry)

    def test_prior_uuid_aliases_unite_current_records(self):
        prior_id = str(uuid.uuid4())
        self.prior_rows([{"identity_key": "record:grounded-news-v1:" + a, "document_id": prior_id} for a in ("a", "b")])
        index = self.build([self.row("a", "one"), self.row("b", "two")])
        self.assertEqual(index.get("a")["document_id"], index.get("b")["document_id"])
        self.assertEqual(index.summary["reused_document_ids"], 1)

    def test_conflicting_old_ids_fail_closed_and_cannot_open(self):
        self.prior_rows([{"identity_key": "record:grounded-news-v1:" + a, "document_id": str(uuid.uuid4())} for a in ("a", "b")])
        with self.assertRaisesRegex(IdentityConflict, "conflicting existing"):
            self.build([self.row("a", "x" * 90), self.row("b", "x" * 90)])
        with self.assertRaisesRegex(ValueError, "completion"):
            GroundedIdentityIndex(self.root / "index1.sqlite3")

    def test_explicit_quarantine_keeps_prior_map_and_marks_every_record(self):
        entries = [{"identity_key": "record:grounded-news-v1:" + a, "document_id": str(uuid.uuid4())} for a in ("a", "b")]
        self.prior_rows(entries)
        index = self.build([self.row("a", "x" * 90), self.row("b", "x" * 90), self.row("c")], conflict_policy="quarantine")
        self.assertEqual(index.summary["quarantined_articles"], 2)
        self.assertIsNone(index.get("b")["document_id"])
        self.assertEqual(list(index.iter_conflicts())[0]["article_ids"], ["a", "b"])
        self.assertEqual(len(list(index.iter_representatives())), 1)
        output = self.root / "quarantine-map.jsonl"
        index.export_id_map(output)
        mapping = list(map(json.loads, output.read_text(encoding="utf-8").splitlines()))
        self.assertEqual(len(mapping), 3)
        self.assertTrue(all(e in mapping for e in entries))

    def test_scope_only_duplicate_is_one_source_base_takes_precedence(self):
        index = self.build([self.row("a", _lake_scope="relation_only"), self.row("a", _lake_scope="base")])
        self.assertEqual(index.get("a")["scope"], "base")
        self.assertEqual(index.summary["repeated_identical_rows"], 1)

    def test_conflicting_same_source_article_rejected(self):
        with self.assertRaisesRegex(ValueError, "Conflicting source fields"):
            self.build([self.row("a"), self.row("a", body="changed", cleaned_body="changed")])

    def test_unknown_timezone_is_never_assigned(self):
        index = self.build([self.row("a", published_at="2020-01-01 08:10"), self.row("b", published_at="2020-01-01")])
        self.assertIsNone(index.get("a")["available_at"])
        self.assertIsNone(index.get("b")["available_at"])
        self.assertEqual(index.get("a")["published_at"], "2020-01-01 08:10")

    def test_source_time_rejection_overrides_parseable_clock(self):
        index = self.build([self.row("a", published_at="1970-01-01T00:00:00Z",
                                     date_audit={"status": "epoch_or_pre_epoch_requires_review", "published_at_utc": None})])
        self.assertIsNone(index.get("a")["available_at"])

    def test_missing_cleaning_or_disagreeing_hash_fails(self):
        row = self.row("a")
        del row["cleaned_body"]
        with self.assertRaisesRegex(ValueError, "cleaned_body"):
            self.build([row])
        with self.assertRaisesRegex(ValueError, "differs from text"):
            self.build([self.row("a", cleaned_text_sha256="a" * 64)])

    def test_grounded_json_hash_basis_differs_from_literal_quote_text(self):
        row = self.row("a", body="원문 본문", cleaned_body="정제 본문")
        def source_hash(title, body):
            return hashlib.sha256(json.dumps({"title": title, "body": body}, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        row["cleaned_text_sha256"] = source_hash(row["cleaned_title"], row["cleaned_body"])
        row["cleaning_audit"] = {"original_text_sha256": source_hash(row["title"], row["body"])}
        mapped = self.build([row]).get("a")
        self.assertEqual(mapped["cleaned_text_sha256"], row["cleaned_text_sha256"])
        self.assertEqual(mapped["original_fields_sha256"], row["cleaning_audit"]["original_text_sha256"])
        self.assertEqual(mapped["original_text_sha256"], hashlib.sha256((row["title"] + "\n" + row["body"]).encode()).hexdigest())
        self.assertNotEqual(mapped["original_fields_sha256"], mapped["original_text_sha256"])

    def test_prior_digest_verified_and_output_never_overwritten(self):
        with self.assertRaisesRegex(ValueError, "SHA256"):
            self.build([], expected_prior_sha256="wrong")
        index = self.build([self.row("a")], expected_prior_sha256=hashlib.sha256(b"").hexdigest())
        with self.assertRaises(FileExistsError):
            build_identity_index([], self.prior, index.db_path)


if __name__ == "__main__":
    unittest.main()
