"""Local publication contracts; no Spark, model inference, HDFS or database calls."""
import copy
import csv
import io
import json
import shutil
import sys
import unittest
import uuid
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import finalize_lake_recent as finalize
from lake_window_features import build_features
from spark.publish_lake_recent import validate_publication


@contextmanager
def contract_directory():
    # Normal inherited ACLs also work under a restricted Windows test token;
    # Python's TemporaryDirectory requests a private 0700 DACL there.
    parent = Path(__file__).resolve().parent
    directory = parent / (".lake-contract-" + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        if directory.resolve().parent != parent:
            raise RuntimeError("Test cleanup escaped its temporary directory")
        shutil.rmtree(directory)


class FinalizeLakeRecentTests(unittest.TestCase):
    as_of = "2026-09-15T00:00:00Z"
    published = "2026-09-14T12:00:00Z"

    def relation(self, document="n1", source="NEWS", confidence=1.0, **extra):
        return {"record_id": document, "src_ticker": "A", "dst_ticker": "B",
                "rel_type": "PARTNER", "published_at": self.published,
                "source": source, "confidence": confidence, **extra}

    def scores(self, news, disclosure, combined):
        return [{"source_stock_code": "A", "target_stock_code": "B",
                 "relationship_type_code": "PARTNER", "window_type": f"{days}D",
                 "news_score": news, "disclosure_score": disclosure, "score": combined}
                for days in (7, 30, 90)]

    @staticmethod
    def fields(value):
        return {field["name"]: field for field in value["fields"]}

    def test_empty_relationship_schemas_have_publication_keys_and_nullable_types(self):
        natural = {"source_market", "source_stock_code", "target_market",
                   "target_stock_code", "relationship_type_code", "window_type"}
        contracts = {
            "news_relationships": {"document_id", "src_ticker", "dst_ticker", "rel_type",
                                   "sentence_start", "source_start", "target_start"},
            "relationship_scores": natural,
            "relationship_features": natural,
            "graph_edges": {"relationship_key", "window_type"},
        }
        for name, keys in contracts.items():
            with self.subTest(dataset=name):
                declared = finalize.empty_schema(name)
                fields = self.fields(declared)
                self.assertEqual(declared["type"], "struct")
                self.assertEqual(len(fields), len(declared["fields"]))
                self.assertTrue(keys <= fields.keys())
                self.assertTrue(all(field["nullable"] for field in fields.values()))
                self.assertEqual(fields["confidence"]["type"], "double")
                if name == "news_relationships":
                    for key in ("sentence_start", "source_start", "target_start"):
                        self.assertEqual(fields[key]["type"], "long")
                else:
                    for key in ("news_score", "disclosure_score", "score"):
                        self.assertEqual(fields[key]["type"], "double")
                    self.assertEqual(fields["evidence_count"]["type"], "long")
                    for key in ("period_start", "period_end"):
                        self.assertEqual(fields[key]["type"], "string")

    def test_empty_and_populated_relationship_output_schemas_match(self):
        companies = [{"ticker": "A", "market": "KOSPI"},
                     {"ticker": "B", "market": "KOSPI"}]
        data = build_features(companies, [], [self.relation()], [], [], self.as_of)
        data["graph_edges"] = [{**row, "relationship_key": "KOSPI:A:KOSPI:B:PARTNER",
                                "snapshot_id": "snapshot"}
                               for row in data["relationship_scores"]]
        data["news_relationships"] = [self.relation(
            document_id="n1", source_record_id="raw1", published_date="2026-09-14",
            pattern="test", sentence="A partners with B", sentence_start=0,
            sentence_end=17, source_start=0, source_end=1, target_start=16,
            target_end=17, offset_basis="title_newline_body")]
        for name in ("news_relationships", "relationship_scores", "relationship_features", "graph_edges"):
            with self.subTest(dataset=name):
                self.assertEqual(finalize.empty_schema(name), finalize.schema(data[name]))

    def test_null_numerics_keep_types_without_fabricated_values(self):
        row = {"score": None, "sentiment_score": None, "positive_count": None,
               "price_return": None, "price_correlation": None, "probs3": None}
        before = copy.deepcopy(row)
        fields = self.fields(finalize.schema([row]))
        self.assertEqual(row, before)
        for key in ("score", "sentiment_score", "price_return", "price_correlation"):
            self.assertEqual(fields[key]["type"], "double")
        self.assertEqual(fields["positive_count"]["type"], "long")
        self.assertEqual(fields["probs3"]["type"],
                         {"type": "array", "elementType": "double", "containsNull": True})

    def test_independent_check_uses_per_document_max_and_equal_source_weight(self):
        relations = [self.relation(), self.relation(confidence=.2, src_ticker="B", dst_ticker="A"),
                     self.relation("d1", "DISCLOSURE"), self.relation("d2", "DISCLOSURE"),
                     self.relation("old", published_at="2026-01-01T00:00:00Z")]
        # One NEWS and two DISCLOSURE documents: blend the bounded source scores,
        # rather than pooling all three documents or weighting by source count.
        expected = self.scores(18.126925, 32.967995, 25.54746)
        finalize.independent_score_check(expected, relations, self.as_of)
        wrong = copy.deepcopy(expected)
        wrong[0]["score"] += .1
        with self.assertRaisesRegex(ValueError, "Independent score disagreement"):
            finalize.independent_score_check(wrong, relations, self.as_of)
        with self.assertRaisesRegex(ValueError, "row count differs"):
            finalize.independent_score_check(expected[:-1], relations, self.as_of)

    def test_independent_check_distinguishes_observed_zero_and_missing_source(self):
        news = self.relation(confidence=0)
        finalize.independent_score_check(self.scores(0, None, 0), [news], self.as_of)
        relations = [news, self.relation("d1", "DISCLOSURE", .5)]
        finalize.independent_score_check(self.scores(0, 9.516258, 4.758129), relations, self.as_of)
        with self.assertRaisesRegex(ValueError, "Independent score disagreement"):
            finalize.independent_score_check(self.scores(None, 9.516258, 9.516258), relations, self.as_of)
        finalize.independent_score_check([], [], self.as_of)

    def test_local_empty_relationship_publication_matches_files_and_preflight(self):
        with contract_directory() as directory:
            base = Path(directory)
            prepared = base / "prepared"
            prepared.mkdir()
            companies = base / "companies.csv"
            with companies.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=["ticker", "market", "name_official"])
                writer.writeheader()
                writer.writerow({"ticker": "A", "market": "KOSPI", "name_official": "Company A"})
            inputs = {
                "cleaned_news": [{"document_id": "n1", "title": "Company A announcement",
                                  "body": "Observed article text", "published_at": self.published}],
                "news_mentions": [{"document_id": "n1", "ticker": "A",
                                   "published_at": self.published, "confidence": 1.0}],
                "news_relationships": [],
                "disclosures": [{"document_id": "d1", "ticker": "A", "published_at": self.published}],
                "disclosure_relationships": [],
                "prices": [],
                "document_id_map": [{"identity_key": "record:test:1", "document_id": "n1"}],
                "sentiment_inputs": [{"document_id": "n1", "title": "Company A announcement",
                                      "body": "Observed article text", "language": "unsupported"}],
            }
            for name, rows in inputs.items():
                finalize.write(prepared / f"{name}.jsonl", rows)
            manifest = {"as_of": self.as_of, "companies_sha256": finalize.sha(companies),
                        "limitations": [], "files": {
                            f"{name}.jsonl": {"sha256": finalize.sha(prepared / f"{name}.jsonl")}
                            for name in inputs}}
            (prepared / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            (prepared / "_PREPARED").write_text("prepared", encoding="utf-8")
            sentiment = base / "sentiment.jsonl"
            finalize.write(sentiment, [{"document_id": "n1", "label": None, "score": None,
                                       "probs3": None, "status": "unsupported_language"}])
            report = {"input_sha256": finalize.sha(prepared / "sentiment_inputs.jsonl"),
                      "output_sha256": finalize.sha(sentiment)}
            Path(str(sentiment) + ".report.json").write_text(json.dumps(report), encoding="utf-8")
            output = base / "publication"
            argv = ["finalize_lake_recent.py", "--prepared", str(prepared), "--sentiment", str(sentiment),
                    "--companies", str(companies), "--out", str(output), "--run-id", "contract-test"]
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                finalize.main()
            # Local-only publisher validation imports no PySpark and writes nothing.
            publication = validate_publication(output, "contract-test")
            self.assertEqual(publication["status"], "LOCAL_VALIDATED_NOT_PUBLISHED")
            datasets = {item["name"]: item for item in publication["datasets"]}
            self.assertEqual(len(datasets), 12)
            for name, item in datasets.items():
                with self.subTest(dataset=name):
                    self.assertEqual(item["local_file"], f"{name}.jsonl")
                    rows = finalize.read(output / item["local_file"])
                    self.assertEqual(len(rows), item["row_count"])
                    self.assertEqual(finalize.sha(output / item["local_file"]), item["source_sha256"])
                    self.assertTrue(set(item["key_columns"]) <= self.fields(item["schema"]).keys())
            for name in ("news_relationships", "relationship_scores", "relationship_features", "graph_edges"):
                self.assertEqual(datasets[name]["row_count"], 0)
                self.assertEqual(datasets[name]["schema"], finalize.empty_schema(name))
            mentions = finalize.read(output / datasets["news_mentions"]["local_file"])
            self.assertIsNone(mentions[0]["sentiment_score"])
            self.assertIsNone(mentions[0]["sentiment_label"])


if __name__ == "__main__":
    unittest.main()
