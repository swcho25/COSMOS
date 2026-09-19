"""Lexical target-name hints from the existing, version-bound NER dictionary.

Only unambiguous company-name spellings actually occurring in the article are
returned. Ticker-only, product, industry and group aliases are excluded, as are
spellings assigned to more than one company. These hints identify possible name
equivalents, not economic relevance or proof that a homonym is the company.
The dictionary is not dated: no historical ownership, business or subsidiary
fact is supplied or inferred. The teacher must still resolve identity in context.
"""
from __future__ import annotations

from collections import defaultdict
import csv
from functools import lru_cache
import hashlib
from pathlib import Path
import re

from kg_semantic_teacher import literal_canonical_surface

VERSION = "kg_target_lexical_aliases_v1"
HERE = Path(__file__).resolve().parent
ALIAS_FILE = HERE.parent / "ner/data/aliases.csv"
ALLOWED_TYPES = frozenset(("official", "official_en", "en_rule", "ko_manual", "en_manual"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def spelling_key(value):
    return re.sub(r"\s+", "", value).casefold()


@lru_cache(maxsize=4)
def _read(path, expected_sha):
    if sha(path) != expected_sha:
        raise ValueError("Alias dictionary changed while loading")
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    owners = defaultdict(set)
    for row in rows:
        if row.get("ticker") and row.get("alias_type") in ALLOWED_TYPES:
            owners[spelling_key(row["alias"])].add(row["ticker"])
    selected = defaultdict(set)
    for row in rows:
        alias = row.get("alias", "").strip()
        if (row.get("ambiguity") == "safe" and row.get("alias_type") in ALLOWED_TYPES
                and row.get("ticker") and len(alias) >= 2
                and owners[spelling_key(alias)] == {row["ticker"]}):
            selected[row["ticker"]].add(alias)
    if sha(path) != expected_sha:
        raise ValueError("Alias dictionary changed while loading")
    return {ticker: tuple(sorted(values, key=lambda x: (-len(x), x))) for ticker, values in selected.items()}


def matched_aliases(row):
    """A matched alias is an identity hint, never a positive relevance label."""
    registry = _read(str(ALIAS_FILE), sha(ALIAS_FILE))
    text = row["title"] + "\n" + row["body"]
    return [alias for alias in registry.get(row["ticker"], ())
            if literal_canonical_surface(alias, text)]


def provenance():
    return {"version": VERSION, "dictionary_path": str(ALIAS_FILE), "dictionary_sha256": sha(ALIAS_FILE),
            "code_sha256": sha(Path(__file__)),
            "matcher_code_sha256": sha(HERE / "kg_semantic_teacher.py"),
            "allowed_types": sorted(ALLOWED_TYPES), "ambiguity": "safe",
            "requires_article_surface_match": True, "historical_identity_verified": False,
            "scope": "lexical_name_hints_only_not_business_or_relationship_context"}
