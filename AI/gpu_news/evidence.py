"""Split one matched article into the sentences that belong to each company.

This is the input shared by the two stages after it: the sentences are what the
classifier reads, and how many survive is what the relevance formula counts.
Running it once for both keeps a company's sentiment and its ``n_sentences``
describing the same set of sentences.

Nothing here is new work.  Both hard parts are imported from the modules that
already own them, because both were arrived at by measurement:

``build_sentence_catalog``
    Lossless punctuation/newline segmentation.  Offsets are character indices
    into the *original* ``title`` and ``body`` separately, and each entry keeps
    its ``source``, so there is no concatenated coordinate space to get wrong.
    A ``title + "\\n" + body`` offset applied to the body alone is shifted by the
    title's length and cites the wrong sentence; carrying ``source`` per segment
    removes that failure mode instead of documenting it.

``_surface``
    The attribution guard.  It does not strip punctuation and checks English
    word boundaries only.  A hand-written guard that also stripped whitespace
    dropped the accepted share to 2.5%, because ``삼성`` then matched inside
    ``삼성전자``; this one keeps it at 92.6%.

Attribution rules, unchanged from kg_target_sentiment:

- a sentence without the target's own name is not evidence about the target
- a sentence naming another company *from this article* is ambiguous and is
  dropped.  The comparison set is this article's other matched companies, not
  the whole 202-name universe: an unrelated name that never appears in the text
  cannot make a sentence ambiguous, and testing against all of them would throw
  away sentences for no reason.

Conflicting labels are resolved in the classifier, not here, because this module
never sees a label.  Only stdlib plus those two imports is used, so importing it
loads no model and reads no file.
"""
from __future__ import annotations

import re

from kg_target_sentiment import _name_pattern, _surface
from matcher import mask_inserted_headlines, trim_boilerplate
from relevance_sentence_evidence import build_sentence_catalog

# Per-sentence, not per-article: a Korean article quoting an English sentence
# should send that sentence to the English model.  The article's own language
# field describes the publication, which is a different question.
HANGUL = re.compile(r"[가-힣]")

STATUS_PENDING = "pending"
STATUS_NO_EVIDENCE = "no_attributed_sentence"


class EvidenceError(ValueError):
    pass


def sentence_language(text: str) -> str:
    """Korean when the sentence contains Hangul, English otherwise."""
    return "ko" if HANGUL.search(text) else "en"


def _company_names(company) -> list[str]:
    """The target's own surface forms: its name plus the aliases that matched."""
    names = [company.get("name")]
    names.extend(company.get("aliases") or [])
    ordered = []
    for name in names:
        if isinstance(name, str) and name.strip() and name not in ordered:
            ordered.append(name)
    if not ordered:
        raise EvidenceError("company has no usable name or alias")
    return ordered


class _Scannable:
    """The region of the article the matcher was willing to match in.

    CompanyMatcher does not scan the raw article. It builds ``title + "\\n" +
    body``, cuts the boilerplate tail with ``trim_boilerplate``, and blanks
    inserted headlines with ``mask_inserted_headlines`` before looking for any
    company. Evidence has to respect the same two limits, or a company gets a
    sentence out of a region it was never matched in - a "주요 뉴스" sidebar
    naming 삼성전자 becomes its evidence and then its sentiment. One earlier
    pass removed 18,097 mentions this way, 97.8% of them a single recommended
    -articles widget.

    ``mask_inserted_headlines`` substitutes spaces, so offsets are unchanged
    and the masked copy can be indexed with the catalog's own coordinates.
    The combined string is used only to locate the limits; stored evidence
    keeps its per-field offsets.
    """

    def __init__(self, title: str, body: str):
        combined = title + "\n" + body
        self.cut = trim_boilerplate(combined)
        self.masked = mask_inserted_headlines(combined[:self.cut])
        self.offset = {"title": 0, "body": len(title) + 1}

    def usable(self, source: str, start: int, end: int) -> tuple[int, str] | None:
        """Clip a segment to the scannable region; None when it is all outside."""
        base = self.offset[source] + start
        limit = min(self.offset[source] + end, self.cut)
        if limit <= base:
            return None
        return limit - self.offset[source], self.masked[base:limit]


def _rejoin_split_names(entries: list[dict], text: str, names) -> list[dict]:
    """Undo segment boundaries that land inside a company name.

    The segmenter breaks on a period, and some company names contain one:
    ``Warner Bros. Discovery`` becomes ``Warner Bros.`` + ``Discovery ...``.
    Neither piece contains the whole name, so the attribution guard finds the
    company nowhere in its own article and it is silently dropped - which under
    the visibility gate means it is never published at all.

    Five of the 1,649 aliases are affected today (Alphabet, Warner Bros.
    Discovery, Booking.com). Extending the segmenter's abbreviation list would
    fix those five and not the next one, so the cut is repaired where it is
    detectable instead: a boundary strictly inside an occurrence of a name is
    not a sentence boundary, whatever produced it.

    Only such boundaries are joined. Everything else keeps the audited
    segmentation, and the result still reconstructs the field exactly.
    """
    if len(entries) < 2:
        return entries
    boundaries = {entry["end"] for entry in entries[:-1]}
    inside = set()
    for name in names:
        for match in _name_pattern(name).finditer(text):
            inside.update(b for b in boundaries if match.start() < b < match.end())
    if not inside:
        return entries

    joined: list[dict] = []
    for entry in entries:
        if joined and joined[-1]["end"] in inside:
            previous = joined[-1]
            previous["end"] = entry["end"]
            previous["text"] = text[previous["start"]:previous["end"]]
        else:
            joined.append(dict(entry))
    return joined


def _company_key(company) -> tuple:
    market, stock_code = company.get("market"), company.get("stock_code")
    if not isinstance(market, str) or not isinstance(stock_code, str):
        raise EvidenceError("company is missing market/stock_code")
    return market, stock_code


def article_evidence(title: str, content: str, companies) -> list[dict]:
    """Return one entry per company with the sentences attributed to it.

    Companies keep their entry even when no sentence survives attribution, so
    the caller can tell "analysed and found nothing" apart from "not analysed"
    and leave those rows NULL rather than guessing a neutral label.
    """
    if not isinstance(title, str) or not isinstance(content, str):
        raise EvidenceError("title and content must be strings")
    companies = list(companies or [])
    catalog = build_sentence_catalog({"title": title, "body": content})

    names = {}
    for company in companies:
        names[_company_key(company)] = _company_names(company)

    every_name = [name for values in names.values() for name in values]
    field = {"title": title, "body": content}
    scannable = _Scannable(title, content)
    catalog = [entry for source in ("title", "body")
               for entry in _rejoin_split_names(
                   [item for item in catalog if item["source"] == source],
                   field[source], every_name)]
    for position, entry in enumerate(catalog, 1):
        entry["id"] = position

    results = []
    for company in companies:
        key = _company_key(company)
        target_names = names[key]
        other_names = [name for other, values in names.items() if other != key
                       for name in values]
        # A name this company also goes by cannot make a sentence ambiguous.
        other_names = [name for name in other_names if name not in target_names]

        evidence = []
        for entry in catalog:
            clipped = scannable.usable(entry["source"], entry["start"], entry["end"])
            if clipped is None:
                continue
            end, scan_text = clipped
            if not scan_text.strip():
                continue
            # Attribution is decided on the masked, trimmed copy; the sentence
            # stored and shown is the original text of the same range.
            if not any(_surface(name, scan_text) for name in target_names):
                continue
            if any(_surface(name, scan_text) for name in other_names):
                continue
            text = field[entry["source"]][entry["start"]:end]
            evidence.append({
                # The catalog id is the article's own reading order and is stable
                # across re-runs, so a stored sentence can always be traced back
                # to the exact source segment it was cut from.
                "sentence_order": entry["id"],
                "source": entry["source"],
                "start": entry["start"],
                "end": end,
                "text": text,
                "language": sentence_language(text),
            })

        results.append({
            "market": key[0],
            "stock_code": key[1],
            "ticker": company.get("ticker"),
            "name": company.get("name"),
            "match_confidence": company.get("confidence"),
            "evidence": evidence,
            "n_sentences": len(evidence),
            "status": STATUS_PENDING if evidence else STATUS_NO_EVIDENCE,
        })
    return results
