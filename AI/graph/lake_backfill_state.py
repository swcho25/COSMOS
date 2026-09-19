"""Disk-backed global news identity and representative state for a full backfill.

Bodies never enter SQLite. The caller owns immutable compressed payload shards;
only their relative shard/byte offset/length and compact identity metadata are
stored here. All iterators stream SQLite cursors and use bounded caches.

Transactions are explicit: ingest/seed/set_checkpoint share the current SQLite
transaction; commit at a source checkpoint. Successful context exit commits.
An identical source semantic version is idempotent even if supplied again with
a different payload_ref. Revisions are preserved as separate versions and
audited. Distinct preexisting UUIDs are never silently merged.
"""
from __future__ import annotations

import datetime as dt
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from lake_document_ids import (DOCUMENT_NAMESPACE, _identity_keys, _record_key,
                               _stamp, _uuid, _validate_identity)

SCHEMA_VERSION = "1"
SEMANTIC_FIELDS = ("title", "body", "published_at", "url", "canonical_url", "region", "language")
ZERO_HASH = "0" * 64
EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def semantic_hash(row: dict) -> str:
    """Hash the explicit source-version contract without persisting article text."""
    values = {key: row.get(key) for key in SEMANTIC_FIELDS}
    values["published_at"] = _stamp(row.get("published_at")).isoformat().replace("+00:00", "Z")
    digest = hashlib.sha256()
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    for chunk in encoder.iterencode(values):
        digest.update(chunk.encode("utf-8"))
    return digest.hexdigest()


class PriorIdentityConflict(ValueError):
    def __init__(self, details, *, audit=None):
        self.details = details
        self.audit = audit
        super().__init__("Conflicting preexisting document UUIDs: " + _json(details))


class BackfillState:
    """Single-writer SQLite state. Do not share one instance between processes."""

    def __init__(self, db_path, *, prior_conflict_policy="raise", read_only=False, cache_mib=64):
        if prior_conflict_policy not in ("raise", "quarantine"):
            raise ValueError("prior_conflict_policy must be raise or quarantine")
        if not isinstance(cache_mib, int) or not 1 <= cache_mib <= 512:
            raise ValueError("cache_mib must be between 1 and 512")
        self.read_only = read_only
        self.prior_conflict_policy = prior_conflict_policy
        self.closed = False
        if read_only:
            uri = Path(db_path).resolve().as_uri() + "?mode=ro"
            self.db = sqlite3.connect(uri, uri=True, isolation_level=None)
        else:
            self.db = sqlite3.connect(str(db_path), isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA busy_timeout=30000")
        self.db.execute(f"PRAGMA cache_size={-cache_mib * 1024}")
        self.db.execute("PRAGMA temp_store=FILE")
        if not read_only:
            existing = {r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if existing and "backfill_meta" not in existing:
                self.db.close()
                raise ValueError("Database is not a backfill-state database")
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            self._create_schema()
        try:
            version = self._meta("schema_version")
        except sqlite3.DatabaseError:
            self.db.close()
            raise ValueError("Database is not a backfill-state database") from None
        if version != SCHEMA_VERSION:
            self.db.close()
            raise ValueError("Unsupported backfill-state schema")

    def _create_schema(self):
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS backfill_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS components(
                id INTEGER PRIMARY KEY,parent INTEGER,size INTEGER NOT NULL DEFAULT 1,
                min_identity TEXT NOT NULL,fixed_uuid TEXT,representative_id INTEGER,
                source_count INTEGER NOT NULL DEFAULT 0,version_count INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS prior_documents(document_id TEXT PRIMARY KEY,node INTEGER NOT NULL) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS identities(
                identity_key TEXT PRIMARY KEY,node INTEGER NOT NULL,prior_metadata TEXT) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS source_records(
                id INTEGER PRIMARY KEY,record_key TEXT NOT NULL UNIQUE,node INTEGER NOT NULL,
                version_count INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS versions(
                id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL,semantic_hash BLOB NOT NULL,
                published_at TEXT NOT NULL,published_us INTEGER NOT NULL,body_length INTEGER NOT NULL,
                payload_ref TEXT NOT NULL,UNIQUE(source_id,semantic_hash));
            CREATE TABLE IF NOT EXISTS conflicts(
                id INTEGER PRIMARY KEY,kind TEXT NOT NULL,record_key TEXT NOT NULL,
                semantic_hash TEXT NOT NULL,payload_ref TEXT,details TEXT NOT NULL,
                UNIQUE(kind,record_key,semantic_hash,details));
            CREATE TABLE IF NOT EXISTS checkpoints(
                source_key TEXT PRIMARY KEY,source_hash TEXT NOT NULL,row_count INTEGER NOT NULL) WITHOUT ROWID;
            CREATE TABLE IF NOT EXISTS counters(name TEXT PRIMARY KEY,value INTEGER NOT NULL) WITHOUT ROWID;
        """)
        for key, value in (("schema_version", SCHEMA_VERSION), ("fingerprint", ZERO_HASH), ("finalized", "0")):
            self.db.execute("INSERT OR IGNORE INTO backfill_meta VALUES(?,?)", (key, value))
        for name in ("identity_count", "component_count", "prior_document_count", "source_record_count",
                     "version_count", "source_revision_count", "prior_conflict_count", "quarantined_count"):
            self.db.execute("INSERT OR IGNORE INTO counters VALUES(?,0)", (name,))

    def _meta(self, key):
        row = self.db.execute("SELECT value FROM backfill_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _begin(self):
        if self.read_only:
            raise ValueError("State is read-only")
        if self._meta("finalized") == "1":
            raise ValueError("State is finalized")
        if not self.db.in_transaction:
            self.db.execute("BEGIN IMMEDIATE")

    def _increment(self, name, amount=1):
        self.db.execute("UPDATE counters SET value=value+? WHERE name=?", (amount, name))

    def _advance(self, event):
        digest = hashlib.sha256(bytes.fromhex(self._meta("fingerprint")) + _json(event).encode("utf-8")).hexdigest()
        self.db.execute("UPDATE backfill_meta SET value=? WHERE key='fingerprint'", (digest,))

    def _find(self, node, *, compress=True):
        path = []
        while True:
            parent = self.db.execute("SELECT parent FROM components WHERE id=?", (node,)).fetchone()
            if parent is None:
                raise ValueError(f"Missing component: {node}")
            if parent[0] is None:
                break
            path.append(node)
            node = parent[0]
        if compress and len(path) > 1:
            self.db.executemany("UPDATE components SET parent=? WHERE id=?", ((node, child) for child in path))
        return node

    def _new_component(self, key, fixed_uuid=None):
        node = self.db.execute("INSERT INTO components(min_identity,fixed_uuid) VALUES(?,?)", (key, fixed_uuid)).lastrowid
        self._increment("component_count")
        if fixed_uuid:
            self.db.execute("INSERT INTO prior_documents VALUES(?,?)", (fixed_uuid, node))
            self._increment("prior_document_count")
        return node

    def _fixed_component(self, document_id, key):
        row = self.db.execute("SELECT node FROM prior_documents WHERE document_id=?", (document_id,)).fetchone()
        return self._find(row[0]) if row else self._new_component(key, document_id)

    def _version(self, version_id):
        return self.db.execute("""SELECT v.*,s.record_key FROM versions v
                                  JOIN source_records s ON s.id=v.source_id WHERE v.id=?""", (version_id,)).fetchone()

    def _better(self, first, second):
        if first is None:
            return second
        if second is None or first == second:
            return first
        def key(version_id):
            row = self._version(version_id)
            _, dataset, record = row["record_key"].split(":", 2)
            # A semantic revision can tie all legacy criteria; its hash is the
            # deterministic final tiebreaker, while every revision stays audited.
            return row["published_us"], -row["body_length"], record, dataset, row["semantic_hash"]
        return first if key(first) <= key(second) else second

    def _union(self, first, second):
        first, second = self._find(first), self._find(second)
        if first == second:
            return first
        a = self.db.execute("SELECT * FROM components WHERE id=?", (first,)).fetchone()
        b = self.db.execute("SELECT * FROM components WHERE id=?", (second,)).fetchone()
        if a["fixed_uuid"] and b["fixed_uuid"] and a["fixed_uuid"] != b["fixed_uuid"]:
            raise PriorIdentityConflict({"document_ids": sorted((a["fixed_uuid"], b["fixed_uuid"]))})
        if (a["size"], -first) < (b["size"], -second):
            a, b, first, second = b, a, second, first
        representative = self._better(a["representative_id"], b["representative_id"])
        self.db.execute("UPDATE components SET parent=? WHERE id=?", (first, second))
        self.db.execute("""UPDATE components SET size=?,min_identity=?,fixed_uuid=?,representative_id=?,
                           source_count=?,version_count=? WHERE id=?""",
                        (a["size"] + b["size"], min(a["min_identity"], b["min_identity"]),
                         a["fixed_uuid"] or b["fixed_uuid"], representative,
                         a["source_count"] + b["source_count"], a["version_count"] + b["version_count"], first))
        self._increment("component_count", -1)
        return first

    def _record_conflict(self, kind, record_key, fingerprint, payload_ref, details):
        written = self.db.execute("""INSERT OR IGNORE INTO conflicts
            (kind,record_key,semantic_hash,payload_ref,details) VALUES(?,?,?,?,?)""",
            (kind, record_key, fingerprint, payload_ref, _json(details))).rowcount
        if written:
            self._increment("source_revision_count" if kind == "source_revision" else "prior_conflict_count")
        return bool(written)

    def seed_prior(self, entries):
        """Stream a complete existing identity map; prior-only documents have no payload."""
        self._begin()
        inserted = 0
        for entry in entries:
            key = entry.get("identity_key")
            _validate_identity(key)
            document_id = _uuid(entry.get("document_id"))
            metadata = {name: entry.get(name) for name in
                        ("representative_record_id", "representative_source_dataset", "available_at")}
            if metadata["available_at"] is not None:
                metadata["available_at"] = _stamp(metadata["available_at"]).isoformat().replace("+00:00", "Z")
            existing = self.db.execute("SELECT node,prior_metadata FROM identities WHERE identity_key=?", (key,)).fetchone()
            if existing:
                node = self._find(existing["node"])
                current_uuid = self.db.execute("SELECT fixed_uuid FROM components WHERE id=?", (node,)).fetchone()[0]
                if current_uuid and current_uuid != document_id:
                    details = {"identity_key": key, "document_ids": sorted((current_uuid, document_id))}
                    if self._record_conflict("prior_seed_conflict", key, "", None, details):
                        self._advance({"prior_seed_conflict": details})
                    raise PriorIdentityConflict(details, audit={"kind": "prior_seed_conflict", "record_key": key,
                                                                "semantic_hash": "", "payload_ref": None})
                if existing["prior_metadata"] is not None:
                    if _json(metadata) != existing["prior_metadata"]:
                        raise ValueError("Conflicting prior metadata for " + key)
                    continue
                fixed = self._fixed_component(document_id, key)
                self._union(node, fixed)
                self.db.execute("UPDATE identities SET prior_metadata=? WHERE identity_key=?", (_json(metadata), key))
            else:
                node = self._fixed_component(document_id, key)
                self.db.execute("INSERT INTO identities VALUES(?,?,?)", (key, node, _json(metadata)))
                self.db.execute("UPDATE components SET min_identity=min(min_identity,?) WHERE id=?", (key, node))
                self._increment("identity_count")
                inserted += 1
            self._advance({"prior": key, "document_id": document_id, "metadata": metadata})
        return inserted

    def ingest(self, row: dict, payload_ref: dict):
        """Index one normalized article. No title/body/source payload is retained."""
        self._begin()
        if (not isinstance(payload_ref, dict) or not isinstance(payload_ref.get("shard"), str)
                or not payload_ref["shard"] or type(payload_ref.get("offset")) is not int
                or type(payload_ref.get("length")) is not int
                or payload_ref["offset"] < 0 or payload_ref["length"] <= 0):
            raise ValueError("payload_ref requires shard:string, offset:nonnegative int, length:positive int")
        ref = _json(payload_ref)
        record_key = _record_key(row)
        keys, body_length = _identity_keys(row)
        stamp = _stamp(row.get("published_at"))
        delta = stamp - EPOCH
        micros = delta.days * 86400000000 + delta.seconds * 1000000 + delta.microseconds
        fingerprint = semantic_hash(row)
        source = self.db.execute("SELECT * FROM source_records WHERE record_key=?", (record_key,)).fetchone()
        if source:
            version = self.db.execute("SELECT id FROM versions WHERE source_id=? AND semantic_hash=?",
                                      (source["id"], bytes.fromhex(fingerprint))).fetchone()
            if version:
                return {"status": "duplicate", "component_id": self._find(source["node"]), "version_id": version[0]}
        self.db.execute("SAVEPOINT ingest_row")
        try:
            nodes = {self._find(r[0]) for key in sorted(keys)
                     if (r := self.db.execute("SELECT node FROM identities WHERE identity_key=?", (key,)).fetchone())}
            if row.get("document_id") is not None:
                nodes.add(self._fixed_component(_uuid(row["document_id"]), min(keys)))
            fixed_ids = {r[0] for node in nodes
                         if (r := self.db.execute("SELECT fixed_uuid FROM components WHERE id=?", (node,)).fetchone())[0]}
            if len(fixed_ids) > 1:
                raise PriorIdentityConflict({"document_ids": sorted(fixed_ids), "identity_keys": sorted(keys)})
            if nodes:
                ordered = sorted(nodes)
                node = ordered[0]
                for other in ordered[1:]:
                    node = self._union(node, other)
            else:
                node = self._new_component(min(keys))
            for key in sorted(keys):
                inserted = self.db.execute("INSERT OR IGNORE INTO identities VALUES(?,?,NULL)", (key, node)).rowcount
                if inserted:
                    self._increment("identity_count")
            self.db.execute("UPDATE components SET min_identity=min(min_identity,?) WHERE id=?", (min(keys), node))
            is_revision = source is not None
            if source is None:
                source_id = self.db.execute("INSERT INTO source_records(record_key,node) VALUES(?,?)", (record_key, node)).lastrowid
                self._increment("source_record_count")
            else:
                source_id = source["id"]
            version_id = self.db.execute("""INSERT INTO versions
                (source_id,semantic_hash,published_at,published_us,body_length,payload_ref) VALUES(?,?,?,?,?,?)""",
                (source_id, bytes.fromhex(fingerprint), stamp.isoformat().replace("+00:00", "Z"), micros, body_length, ref)).lastrowid
            previous = self.db.execute("SELECT representative_id FROM components WHERE id=?", (node,)).fetchone()[0]
            representative = self._better(previous, version_id)
            self.db.execute("UPDATE components SET representative_id=?,source_count=source_count+?,version_count=version_count+1 WHERE id=?",
                            (representative, 0 if is_revision else 1, node))
            self.db.execute("UPDATE source_records SET version_count=version_count+1 WHERE id=?", (source_id,))
            self._increment("version_count")
            if is_revision:
                self._record_conflict("source_revision", record_key, fingerprint, ref,
                                      {"version_id": version_id, "previous_version_count": source["version_count"]})
            self._advance({"record_key": record_key, "semantic_hash": fingerprint, "payload_ref": payload_ref,
                           "identity_keys": sorted(keys), "declared_document_id": row.get("document_id")})
            self.db.execute("RELEASE SAVEPOINT ingest_row")
            return {"status": "version_added" if is_revision else "inserted", "component_id": node, "version_id": version_id}
        except PriorIdentityConflict as error:
            self.db.execute("ROLLBACK TO SAVEPOINT ingest_row")
            self.db.execute("RELEASE SAVEPOINT ingest_row")
            details = {**error.details, "record_key": record_key}
            if self._record_conflict("prior_identity_conflict", record_key, fingerprint, ref, details):
                if self.prior_conflict_policy == "quarantine":
                    self._increment("quarantined_count")
                self._advance({"prior_identity_conflict": details, "semantic_hash": fingerprint})
            if self.prior_conflict_policy == "raise":
                raise PriorIdentityConflict(details, audit={"kind": "prior_identity_conflict", "record_key": record_key,
                                                            "semantic_hash": fingerprint, "payload_ref": ref}) from error
            return {"status": "quarantined", "component_id": None, "version_id": None, "conflict": details}
        except BaseException:
            self.db.execute("ROLLBACK TO SAVEPOINT ingest_row")
            self.db.execute("RELEASE SAVEPOINT ingest_row")
            raise

    def set_checkpoint(self, sourcekey, source_hash, rowcount):
        """Checkpoint and ingests commit atomically; changed input hashes fail."""
        self._begin()
        if not isinstance(sourcekey, str) or not sourcekey or not isinstance(source_hash, str) or not source_hash:
            raise ValueError("sourcekey and source_hash must be nonempty strings")
        if type(rowcount) is not int or rowcount < 0:
            raise ValueError("rowcount must be a nonnegative integer")
        existing = self.get_checkpoint(sourcekey)
        if existing:
            if existing["sha256"] != source_hash:
                raise ValueError("Checkpoint source hash changed: " + sourcekey)
            if existing["row_count"] > rowcount:
                raise ValueError("Checkpoint cannot move backwards: " + sourcekey)
            if existing["row_count"] == rowcount:
                return
        self.db.execute("INSERT INTO checkpoints VALUES(?,?,?) ON CONFLICT(source_key) DO UPDATE SET row_count=excluded.row_count",
                        (sourcekey, source_hash, rowcount))
        self._advance({"checkpoint": sourcekey, "hash": source_hash, "rowcount": rowcount})

    def get_checkpoint(self, sourcekey):
        row = self.db.execute("SELECT * FROM checkpoints WHERE source_key=?", (sourcekey,)).fetchone()
        return {"sourcekey": row["source_key"], "sha256": row["source_hash"], "row_count": row["row_count"]} if row else None

    def _representative(self, component):
        row = self._version(component["representative_id"])
        _, dataset, record = row["record_key"].split(":", 2)
        document_id = component["fixed_uuid"] or str(uuid.uuid5(DOCUMENT_NAMESPACE, component["min_identity"]))
        return {"document_id": document_id, "record_id": record, "source_dataset": dataset,
                "published_at": row["published_at"], "body_length": row["body_length"],
                "payload_ref": json.loads(row["payload_ref"]), "semantic_hash": row["semantic_hash"].hex(),
                "version_id": row["id"], "duplicate_count": component["source_count"],
                "version_count": component["version_count"]}

    def iter_representatives(self):
        for component in self.db.execute("SELECT * FROM components WHERE parent IS NULL AND representative_id IS NOT NULL ORDER BY id"):
            yield self._representative(component)

    def iter_identity_map(self):
        @lru_cache(maxsize=4096)
        def metadata(node):
            root = self._find(node, compress=False)
            component = self.db.execute("SELECT * FROM components WHERE id=?", (root,)).fetchone()
            document_id = component["fixed_uuid"] or str(uuid.uuid5(DOCUMENT_NAMESPACE, component["min_identity"]))
            if component["representative_id"] is None:
                return document_id, None
            representative = self._representative(component)
            return document_id, {"representative_record_id": representative["record_id"],
                                 "representative_source_dataset": representative["source_dataset"],
                                 "available_at": representative["published_at"]}
        for row in self.db.execute("SELECT * FROM identities ORDER BY identity_key"):
            document_id, current = metadata(row["node"])
            preserved = json.loads(row["prior_metadata"]) if row["prior_metadata"] else {
                "representative_record_id": None, "representative_source_dataset": None, "available_at": None}
            yield {"identity_key": row["identity_key"], "document_id": document_id, **(current or preserved)}

    iter_id_map = iter_identity_map

    def iter_conflicts(self):
        for row in self.db.execute("SELECT * FROM conflicts ORDER BY id"):
            value = dict(row)
            value["details"] = json.loads(value["details"])
            value["payload_ref"] = json.loads(value["payload_ref"]) if value["payload_ref"] else None
            yield value

    def summary(self):
        return {row["name"]: row["value"] for row in self.db.execute("SELECT * FROM counters")} | {
            "schema_version": SCHEMA_VERSION, "finalized": self._meta("finalized") == "1",
            "fingerprint": self._meta("fingerprint"), "document_namespace": str(DOCUMENT_NAMESPACE),
            "semantic_fields": list(SEMANTIC_FIELDS), "body_storage": "external_payload_ref_only"}

    def finalize(self):
        if self._meta("finalized") == "1":
            return self.summary()
        self._begin()
        self._advance({"finalized": True})
        self.db.execute("UPDATE backfill_meta SET value='1' WHERE key='finalized'")
        return self.summary()

    def commit(self):
        if self.db.in_transaction:
            self.db.commit()

    def rollback(self):
        if self.db.in_transaction:
            self.db.rollback()

    def close(self):
        if not self.closed:
            self.commit()
            self.db.close()
            self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, error_type, error, traceback):
        if error is not None:
            self.rollback()
        # Do not commit an uncheckpointed source file merely to preserve a
        # conflict. Roll back that entire transaction, then persist audit alone.
        if isinstance(error, PriorIdentityConflict) and error.audit:
            self._begin()
            audit = error.audit
            if self._record_conflict(audit["kind"], audit["record_key"], audit["semantic_hash"],
                                     audit["payload_ref"], error.details):
                self._advance({"unhandled_prior_conflict": error.details, "semantic_hash": audit["semantic_hash"]})
        self.close()
