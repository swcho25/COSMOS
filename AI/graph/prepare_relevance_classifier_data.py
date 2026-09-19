"""Freeze the 100 AI-assisted human choices as classifier seed train/dev data.

This is a data-only step. Labels come exclusively from human_selection. Article
identity, time, AI recommendations and disagreement flags are provenance, never
model features. The development set is not an independent human-gold test set.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import unicodedata

from lake_news_normalize import canonicalize_url

HERE = Path(__file__).resolve().parent
ARTIFACTS = HERE / "artifacts"
SEED = "relevance-classifier-seed100-v1-20260917"
LABELS = ("제외", "채택")
FEATURE_FIELDS = ("title", "body", "company_name")
DEFAULT_PATHS = {
    "inputs": ARTIFACTS / "relevance_two_stage_data_20260917_v1/human_inputs.jsonl",
    "input_manifest": ARTIFACTS / "relevance_two_stage_data_20260917_v1/manifest.json",
    "selections": ARTIFACTS / "relevance_assisted_results_20260917_v1/selection_manifest.jsonl",
    "original": ARTIFACTS / "relevance_assisted_results_20260917_v1/user_selections_original.jsonl",
    "summary": ARTIFACTS / "relevance_assisted_results_20260917_v1/summary.json",
    "differences": ARTIFACTS / "relevance_assisted_results_20260917_v1/differences_from_ai.jsonl",
}
PINNED_SHA256 = {
    "inputs": "4258f9562b8fdd3e72aa22b6f3b9bd7ab971bbab85bd9e7bc03e15705e524196",
    "input_manifest": "6f289ddb1ad5b7b5f2e82e49796a54cf750673b650301913c0a72d4d95850814",
    "selections": "3ceaadbf7e7c9f60c94ee2992e9c79020fe3c38f17d32e962ff545b531fa483e",
    "original": "9e4254681b4970264a6536c6d7d480d913ba4552b2d1c30e57a998cf03c55030",
    "summary": "242abb73c97324ad9dba3596a9043570a6761bc3f2c4ca11c212d9dda73e62ca",
    "differences": "c89860117f94b58258f16202e9564e5be057e697645e474033020edc81511be9",
}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def key(row):
    values = row.get("article_id"), row.get("ticker")
    if not all(isinstance(value, str) and value.strip() for value in values):
        raise ValueError("Missing article/ticker identity")
    return values


def index(rows, *, allow_empty=False):
    result = {}
    for row in rows:
        identity = key(row)
        if identity in result:
            raise ValueError("Duplicate article/company pair")
        result[identity] = row
    if not result and not allow_empty:
        raise ValueError("Empty source")
    return result


def normalized_body_hash(body):
    if not isinstance(body, str) or not body.strip():
        raise ValueError("Missing article body")
    return digest(" ".join(unicodedata.normalize("NFC", body).split()))


def identity_tokens(row):
    tokens = {("article_id", key(row)[0]), ("body_sha256", normalized_body_hash(row["body"]))}
    for field in ("source_url", "url", "canonical_url"):
        raw = row.get(field)
        if raw:
            normalized = canonicalize_url(raw)
            if normalized is None:
                raise ValueError("Invalid article URL: " + field)
            tokens.add(("url", normalized))
    return tokens


def sample_id(row):
    return digest("relevance-classifier-pair-v1:" + canonical(key(row)))


def validate_sources(paths, expected_hashes, expected_rows, expected_counts):
    """Validate pinned bytes and the original selection/export provenance chain."""
    if set(paths) != set(DEFAULT_PATHS) or set(expected_hashes) != set(paths):
        raise ValueError("All six source paths and trusted hashes are required")
    blobs = {name: Path(path).read_bytes() for name, path in paths.items()}
    actual_hashes = {name: hashlib.sha256(blob).hexdigest() for name, blob in blobs.items()}
    if actual_hashes != expected_hashes:
        raise ValueError("Pinned source SHA256 mismatch")
    documents = {}
    for name, blob in blobs.items():
        text = blob.decode("utf-8-sig")
        documents[name] = (json.loads(text) if name in {"summary", "input_manifest"}
                           else [json.loads(line) for line in text.splitlines() if line.strip()])
    inputs = index(documents["inputs"])
    selections = index(documents["selections"])
    original = index(documents["original"])
    differences = index(documents["differences"], allow_empty=True)
    if len(inputs) != expected_rows or set(inputs) != set(selections) or set(inputs) != set(original):
        raise ValueError("Source pair coverage mismatch")
    summary = documents["summary"]
    manifest = documents["input_manifest"]
    if (summary.get("source_inputs_sha256") != actual_hashes["inputs"]
            or summary.get("source_export_sha256") != actual_hashes["original"]
            or manifest.get("files", {}).get("human_inputs.jsonl", {}).get("sha256") != actual_hashes["inputs"]):
        raise ValueError("Input/original export provenance mismatch")
    for source, filename in (("selections", "selection_manifest.jsonl"),
                             ("differences", "differences_from_ai.jsonl"),
                             ("original", "user_selections_original.jsonl")):
        if summary.get("file_sha256", {}).get(filename) != actual_hashes[source]:
            raise ValueError("Selection summary file hash mismatch")
    validation = summary.get("validation", {})
    if (summary.get("review_mode") != "ai_assisted_selection"
            or summary.get("independent_human_gold") is not False
            or summary.get("user_reasons_collected") is not False
            or validation.get("human_selected") != expected_rows
            or validation.get("pending") != 0
            or validation.get("independent_human_gold") is not False):
        raise ValueError("Unsupported human review provenance")
    records, differing = [], set()
    for identity in sorted(inputs):
        row, selected, exported = inputs[identity], selections[identity], original[identity]
        if (row.get("source_split") != "train" or row.get("case_kind") != "real"
                or row.get("company_context") is not None
                or any(not isinstance(row.get(field), str) or not row[field].strip() for field in FEATURE_FIELDS)):
            raise ValueError("Unsupported seed input or feature value")
        input_hash = digest(canonical(row))
        assessment = exported.get("ai_assessment")
        if not isinstance(assessment, dict):
            raise ValueError("Missing original assessment provenance")
        assessment_hash = digest(canonical(assessment))
        for source in (selected, exported):
            if (source.get("input_sha256") != input_hash
                    or source.get("ai_assessment_sha256") != assessment_hash
                    or source.get("review_mode") != "ai_assisted_selection"
                    or source.get("independent_human_gold") is not False):
                raise ValueError("Per-pair source provenance mismatch")
        if (key(assessment) != identity or assessment.get("input_sha256") != input_hash
                or selected.get("title") != row["title"]
                or selected.get("company_name") != row["company_name"]
                or selected.get("user_reason_collected") is not False
                or selected.get("human_selected_at") != exported.get("human_selected_at")
                or not exported.get("human_selected_at")):
            raise ValueError("Original/manifest pair metadata differs")
        label = exported.get("human_selection")
        if label not in LABELS or selected.get("human_selection") != label:
            raise ValueError("Missing, conflicting, or unsupported human label")
        ai_choice = assessment.get("ai_selection")
        if ai_choice not in (*LABELS, "보류") or selected.get("ai_selection") != ai_choice:
            raise ValueError("AI provenance differs; it is not a training label")
        differs = ai_choice != label
        if differs:
            differing.add(identity)
        records.append({"row": row, "sample_id": sample_id(row), "label": label,
                        "input_sha256": input_hash, "differs_from_ai": differs})
    counts = Counter(record["label"] for record in records)
    if dict(counts) != expected_counts or {label: summary.get("counts", {}).get(label, 0) for label in LABELS} != expected_counts:
        raise ValueError("Unexpected human label distribution")
    if summary.get("counts", {}).get("보류", 0) != 0:
        raise ValueError("This seed contract has no human hold labels")
    if set(differences) != differing or summary.get("differences_from_ai") != len(differing):
        raise ValueError("Disagreement coverage mismatch")
    if any(differences[identity] != selections[identity] for identity in differing):
        raise ValueError("Disagreement source row differs from selection manifest")
    return records, actual_hashes


def group_records(records, seed=SEED):
    """Union all transitive ID/URL/body links, including links across targets."""
    by_id = {record["sample_id"]: record for record in records}
    if len(by_id) != len(records):
        raise ValueError("Duplicate sample identity")
    parent = {identity: identity for identity in by_id}

    def find(identity):
        while parent[identity] != identity:
            parent[identity] = parent[parent[identity]]
            identity = parent[identity]
        return identity

    owners = {}
    for identity, record in sorted(by_id.items()):
        for token in sorted(identity_tokens(record["row"])):
            if token in owners:
                a, b = find(identity), find(owners[token])
                if a != b:
                    parent[max(a, b)] = min(a, b)
            else:
                owners[token] = identity
    components = {}
    for identity in sorted(by_id):
        components.setdefault(find(identity), []).append(identity)
    groups = []
    for ids in components.values():
        group_id = digest(canonical(ids))
        groups.append({"group_id": group_id, "sample_ids": ids,
                       "counts": dict(Counter(by_id[identity]["label"] for identity in ids)),
                       "rank": digest(seed + ":group:" + group_id)})
    return sorted(groups, key=lambda group: (group["rank"], group["group_id"]))


def allocate_targets(counts, dev_size):
    total = sum(counts.values())
    if not 0 < dev_size < total or set(counts) != set(LABELS):
        raise ValueError("Need both labels and a nonempty train/dev target")
    result = {label: counts[label] * dev_size // total for label in LABELS}
    priority = sorted(LABELS, key=lambda label: (-(counts[label] * dev_size % total), label))
    for label in priority[:dev_size - sum(result.values())]:
        result[label] += 1
    return result


def split_records(records, dev_size=20, seed=SEED):
    """Exact grouped two-label subset allocation when possible, closest otherwise.

    Disagreement flags and AI choices are never read by this function. Hash order
    breaks equivalent subset choices. DP is bounded by the two human label counts
    (70*32 states for this fixed 100-row seed), not by an exponential search.
    """
    counts = Counter(record["label"] for record in records)
    target = allocate_targets(counts, dev_size)
    groups = group_records(records, seed)
    if len(groups) < 2:
        raise ValueError("All rows form one duplicate group; cannot split without leakage")
    states = {(0, 0): ()}
    for i, group in enumerate(groups):
        negative, positive = (group["counts"].get(label, 0) for label in LABELS)
        for (n, p), selected in list(states.items()):
            state = n + negative, p + positive
            if state not in states:
                states[state] = selected + (i,)
    total = len(records)
    candidates = [state for state in states if 0 < sum(state) < total]
    best = min(candidates, key=lambda state: (
        abs(sum(state) - dev_size),
        sum(abs(value - target[label]) for label, value in zip(LABELS, state)),
        states[state],
    ))
    dev_groups = set(states[best])
    assignments, group_ids = {}, {}
    for i, group in enumerate(groups):
        for identity in group["sample_ids"]:
            assignments[identity] = "dev" if i in dev_groups else "train"
            group_ids[identity] = group["group_id"]
    validate_no_leakage(records, assignments)
    return assignments, group_ids, {
        "seed": seed, "target_dev_rows": dev_size, "target_dev_label_counts": target,
        "actual_dev_rows": sum(best), "actual_dev_label_counts": dict(zip(LABELS, best)),
        "group_count": len(groups), "largest_group_rows": max(len(group["sample_ids"]) for group in groups),
        "exact_target_achieved": tuple(target[label] for label in LABELS) == best,
        "algorithm": "Transitive identity groups; hash-ordered grouped label-count subset DP; minimize row-count error then label-count error; stable hash order tie-break.",
        "disagreement_used_in_split": False,
        "group_no_leakage_overrides_exact_counts": True,
    }


def validate_no_leakage(records, assignments):
    if set(assignments) != {record["sample_id"] for record in records}:
        raise ValueError("Split coverage mismatch")
    tokens = {"train": set(), "dev": set()}
    for record in records:
        split = assignments[record["sample_id"]]
        if split not in tokens:
            raise ValueError("Unknown split")
        tokens[split].update(identity_tokens(record["row"]))
    if tokens["train"] & tokens["dev"]:
        raise ValueError("Cross-split article/URL/body leakage")


def prepare(paths, out, *, expected_hashes=PINNED_SHA256, expected_rows=100,
            expected_counts=None, dev_size=20, seed=SEED):
    out = Path(out)
    if out.exists():
        raise FileExistsError("Use a new output directory; frozen artifacts are immutable")
    expected_counts = expected_counts or {"채택": 69, "제외": 31}
    records, source_hashes = validate_sources(paths, expected_hashes, expected_rows, expected_counts)
    assignments, group_ids, allocation = split_records(records, dev_size, seed)
    ordered = sorted(records, key=lambda record: (digest(seed + ":output:" + record["sample_id"]), record["sample_id"]))
    features, labels, provenance = [], [], []
    for record in ordered:
        row, identity = record["row"], record["sample_id"]
        features.append({"sample_id": identity, **{field: row[field] for field in FEATURE_FIELDS}})
        labels.append({"sample_id": identity, "label": record["label"]})
        provenance.append({
            "sample_id": identity, "article_id": row["article_id"], "ticker": row["ticker"],
            "published_at": row.get("published_at"), "source_url": row.get("source_url"),
            "source_input_sha256": record["input_sha256"], "features_sha256": digest(canonical(features[-1])),
            "normalized_body_sha256": normalized_body_hash(row["body"]),
            "canonical_urls": sorted(value for kind, value in identity_tokens(row) if kind == "url"),
            "group_id": group_ids[identity], "split": assignments[identity],
            "flags": {"differs_from_ai": record["differs_from_ai"]},
            "review_mode": "ai_assisted_selection", "independent_human_gold": False,
            "label_source": "user_selections_original.jsonl:human_selection",
        })
    payloads = {"seed100_inputs.jsonl": features, "seed100_labels.jsonl": labels,
                "provenance.jsonl": provenance}
    for split in ("train", "dev"):
        payloads[split + "_inputs.jsonl"] = [row for row in features if assignments[row["sample_id"]] == split]
        payloads[split + "_labels.jsonl"] = [row for row in labels if assignments[row["sample_id"]] == split]
    for name, path in paths.items():
        if sha(path) != source_hashes[name]:
            raise ValueError("Source changed during preparation")
    out.mkdir(parents=True, exist_ok=False)
    files = {}
    for name, rows in payloads.items():
        path = out / name
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(canonical(row) + "\n")
        recovered = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if recovered != rows:
            raise ValueError("Output reread differs")
        files[name] = {"sha256": sha(path), "rows": len(rows), "bytes": path.stat().st_size}
    if any(sha(path) != source_hashes[name] for name, path in paths.items()):
        raise ValueError("Source changed before final manifest; output is not frozen")
    manifest = {
        "schema": "relevance-classifier-seed-data-1", "status": "frozen",
        "source_files": {name: {"path": str(Path(path).resolve()), "sha256": source_hashes[name]} for name, path in paths.items()},
        "rows": len(records), "label_counts": dict(Counter(record["label"] for record in records)),
        "split_counts": {split: dict(Counter(record["label"] for record in records if assignments[record["sample_id"]] == split)) for split in ("train", "dev")},
        "allocation": allocation, "feature_fields": list(FEATURE_FIELDS),
        "join_metadata_fields": ["sample_id"], "label_field": "human_selection", "label_mapping": {"제외": 0, "채택": 1},
        "features_exclude": ["article_id", "ticker", "published_at", "source_url", "human_selection", "AI verdict/reason/evidence", "disagreement flag", "human_selected_at", "company_context"],
        "source_text_preserved": True, "labels_and_inputs_separate": True,
        "review_mode": "ai_assisted_selection", "independent_human_gold": False,
        "human_reasons_collected": False, "human_hold_labels": 0, "hold_gold_available": False,
        "disagreement_count": sum(record["differs_from_ai"] for record in records),
        "disagreement_counts_by_split": {split: sum(record["differs_from_ai"] for record in records if assignments[record["sample_id"]] == split) for split in ("train", "dev")},
        "leakage_check": {"article_id": "PASS", "canonical_url": "PASS", "normalized_body_sha256": "PASS", "transitive_groups": "PASS"},
        "body_normalization": "NFC Unicode plus whitespace collapse; grouping only; feature body unchanged",
        "url_normalization": "lake_news_normalize.canonicalize_url; remove utm_* and fragment; retain semantic query values and HTTP/HTTPS distinctions",
        "limitations": [
            "User choices were made with AI recommendations visible; this is not independent human gold.",
            "The 100 pairs were already used for development of earlier relevance policies; dev is internal development data, not a new untouched test.",
            "No hold labels or user reasons were collected; do not convert AI holds into human hold gold or train hold as exclusion.",
            "The seed is small: 2020–2022 training-period articles, 200–6000 cleaned-body character cohort, 50 per source region. It does not represent population accuracy.",
            "Company display names were not historically verified for every source; current names do not prove historical identity.",
            "Dates/codes naturally appearing inside original article text are preserved; their dedicated metadata fields are excluded from features.",
            "No prices, return labels, model inference, GNN training, service or HDFS writes occur in this preparation.",
        ],
        "code_sha256": {Path(__file__).name: sha(__file__), "lake_news_normalize.py": sha(HERE / "lake_news_normalize.py")},
        "files": files,
    }
    with (out / "manifest.json").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return manifest


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name, default in DEFAULT_PATHS.items():
        parser.add_argument("--" + name.replace("_", "-"), type=Path, default=default)
    parser.add_argument("--out", type=Path, default=ARTIFACTS / "relevance_classifier_v1_20260917_v1/seed_data")
    args = parser.parse_args(argv)
    manifest = prepare({name: getattr(args, name) for name in DEFAULT_PATHS}, args.out)
    print(json.dumps({"out": str(args.out.resolve()), "rows": manifest["rows"],
                      "split_counts": manifest["split_counts"], "allocation": manifest["allocation"],
                      "manifest_sha256": sha(args.out / "manifest.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
