"""Publication must preserve source identity, exact citations and deferred sentiment."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_grounded_lake as prepare


def text_sha(value):
    return hashlib.sha256(value.encode("utf8")).hexdigest()


def fixture():
    text = "Target won a supply contract."
    article = {"article_id": "article-one", "title": "New contract", "body": text,
               "cleaned_title": "New contract", "cleaned_body": text}
    content_sha = text_sha(prepare.encoded({"title": article["title"], "body": article["body"]}))
    article.update(cleaned_text_sha256=content_sha, cleaning_audit={"original_text_sha256": content_sha})
    probability = {"positive": .8, "negative": .1, "neutral": .1}
    evidence = {"source": "body", "start": 0, "end": len(text), "quote": text,
                "cleaned_start": 0, "cleaned_end": len(text), "cleaned_quote": text}
    context = {"field": "body", "start": 0, "end": len(text), "quote": text,
               "actual_input": text, "input_sha256": text_sha(text), "status": "analyzed",
               "label": "POSITIVE", "probabilities": probability,
               "source_validation": {"method": "full_article_cleaned_slice_and_original_offset_map_v1",
                                     "cleaned_start": 0, "cleaned_end": len(text)}}
    sentiment = {"article_id": article["article_id"], "ticker": "TARGET", "status": "analyzed",
                 "label": "POSITIVE", "score": .7, "model_max_probability": .8,
                 "probabilities": probability, "input_sha256": "input-id", "contexts": [context],
                 "source_content_sha256": content_sha, "cleaned_content_sha256": content_sha}
    edge = {"edge_id": "edge-one", "article_id": article["article_id"], "ticker": "TARGET",
            "cleaned_text_sha256": content_sha, "source_input_sha256": "input-id",
            "evidence": [evidence], "sentiment": sentiment}
    return article, edge


class SourcePreservationTests(unittest.TestCase):
    def test_v1_and_v11_contexts_both_have_exact_source_evidence(self):
        article, edge = fixture()
        prepare.validate_company_edge(edge, article)
        del edge["sentiment"]["contexts"][0]["source_validation"]
        prepare.validate_company_edge(edge, article)
        edge["sentiment"]["contexts"][0]["actual_input"] = "Target won a different contract."
        with self.assertRaisesRegex(ValueError, "not grounded"):
            prepare.validate_company_edge(edge, article)

    def test_article_hash_is_canonical_json_not_joined_citation_text(self):
        article, edge = fixture()
        self.assertNotEqual(article["cleaned_text_sha256"], text_sha(article["title"] + "\n" + article["body"]))
        prepare.validate_company_edge(edge, article)
        article["body"] += " Changed outside the quoted span."
        with self.assertRaisesRegex(ValueError, "original fields differ"):
            prepare.validate_company_edge(edge, article)

    def test_identity_and_quote_rebinding_are_rejected(self):
        original_article, original_edge = fixture()
        changes = [lambda edge: edge.update(article_id="other"),
                   lambda edge: edge["sentiment"].update(ticker="OTHER"),
                   lambda edge: edge["sentiment"].update(input_sha256="other"),
                   lambda edge: edge["evidence"][0].update(start=1),
                   lambda edge: edge["sentiment"]["contexts"][0].update(input_sha256="tampered"),
                   lambda edge: edge["sentiment"]["contexts"][0]["source_validation"].update(cleaned_start=1)]
        for change in changes:
            edge = copy.deepcopy(original_edge)
            change(edge)
            with self.subTest(change=change), self.assertRaises(ValueError):
                prepare.validate_company_edge(edge, original_article)

    def test_deferred_is_not_converted_to_neutral_or_retained_probabilities(self):
        article, edge = fixture()
        sentiment = edge["sentiment"]
        sentiment.update(status="ambiguous_target_context", label=None, score=None,
                         probabilities=None, model_max_probability=None)
        prepare.validate_company_edge(edge, article)
        for field, value in [("label", "NEUTRAL"), ("score", 0.0),
                             ("probabilities", {"positive": .1, "negative": .1, "neutral": .8})]:
            changed = copy.deepcopy(edge)
            changed["sentiment"][field] = value
            with self.assertRaisesRegex(ValueError, "remain NULL"):
                prepare.validate_company_edge(changed, article)

    def test_predictions_require_finite_consistent_context_aggregation(self):
        article, edge = fixture()
        for field, value in [("score", .8), ("score", float("nan")),
                             ("label", "NEGATIVE"), ("model_max_probability", .6)]:
            changed = copy.deepcopy(edge)
            changed["sentiment"][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                prepare.validate_company_edge(changed, article)
        changed = copy.deepcopy(edge)
        changed["sentiment"]["contexts"][0]["probabilities"]["positive"] = 1.1
        with self.assertRaisesRegex(ValueError, "Invalid probabilities"):
            prepare.validate_company_edge(changed, article)

    def test_duplicate_context_cannot_double_weight_support(self):
        article, edge = fixture()
        edge["sentiment"]["contexts"].append(copy.deepcopy(edge["sentiment"]["contexts"][0]))
        with self.assertRaisesRegex(ValueError, "Repeated sentiment context"):
            prepare.validate_company_edge(edge, article)

    def test_date_unknown_zone_is_never_promoted_to_exact_utc_publication(self):
        naive = prepare.publication_time("2026-09-15 09:00")
        self.assertIsNone(naive["published_at"])
        self.assertEqual(naive["available_at"], "2026-09-16T12:00:00Z")
        aware = prepare.publication_time("2026-09-15T09:00:00+09:00")
        self.assertEqual(aware["published_at"], "2026-09-15T00:00:00Z")
        date_only = prepare.publication_time("2026-09-15T09:00:00+09:00", "date")
        self.assertIsNone(date_only["published_at"])
        self.assertEqual(date_only["available_at"], "2026-09-16T12:00:00Z")
        invalid = prepare.publication_time("not a date")
        self.assertIsNone(invalid["available_at"])

    def test_upstream_unusable_date_cannot_reenter_windows_from_plausible_raw_clock(self):
        invalid_audit = {"usable_day": False, "published_date": "2026-09-15", "status": "source_date_invalid"}
        value = prepare.publication_time("2026-09-15T09:00:00Z", audit=invalid_audit)
        self.assertIsNone(value["available_at"])
        self.assertIsNone(value["published_at"])
        self.assertEqual(value["availability_policy"], "source_date_audit_unusable")
        naive_audit = {"usable_day": True, "published_date": "2026-09-15", "status": "day_known_timezone_unverified"}
        naive = prepare.publication_time("2026-09-15 09:00", audit=naive_audit)
        self.assertIsNone(naive["published_at"])
        self.assertEqual(naive["available_at"], "2026-09-16T12:00:00Z")
        exact_audit = {"usable_day": True, "published_date": "2026-09-15", "status": "explicit_source_time",
                       "published_at_utc": "2026-09-15T00:00:00Z"}
        exact = prepare.publication_time("2026-09-15T09:00:00+09:00", audit=exact_audit)
        self.assertEqual(exact["published_at"], exact_audit["published_at_utc"])


class LocalArtifactTests(unittest.TestCase):
    def setUp(self):
        self.workspace = (Path(__file__).resolve().parents[1] / "artifacts").resolve()
        self.root = self.workspace / ("prepare-grounded-tests-" + uuid.uuid4().hex)
        (self.root / "parts").mkdir(parents=True)

    def tearDown(self):
        path = self.root.resolve()
        if not path.is_relative_to(self.workspace) or not path.name.startswith("prepare-grounded-tests-"):
            raise AssertionError("Refusing cleanup outside generated workspace")
        shutil.rmtree(path)

    def test_bounded_shards_manifest_detects_changed_source(self):
        writer = prepare.DatasetWriter(self.root, "test", "/data-lake/cleaned/news", ["id"],
                                       {"id": "string", "value": "double"}, shard_bytes=1)
        writer.add({"id": "first", "value": None})
        writer.add({"id": "second", "value": .2})
        result = writer.finish()
        self.assertEqual(result["row_count"], 2)
        self.assertEqual(len(result["files"]), 2)
        for part in result["files"]:
            self.assertEqual(part["sha256"], prepare.sha(self.root / part["local_file"]))
        source = self.root / result["files"][0]["local_file"]
        manifest = {"files": {"one": {"path": result["files"][0]["local_file"], "sha256": prepare.sha(source)}}}
        prepare.source_file(self.root, manifest, "one")
        source.write_text('{"id":"changed"}\n', encoding="utf8")
        with self.assertRaisesRegex(ValueError, "Frozen source changed"):
            prepare.source_file(self.root, manifest, "one")

    def test_source_escape_duplicate_null_and_unknown_keys_fail(self):
        with self.assertRaisesRegex(ValueError, "escapes"):
            prepare.source_file(self.root, {"files": {"one": {"path": "../outside.jsonl", "sha256": "a"}}}, "one")
        writer = prepare.DatasetWriter(self.root, "test", "/data-lake/cleaned/news", ["id"], {"id": "string"})
        writer.add({"id": "one"})
        for row in [{"id": "one"}, {"id": None}, {"id": "two", "extra": "lost"}]:
            with self.assertRaises(ValueError):
                writer.add(row)
        writer.finish()


if __name__ == "__main__":
    unittest.main()
