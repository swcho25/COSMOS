"""Typed company relations, read off the sentences sentiment has to throw away.

A sentence naming two companies cannot say how either one is doing - that is why
the attribution guard in evidence.py drops it. It is exactly what a relation
needs: the predicate between the two names is what makes a pair SUPPLY rather
than COMPETE. So the two readers split the same article between them and neither
needs the other's sentences.

The classifiers are not written here. ``extract_relations`` (Korean) and
``extract_relations_en`` (English) already encode rules that were arrived at by
measurement - conjunction handling, keyword distance, refusing to guess a
direction - and their headers document the traps behind each one. This module
finds the candidate sentences, calls them, and records what they return.

Direction comes back in the classifier's answer, not from input order: "B is
supplied by A" yields src=A even though B is read first. The returned aliases
are mapped back to tickers rather than assuming the pair order held.

Relation extraction does not need a model and does not load one. It is cheap
enough to run over a day of articles in one pass.
"""
from __future__ import annotations

import collections

from evidence import _name_pattern, sentence_view, usable_sentences

RELATION_TYPES = ("SUPPLY", "PARTNER", "COMPETE")
VERSION = "news-relations-1.0"

# The input contract spark/extract_relation_candidates.py states: only dictionary
# hits the matcher was certain about. A group-name guess (0.5) or a sports-club
# demotion (0.4) is a weak identity claim, and a relation asserts something about
# both parties - a wrong identity on either side invents an edge between real
# companies. Measured on live batches: 19 of 530 mentions are below 1.0.
MIN_MATCH_CONFIDENCE = 1.0

# The matcher expands "신한, KB국민, 하나, 우리은행" by carrying the suffix across
# the list. Those sentences are lists, not statements about a pair, so the flag
# travels with the row for the caller to weigh.
ELLIPSIS_METHOD = "ellipsis"

# Patterns the classifiers hand back with a hit. An enumerating conjunction is
# kept but flagged: "신한은행·카카오·배달의민족과 협약" names three companies that
# each contracted with a fourth party, not with each other. The Korean module
# already rejects enumerations for SUPPLY; for PARTNER it lets them through, so
# the caller decides. Measured on 30 live batches, enumeration was the source of
# every PARTNER false positive we found by hand.
ENUM_MARKER = "enum"


class RelationError(ValueError):
    pass


def _positions(text: str, names: dict) -> list[tuple]:
    """(key, alias, start, end) for every company actually named, longest wins.

    Korean has no word boundary, so a short name matches inside a longer one:
    "카카오페이와 카카오뱅크는 …" contains neither the word 카카오 nor a mention of
    that company, yet a plain search finds it three times. Left alone this
    invents pairs - 카카오→카카오뱅크 from a sentence that never names 카카오 -
    which is the substring trap the sentiment guard is also built around.

    A hit whose span sits inside another company's hit is that other company's
    name, not this one's, so it is dropped.
    """
    hits = []
    for key, aliases in names.items():
        best = None
        for alias in aliases:
            match = _name_pattern(alias).search(text)
            if match and (best is None or match.start() < best[1]
                          or (match.start() == best[1] and match.end() > best[2])):
                best = (alias, match.start(), match.end())
        if best:
            hits.append((key, *best))

    kept = []
    for hit in hits:
        inside = any(other is not hit
                     and other[2] <= hit[2] and hit[3] <= other[3]
                     and (other[3] - other[2]) > (hit[3] - hit[2])
                     for other in hits)
        if not inside:
            kept.append(hit)
    return sorted(kept, key=lambda hit: hit[2])


def article_relations(title: str, content: str, companies, language: str) -> list[dict]:
    """Typed relations found in one article, each with the sentence behind it.

    One row per (relation type, source, target, sentence). The same pair stated
    twice in one article is two rows, because the loader scores by evidence
    count and each sentence is a separate mention.
    """
    certain = [company for company in (companies or [])
               if float(company.get("confidence") or 0) >= MIN_MATCH_CONFIDENCE]
    ellipsis = {(company.get("market"), company.get("stock_code"))
                for company in certain
                if ELLIPSIS_METHOD in (company.get("method") or "")}
    companies, catalog, names, field, scannable = sentence_view(title, content, certain)
    if len(companies) < 2:
        return []

    korean = language != "en"
    found = []
    for entry, _end, stripped, text in usable_sentences(catalog, field, scannable):
        hits = _positions(stripped, names)
        if len(hits) < 2:
            continue
        alias_to_key = {hit[1]: hit[0] for hit in hits}
        for rel, src, dst, pattern in _classify(stripped, hits, korean):
            source_key = alias_to_key.get(src)
            target_key = alias_to_key.get(dst)
            if source_key is None or target_key is None or source_key == target_key:
                continue
            found.append({
                "relation_type": rel,
                "source_market": source_key[0], "source_stock_code": source_key[1],
                "target_market": target_key[0], "target_stock_code": target_key[1],
                "pattern": pattern,
                "enumeration": (ENUM_MARKER in pattern
                                or source_key in ellipsis or target_key in ellipsis),
                "sentence_order": entry["id"],
                "source": entry["source"],
                "text": text,
                "language": "ko" if korean else "en",
            })
    return _dedupe(found)


def _classify(text: str, hits: list[tuple], korean: bool):
    """Yield (relation, src_alias, dst_alias, pattern) from the owning module."""
    if korean:
        import extract_relations as ko
        for i, first in enumerate(hits):
            for j, second in enumerate(hits):
                if i == j:
                    continue
                answer = ko.classify(text, first[1], second[1])
                if answer:
                    src, dst, rel, _confidence, pattern = answer
                    yield rel, src, dst, pattern
    else:
        import extract_relations_en as en
        drops: collections.Counter = collections.Counter()
        # The English module keys on ticker rather than alias and wants
        # (ticker, start, end); it finds the keyword first and reads the
        # companies on either side of it.
        by_ticker = {hit[0][1]: hit[1] for hit in hits}
        positions = [(hit[0][1], hit[2], hit[3]) for hit in hits]
        for rel, src, dst, keyword in en.classify_sentence(text, positions, drops):
            yield rel, by_ticker.get(src, src), by_ticker.get(dst, dst), "en:" + keyword


def _dedupe(found: list[dict]) -> list[dict]:
    """One row per (relation, pair, sentence).

    The Korean classifier is asked about both orders of every pair, so a
    symmetric statement comes back twice for the same sentence.
    """
    seen, unique = set(), []
    for row in found:
        key = (row["relation_type"], row["source_stock_code"], row["target_stock_code"],
               row["sentence_order"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)
    return unique
