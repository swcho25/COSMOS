"""Add sourced historical context to the frozen 13 development articles only.

This does not run a model, assign labels, or establish continuous ownership.
The manifest is published last; an output without it is incomplete.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DIR = Path(__file__).resolve().parent / "artifacts/relevance_policy_v2_development_20260917_v1"
RESEARCH_SHA256 = "57a7f9bccabe2f605895447fc035d5fdd2e34fded3e29f85827d023b87cac065"
OFFICIAL_HOSTS = {"www.sec.gov", "sec.gov", "www.hanafn.com", "hanafn.com"}


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical_sha(row):
    return digest(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode("utf8"))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def day(value):
    require(isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value), "Expected ISO date")
    return date.fromisoformat(value)


def midnight(value):
    return datetime.combine(value, datetime.min.time(), timezone.utc).isoformat().replace("+00:00", "Z")


def text(value):
    require(isinstance(value, str) and bool(value.strip()), "Expected nonempty sourced text")
    return value


def text_list(value):
    require(isinstance(value, list) and bool(value), "Expected a nonempty text list")
    return [text(item) for item in value]


def official_url(value):
    parsed = urlsplit(text(value))
    require(parsed.scheme == "https" and parsed.hostname in OFFICIAL_HOSTS
            and not parsed.username and not parsed.password and parsed.port in (None, 443),
            "Source must be a verified official HTTPS host")
    return value


def parse_rows(raw):
    rows = [json.loads(line) for line in raw.decode("utf-8-sig").splitlines() if line.strip()]
    require(all(isinstance(row, dict) for row in rows), "JSONL rows must be objects")
    return rows


def snapshot(path, snapshots):
    path = Path(path).resolve()
    raw = path.read_bytes()
    current = digest(raw)
    require(path not in snapshots or snapshots[path] == current, "Input changed during reading")
    snapshots[path] = current
    return raw


def unchanged(snapshots):
    for path, expected in snapshots.items():
        require(digest(path.read_bytes()) == expected, f"Input changed before publication: {path}")


def build_context_rows(rows, research, ledger):
    """Validate all identities and temporal constraints before constructing any output."""
    require(research.get("schema_version") == "point_in_time_context_research_v1", "Research schema mismatch")
    require(research.get("development_case_count") == 13 and len(rows) == len(ledger) == 13,
            "Exactly the 13 development articles are required")
    expected = {}
    for item in ledger:
        key = (text(item.get("article_id")), text(item.get("ticker")))
        require(key not in expected, "Duplicate development identity")
        expected[key] = text(item.get("input_sha256"))
    originals = {}
    for row in rows:
        key = (text(row.get("article_id")), text(row.get("ticker")))
        require(key not in originals and key in expected, "Duplicate or non-development article")
        require("company_context" in row and row["company_context"] is None, "Original context must be null")
        require(canonical_sha(row) == expected[key], "Original canonical row hash mismatch")
        day(row.get("published_at"))
        originals[key] = row
    require(set(originals) == set(expected), "Development identity coverage mismatch")

    sources = {}
    for source in research.get("sources", []):
        sid = text(source.get("source_id"))
        require(sid not in sources, "Duplicate source ID")
        require(source.get("primary_source") is True, "Non-primary source")
        official_url(source.get("source_url"))
        official_url(source.get("publication_date_source_url"))
        require(source.get("source_published_at_precision") == "date", "Only date-precision sources are supported")
        published = day(source.get("source_published_at"))
        require(day(source.get("observed_at")) <= published, "Source observation is after publication")
        require(source.get("available_from") == midnight(published + timedelta(days=1)),
                "Source availability must be next-day UTC")
        text_list(source.get("excerpts"))
        text_list(source.get("limitations"))
        sources[sid] = source
    contexts = research.get("contexts")
    require(isinstance(contexts, list) and len(contexts) == 5
            and research.get("supported_development_context_count") == 5, "Exactly five researched contexts are required")
    additions = {}
    for context in contexts:
        key = (text(context.get("article_id")), text(context.get("ticker")))
        require(key in originals and key not in additions, "Context targets another article or repeats one")
        row = originals[key]
        require(context.get("input_sha256") == canonical_sha(row), "Context canonical input hash mismatch")
        require(context.get("article_published_at") == row["published_at"], "Context article date mismatch")
        require(context.get("use_scope") == "development_13_only", "Context is not scoped to development")
        require(context.get("continuous_ownership_through_article_verified") is False
                and context.get("is_gold") is False, "Snapshot must not assert continuous ownership or gold labels")
        require(context.get("source_publication_lte_article") is True
                and context.get("all_sources_available_by_article_day_start") is True,
                "Context publication checks were not recorded")
        sids = text_list(context.get("source_ids"))
        require(len(set(sids)) == len(sids) and all(sid in sources for sid in sids), "Invalid source IDs")
        selected = [sources[sid] for sid in sids]
        latest_publication = max(s["source_published_at"] for s in selected)
        latest_available = max(s["available_from"] for s in selected)
        require(context.get("source_published_at") == latest_publication, "Context publication does not match sources")
        require(context.get("available_from") == latest_available, "Context availability does not match sources")
        require(context.get("observed_at") == min(s["observed_at"] for s in selected),
                "Context observation must match the earliest evidence snapshot")
        article_day = day(row["published_at"])
        start, end = midnight(article_day), midnight(article_day + timedelta(days=1))
        require(context.get("valid_from") == start and context.get("valid_to") == end
                and context.get("valid_to_exclusive") is True, "Context bounds must cover this article day only")
        require(latest_available <= start and day(context["observed_at"]) <= day(latest_publication)
                <= article_day, "Future context is forbidden")
        text(context.get("valid_bounds_meaning"))
        relationships = [text(context.get("context_text"))] + text_list(context.get("limitations"))
        for source in selected:
            relationships.extend(source["limitations"])
        relationships = list(dict.fromkeys(relationships))
        additions[key] = {
            "as_of": latest_available,
            "source": "\n".join(dict.fromkeys(s["source_url"] for s in selected)),
            "relationships": relationships,
        }

    new_rows, row_manifest = [], []
    for original in rows:
        key = (original["article_id"], original["ticker"])
        new = deepcopy(original)
        if key in additions:
            new["company_context"] = additions[key]
        require({k: v for k, v in new.items() if k != "company_context"}
                == {k: v for k, v in original.items() if k != "company_context"}, "Original field changed")
        new_rows.append(new)
        row_manifest.append({"article_id": key[0], "ticker": key[1],
                             "original_row_sha256": canonical_sha(original),
                             "context_row_sha256": canonical_sha(new), "context_added": key in additions})
    return new_rows, row_manifest


def write_new(path, raw):
    with Path(path).open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())


def prepare(*, inputs, research, out_dir, expected_research_sha256=RESEARCH_SHA256):
    snapshots = {}
    raw_research = snapshot(research, snapshots)
    require(digest(raw_research) == expected_research_sha256, "Research file hash differs from reviewed version")
    research_data = json.loads(raw_research)
    raw_inputs = snapshot(inputs, snapshots)
    provenance = research_data.get("input_sources")
    require(isinstance(provenance, list) and len(provenance) == 2, "Missing frozen source inventory")
    inventories = {}
    for item in provenance:
        source_path = Path(item["path"])
        if not source_path.is_absolute():
            source_path = REPO_ROOT / source_path
        require(source_path.name not in inventories, "Duplicate source inventory")
        raw = snapshot(source_path, snapshots)
        require(digest(raw) == item.get("sha256"), "Research provenance file changed")
        inventories[source_path.name] = raw
    require(set(inventories) == {"differences_from_ai.jsonl", "human_inputs.jsonl"}, "Unexpected provenance sources")
    rows, records = build_context_rows(parse_rows(raw_inputs), research_data,
                                      parse_rows(inventories["differences_from_ai.jsonl"]))
    out_dir = Path(out_dir).resolve()
    output = out_dir / "context_inputs.jsonl"
    manifest_path = out_dir / "context_manifest.json"
    require(not output.exists() and not manifest_path.exists(), "Output exists; refusing to overwrite")
    out_raw = ("".join(json.dumps(row, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
                       + "\n" for row in rows)).encode("utf8")
    manifest = {
        "schema_version": "development_context_inputs_v1", "complete": True,
        "use_scope": "development_13_only", "records": len(rows), "contexts_added": 5,
        "original_inputs": {"path": str(Path(inputs).resolve()), "sha256": digest(raw_inputs)},
        "research": {"path": str(Path(research).resolve()), "sha256": digest(raw_research)},
        "producer_sha256": digest(Path(__file__).read_bytes()),
        "output": {"path": str(output), "sha256": digest(out_raw)}, "rows": records,
        "limitations": ["문맥은 과거 관측사실이며 기사일까지 소유불변을 보장하지 않는다.",
                        "신규 표본이나 개발 13건 밖의 기사에는 적용하지 않는다.",
                        "원문 및 기존 사람 선택·AI 추천은 수정하지 않는다. 모델 추론을 수행하지 않는다."],
    }
    unchanged(snapshots)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_new(output, out_raw)
    unchanged(snapshots)
    require(digest(output.read_bytes()) == manifest["output"]["sha256"], "Output verification failed")
    write_new(manifest_path, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode("utf8"))
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inputs", type=Path, default=DEFAULT_DIR / "development_inputs.jsonl")
    parser.add_argument("--research", type=Path, default=DEFAULT_DIR / "context_research.json")
    parser.add_argument("--out-dir", type=Path, default=DEFAULT_DIR)
    parser.add_argument("--research-sha256", default=RESEARCH_SHA256,
                        help="Expected hash of the explicitly reviewed research file")
    args = parser.parse_args()
    result = prepare(inputs=args.inputs, research=args.research, out_dir=args.out_dir,
                     expected_research_sha256=args.research_sha256)
    print(json.dumps({"records": result["records"], "contexts_added": result["contexts_added"],
                      "output": result["output"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
