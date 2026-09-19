"""Entirely synthetic challenge fixtures; no real articles, labels or models."""
import copy
import json
from pathlib import Path
import shutil
import sys
import unittest
from unittest import mock
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_relevance_identity_challenge as sample


def fact(ticker, **changes):
    value = {"record_id": "source-" + ticker, "ticker": ticker, "entity_id": "fiction-entity-" + ticker,
             "observed_at": "2020-12-31", "published_at": "2021-03-03",
             "source_url": "https://official.example/" + ticker, "display_name": "가상 과거법인 " + ticker,
             "aliases": ["가상 약칭 " + ticker], "business": "관측일에 보고된 가상 사업이다.",
             "relationships": [], "limitations": ["이후 변동 여부는 확인하지 않았다."],
             "not_before": None, "not_after": None}
    value.update(changes)
    return value


class IdentityChallengeTests(unittest.TestCase):
    def setUp(self):
        self.parent = Path(__file__).resolve().parent
        self.root = self.parent / (".identity-challenge-test-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.source = self.root / "training_candidates.jsonl"
        self.companies = self.root / "companies.csv"
        self.tickers = [prefix + str(index) for prefix in ("K", "U") for index in range(4)]
        self.companies.write_text("ticker,market,name_official\n" + "".join(
            ticker + "," + ("KOSPI" if ticker[0] == "K" else "NASDAQ") + ",가상 현재명 " + ticker + "\n"
            for ticker in self.tickers), encoding="utf8")
        self.data = {"schema": sample.context.SCHEMA, "records": [fact(ticker) for ticker in self.tickers]}
        self.registry = self.root / "registry.json"
        self.save_registry()
        self.rows = [self.article(ticker + "-" + str(index), ticker) for ticker in self.tickers for index in range(6)]
        self.known = self.article("known", "K0")
        self.reserved_row = self.article("reserved", "U0")
        self.held = {**self.article("held", "K1"), "split": "test"}
        self.rows += [self.known, self.reserved_row]
        self.review = self.root / "review.jsonl"
        self.write_rows(self.review, [{"record_id": "known", "unused_judgment": "DO_NOT_COPY"}])
        reserve_dir = self.root / "reserved"
        reserve_dir.mkdir()
        self.reserved = reserve_dir / "new_inputs.jsonl"
        self.write_rows(self.reserved, [sample.previous.make_input(self.reserved_row,
            sample.pilot.minimal_clean(self.reserved_row), "U0", sample.previous.company_names(self.companies))[0]])
        self.reserved.with_name("manifest.json").write_text(json.dumps({"files": {
            self.reserved.name: {"sha256": sample.pilot.sha(self.reserved)}}}), encoding="utf8")
        self.legacy, self.metadata = self.root / "legacy.jsonl", self.root / "metadata.jsonl"
        self.write_rows(self.legacy, [self.held])
        self.write_rows(self.metadata, [{"record_id": "held", "url": self.held["url"], "body_sha256": self.held["body_sha256"]}])
        for original in (self.known, self.reserved_row, self.held):
            for kind in ("raw", "clean", "url", "stored"):
                row = self.article(original["record_id"] + "-" + kind, "K2")
                if kind in {"raw", "clean"}:
                    row["body"] = original["body"] + ("\n관련 뉴스\n제거되는 가상 꼬리" if kind == "clean" else "")
                elif kind == "url":
                    row["url"] = original["url"] + "?utm_source=ignored"
                else:
                    row["body_sha256"] = original["body_sha256"]
                self.rows.append(row)
        self.rows += [{**self.article("too-short", "K1"), "body": "가" * 199},
                      {**self.article("too-long", "U1"), "body": "나" * 6001},
                      {**self.article("future", "K2"), "published_date": "2023-01-01"},
                      {**self.article("before-context", "K3"), "published_date": "2021-03-03"},
                      self.article("unsupported-company", "MISSING")]
        self.save_source()
        self.out = self.root / "output"

    def tearDown(self):
        if self.root.resolve().parent != self.parent:
            raise RuntimeError("Cleanup escaped test workspace")
        shutil.rmtree(self.root)

    @staticmethod
    def article(identity, ticker):
        body = ("가상 중립 자료의 구별 가능한 식별자는 " + identity + " 이며 문장은 온전히 보존된다. ") * 10
        return {"record_id": identity, "region": "domestic" if ticker.startswith("K") else "overseas",
                "published_date": "2021-06-15", "title": "가상 제목 " + identity, "body": body,
                "url": "https://news.example/" + identity, "body_sha256": sample.previous.digest(body),
                "companies": [{"ticker": ticker, "unused_prediction": "DO_NOT_COPY"}],
                "expected_selection": "DO_NOT_COPY", "future_return": "DO_NOT_COPY", "source_dataset": "fiction"}

    @staticmethod
    def write_rows(path, rows):
        Path(path).write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf8")

    def save_source(self):
        self.write_rows(self.source, self.rows)
        self.source.with_name("manifest.json").write_text(json.dumps({"complete": True, "training_candidates": len(self.rows),
            "training_candidates_sha256": sample.pilot.sha(self.source)}), encoding="utf8")

    def save_registry(self):
        self.registry.write_text(sample.context.canonical(self.data), encoding="utf8")

    def prepare(self, **changes):
        options = dict(source=self.source, companies=self.companies, registry=self.registry, review_paths=[self.review],
            reserved_random=self.reserved, legacy_articles=self.legacy, legacy_metadata=self.metadata, out=self.out,
            expected_counts={"known_reviews": 1, "reserved_random": 1, "holdout": 1})
        options.update(changes)
        return sample.prepare(**options)

    def indexes(self):
        known, held, _ = sample.previous.exclusion_indexes([self.review], self.legacy, self.metadata, self.source)
        reserved, _, _ = sample.previous.exclusion_indexes([self.reserved], self.legacy, self.metadata, self.source)
        return {"known_reviews": known, "reserved_random": reserved, "holdout": held}

    def test_frozen_paired_outputs_preserve_source_and_exclude_all_overlap_kinds(self):
        protected = [self.source, self.registry, self.review, self.reserved, self.legacy, self.metadata, self.companies]
        before = {path: path.read_bytes() for path in protected}
        manifest = self.prepare()
        self.assertEqual(manifest["regions"], {"domestic": 8, "overseas": 8})
        self.assertEqual(set(manifest["ticker_counts"].values()), {2})
        self.assertEqual(manifest["eligible_context_rows"], 48)
        self.assertEqual(manifest["context_application_errors"], 0)
        self.assertEqual(manifest["context_candidate_pair_reason_counts"]["no_source_available_by_article"], 1)
        self.assertEqual(manifest["context_candidate_pair_reason_counts"]["source_company_not_in_inventory"], 1)
        inputs = list(sample.pilot.rows(self.out / "inputs.jsonl"))
        enriched = list(sample.pilot.rows(self.out / "context_inputs.jsonl"))
        originals = {row["record_id"]: row for row in self.rows}
        for old, new in zip(inputs, enriched):
            self.assertIsNone(old["company_context"])
            self.assertIsNotNone(new["company_context"])
            self.assertEqual({k: v for k, v in old.items() if k not in {"company_name", "company_context"}},
                             {k: v for k, v in new.items() if k not in {"company_name", "company_context"}})
            self.assertEqual(old["body"], sample.pilot.minimal_clean(originals[old["article_id"]])["body"])
            self.assertEqual(old["ticker"], originals[old["article_id"]]["companies"][0]["ticker"])
            self.assertFalse(old["article_id"].startswith(("known", "reserved", "held")))
        for name, index in manifest["exclusion_identity_coverage"].items():
            self.assertEqual(index["ids"], 1)
        for prefix in ("known_reviews", "reserved_random", "holdout"):
            for kind in ("body", "url", "stored_source_body_hash"):
                self.assertGreater(manifest["overlap_match_counts_nonexclusive"][prefix + "_" + kind], 0)
        for name, info in manifest["files"].items():
            self.assertEqual(sample.pilot.sha(self.out / name), info["sha256"])
            self.assertNotIn("DO_NOT_COPY", (self.out / name).read_text(encoding="utf8"))
        self.assertEqual(before, {path: path.read_bytes() for path in protected})
        with self.assertRaisesRegex(ValueError, "new directory"):
            self.prepare()

    def test_source_order_company_order_unused_labels_do_not_change_pairs(self):
        for row in self.rows:
            if row["record_id"].startswith(("K", "U")):
                row["companies"].append({"ticker": row["companies"][0]["ticker"][0] + "UNSUPPORTED"})
        self.save_source()
        self.prepare()
        expected = {name: (self.out / name).read_bytes() for name in ("inputs.jsonl", "context_inputs.jsonl")}
        self.rows.reverse()
        for row in self.rows:
            row["companies"].reverse()
            row["expected_selection"] = "CHANGED_UNREAD_VALUE"
        self.save_source()
        second = self.root / "second-output"
        self.prepare(out=second)
        self.assertEqual(expected, {name: (second / name).read_bytes() for name in expected})

    def test_hash_target_only_among_usable_contexts_and_expiry_counted(self):
        names = sample.previous.company_names(self.companies)
        row = self.article("multi-target", "K0")
        row["companies"] = [{"ticker": "K1"}, {"ticker": "K0"}, {"ticker": "unknown"}]
        data = {"schema": sample.context.SCHEMA, "records": [fact("K0"), fact("K1", observed_at="2019-01-01")]}
        chosen, reasons, supported = sample.supported_target(row, sample.pilot.minimal_clean(row), names, data)
        self.assertEqual(chosen[0], "K0")
        self.assertEqual(supported, ["K0"])
        self.assertEqual(reasons["snapshot_expired"], 1)
        row["companies"].append({"ticker": "K2"})
        data["records"].append(fact("K2"))
        chosen, _, supported = sample.supported_target(row, sample.pilot.minimal_clean(row), names, data)
        self.assertEqual(chosen[0], min(("K0", "K2"), key=lambda ticker: (sample.rank("multi-target:" + ticker, "target"), ticker)))
        self.assertEqual(supported, ["K0", "K2"])

    def test_insufficient_ticker_diversity_fails_instead_of_reducing_quota(self):
        candidates = [{"article_id": region + str(index), "ticker": region, "region": region,
                       "rank": str(index).zfill(3), "bodies": {region + str(index)}, "urls": set(), "source_hashes": set()}
                      for region in sample.REGIONS for index in range(12)]
        with self.assertRaisesRegex(ValueError, "2-per-ticker cap"):
            sample.select_unique(candidates)

    def test_adaptive_quota_keeps_cap_dedup_balance_and_determinism(self):
        candidates = [{"article_id": region + str(index), "ticker": region + str(index // 3), "region": region,
                       "rank": region + str(index).zfill(3), "bodies": {region + str(index)},
                       "urls": set(), "source_hashes": set()}
                      for region in sample.REGIONS for index in range(6)]
        # Four per region would fit the ticker cap, but the second overseas
        # ticker has only one distinct body, leaving three eligible articles.
        for row in candidates:
            if row["region"] == "overseas" and row["ticker"] == "overseas1":
                row["bodies"] = {"same-body"}
        with self.assertRaisesRegex(ValueError, "Fewer than 8"):
            sample.select_unique(candidates)
        selected, skipped = sample.select_unique(candidates, allow_smaller_balanced_sample=True)
        self.assertEqual(sample.Counter(row["region"] for row in selected), {"domestic": 3, "overseas": 3})
        self.assertLessEqual(max(sample.Counter(row["ticker"] for row in selected).values()), 2)
        self.assertEqual(len(set.union(*(row["bodies"] for row in selected))), len(selected))
        self.assertGreater(skipped["ticker_cap"], 0)
        self.assertEqual((selected, skipped), sample.select_unique(list(reversed(candidates)), True))
        with self.assertRaisesRegex(ValueError, "at least 1"):
            sample.select_unique([row for row in candidates if row["region"] == "domestic"], True)

    def test_adaptive_prepare_records_realized_shortfall_and_keeps_input_fields(self):
        self.data["records"] = [record for record in self.data["records"] if record["ticker"] in {"K0", "U0"}]
        self.save_registry()
        with self.assertRaisesRegex(ValueError, "Fewer than 8"):
            self.prepare()
        self.assertFalse(self.out.exists())
        result = self.prepare(allow_smaller_balanced_sample=True)
        self.assertEqual((result["planned_per_region"], result["actual_per_region"], result["shortfall"]), (8, 2, 12))
        self.assertEqual(result["regions"], {"domestic": 2, "overseas": 2})
        self.assertEqual(result["ticker_eligible_counts"], {"K0": 6, "U0": 6})
        self.assertEqual(result["size_adaptation"]["attempted_per_region"], list(range(8, 1, -1)))
        self.assertTrue(result["size_adaptation"]["used"])
        self.assertFalse(result["size_adaptation"]["time_source_body_duplicate_and_ticker_cap_constraints_relaxed"])
        self.assertIn("with 2 per region", result["selection"])
        for original, enriched in zip(sample.pilot.rows(self.out / "inputs.jsonl"), sample.pilot.rows(self.out / "context_inputs.jsonl")):
            self.assertEqual({k: v for k, v in original.items() if k not in {"company_name", "company_context"}},
                             {k: v for k, v in enriched.items() if k not in {"company_name", "company_context"}})

    def test_unregistered_inventory_ticker_skips_resolver_without_changing_target(self):
        names = sample.previous.company_names(self.companies)
        row = self.article("fast-path", "K0")
        row["companies"].append({"ticker": "K1"})
        data = {"schema": sample.context.SCHEMA, "records": [fact("K0")]}
        clean = sample.pilot.minimal_clean(row)
        with mock.patch.object(sample.context, "resolve", wraps=sample.context.resolve) as resolve:
            chosen, reasons, supported = sample.supported_target(row, clean, names, data)
        self.assertEqual(resolve.call_count, 1)
        self.assertEqual(chosen[0], "K0")
        self.assertEqual(supported, ["K0"])
        self.assertEqual(reasons, {"matched": 1, "ticker_not_in_registry": 1})
        self.assertEqual(chosen[2], sample.context.resolve(chosen[1], data)[0])
        row["companies"] = [{"ticker": "K1"}]
        with mock.patch.object(sample.context, "resolve", side_effect=AssertionError("must not resolve")):
            chosen, reasons, supported = sample.supported_target(row, clean, names, data)
        self.assertIsNone(chosen)
        self.assertEqual(reasons, {"ticker_not_in_registry": 1})
        self.assertEqual(supported, [])

    def test_materialization_detects_source_change(self):
        names = sample.previous.company_names(self.companies)
        indexes = self.indexes()
        candidates, _ = sample.collect(self.source, names, self.data, indexes)
        selected, _ = sample.select_unique(candidates)
        identity = selected[0]["article_id"]
        next(row for row in self.rows if row["record_id"] == identity)["title"] += " altered"
        self.save_source()
        with self.assertRaises((ValueError, json.JSONDecodeError)):
            sample.materialize(self.source, selected, names, self.data, indexes)

    def test_registry_invalid_reserved_tamper_and_wrong_counts_fail_before_output(self):
        self.data["records"].append(fact("unrelated", aliases="bad type"))
        self.save_registry()
        with self.assertRaisesRegex(ValueError, "aliases"):
            self.prepare()
        self.assertFalse(self.out.exists())
        self.data["records"].pop()
        self.save_registry()
        with self.assertRaisesRegex(ValueError, "Unexpected known_reviews"):
            self.prepare(expected_counts={"known_reviews": 2, "reserved_random": 1, "holdout": 1})
        with self.reserved.open("a", encoding="utf8") as stream:
            stream.write("\n")
        with self.assertRaisesRegex(ValueError, "Reserved random"):
            self.prepare()
        self.assertFalse(self.out.exists())

    def test_dependency_mutation_during_output_prevents_completion_marker(self):
        real_write = sample.context._write_new
        def changed(path, raw):
            real_write(path, raw)
            if Path(path).name == "inputs.jsonl":
                self.registry.write_text("changed", encoding="utf8")
        with mock.patch.object(sample.context, "_write_new", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "Dependency changed"):
                self.prepare()
        self.assertTrue((self.out / "inputs.jsonl").exists())
        self.assertFalse((self.out / "manifest.json").exists())


if __name__ == "__main__":
    unittest.main()
