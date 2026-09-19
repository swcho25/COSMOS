"""CPU fixtures: exact relationship retrieval and fake-generator grounding."""
from contextlib import closing
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys
import unittest
import uuid

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
from grounded_relation_graphrag import GroundedRelationRetriever, GroundedRelationExplainer


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def dump(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class Generator:
    provenance = {"fake_cpu_generator": True}
    def __init__(self, claims=None, error=None):
        self.claims, self.error, self.calls = claims, error, []
    def generate(self, payload):
        self.calls.append(payload)
        if self.error:
            raise self.error
        if self.claims is not None:
            return json.dumps(self.claims, ensure_ascii=False)
        e = payload["evidence"][0]
        return json.dumps({"claims": [{"evidence_id": e["id"], "quote": e["text"]}]}, ensure_ascii=False)


class Verifier:
    def __init__(self, supported):
        self.supported, self.calls = supported, []
    def verify(self, claims):
        self.calls.append(claims)
        return self.supported, [{"entailment": 0.99 if self.supported else 0.1}]


class RelationGraphRagTests(unittest.TestCase):
    def setUp(self):
        self.workspace = (HERE / "artifacts").resolve()
        self.root = self.workspace / ("_relation_rag_cpu_" + uuid.uuid4().hex)
        self.root.mkdir()
        self.kg = self.root / "kg"
        self.kg.mkdir()
        self.db = self.kg / "state.sqlite3"
        with closing(sqlite3.connect(self.db)) as con:
            con.executescript("""CREATE TABLE articles(article_id TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE pairs(article_id TEXT,ticker TEXT,decision TEXT,PRIMARY KEY(article_id,ticker));
                CREATE TABLE relations(edge_id TEXT PRIMARY KEY,article_id TEXT,src_ticker TEXT,dst_ticker TEXT,
                    rel_type TEXT,directed INTEGER,payload TEXT);""")
        dump(self.kg / "protocol.json", {"fixture": True})

    def tearDown(self):
        target = self.root.resolve()
        assert target.is_relative_to(self.workspace) and target.name.startswith("_relation_rag_cpu_")
        shutil.rmtree(target)

    def add(self, article="a1", kind="SUPPLY", published="2024-01-01", quote=None, edge_id=None):
        quote = quote or ("삼성전자와 애플은 업무협약을 체결했다." if kind == "PARTNER" else "삼성전자는 애플에 반도체를 공급했다.")
        news = {"article_id": article, "title": "🙂 계약 기사", "body": quote,
                "source_url": "https://example.invalid/" + article, "published_at": published}
        text = news["title"] + "\n" + quote
        edge = {"edge_id": edge_id or "relation:" + article + ":" + kind,
                "article_id": article, "src_ticker": "S", "dst_ticker": "A", "rel_type": kind,
                "directed": kind not in {"PARTNER", "COMPETE"}, "sentence": quote,
                "sentence_start": len(news["title"]) + 1, "sentence_end": len(text),
                "source_start": text.index("삼성전자"), "source_end": text.index("삼성전자") + 4,
                "source_quote": "삼성전자", "target_start": text.index("애플"),
                "target_end": text.index("애플") + 2, "target_quote": "애플",
                "source_url": news["source_url"], "published_at": published}
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("INSERT OR IGNORE INTO articles VALUES (?,?)", (article, json.dumps(news, ensure_ascii=False)))
            for ticker in ("S", "A"):
                con.execute("INSERT OR IGNORE INTO pairs VALUES (?,?,?)", (article, ticker, "candidate_ready"))
            con.execute("INSERT INTO relations VALUES (?,?,?,?,?,?,?)", (edge["edge_id"], article, "S", "A", kind, int(edge["directed"]), json.dumps(edge, ensure_ascii=False)))
            con.commit()
        return edge

    def seal(self):
        manifest = {"schema": "cosmos-grounded-candidate-kg-1", "artifact_complete": True,
                    "graph_materialized": True, "state_sqlite_sha256": sha(self.db),
                    "protocol_sha256": sha(self.kg / "protocol.json")}
        dump(self.kg / "manifest.json", manifest)
        return GroundedRelationRetriever(self.kg)

    def test_exact_direction_type_and_no_comention_fallback(self):
        self.add()
        reader = self.seal()
        self.assertEqual(len(reader.retrieve("S", "A", "SUPPLY")["evidence"]), 1)
        for src, dst, kind in [("A", "S", "SUPPLY"), ("S", "A", "INVEST"), ("S", "B", "SUPPLY")]:
            self.assertEqual(reader.retrieve(src, dst, kind)["status"], "NO_EVIDENCE")
        for kind in ("ACQUIRE", "co_mention", "correlation"):
            with self.assertRaises(ValueError):
                reader.retrieve("S", "A", kind)

    def test_partner_is_symmetric_but_retains_evidence_orientation(self):
        self.add(kind="PARTNER")
        result = self.seal().retrieve("A", "S", "PARTNER")
        self.assertEqual(result["status"], "FOUND")
        self.assertFalse(result["relationship"]["directed"])
        self.assertEqual(result["evidence"][0]["src_ticker"], "S")

    def test_publication_cutoff_excludes_future_and_unknown(self):
        self.add("past", published="2023-12-31")
        self.add("future", published="2024-01-02")
        self.add("unknown", published="not-a-date")
        result = self.seal().retrieve("S", "A", "SUPPLY", as_of="2024-01-01")
        self.assertEqual([e["article_id"] for e in result["evidence"]], ["past"])
        self.assertEqual(result["excluded_unknown_or_after_cutoff"], 2)

    def test_intraday_cutoff_never_assumes_unknown_timezone(self):
        self.add("naive", published="2024-01-01 01:00")
        self.add("before", published="2024-01-01T10:00:00+09:00")
        self.add("after", published="2024-01-01T12:00:00+09:00")
        reader = self.seal()
        result = reader.retrieve("S", "A", "SUPPLY", as_of="2024-01-01T02:00:00Z")
        self.assertEqual([e["article_id"] for e in result["evidence"]], ["before"])
        with self.assertRaises(ValueError):
            reader.retrieve("S", "A", "SUPPLY", as_of="2024-01-01T02:00:00")

    def test_same_article_quote_dedup_and_full_quote_metadata(self):
        quote = "삼성전자는 애플에 " + "반도체 " * 220 + "공급했다."
        self.add(quote=quote)
        self.add(quote=quote, edge_id="duplicate-evidence")
        result = self.seal().retrieve("S", "A", "SUPPLY")
        self.assertEqual(len(result["evidence"]), 1)
        e = result["evidence"][0]
        self.assertEqual(e["quote"], quote)
        self.assertEqual(e["source_url"], "https://example.invalid/a1")
        self.assertEqual(e["id"], "E1")
        self.assertNotEqual(e["evidence_id"], e["article_id"])

    def test_checksum_and_incomplete_manifest_rejected(self):
        self.add(); self.seal()
        manifest = json.loads((self.kg / "manifest.json").read_text())
        manifest["artifact_complete"] = False
        dump(self.kg / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "completed"):
            GroundedRelationRetriever(self.kg)
        manifest["artifact_complete"] = True
        manifest["state_sqlite_sha256"] = "wrong"
        dump(self.kg / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "checksum"):
            GroundedRelationRetriever(self.kg)

    def test_nonempty_wal_is_rejected_before_immutable_read(self):
        self.add(); self.seal()
        wal = Path(str(self.db) + "-wal")
        wal.write_bytes(b"uncheckpointed data")
        with self.assertRaisesRegex(ValueError, "empty WAL"):
            GroundedRelationRetriever(self.kg)

    def test_ungrounded_endpoint_and_forged_span_fail_closed(self):
        self.add()
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("UPDATE pairs SET decision='review' WHERE ticker='A'"); con.commit()
        with self.assertRaisesRegex(ValueError, "ungrounded"):
            self.seal().retrieve("S", "A", "SUPPLY")
        with closing(sqlite3.connect(self.db)) as con:
            con.execute("UPDATE pairs SET decision='candidate_ready'")
            data = json.loads(con.execute("SELECT payload FROM relations").fetchone()[0])
            data["sentence"] = "fabricated relation"
            con.execute("UPDATE relations SET payload=?", (json.dumps(data),)); con.commit()
        with self.assertRaisesRegex(ValueError, "quote/span"):
            self.seal().retrieve("S", "A", "SUPPLY")

    def test_external_revision_joins_original_articles_and_binds_parent(self):
        self.add(); self.seal()
        revision = self.root / "revision"; revision.mkdir()
        target = revision / "state.sqlite3"
        with closing(sqlite3.connect(self.db)) as source, closing(sqlite3.connect(target)) as con:
            con.execute("CREATE TABLE relations(edge_id TEXT PRIMARY KEY,article_id TEXT,src_ticker TEXT,dst_ticker TEXT,rel_type TEXT,directed INTEGER,payload TEXT)")
            con.executemany("INSERT INTO relations VALUES (?,?,?,?,?,?,?)", source.execute("SELECT * FROM relations")); con.commit()
        parent = json.loads((self.kg / "manifest.json").read_text())
        manifest = {"schema": "cosmos-grounded-relations-revision-1", "artifact_complete": True,
                    "state_sqlite_sha256": sha(target), "parent_kg": {"directory": str(self.kg),
                    "manifest_sha256": sha(self.kg / "manifest.json"), "state_sqlite_sha256": parent["state_sqlite_sha256"],
                    "protocol_sha256": parent["protocol_sha256"]}}
        dump(revision / "manifest.json", manifest)
        self.assertEqual(GroundedRelationRetriever(self.kg, target).retrieve("S", "A", "SUPPLY")["status"], "FOUND")
        manifest["parent_kg"]["manifest_sha256"] = "wrong"
        dump(revision / "manifest.json", manifest)
        with self.assertRaisesRegex(ValueError, "different parent"):
            GroundedRelationRetriever(self.kg, target)

    def test_no_evidence_skips_generator(self):
        self.add(); generator = Generator()
        result = GroundedRelationExplainer(self.seal(), generator=generator).explain("A", "S", "SUPPLY")
        self.assertEqual(result["status"], "NO_EVIDENCE")
        self.assertIsNone(result["explanation"])
        self.assertEqual(generator.calls, [])

    def test_exact_generation_has_citations_and_no_scores(self):
        edge = self.add(); generator = Generator()
        result = GroundedRelationExplainer(self.seal(), generator=generator).explain("S", "A", "SUPPLY")
        self.assertEqual(result["status"], "GENERATED")
        self.assertIn(edge["sentence"], result["explanation"])
        self.assertEqual(result["evidence_ids"], ["E1"])
        self.assertFalse(result["production_approved"])
        self.assertFalse(result["graph_quality_validated"])
        self.assertNotIn("prediction", generator.calls[0])
        self.assertNotIn("score", result)
        self.assertEqual(result["source_kind"], "NEWS")
        self.assertFalse(result["disclosures_searched"])
        self.assertEqual(generator.calls[0]["evidence"][0]["source_quote"], "삼성전자")
        self.assertEqual(generator.calls[0]["evidence"][0]["target_quote"], "애플")

    def test_verified_paraphrase_and_semantic_failure_fallback(self):
        edge = self.add()
        claims = {"claims": [{"evidence_id": "E1", "quote": edge["sentence"], "summary": "삼성전자는 애플에 반도체를 공급했다."}]}
        reader = self.seal()
        for supported in (True, False):
            verifier = Verifier(supported)
            result = GroundedRelationExplainer(reader, generator=Generator(claims), verifier=verifier).explain("S", "A", "SUPPLY")
            self.assertEqual(result["status"], "GENERATED" if supported else "EXTRACTIVE_FALLBACK")
            self.assertEqual(len(verifier.calls), 1)
            self.assertEqual("summary" in result["claims"][0], supported)

    def test_wrong_citation_invented_number_or_dropped_party_falls_back(self):
        edge = self.add(); reader = self.seal()
        good = {"evidence_id": "E1", "quote": edge["sentence"], "summary": edge["sentence"]}
        for claim in [{**good, "evidence_id": "E99"}, {**good, "summary": "2025년 삼성전자는 애플에 반도체를 공급했다."},
                      {**good, "summary": "삼성전자가 반도체 공급을 담당했다."}]:
            with self.subTest(claim=claim):
                verifier = Verifier(True)
                result = GroundedRelationExplainer(reader, generator=Generator({"claims": [claim]}), verifier=verifier).explain("S", "A", "SUPPLY")
                self.assertEqual(result["status"], "EXTRACTIVE_FALLBACK")
                self.assertEqual(result["claims"][0]["quote"], edge["sentence"])
                self.assertEqual(verifier.calls, [])

    def test_generator_failure_and_extra_score_never_invent_explanation(self):
        edge = self.add(); reader = self.seal()
        for generator in [Generator(error=RuntimeError("model unavailable")),
                          Generator({"claims": [{"evidence_id": "E1", "quote": edge["sentence"]}], "impact_score": 99})]:
            result = GroundedRelationExplainer(reader, generator=generator).explain("S", "A", "SUPPLY")
            self.assertEqual(result["status"], "EXTRACTIVE_FALLBACK")
            self.assertIn(edge["sentence"], result["explanation"])
            self.assertNotIn("impact_score", result)

    def test_malformed_limits_types_and_self_pair_fail_closed(self):
        reader = self.seal()
        for kwargs in [{"limit": True}, {"limit": 0}, {"limit": 21}, {"as_of": "2024-99-99"}]:
            with self.assertRaises(ValueError):
                reader.retrieve("S", "A", "SUPPLY", **kwargs)
        with self.assertRaises(ValueError):
            reader.retrieve("S", "S", "PARTNER")


if __name__ == "__main__":
    unittest.main()
