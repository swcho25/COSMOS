"""Bounded historical analysis, immutable shard resume and payload contracts."""
import copy
import csv
import hashlib
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest.mock import patch
import uuid
import zlib

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "ner"))
import lake_backfill_analyze as analyze
from lake_news_normalize import normalize_article
from lake_backfill_state import BackfillState, semantic_hash


class FakeState:
    def __init__(self, references, identities):
        self.references, self.identities = references, identities
        self.finalized = True
        self.fingerprint = hashlib.sha256(analyze.encoded([references, identities])).hexdigest()

    def summary(self):
        return {"finalized": self.finalized, "fingerprint": self.fingerprint}

    def iter_representatives(self):
        yield from copy.deepcopy(self.references)

    def iter_identity_map(self):
        yield from copy.deepcopy(self.identities)


class BackfillAnalyzeTests(unittest.TestCase):
    def setUp(self):
        # Inherited workspace ACLs avoid Windows TemporaryDirectory 0700 issues.
        self.parent = Path(__file__).resolve().parent
        self.base = self.parent / (".backfill-analyze-test-" + uuid.uuid4().hex)
        self.base.mkdir()
        self.normalized = self.base / "normalized"
        self.normalized.mkdir()
        self.aliases, self.companies = self.base / "aliases.csv", self.base / "companies.csv"
        self.write_csv(self.aliases, ["alias", "ticker", "name_official", "alias_type", "ambiguity", "group_id"], [
            {"alias": name, "ticker": ticker, "name_official": name,
             "alias_type": "official", "ambiguity": "unambiguous", "group_id": ""}
            for name, ticker in (("삼성전자", "005930"), ("엔비디아", "NVDA"), ("Apple", "AAPL"))])
        self.write_csv(self.companies, ["ticker", "market", "name_official"], [
            {"ticker": "005930", "market": "KOSPI", "name_official": "삼성전자"},
            {"ticker": "NVDA", "market": "NASDAQ", "name_official": "NVIDIA"},
            {"ticker": "AAPL", "market": "NASDAQ", "name_official": "Apple"}])

    def tearDown(self):
        if self.base.resolve().parent != self.parent:
            raise RuntimeError("Test cleanup escaped fixture directory")
        shutil.rmtree(self.base)

    @staticmethod
    def write_csv(path, fields, rows):
        with path.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            writer.writerows(rows)

    def state(self, bodies=None):
        bodies = bodies or ["삼성전자와 엔비디아는 시장 경쟁을 이어갔다.",
                            "일반 지역 소식이며 기업 이름이 없는 기사입니다.",
                            "Apple announced a new product. " + "Details are provided in the full article. " * 12]
        refs, identities, articles = [], [], []
        with (self.normalized / "payloads.zlib").open("xb") as stream:
            for index, body in enumerate(bodies):
                article = normalize_article({"record_id": f"source-{index}", "source_dataset": "test_prepared",
                    "source_path": f"/raw/{index}", "title": "기업 소식" if index == 0 else "News",
                    "body": body, "published_date": "2010-01-01", "region": "domestic",
                    "language": None if index < 2 else "en"})
                article["optional_numeric"] = None if index == 0 else 4
                article["optional_array"] = [] if index == 0 else [3]
                block = zlib.compress(analyze.encoded(article))
                ref = {"shard": "payloads.zlib", "offset": stream.tell(), "length": len(block)}
                stream.write(block)
                document = str(uuid.uuid5(uuid.NAMESPACE_URL, f"test:{index}"))
                refs.append({"document_id": document, "record_id": article["record_id"],
                    "source_dataset": article["source_dataset"], "published_at": article["published_at"],
                    "body_length": len(body), "payload_ref": ref, "semantic_hash": semantic_hash(article),
                    "version_id": index + 1, "duplicate_count": 2, "version_count": 1})
                identities.append({"identity_key": f"record:test_prepared:source-{index}", "document_id": document,
                    "representative_record_id": article["record_id"],
                    "representative_source_dataset": article["source_dataset"], "available_at": article["published_at"]})
                articles.append(article)
        return FakeState(refs, sorted(identities, key=lambda row: row["identity_key"])), articles

    def run_analysis(self, state, **extra):
        return analyze.run_analysis(state, normalized_dir=self.normalized,
            out_dir=self.base / "analysis", aliases_path=self.aliases, companies_path=self.companies,
            expected_aliases_sha256=analyze.digest(self.aliases),
            expected_companies_sha256=analyze.digest(self.companies), shard_size=1,
            id_map_shard_size=2, **extra)

    def dataset_rows(self, manifest, name):
        dataset = next(item for item in manifest["datasets"] if item["name"] == name)
        rows = []
        for item in dataset["files"]:
            path = self.base / "analysis" / item["local_file"]
            self.assertEqual(analyze.digest(path), item["sha256"])
            self.assertEqual(path.stat().st_size, item["bytes"])
            content = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(len(content), item["row_count"])
            self.assertEqual(item["schema"], dataset["schema"])
            rows.extend(content)
        self.assertEqual(len(rows), dataset["row_count"])
        return rows

    def test_old_news_compete_full_text_null_language_and_schema_union(self):
        state, articles = self.state()
        manifest = self.run_analysis(state)
        self.assertEqual(manifest["news_shards"], 3)
        self.assertEqual(manifest["id_map_shards"], 2)
        cleaned = self.dataset_rows(manifest, "cleaned_news")
        self.assertEqual([row["body"] for row in cleaned], [row["body"] for row in articles])
        self.assertTrue(all(row["published_date"] == "2010-01-01" for row in cleaned))
        self.assertTrue(all(row["duplicate_count"] == 2 for row in cleaned))
        self.assertTrue(all("affiliate_competition_filter_unavailable" in row["analysis_flags"] for row in cleaned))
        relations = self.dataset_rows(manifest, "news_relationships")
        self.assertEqual(len(relations), 1)
        self.assertEqual(relations[0]["rel_type"], "COMPETE")
        self.assertEqual(relations[0]["record_id"], cleaned[0]["document_id"])
        self.assertEqual(relations[0]["source_record_id"], "source-0")
        self.assertEqual(relations[0]["source"], "NEWS")
        self.assertEqual(len(self.dataset_rows(manifest, "news_mentions")), 3)
        sentiment = self.dataset_rows(manifest, "sentiment_inputs")
        self.assertEqual(len(sentiment), 2)
        self.assertIsNone(sentiment[0]["language"])
        self.assertEqual(sentiment[1]["body"], articles[2]["body"][:200])
        self.assertEqual(len(sentiment[1]["body"]), 200)
        self.assertEqual(len(self.dataset_rows(manifest, "document_id_map")), 3)
        dataset = next(row for row in manifest["datasets"] if row["name"] == "cleaned_news")
        fields = {row["name"]: row["type"] for row in dataset["schema"]["fields"]}
        self.assertEqual(fields["optional_numeric"], "long")
        self.assertEqual(fields["optional_array"]["elementType"], "long")
        rel = next(row for row in manifest["datasets"] if row["name"] == "news_relationships")
        self.assertEqual([row["row_count"] for row in rel["files"]], [1, 0, 0])

    def test_completed_resume_verifies_files_without_matching_again(self):
        state, _ = self.state()
        manifest = self.run_analysis(state)
        before = {path: path.stat().st_mtime_ns for path in (self.base / "analysis" / "news").rglob("*.jsonl")}
        with patch.object(analyze, "analyze_article", side_effect=AssertionError("completed shard was reanalyzed")):
            resumed = self.run_analysis(state)
        self.assertEqual(manifest, resumed)
        self.assertEqual(before, {path: path.stat().st_mtime_ns for path in before})
        path = next(iter(before))
        with path.open("ab") as stream:
            stream.write(b"{}\n")
        with self.assertRaisesRegex(ValueError, "Null key|checksum/count/schema"):
            self.run_analysis(state)

    def test_interrupted_attempt_is_retained_and_only_incomplete_shard_retries(self):
        state, _ = self.state()
        real = analyze.analyze_article
        count = 0
        def fail_second(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise RuntimeError("simulated interruption")
            return real(*args, **kwargs)
        with patch.object(analyze, "analyze_article", side_effect=fail_second):
            with self.assertRaisesRegex(RuntimeError, "simulated interruption"):
                self.run_analysis(state)
        out = self.base / "analysis"
        self.assertTrue((out / "news/shard-000000/chunk_manifest.json").is_file())
        self.assertFalse((out / "news/shard-000001").exists())
        self.assertFalse((out / "analysis_manifest.json").exists())
        attempts = list((out / "attempts").iterdir())
        self.assertEqual(len(attempts), 1)
        with patch.object(analyze, "analyze_article", wraps=real) as matcher:
            manifest = self.run_analysis(state)
        self.assertEqual(matcher.call_count, 2)
        self.assertEqual(len(self.dataset_rows(manifest, "cleaned_news")), 3)
        self.assertTrue(attempts[0].exists())

    def test_unfinalized_and_changed_state_contracts_rejected(self):
        state, _ = self.state()
        state.finalized = False
        with self.assertRaisesRegex(ValueError, "must be finalized"):
            self.run_analysis(state)
        state.finalized = True
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            self.run_analysis(state, state_fingerprint="wrong")
        self.run_analysis(state)
        state.fingerprint = "changed"
        with self.assertRaisesRegex(ValueError, "changed input/code/shard"):
            self.run_analysis(state)

    def test_payload_identity_and_zlib_path_lru_contracts(self):
        state, _ = self.state()
        reader = analyze.PayloadReader(self.normalized, max_open=2)
        try:
            row = reader.read(state.references[0]["payload_ref"])
            for index in range(3):
                block = zlib.compress(analyze.encoded(row))
                (self.normalized / f"other-{index}.zlib").write_bytes(block)
                reader.read({"shard": f"other-{index}.zlib", "offset": 0, "length": len(block)})
                self.assertLessEqual(len(reader.handles), 2)
            with self.assertRaisesRegex(ValueError, "relative path"):
                reader.read({"shard": "../outside", "offset": 0, "length": 3})
            broken = dict(state.references[0], semantic_hash="wrong")
            with self.assertRaisesRegex(ValueError, "semantic hash mismatch"):
                analyze._article(broken, reader, semantic_hash)
            reference = {**state.references[0]["payload_ref"], "length": 1}
            with self.assertRaisesRegex(ValueError, "truncated"):
                reader.read(reference)
        finally:
            reader.close()
        self.assertFalse(reader.handles)

    def test_two_spawn_workers_match_serial_outputs(self):
        state, _ = self.state()
        serial = self.run_analysis(state)
        parallel = analyze.run_analysis(state, normalized_dir=self.normalized,
            out_dir=self.base / "parallel", aliases_path=self.aliases, companies_path=self.companies,
            expected_aliases_sha256=analyze.digest(self.aliases), expected_companies_sha256=analyze.digest(self.companies),
            shard_size=1, id_map_shard_size=2, workers=2)
        self.assertEqual(serial, parallel)

    def test_real_finalized_sqlite_read_only_and_prior_only_identity_preserved(self):
        fake, articles = self.state()
        database = self.base / "state.sqlite"
        prior = {"identity_key": "record:prior:old-only", "document_id": str(uuid.uuid4()),
                 "representative_record_id": "old-only", "representative_source_dataset": "prior",
                 "available_at": "2009-01-01T00:00:00Z"}
        with BackfillState(database) as state:
            state.seed_prior([prior])
            for article, reference in zip(articles, fake.references):
                state.ingest(article, reference["payload_ref"])
            state.finalize()
        before = analyze.digest(database)
        with BackfillState(database, read_only=True) as state:
            manifest = self.run_analysis(state)
        self.assertEqual(analyze.digest(database), before)
        self.assertEqual(len(self.dataset_rows(manifest, "cleaned_news")), 3)
        self.assertIn(prior, self.dataset_rows(manifest, "document_id_map"))


if __name__ == "__main__":
    unittest.main()
