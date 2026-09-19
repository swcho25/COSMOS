"""CPU-only immutable-parent, transactional resume and source-evidence tests."""
from argparse import Namespace
import copy
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import unittest
from unittest.mock import patch
import uuid

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import rebuild_grounded_relations as build


def extraction(title, body, entities):
    if len(entities) < 2:
        return {"relationships": [], "review_candidates": []}
    text = title + "\n" + body
    edges = []
    for sentence_start in ([0, len(title) + 1] if title == body else [len(title) + 1]):
        a, b = text.index("Seller", sentence_start), text.index("Buyer", sentence_start)
        edges.append({"src_ticker": "S", "dst_ticker": "B", "rel_type": "SUPPLY", "directed": True,
                      "sentence": body, "sentence_start": sentence_start, "sentence_end": sentence_start + len(body),
                      "source_start": a, "source_end": a + 6, "source_quote": "Seller",
                      "target_start": b, "target_end": b + 5, "target_quote": "Buyer",
                      "relation_subtype": "goods_supply", "direction_semantics": "supplier_to_customer"})
    return {"relationships": edges, "review_candidates": []}


class RebuildRelationsTests(unittest.TestCase):
    def setUp(self):
        self.root = HERE / "artifacts" / ("_relation_revision_test_" + uuid.uuid4().hex)
        self.root.mkdir()
        self.source = self.root / "parent"
        self.source.mkdir()
        self.out = self.root / "out"
        self.args = Namespace(source_dir=self.source, out=self.out, batch_size=1,
                              endpoint_deny=None, limit_articles=None, resume=False)
        self.assets = patch.object(build, "code_assets", return_value={"synthetic_code": "bound"})
        self.assets.start()
        self.no_print = patch("builtins.print")
        self.no_print.start()

    def tearDown(self):
        self.no_print.stop()
        self.assets.stop()
        root = self.root.resolve()
        assert root.parent == (HERE / "artifacts").resolve() and root.name.startswith("_relation_revision_test_")
        shutil.rmtree(root)

    def parent(self, articles=2, duplicate_title=False):
        db = sqlite3.connect(self.source / "state.sqlite3")
        db.executescript("CREATE TABLE articles(article_id TEXT PRIMARY KEY,source_sha TEXT,cleaned_sha TEXT,payload TEXT);"
                         "CREATE TABLE pairs(article_id TEXT,ticker TEXT,decision TEXT,payload TEXT,PRIMARY KEY(article_id,ticker));"
                         "CREATE TABLE sentiments(article_id TEXT,ticker TEXT,payload TEXT);")
        for i in range(articles):
            body = "Seller supplies chips to Buyer."
            article = {"article_id": "a" + str(i), "title": body if duplicate_title else "Results", "body": body,
                       "published_at": "2026-01-01", "source_url": "https://example.invalid/" + str(i)}
            cleaned = {"title": article["title"], "body": article["body"]}
            payload = {**article, "cleaned_title": cleaned["title"], "cleaned_body": cleaned["body"]}
            db.execute("INSERT INTO articles VALUES (?,?,?,?)", (article["article_id"], build.digest(article), build.digest(cleaned), build.canonical(payload)))
            for ticker, name in (("S", "Seller"), ("B", "Buyer")):
                pair = {"article_id": article["article_id"], "ticker": ticker, "company_name": name,
                        "materialization_decision": "candidate_ready", "target_context": {"matched_names": [name]},
                        "source_input_sha256": build.common.preview.canonical_input_sha256({**article, "ticker": ticker, "company_name": name})}
                db.execute("INSERT INTO pairs VALUES (?,?,?,?)", (article["article_id"], ticker, "candidate_ready", build.canonical(pair)))
                db.execute("INSERT INTO sentiments VALUES (?,?,?)", (article["article_id"], ticker, '{"unchanged":true}'))
        db.commit()
        db.close()
        build.atomic(self.source / "protocol.json", {"synthetic": True})
        manifest = {"schema": "cosmos-grounded-candidate-kg-1", "artifact_complete": True, "graph_materialized": True, **build.common.FLAGS,
                    "protocol_sha256": build.sha(self.source / "protocol.json"),
                    "state_sqlite_sha256": build.sha(self.source / "state.sqlite3"), "bounded_pilot": False,
                    "files": {"news_nodes": {"rows": articles}}, "decision_counts": {"candidate_ready": articles * 2}}
        build.atomic(self.source / "manifest.json", manifest)
        return {p.name: build.sha(p) for p in self.source.iterdir()}

    def run_builder(self, extractor=extraction):
        return build.run(self.args, extractor=extractor)

    def test_complete_exports_keep_parent_bytes_and_subtype(self):
        before = self.parent()
        result = self.run_builder()
        self.assertTrue(result["artifact_complete"])
        self.assertEqual(result["relation_counts"], {"SUPPLY": 2})
        self.assertEqual(result["coverage"]["parent_graph_news"], 2)
        self.assertEqual({p.name: build.sha(p) for p in self.source.iterdir()}, before)
        self.assertEqual(set(result["files"]), {"relationship_evidence", "company_relationships", "relation_review"})
        exported = [json.loads(x) for x in (self.out / result["files"]["relationship_evidence"]["path"]).read_text(encoding="utf-8").splitlines()]
        self.assertEqual(exported[0]["relation_subtype"], "goods_supply")
        self.assertFalse(exported[0]["production_approved"])
        self.args.resume = True
        self.assertEqual(self.run_builder(), result)

    def test_stop_commits_batch_then_resume_no_duplicate(self):
        self.parent(3)
        calls = []
        def stopping(*args):
            calls.append(args[1])
            (self.out / "STOP").touch()
            return extraction(*args)
        paused = self.run_builder(stopping)
        self.assertEqual(paused["state"], "paused_stop_file")
        self.assertEqual(paused["processed_articles"], 1)
        self.args.resume = True
        self.assertEqual(self.run_builder()["state"], "paused_stop_file")
        (self.out / "STOP").unlink()
        self.assertEqual(self.run_builder()["relation_counts"], {"SUPPLY": 3})

    def test_failure_rolls_back_entire_batch_and_resume(self):
        self.parent(2)
        self.args.batch_size = 2
        count = 0
        def crash(*args):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("injected before batch commit")
            return extraction(*args)
        with self.assertRaisesRegex(RuntimeError, "injected"):
            self.run_builder(crash)
        db = sqlite3.connect(self.out / "state.sqlite3")
        self.assertEqual(build.meta(db, "cursor"), 0)
        self.assertEqual(db.execute("SELECT COUNT(*) FROM relations").fetchone()[0], 0)
        db.close()
        self.args.resume = True
        self.assertEqual(self.run_builder()["relation_counts"], {"SUPPLY": 2})

    def test_source_code_policy_or_settings_change_rejected(self):
        self.parent()
        def stop(*args):
            (self.out / "STOP").touch()
            return extraction(*args)
        self.run_builder(stop)
        self.args.resume = True
        self.args.batch_size = 2
        with self.assertRaises(ValueError): self.run_builder()
        self.args.batch_size = 1
        with patch.object(build, "code_assets", return_value={"changed": True}), self.assertRaises(ValueError):
            self.run_builder()
        with (self.source / "state.sqlite3").open("ab") as stream: stream.write(b"tamper")
        with self.assertRaisesRegex(ValueError, "SQLite changed"): self.run_builder()

    def test_committed_output_tampering_detected_on_resume(self):
        self.parent()
        def stop(*args):
            (self.out / "STOP").touch()
            return extraction(*args)
        self.run_builder(stop)
        db = sqlite3.connect(self.out / "state.sqlite3")
        payload = json.loads(db.execute("SELECT payload FROM relations").fetchone()[0])
        payload["sentence"] = "fabricated"
        db.execute("UPDATE relations SET payload=?", (build.canonical(payload),))
        db.commit(); db.close()
        self.args.resume = True
        with self.assertRaisesRegex(ValueError, "outputs changed"): self.run_builder()

    def test_wrong_endpoint_offsets_type_and_alias_are_rejected(self):
        self.parent(1)
        cases = ({"src_ticker": "UNKNOWN"}, {"rel_type": "ACQUIRE"}, {"sentence_start": 0},
                 {"source_quote": "fiction"}, {"directed": False}, {"src_ticker": "B", "dst_ticker": "S"})
        for i, changes in enumerate(cases):
            self.args.out = self.root / ("bad" + str(i))
            def corrupt(*args):
                result = extraction(*args)
                result["relationships"][0].update(changes)
                return result
            with self.subTest(changes=changes), self.assertRaises(ValueError): self.run_builder(corrupt)

    def test_duplicate_title_body_sentence_keeps_one_evidence_with_both_offsets(self):
        self.parent(1, duplicate_title=True)
        result = self.run_builder()
        self.assertEqual(result["relation_counts"], {"SUPPLY": 1})
        values = (self.out / result["files"]["relationship_evidence"]["path"]).read_text(encoding="utf-8").splitlines()
        edge = json.loads(values[0])
        self.assertEqual(len(edge["duplicate_evidence_spans"]), 1)
        self.assertNotEqual(edge["sentence_start"], edge["duplicate_evidence_spans"][0]["sentence_start"])

    def test_article_specific_identity_review_preserves_other_article_and_source(self):
        before = self.parent(2)
        deny = self.root / "deny.json"
        build.atomic(deny, {"tickers": [], "article_companies": [{"article_id": "a0", "ticker": "S", "reason": "historical identity verification pending"}]})
        self.args.endpoint_deny = deny
        result = self.run_builder()
        self.assertEqual(result["relation_counts"], {"SUPPLY": 1})
        review = json.loads((self.out / result["files"]["relation_review"]["path"]).read_text(encoding="utf-8"))
        self.assertEqual(review["candidates"][0]["reason"], "endpoint_identity_audit_pending")
        self.assertFalse(review["candidates"][0]["is_asserted_relationship"])
        self.assertEqual({p.name: build.sha(p) for p in self.source.iterdir()}, before)

    def test_pilot_coverage_is_explicit_and_old_directory_is_refused(self):
        self.parent(3)
        self.args.limit_articles = 1
        result = self.run_builder()
        self.assertTrue(result["bounded_pilot"])
        self.assertEqual(result["coverage"]["multi_endpoint_articles_outside_pilot"], 2)
        self.assertEqual(result["coverage"]["parent_articles"], 3)
        self.args.out = self.source / "illegal-child"
        with self.assertRaisesRegex(ValueError, "separate"): self.run_builder()

    def test_completed_export_or_flags_tampering_rejected(self):
        self.parent(1)
        result = self.run_builder()
        self.args.resume = True
        changed = copy.deepcopy(result)
        changed["production_approved"] = True
        build.atomic(self.out / "manifest.json", changed)
        with self.assertRaisesRegex(ValueError, "flags"): self.run_builder()
        build.atomic(self.out / "manifest.json", result)
        export = self.out / result["files"]["relationship_evidence"]["path"]
        export.write_text("changed\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Export changed"): self.run_builder()


if __name__ == "__main__":
    unittest.main()
