"""Disk-backed canonical news identities with immutable citation revisions.

The historical lake identity map is the authority for existing document UUIDs.
This adapter uses exactly its URL/long-body key rules, keeps all prior aliases
and metadata, and fails if a duplicate component would merge distinct old IDs.
The supplied rows, their URLs, and old mapped aliases are the entire dedup scope;
no claim is made about all raw news in HDFS.

Only per-article hashes and metadata are retained in SQLite. Callers stream the
original JSONL again to write cleaned representatives and original-text revision
records. Quotes reference source_revision_id and original_title_newline_body,
not whichever text happens to represent the canonical document. Publication
timestamps are not used in identity generation; naive clocks/days are never
upgraded to UTC. Representative selection prefers a base news node, then the
lexicographically smallest article_id; its own time metadata must be retained.

This is an append-only new-run builder, not a resume or in-place migration tool.
An existing output database is rejected. Persist the complete exported map for
future runs. Optional component quarantine retains all conflicting old aliases
without assigning a new UUID; the caller must separately account for every
quarantined article and exclude it from published references.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Iterable, Iterator

from lake_document_ids import DOCUMENT_NAMESPACE, _identity_keys, _uuid, _validate_identity
from lake_news_normalize import canonicalize_url


REVISION_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:cosmos:data-lake:news:source-revision:v1")
SCHEMA = "cosmos-grounded-lake-identity-1"


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _explicit_utc(value) -> str | None:
    if not isinstance(value, str) or len(value) <= 10:
        return None
    try:
        stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        return stamp.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, OverflowError):
        return None


def _metadata(row: dict, source_dataset: str) -> tuple[dict, set[str], str]:
    article_id = row.get("article_id")
    if not isinstance(article_id, str) or not article_id or article_id.strip() != article_id:
        raise ValueError("article_id must be a nonempty trimmed string")
    fields = {}
    for field in ("title", "body", "cleaned_title", "cleaned_body"):
        value = row.get(field)
        # Integrated relation documents can have only original title/body. Root
        # must supply their already-sanitized cleaned fields; never silently use
        # original boilerplate as a dedup key.
        if not isinstance(value, str):
            raise ValueError(f"{article_id}: {field} must be supplied as a string")
        fields[field] = value
    original_sha = _sha(fields["title"] + "\n" + fields["body"])
    original_fields_sha = _sha(_json({"title": fields["title"], "body": fields["body"]}))
    # Existing grounded KG hashes JSON title/body fields, whereas citation
    # revisions hash the joined literal span text. Keep the two bases explicit.
    cleaned_sha = _sha(_json({"title": fields["cleaned_title"], "body": fields["cleaned_body"]}))
    for owner, field, expected in (
        (row, "cleaned_text_sha256", cleaned_sha),
        (row.get("cleaning_audit") or {}, "original_text_sha256", original_fields_sha),
    ):
        if owner.get(field) is not None and owner[field] != expected:
            raise ValueError(f"{article_id}: supplied {field} differs from text")
    canonical_url = canonicalize_url(row.get("source_url"))
    keys, body_length = _identity_keys({
        "source_dataset": source_dataset, "record_id": article_id,
        "canonical_url": canonical_url, "body": fields["cleaned_body"],
    })
    source_revision_id = str(uuid.uuid5(REVISION_NAMESPACE, _json([source_dataset, article_id, original_sha])))
    scope = row.get("_lake_scope", "base")
    if scope not in ("base", "relation_only"):
        raise ValueError(f"{article_id}: invalid _lake_scope {scope!r}")
    date_audit = row.get("date_audit")
    available_at = _explicit_utc(row.get("published_at"))
    if isinstance(date_audit, dict):
        # A prior source-time audit can deliberately reject a syntactically
        # valid epoch/normalized timestamp. Its provenance takes precedence.
        available_at = (_explicit_utc(date_audit.get("published_at_utc"))
                        if date_audit.get("status") == "explicit_source_time" else None)
    metadata = {
        "article_id": article_id, "source_dataset": source_dataset,
        "source_revision_id": source_revision_id,
        "original_text_sha256": original_sha, "cleaned_text_sha256": cleaned_sha,
        "original_fields_sha256": original_fields_sha,
        "canonical_url": canonical_url, "body_normalized_characters": body_length,
        "published_at": row.get("published_at"), "date_audit": row.get("date_audit"),
        "available_at": available_at,
        "scope": scope, "offset_basis": "original_title_newline_body",
    }
    # Additional KG flags/graph node IDs can differ across exports. Original
    # source fields, cleaning, and source identity must agree for repeated IDs.
    signature = _sha(_json({"article_id": article_id, **fields,
                            "published_at": row.get("published_at"),
                            "source_url": row.get("source_url")}))
    return metadata, keys, signature


class IdentityConflict(ValueError):
    """Conflicting established UUIDs; database contains a diagnostic table."""


class GroundedIdentityIndex:
    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path)
        self.db = sqlite3.connect(self.db_path.resolve().as_uri() + "?mode=ro", uri=True)
        record = self.db.execute("SELECT value FROM info WHERE key='summary'").fetchone()
        if record is None:
            self.close()
            raise ValueError("Identity index has no successful completion summary")
        self.summary = json.loads(record[0])

    def close(self):
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def get(self, article_id: str) -> dict:
        result = self.db.execute("SELECT resolved FROM records WHERE article_id=?", (article_id,)).fetchone()
        if result is None:
            raise KeyError(article_id)
        return json.loads(result[0])

    def iter_records(self) -> Iterator[dict]:
        for row in self.db.execute("SELECT resolved FROM records ORDER BY article_id"):
            yield json.loads(row[0])

    def iter_representatives(self) -> Iterator[dict]:
        for row in self.db.execute("SELECT r.resolved FROM groups g JOIN records r ON r.idx=g.representative_idx WHERE g.document_id IS NOT NULL ORDER BY g.document_id"):
            yield json.loads(row[0])

    def iter_conflicts(self) -> Iterator[dict]:
        for row in self.db.execute("SELECT payload FROM conflicts ORDER BY component"):
            yield json.loads(row[0])

    def export_id_map(self, path: Path | str) -> dict:
        """Export the complete map; existing aliases/metadata remain unchanged."""
        digest = hashlib.sha256()
        count = 0
        with Path(path).open("xb") as stream:
            query = """SELECT identity_key,payload FROM prior_map
                UNION ALL SELECT k.identity_key,g.map_metadata FROM current_keys k
                JOIN records r ON r.idx=k.owner JOIN groups g ON g.component=r.component
                LEFT JOIN prior_map p ON p.identity_key=k.identity_key
                WHERE p.identity_key IS NULL AND g.document_id IS NOT NULL
                ORDER BY identity_key"""
            for key, payload in self.db.execute(query):
                value = json.loads(payload)
                value["identity_key"] = key
                line = (_json(value) + "\n").encode("utf-8")
                stream.write(line)
                digest.update(line)
                count += 1
        return {"path": str(path), "rows": count, "sha256": digest.hexdigest()}


def build_identity_index(
    rows: Iterable[dict], prior_map_path: Path | str, db_path: Path | str,
    *, source_dataset: str = "grounded-news-v1", conflict_policy: str = "error",
    expected_prior_sha256: str | None = None,
) -> GroundedIdentityIndex:
    """Stream records into a new SQLite index, resolving duplicates transitively.

    If ``expected_prior_sha256`` is supplied, source bytes must match exactly.
    Every existing map entry is retained, including entries absent in this run.
    This method reads rows once; callers can independently verify JSONL hashes
    while yielding rows and stream them again when writing lake datasets.
    """
    if conflict_policy not in ("error", "quarantine"):
        raise ValueError("conflict_policy must be error or quarantine")
    _identity_keys({"source_dataset": source_dataset, "record_id": "validation", "body": ""})
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation avoids accidentally mutating an old or partial index.
    with db_path.open("xb"):
        pass
    db = sqlite3.connect(db_path)
    parent: list[int] = []
    rank: list[int] = []

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(a, b):
        a, b = find(a), find(b)
        if a != b:
            if rank[a] < rank[b]:
                a, b = b, a
            parent[b] = a
            if rank[a] == rank[b]:
                rank[a] += 1

    try:
        db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=NORMAL;
            CREATE TABLE prior_map(identity_key TEXT PRIMARY KEY,document_id TEXT NOT NULL,payload TEXT NOT NULL);
            CREATE INDEX prior_document_id ON prior_map(document_id);
            CREATE TABLE current_keys(identity_key TEXT PRIMARY KEY,owner INTEGER NOT NULL);
            CREATE TABLE prior_owners(document_id TEXT PRIMARY KEY,owner INTEGER NOT NULL);
            CREATE TABLE record_old_ids(idx INTEGER,document_id TEXT,PRIMARY KEY(idx,document_id));
            CREATE TABLE records(idx INTEGER PRIMARY KEY,article_id TEXT UNIQUE NOT NULL,signature TEXT NOT NULL,
                metadata TEXT NOT NULL,component INTEGER,resolved TEXT);
            CREATE TABLE groups(component INTEGER PRIMARY KEY,document_id TEXT,representative_idx INTEGER,
                size INTEGER NOT NULL,map_metadata TEXT);
            CREATE TABLE conflicts(component INTEGER PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE info(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)
        prior_digest, prior_rows = hashlib.sha256(), 0
        with Path(prior_map_path).open("rb") as stream:
            for raw in stream:
                prior_digest.update(raw)
                if not raw.strip():
                    continue
                entry = json.loads(raw)
                key = entry.get("identity_key")
                _validate_identity(key)
                document_id = _uuid(entry.get("document_id"))
                if document_id != entry["document_id"]:
                    raise ValueError("Prior document_id must use canonical UUID spelling")
                old = db.execute("SELECT document_id,payload FROM prior_map WHERE identity_key=?", (key,)).fetchone()
                if old is not None:
                    if old[0] != document_id or old[1] != _json(entry):
                        raise IdentityConflict(f"Conflicting prior map entries for {key}")
                else:
                    db.execute("INSERT INTO prior_map VALUES (?,?,?)", (key, document_id, _json(entry)))
                prior_rows += 1
        prior_sha = prior_digest.hexdigest()
        if expected_prior_sha256 is not None and expected_prior_sha256 != prior_sha:
            raise ValueError("Prior identity map SHA256 differs from expected source receipt")
        db.commit()
        input_rows = repeated = 0
        for row in rows:
            input_rows += 1
            metadata, keys, signature = _metadata(row, source_dataset)
            old = db.execute("SELECT idx,signature,metadata FROM records WHERE article_id=?", (metadata["article_id"],)).fetchone()
            if old is not None:
                if old[1] != signature:
                    raise ValueError(f"Conflicting source fields for article_id {metadata['article_id']}")
                old_metadata = json.loads(old[2])
                if old_metadata["scope"] != "base" and metadata["scope"] == "base":
                    db.execute("UPDATE records SET metadata=? WHERE idx=?", (_json(metadata), old[0]))
                repeated += 1
                continue
            idx = len(parent)
            parent.append(idx)
            rank.append(0)
            db.execute("INSERT INTO records(idx,article_id,signature,metadata) VALUES (?,?,?,?)",
                       (idx, metadata["article_id"], signature, _json(metadata)))
            for key in sorted(keys):
                owner = db.execute("SELECT owner FROM current_keys WHERE identity_key=?", (key,)).fetchone()
                if owner is None:
                    db.execute("INSERT INTO current_keys VALUES (?,?)", (key, idx))
                else:
                    union(idx, owner[0])
                established = db.execute("SELECT document_id FROM prior_map WHERE identity_key=?", (key,)).fetchone()
                if established is not None:
                    document_id = established[0]
                    db.execute("INSERT OR IGNORE INTO record_old_ids VALUES (?,?)", (idx, document_id))
                    owner = db.execute("SELECT owner FROM prior_owners WHERE document_id=?", (document_id,)).fetchone()
                    if owner is None:
                        db.execute("INSERT INTO prior_owners VALUES (?,?)", (document_id, idx))
                    else:
                        union(idx, owner[0])
            if input_rows % 10000 == 0:
                db.commit()
        db.executemany("UPDATE records SET component=? WHERE idx=?", ((find(idx), idx) for idx in range(len(parent))))
        db.execute("CREATE INDEX records_component ON records(component)")
        db.execute("CREATE INDEX current_keys_owner ON current_keys(owner)")
        db.commit()
        reused = new = conflicts = quarantined = 0
        components = db.execute("SELECT component,COUNT(*) FROM records GROUP BY component").fetchall()
        for component, size in components:
            existing = [r[0] for r in db.execute("SELECT DISTINCT o.document_id FROM record_old_ids o JOIN records r ON r.idx=o.idx WHERE r.component=? ORDER BY o.document_id", (component,))]
            chosen_idx, chosen_raw = db.execute("""SELECT idx,metadata FROM records WHERE component=?
                ORDER BY CASE json_extract(metadata,'$.scope') WHEN 'base' THEN 0 ELSE 1 END,article_id LIMIT 1""", (component,)).fetchone()
            chosen = json.loads(chosen_raw)
            if len(existing) > 1:
                conflicts += 1
                quarantined += size
                conflict = {"component": component, "existing_document_ids": existing,
                            "article_ids": [r[0] for r in db.execute("SELECT article_id FROM records WHERE component=? ORDER BY article_id", (component,))],
                            "reason": "duplicate_component_joins_distinct_established_document_ids"}
                db.execute("INSERT INTO conflicts VALUES (?,?)", (component, _json(conflict)))
                document_id = None
            elif existing:
                document_id = existing[0]
                reused += 1
            else:
                first_key = db.execute("SELECT MIN(k.identity_key) FROM current_keys k JOIN records r ON r.idx=k.owner WHERE r.component=?", (component,)).fetchone()[0]
                document_id = str(uuid.uuid5(DOCUMENT_NAMESPACE, first_key))
                if db.execute("SELECT 1 FROM prior_map WHERE document_id=? LIMIT 1", (document_id,)).fetchone():
                    raise IdentityConflict("New UUID would collide with an unrelated prior document")
                new += 1
            map_metadata = {"document_id": document_id, "representative_record_id": chosen["article_id"],
                            "representative_source_dataset": source_dataset, "available_at": chosen["available_at"]}
            db.execute("INSERT INTO groups VALUES (?,?,?,?,?)", (component, document_id, chosen_idx, size, _json(map_metadata)))
            update = []
            for idx, raw in db.execute("SELECT idx,metadata FROM records WHERE component=?", (component,)):
                resolved = {**json.loads(raw), "document_id": document_id,
                            "representative_article_id": chosen["article_id"] if document_id else None,
                            "duplicate_count": size, "identity_status": "mapped" if document_id else "quarantined_conflict"}
                update.append((_json(resolved), idx))
            db.executemany("UPDATE records SET resolved=? WHERE idx=?", update)
        db.commit()
        if conflicts and conflict_policy == "error":
            raise IdentityConflict(f"{conflicts} duplicate components join conflicting existing document_ids; {quarantined} articles blocked. Inspect SQLite conflicts table; no completed index or identity map published.")
        prior_count = db.execute("SELECT COUNT(*) FROM prior_map").fetchone()[0]
        new_key_count = db.execute("""SELECT COUNT(*) FROM current_keys k LEFT JOIN prior_map p USING(identity_key)
            JOIN records r ON r.idx=k.owner JOIN groups g USING(component)
            WHERE p.identity_key IS NULL AND g.document_id IS NOT NULL""").fetchone()[0]
        summary = {
            "schema": SCHEMA, "input_rows": input_rows, "unique_source_records": len(parent),
            "repeated_identical_rows": repeated, "representative_count": new + reused,
            "new_document_ids": new, "reused_document_ids": reused,
            "duplicate_source_records": len(parent) - quarantined - new - reused,
            "conflict_components": conflicts, "quarantined_articles": quarantined,
            "prior_map_sha256": prior_sha, "prior_input_rows": prior_rows,
            "prior_key_count": prior_count, "identity_key_count": prior_count + new_key_count,
            "source_revision_count": len(parent), "source_dataset": source_dataset,
            "prior_metadata_policy": "preserve_all_existing_entries_unchanged",
            "representative_policy": "base_scope_then_lexicographic_article_id_actual_revision_time",
            "dedup_scope": "supplied_rows_and_prior_identities", "global_dedup_certified": False,
            "conflict_policy": conflict_policy, "citation_offset_basis": "original_title_newline_body",
        }
        db.execute("INSERT INTO info VALUES ('summary',?)", (_json(summary),))
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        db.close()
    return GroundedIdentityIndex(db_path)
