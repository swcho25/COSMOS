"""Exercise exact-source retrieval and real local news explanation, without serving."""
import argparse
import json
from pathlib import Path
import time

from integrated_relation_graph import IntegratedRelationRetriever, IntegratedRelationExplainer, sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, default=Path(__file__).parent / "artifacts/news_impact_v2_bundle")
    parser.add_argument("--device", default="cuda")
    args = parser.parse_args()
    if args.out.exists():
        raise ValueError("Use a fresh report path")
    started = time.monotonic()
    reader = IntegratedRelationRetriever(args.graph)
    engine = IntegratedRelationExplainer(reader, args.bundle, args.device)
    queries = [("009150", "005930", "SUPPLY"), ("005930", "047040", "PARTNER"),
               ("005930", "066570", "COMPETE"), ("005930", "009150", "INVEST"),
               ("307950", "005380", "SUPPLY")]
    results = []
    for src, dst, kind in queries:
        begin = time.monotonic()
        result = engine.explain(src, dst, kind, as_of="2026-09-19T00:00:00Z", limit=5)
        assert result["status"] in {"GENERATED", "EXTRACTIVE_FALLBACK"}
        assert result["disclosures_searched"] is True and result["evidence"]
        by_id = {row["id"]: row for row in result["evidence"]}
        for claim in result["claims"]:
            assert claim["quote"] in by_id[claim["evidence_id"]]["quote"]
        assert not reader.retrieve(src, dst, kind, as_of="2000-01-01T00:00:00Z")["evidence"]
        result["elapsed_seconds"] = time.monotonic() - begin
        results.append(result)
        print(json.dumps({"query": [src, dst, kind], "status": result["status"],
                          "sources": result["sources_returned"], "seconds": result["elapsed_seconds"]}), flush=True)
    result = {"graph_manifest_sha256": sha(args.graph / "manifest.json"),
              "smoke_code_sha256": sha(Path(__file__)), "real_local_model_requested": True,
              "disclosure_tables_use_extracts_without_llm": True, "semantic_accuracy_estimated": False,
              "checks_passed": True, "elapsed_seconds": time.monotonic() - started, "results": results}
    args.out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
