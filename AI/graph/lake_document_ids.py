"""Stable UUIDs and conservative duplicate representatives for cleaned lake news.

This module performs no I/O. The caller MUST durably retain the complete returned
id_map and supply it as prior_map on every subsequent run. UUIDv5 is deterministic
for an initial component, but a later URL or body may sort before its original
identity key; only the persisted mapping prevents an existing UUID from changing.
Conflicting existing UUIDs require an explicit reconciliation outside this module.

Duplicate checks cover the supplied rows and prior identities only. In particular,
a 90-day slice cannot certify global deduplication: its body may already exist in
an older raw article that was never supplied or mapped. A prior map also contains
no old article text. If only a later revision is supplied, its representative keeps
that revision's actual publication timestamp, never an older map's available_at.

Identity keys are url:<SHA256>, content:<SHA256> (body >=80 normalized characters),
and record:<source_dataset>:<record_id>. source_dataset cannot contain a colon.
duplicate_count counts distinct source records including the representative.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import re
import unicodedata
import uuid


DOCUMENT_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:cosmos:data-lake:news:document:v1")
BODY_MIN_CHARS = 80
UTC = dt.timezone.utc
HASH_KEY = re.compile(r"(?:url|content):[0-9a-f]{64}\Z")


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _normalized_body(row: dict) -> str:
    return " ".join(unicodedata.normalize("NFC", _text(row.get("body"))).split())


def _stamp(value) -> dt.datetime:
    try:
        stamp = value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            raise ValueError("timezone required")
        return stamp.astimezone(UTC)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"published_at/available_at must be an explicit UTC-convertible timestamp: {value!r}") from error


def _uuid(value) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError(f"Invalid existing document_id: {value!r}") from error


def _record_key(row: dict) -> str:
    source, record = row.get("source_dataset"), row.get("record_id")
    if not isinstance(source, str) or not source.strip() or source != source.strip() or ":" in source:
        raise ValueError("source_dataset must be a nonempty trimmed string without ':'")
    if not isinstance(record, str) or not record.strip() or record != record.strip():
        raise ValueError("record_id must be a nonempty trimmed string")
    return f"record:{source}:{record}"


def _validate_identity(key):
    if not isinstance(key, str):
        raise ValueError("prior_map identity_key must be a string")
    if HASH_KEY.fullmatch(key):
        return
    if key.startswith("record:"):
        parts = key.split(":", 2)
        if len(parts) == 3:
            if _record_key({"source_dataset": parts[1], "record_id": parts[2]}) == key:
                return
    raise ValueError(f"Invalid prior_map identity_key: {key!r}")


def _identity_keys(row: dict) -> tuple[set[str], int]:
    keys = {_record_key(row)}
    canonical = row.get("canonical_url")
    if canonical:
        if not isinstance(canonical, str):
            raise ValueError("canonical_url must be a normalized string or None")
        keys.add("url:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest())
    body = _normalized_body(row)
    if body:
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        if row.get("content_hash") and row["content_hash"] != digest:
            raise ValueError(f"content_hash disagrees with cleaned body for {_record_key(row)}")
        if len(body) >= BODY_MIN_CHARS:
            keys.add("content:" + digest)
    # identity_candidate can refer to a short or title-only hash; do not use it.
    return keys, len(body)


class _UnionFind:
    def __init__(self, size):
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, index):
        while self.parent[index] != index:
            self.parent[index] = self.parent[self.parent[index]]
            index = self.parent[index]
        return index

    def union(self, a, b):
        a, b = self.find(a), self.find(b)
        if a == b:
            return
        if self.rank[a] < self.rank[b]:
            a, b = b, a
        self.parent[b] = a
        if self.rank[a] == self.rank[b]:
            self.rank[a] += 1


def assign_document_ids(rows: list[dict], prior_map: list[dict] | None = None) -> tuple[list[dict], list[dict], dict]:
    """Return cleaned representatives, complete identity map, and scope summary.

    Union by shared URL, sufficient body hash, or a shared prior document UUID.
    A repeated source record must be an identical input dict; otherwise fail.
    Prior keys not seen in this batch remain in the returned map. Representative
    selection first minimizes published_at, then maximizes normalized body length
    among rows at that same instant, then orders by record_id/source_dataset.
    No new body is backdated to an earlier row's or prior mapping's timestamp.
    """
    if not isinstance(rows, list) or (prior_map is not None and not isinstance(prior_map, list)):
        raise TypeError("rows and prior_map must be lists of dictionaries")

    prior = {}
    for entry in prior_map or []:
        if not isinstance(entry, dict):
            raise TypeError("prior_map entries must be dictionaries")
        key = entry.get("identity_key")
        _validate_identity(key)
        document_id = _uuid(entry.get("document_id"))
        if key in prior and prior[key]["document_id"] != document_id:
            raise ValueError(f"Conflicting prior document_ids for identity {key}")
        mapped = dict(entry)
        mapped.update(identity_key=key, document_id=document_id)
        for field in ("representative_record_id", "representative_source_dataset", "available_at"):
            mapped.setdefault(field, None)
        if mapped["available_at"] is not None:
            mapped["available_at"] = _stamp(mapped["available_at"]).isoformat().replace("+00:00", "Z")
        if key not in prior:
            prior[key] = mapped

    unique, record_rows, repeated = [], {}, 0
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("rows entries must be dictionaries")
        record_key = _record_key(row)
        if record_key in record_rows:
            if row != record_rows[record_key]:
                raise ValueError(f"Conflicting rows for source identity {record_key}")
            repeated += 1
            continue
        record_rows[record_key] = row
        keys, body_length = _identity_keys(row)
        existing = {prior[key]["document_id"] for key in keys if key in prior}
        if row.get("document_id") is not None:
            existing.add(_uuid(row["document_id"]))
        unique.append({"row": row, "keys": keys, "body_length": body_length,
                       "published_at": _stamp(row.get("published_at")), "existing": existing})

    union = _UnionFind(len(unique))
    owners, uuid_owners = {}, {}
    for index, item in enumerate(unique):
        for key in item["keys"]:
            if key in owners:
                union.union(index, owners[key])
            else:
                owners[key] = index
        for document_id in item["existing"]:
            if document_id in uuid_owners:
                union.union(index, uuid_owners[document_id])
            else:
                uuid_owners[document_id] = index

    groups = {}
    for index in range(len(unique)):
        groups.setdefault(union.find(index), []).append(unique[index])

    representatives, complete_map, current_metadata = [], dict(prior), {}
    prior_document_ids = {entry["document_id"] for entry in prior.values()}
    new_ids, reused_ids, current_keys = set(), set(), set()
    for group in groups.values():
        keys = set().union(*(item["keys"] for item in group))
        existing = set().union(*(item["existing"] for item in group))
        if len(existing) > 1:
            raise ValueError("Duplicate component joins conflicting existing document_ids: " + ", ".join(sorted(existing)))
        if existing:
            document_id = next(iter(existing))
            reused_ids.add(document_id)
        else:
            document_id = str(uuid.uuid5(DOCUMENT_NAMESPACE, min(keys)))
            # A coincident generated UUID must not hijack an unrelated prior ID.
            if document_id in prior_document_ids:
                raise ValueError(f"Generated UUID collides with an unrelated prior identity: {document_id}")
            new_ids.add(document_id)
        chosen = min(group, key=lambda item: (
            item["published_at"], -item["body_length"],
            item["row"]["record_id"], item["row"]["source_dataset"],
        ))
        representative = dict(chosen["row"])
        representative.update(document_id=document_id, duplicate_count=len(group))
        representatives.append(representative)
        metadata = {
            "document_id": document_id,
            "representative_record_id": representative["record_id"],
            "representative_source_dataset": representative["source_dataset"],
            "available_at": chosen["published_at"].isoformat().replace("+00:00", "Z"),
        }
        current_metadata[document_id] = metadata
        for key in keys:
            complete_map[key] = {"identity_key": key, **metadata}
        current_keys.update(keys)

    # All aliases of a currently represented document describe this same actual
    # representative. Prior-only documents retain their previous metadata.
    for key, entry in complete_map.items():
        if entry["document_id"] in current_metadata:
            complete_map[key] = {**entry, **current_metadata[entry["document_id"]]}
    representatives.sort(key=lambda row: (_stamp(row["published_at"]), row["document_id"]))
    id_map = [complete_map[key] for key in sorted(complete_map)]
    summary = {
        "input_rows": len(rows), "unique_source_records": len(unique),
        "repeated_identical_rows": repeated, "representative_count": len(representatives),
        "duplicate_source_records": len(unique) - len(representatives),
        "merged_group_count": sum(len(group) > 1 for group in groups.values()),
        "new_document_ids": len(new_ids), "reused_document_ids": len(reused_ids),
        "identity_key_count": len(id_map), "retained_prior_key_count": len(set(prior) - current_keys),
        "document_namespace": str(DOCUMENT_NAMESPACE), "body_min_chars": BODY_MIN_CHARS,
        "prior_map_supplied": prior_map is not None, "prior_map_required_for_subsequent_runs": True,
        "dedup_scope": "supplied_rows_and_prior_identities", "global_dedup_certified": False,
        "limitations": [
            "A 90-day slice cannot certify global duplicates against older unmapped articles.",
            "Persist this complete identity map and supply it for every subsequent run.",
            "Prior identities contain no old text; later revisions retain their own availability time.",
        ],
    }
    return representatives, id_map, summary
