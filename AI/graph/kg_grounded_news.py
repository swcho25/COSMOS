"""CPU-only, provisional materialization of already selected news/company pairs.

The B classifier's decision is not read, relabeled, or overwritten. This separate
stage removes known crawl UI, retains exact input provenance, and looks for
target-specific source passages for subsequent sentiment/relationship processing.
``candidate_ready`` means a grounded *candidate*, not validated economic relevance,
a price-impact prediction, a human label, or permission to publish to a service.

Reuse the existing sanitizer, lossless sentence catalog, financial-event patterns,
target-name dictionary, homonym checks and complete tape-template recognizer.
Same-sentence lexical attribution remains a heuristic; ambiguous roles, group
ownership and unsupported historical identity are deferred rather than invented.
Literal evidence attests only that text occurs in the supplied source. That source
may itself already be a cleaned lake snapshot; raw publisher text is not recovered.

Offsets are Python character indices into the UNCHANGED input title/body, with an
exclusive end. ``quote`` is always the exact original slice. ``cleaned_quote`` and
target_context.text omit known widgets/UI and must not be presented as raw quotes.
Generic cleaned_title/body are independent of the target company. Importing or
calling this module does not load a model, download data, or write output files.
"""
from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from datetime import date, datetime, timezone
from functools import lru_cache
import hashlib
from pathlib import Path
import re

import historical_company_context as historical
import kg_target_aliases as aliases
from kg_semantic_teacher import literal_canonical_surface
from kg_tape_only_guard import detect_tape_only
from knowledge_graph_news_preview import canonical, canonical_input_sha256, validate_input
from news_content import GROUP, remove_live_quotes, sanitize_article
from news_quality import BAD_METHODS, EVENT_RE, financial_evidence, parse_explicit_time
from relevance_sentence_evidence import build_sentence_catalog
from target_news_features import valid_target_hit
from matcher import (CompanyMatcher, NEGATIVE_CONTEXT, PARTICLES, SPORTS_TEAM_RE,
                     mask_inserted_headlines, trim_boilerplate)

VERSION = "kg_grounded_news_candidate_v2_1"
EXTRA_EVENTS = {
    "product_service": re.compile(r"(?<![가-힣])출시|신제품|신약|허가|승인|\blaunch(?:ed|es|ing)?\b|\bintroduced\b|\bapproval\b", re.I),
    "demand_operations": re.compile(r"판매량|판매대수|점유율|수요|공급난|공급망|구독자|가입자|고객|생산량|출하량|가동|공장|\bsales\b|\bdemand\b|\bcustomers?\b|\bsubscribers?\b|\bproduction\b|\bshipments?\b|\bfactor(?:y|ies)\b|\bmarket share\b", re.I),
    "financing_investment": re.compile(r"자금\s*조달|신주|회사채|설비\s*투자|투자\s*유치|지분|\bfinancing\b|\bfunding\b|\bcapital expenditure\b|\bstake\b|\bissued bonds\b|\binvest(?:s|ed|ing|ment|ments)?\b|\bspending\b", re.I),
    "labor_cost_restructuring": re.compile(r"감원|해고|구조조정|인력\s*(?:감축|채용|확보|유출)|(?:직원|엔지니어).{0,15}(?:이직|감축|해고|채용)|비용\s*(?:절감|증가|감축)|\blayoffs?\b|\blaid off\b|\bworkforce\b|\bhiring\b|\bheadcount\b|\brestructur\w*\b|\bcosts?\b", re.I),
    "business_agreement": re.compile(r"협약|공동\s*개발|업무\s*제휴|\bMOU\b|\bpartnership\b|\bmemorandum of understanding\b|\b(?:supply|commercial|licensing) agreement\b", re.I),
    "operational_incident": re.compile(r"회항|기체\s*(?:이상|결함)|운항\s*(?:중단|차질)|조업\s*중단|공장\s*화재|정보\s*유출|\bdata breach\b|\bfactory fire\b|\bemergency landing\b|\bflight diversion\b|\boperational disruption\b", re.I),
    "customer_order_volume": re.compile(r"주문량|주문\s*건수|\border volume\b|\borders?\s+(?:more than\s+|nearly\s+|almost\s+)?(?:increased|decreased|rose|fell|doubled|tripled|grew|growth)\b", re.I),
    "regulation": re.compile(r"규제|제재|과징금|관세|반독점|\bregulat\w*\b|\bsanctions?\b|\btariffs?\b|\bantitrust\b", re.I),
    "management_change": re.compile(r"(?:대표|사장|임원|회장|CFO|CEO).{0,45}(?:사임|사퇴|이직|퇴임|선임|임명)|(?:사임|사퇴|선임|임명).{0,35}(?:대표|사장|임원|CFO|CEO)|\b(?:CEO|CFO|chief\s+(?:executive|financial)\s+officer|executive)\b.{0,65}\b(?:resign\w*|leav\w*|left|join\w*|appoint\w*)\b|\bappoint\w*\b.{0,65}\b(?:CEO|CFO|executive)\b", re.I),
}
PRICE = re.compile(r"주가|등락률|거래량|체결강도|목표\s*주가|매수\s*의견|\bshare price\b|\bstock price\b|\bshares?\s+(?:rose|fell|gained|lost|traded|jumped|slid)\b|\bprice target\b|\btrading volume\b|\btechnical analysis\b", re.I)
BIO = re.compile(r"과거\s*(?:직장|경력)|약력|프로필|(?:전직|이전|과거에|과거|예전에).{0,60}(?:근무|직원|부장|사원|재직)|(?:근무|재직)(?:했던|하던|했다)|\b(?:previously|formerly|used to)\b.{0,70}\b(?:work\w*|employ\w*|at|with)\b|\bformer\b.{0,50}\b(?:employee|manager|executive|director|engineer)\b|\bbackground includes\b|\b(?:his|her|their) time (?:as|at)\b|\bbefore that\b", re.I)
ROLE = re.compile(r"연구원|애널리스트|\b(?:analyst|strategist|researcher)\b", re.I)
BROKER_REPORT = re.compile(r"목표\s*주가|투자\s*의견|매수\s*의견|\bprice target\b|\b(?:buy|sell|hold) rating\b", re.I)
ENTITY_QUALIFIER = re.compile(r"\s*(?:코리아|재팬|차이나|유럽법인|한국법인|일본법인|중국법인|그룹|그룹사|계열사|자회사|현지\s*법인|Korea\b|Japan\b|China\b|Group\b|subsidiary\b|affiliate\b|local operator\b)", re.I)
HISTORICAL_NAME = re.compile(r"옛\s*사명|구\s*사명|사명\s*변경|상호\s*변경|\bformerly known as\b|\brenamed\b", re.I)
UNCERTAIN = re.compile(r"확인되지|확인할\s*수\s*없|계약설|인수설|소문|\brumou?rs?\b|\bunconfirmed\b|\bnot confirmed\b", re.I)
RELATED_TAIL = re.compile(r"(?m)^\s*관련\s*뉴스\s*$")
EXECUTIVE_ROLE = re.compile(r"\s*(?:의\s*)?(?:최고\s*경영자|최고\s*재무\s*책임자|대표이사|회장|CEO\b|CFO\b|chief\s+(?:executive|financial)\s+officer\b)", re.I)
PLACE_SUFFIX = re.compile(r"\s*(?:타운십|타운쉽|카운티|township\b|county\b|river\b|valley\b)", re.I)
FACILITY_SUFFIX = re.compile(r"\s*(?:조립\s*공장|assembly\s+plant\b)", re.I)
JOINED_ENTITY_SUFFIX = re.compile(r"[-–—][A-Za-z][A-Za-z0-9]{1,}")


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf8")).hexdigest()


def date_audit(row):
    """Preserve source dates; never infer a timezone from a company's market."""
    raw = row.get("published_at")
    result = {"raw": raw, "published_date": None, "published_at_utc": None,
              "status": "invalid_or_missing", "usable_day": False,
              "event_time_is_publication_time": False}
    if row.get("date_normalization_method") in BAD_METHODS:
        result["status"] = "known_invalid_source_time"
        return result
    if not isinstance(raw, str) or not raw.strip():
        return result
    text = raw.strip()
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            day = date.fromisoformat(text)
            status = "date_only"
        else:
            if not re.match(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text):
                return result
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            day = parsed.date()
            verified = parse_explicit_time(text, row.get("published_timezone"))
            status = "explicit_source_time" if verified else "day_known_timezone_unverified"
            if verified:
                result["published_at_utc"] = verified.astimezone(timezone.utc).isoformat()
        result["published_date"] = day.isoformat()
        if day.year <= 1970:
            result.update(status="epoch_or_pre_epoch_requires_review", published_at_utc=None)
        else:
            result.update(status=status, usable_day=True)
    except (ValueError, TypeError, OverflowError):
        pass
    return result


def _without_widgets(text, origin):
    keep, cursor = [], 0
    for match in GROUP.finditer(text):
        if remove_live_quotes(match.group()) == "":
            keep.extend(range(cursor, match.start()))
            cursor = match.end()
    keep.extend(range(cursor, len(text)))
    return "".join(text[i] for i in keep), [origin[i] for i in keep]


def clean_article(row):
    """Existing sanitizer output plus a deterministic, linear source offset map."""
    title, body = row["title"], row["body"]
    joined = title + "\n" + body
    cut = trim_boilerplate(joined)
    masked = mask_inserted_headlines(joined[:cut])
    raw_body_prefix = masked[len(title) + 1:]
    cleaned_title, title_map = _without_widgets(title, list(range(len(title))))
    cleaned_body, body_map = _without_widgets(raw_body_prefix, list(range(len(raw_body_prefix))))
    expected = sanitize_article(row)
    if (cleaned_title, cleaned_body) != (expected["title"], expected["body"]):
        raise RuntimeError("Sanitizer changed: source-offset mapping must be revalidated")
    flags = []
    tail = RELATED_TAIL.search(cleaned_body)
    if tail:
        cleaned_body, body_map = cleaned_body[:tail.start()], body_map[:tail.start()]
        flags.append("related_news_tail_removed")
    quarantine = len(re.findall(r"read\s+more", cleaned_body, re.I)) >= 2
    if quarantine:
        flags.append("read_more_feed_quarantine")
    cleaned = {**row, "title": cleaned_title, "body": cleaned_body}
    audit = {"sanitizer": "news_content.sanitize_article", "flags": flags,
             "original_text_sha256": digest({"title": title, "body": body}),
             "changed": (title, body) != (cleaned_title, cleaned_body),
             "original_lengths": {"title": len(title), "body": len(body)},
             "cleaned_lengths": {"title": len(cleaned_title), "body": len(cleaned_body)},
             "source_scope": "supplied_input_fields_not_recovered_publisher_raw",
             "quarantine": quarantine}
    return cleaned, {"title": title_map, "body": body_map}, audit


def _base_name(value):
    value = re.sub(r"\s*\([^)]*\)\s*", " ", value).strip()
    return re.sub(r"(?:[\s,]+(?:incorporated|inc\.?|corporation|corp\.?|co\.?|ltd\.?|plc|limited))+$", "", value, flags=re.I).strip()


@lru_cache(maxsize=4096)
def _name_pattern(name):
    value = _base_name(name)
    if not value:
        return re.compile(r"(?!x)x")
    surface = r"\s*".join(re.escape(c) for c in value if not c.isspace())
    particles = "|".join(re.escape(p) for p in sorted(PARTICLES, key=len, reverse=True))
    return re.compile(r"(?<!\w)" + surface + r"(?=(?:" + particles + r")?(?!\w))", re.I)


def _is_affiliation(segment, start, end):
    before, after = segment[max(0, start - 65):start], segment[end:end + 55]
    if re.search(r"\b(?:analyst|strategist|researcher)\b.{0,40}\b(?:at|from|with|of)\s*$", before, re.I):
        return True
    if re.match(r"\s*(?:의\s*)?(?:(?:[가-힣]{2,4}|[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)\s+)?(?:연구원|애널리스트|analyst|strategist|researcher)\b", after, re.I):
        return True
    return False


def _event_matches(text):
    return [(kind, match.start(), match.end()) for kind, pattern in {**EVENT_RE, **EXTRA_EVENTS}.items()
            for match in pattern.finditer(text)]


class GroundedNewsProcessor:
    """Bind dictionaries once, then process rows without filesystem/model I/O.

    ``historical_registry`` uses historical_company_context's exact dated schema.
    Missing registry coverage is recorded, not invented; known conflicts/boundary
    failures are held. Optional alias_registry is for bounded synthetic tests and
    must contain lexical name hints, never business facts.
    """
    def __init__(self, historical_registry=None, *, alias_registry=None, max_evidence=4):
        if type(max_evidence) is not int or not 1 <= max_evidence <= 20:
            raise ValueError("max_evidence must be an integer from 1 to 20")
        self.max_evidence = max_evidence
        self.registry = deepcopy(historical_registry) if historical_registry is not None else None
        if self.registry is not None:
            historical.validate_registry(self.registry)
        self.records = defaultdict(list)
        for record in (self.registry or {}).get("records", []):
            self.records[record["ticker"]].append(record)
        if alias_registry is None:
            self.alias_provenance = aliases.provenance()
            self.alias_registry = aliases._read(str(aliases.ALIAS_FILE), self.alias_provenance["dictionary_sha256"])
            self.matcher = CompanyMatcher.from_csv(aliases.ALIAS_FILE)
        else:
            self.alias_registry = deepcopy(alias_registry)
            self.alias_provenance = {"scope": "caller_supplied_lexical_hints", "sha256": digest(alias_registry)}
            self.matcher = None
        self.group_names = {name.casefold() for name, spec in self.matcher.aliases.items()
                            if spec.alias_type == "group" or spec.ambiguity == "needs_context"} if self.matcher else set()

    def provenance(self):
        files = [Path(__file__), Path(__file__).with_name("news_content.py"),
                 Path(__file__).with_name("news_quality.py"), Path(__file__).with_name("kg_tape_only_guard.py"),
                 Path(__file__).with_name("target_news_features.py"), Path(__file__).with_name("news_company_context.py"),
                 Path(__file__).with_name("knowledge_graph_news_preview.py"),
                 Path(__file__).with_name("relevance_sentence_evidence.py"), Path(historical.__file__),
                 Path(__file__).resolve().parent.parent / "ner/matcher.py"]
        return {"version": VERSION, "code_hashes": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files},
                "aliases": deepcopy(self.alias_provenance),
                "historical_registry_sha256": digest(self.registry) if self.registry is not None else None,
                "max_evidence": self.max_evidence,
                "matcher_backend": self.matcher.backend if self.matcher else "synthetic_name_patterns",
                "scope": "cpu_rule_grounding_of_provisional_pairs_not_validated_economic_relevance"}

    def _historical(self, row, dates):
        audit = {"reason": "registry_not_provided" if self.registry is None else "ticker_not_in_registry",
                 "context_added": False, "continuous_ownership_assumed": False}
        if row["ticker"] not in self.records:
            return row, audit, []
        if not dates["usable_day"] or dates["status"] == "day_known_timezone_unverified":
            return row, {**audit, "reason": "historical_asof_time_unverified"}, []
        # Registry validation occurs once above; use the existing exact resolver.
        # A space-separated ISO timestamp may be valid but the dated resolver's
        # schema requires T. Normalize only an explicitly verified instant (or
        # the unchanged date-only day) in a separate resolver input.
        resolver_row = {**row, "published_at": dates["published_at_utc"] or dates["published_date"]}
        article_at = historical.validate_row(resolver_row)
        enriched, audit = historical._resolve(resolver_row, self.records[row["ticker"]], article_at)
        audit["source_publication_text"] = row["published_at"]
        audit["resolver_publication_text"] = resolver_row["published_at"]
        ids = set(audit.get("record_ids", [])) if audit["context_added"] else set()
        names = [name for record in self.records[row["ticker"]] if record["record_id"] in ids
                 for name in [record["display_name"], *record["aliases"]]]
        return enriched, audit, names

    def process(self, row):
        original = validate_input(row)
        dates = date_audit(row)
        cleaned, maps, cleaning = clean_article(original)
        historical_row, historical_audit, dated_names = self._historical(original, dates)
        names = list(dict.fromkeys([original["company_name"], *dated_names,
                                   *self.alias_registry.get(original["ticker"], ())]))
        full = cleaned["title"] + "\n" + cleaned["body"]
        lexical = [n for n in names if literal_canonical_surface(n, full)]
        all_mentions = self.matcher.match(cleaned["title"], cleaned["body"]).mentions if self.matcher else []
        evidence, rejected, unresolved = [], [], []
        matched_names = set()
        for entry in build_sentence_catalog(cleaned):
            text = entry["text"]
            if not text.strip():
                continue
            hits = []
            global_base = entry["start"] + (len(cleaned["title"]) + 1 if entry["source"] == "body" else 0)
            for name in lexical:
                for match in _name_pattern(name).finditer(text):
                    key = (match.start(), match.end())
                    if any((h["start"], h["end"]) == key for h in hits):
                        continue
                    matched_names.add(name)
                    lo, hi = global_base + match.start(), global_base + match.end()
                    collision = (not valid_target_hit(full, original["ticker"], lo, hi)
                                 or any(check(full, lo, hi) for alias, check in NEGATIVE_CONTEXT.items()
                                        if alias.casefold() == match.group().casefold())
                                 or any(m.start() < match.end() and match.start() < m.end()
                                        for m in SPORTS_TEAM_RE.finditer(text))
                                 or bool(PLACE_SUFFIX.match(text, match.end())))
                    joined = JOINED_ENTITY_SUFFIX.match(text, match.end())
                    # A hyphen between two explicitly recognized companies is
                    # different from a single unmatched local-entity compound.
                    known_peer = joined and any(m.ticker != original["ticker"] and m.start == global_base + match.end() + 1 for m in all_mentions)
                    explicit_issuer_code = bool(re.match(r"\s*\(\s*" + re.escape(original["ticker"]) + r"\s*\)", text[match.end():]))
                    hits.append({"start": match.start(), "end": match.end(), "name": name,
                                 "collision": collision, "affiliation": _is_affiliation(text, *key),
                                 "qualified": bool(ENTITY_QUALIFIER.match(text, match.end())),
                                 "facility": bool(FACILITY_SUFFIX.match(text, match.end())),
                                 "joined_entity": bool(joined and not known_peer),
                                 "group_name": match.group().casefold() in self.group_names and not explicit_issuer_code,
                                 "executive_affiliation": bool(EXECUTIVE_ROLE.match(text, match.end()))})
            if not hits:
                continue
            # source ranges enclose the cleaned passage; removed widgets can occur
            # between endpoints, so cleaned_quote is explicitly a separate field.
            start, end = entry["start"], entry["end"]
            left = len(text) - len(text.lstrip())
            right = len(text.rstrip())
            start, end = start + left, start + right
            if start >= end:
                continue
            source_map = maps[entry["source"]]
            original_start, original_end = source_map[start], source_map[end - 1] + 1
            item = {"source": entry["source"], "start": original_start, "end": original_end,
                    "quote": original[entry["source"]][original_start:original_end],
                    "cleaned_quote": cleaned[entry["source"]][start:end],
                    "evidence_type": "target_name_same_source_segment", "event_types": [],
                    "cleaned_start": start, "cleaned_end": end}
            valid = [h for h in hits if not h["collision"]]
            if not valid:
                rejected.append({**item, "reason": "explicit_name_collision_or_sports_team"})
                continue
            if all(h["affiliation"] for h in valid):
                rejected.append({**item, "reason": "analyst_affiliation_only"})
                continue
            valid = [h for h in valid if not h["affiliation"]]
            events = _event_matches(text)
            management = any(kind == "management_change" for kind, _, _ in events)
            if all(h["executive_affiliation"] for h in valid) and not management:
                unresolved.append({**item, "reason": "executive_affiliation_without_target_management_event"})
                continue
            if BIO.search(text) and not management:
                unresolved.append({**item, "reason": "past_employment_or_biography_requires_review"})
                continue
            if any(h["qualified"] or h["facility"] or h["joined_entity"] or h["group_name"] for h in valid):
                unresolved.append({**item, "reason": "group_or_local_issuer_attribution_unverified"})
                continue
            if HISTORICAL_NAME.search(text) and not historical_audit["context_added"]:
                unresolved.append({**item, "reason": "historical_name_requires_dated_identity"})
                continue
            if UNCERTAIN.search(text):
                unresolved.append({**item, "reason": "unconfirmed_event_or_attribution"})
                continue
            # Reuse the original financial-event witness extraction; it is a
            # weak sentence-level witness, not a positive economic classifier.
            weak = financial_evidence(text, "", [{"ticker": original["ticker"], "start": h["start"], "end": h["end"]} for h in valid])
            kinds = set(w["event_type"] for w in weak)
            conflicts = False
            for kind, a, b in events:
                target_distance = min(min(abs(a - h["start"]), abs(a - h["end"])) for h in valid)
                if target_distance > 180:
                    continue
                if any(m.ticker != original["ticker"]
                       and global_base <= m.start < global_base + len(text)
                       and min(abs(global_base + a - m.start), abs(global_base + a - m.end)) < target_distance
                       for m in all_mentions):
                    conflicts = True
                for hit in valid:
                    # Another identified company between target and event is a
                    # role ambiguity, never evidence for inheriting its earnings.
                    lo, hi = sorted((global_base + hit["end"], global_base + a))
                    if any(m.ticker != original["ticker"] and lo <= m.start < hi for m in all_mentions):
                        conflicts = True
                kinds.add(kind)
            if conflicts:
                unresolved.append({**item, "reason": "other_company_between_target_and_event"})
            elif kinds:
                evidence.append({**item, "evidence_type": "target_linked_economic_candidate", "event_types": sorted(kinds)})
            else:
                reason = "price_or_analyst_opinion_without_target_business_evidence" if PRICE.search(text) or BROKER_REPORT.search(text) else "target_mention_without_grounded_business_event"
                unresolved.append({**item, "reason": reason})
        tape = detect_tape_only(full)
        reasons = []
        decision = "candidate_ready" if evidence else "review"
        if tape["matched"]:
            decision = "exclude"
            reasons.append("complete_known_price_volume_template")
            evidence = []
        elif not evidence and rejected and not unresolved:
            decision = "exclude"
            reasons.append("all_retained_target_mentions_rejected")
        elif not evidence:
            reasons.append("no_grounded_target_economic_candidate")
        if not dates["usable_day"]:
            decision = "review"
            reasons.append("invalid_or_epoch_publication_date")
        if cleaning["quarantine"]:
            decision = "review"
            reasons.append("article_boundary_ambiguous_read_more_feed")
        if original["ticker"] in self.records and not historical_audit["context_added"]:
            decision = "review"
            reasons.append("historical_identity_" + historical_audit["reason"])
        if decision == "candidate_ready":
            reasons.append("literal_target_and_economic_event_candidate")
        evidence_count = len(evidence)
        evidence = evidence[:self.max_evidence]
        selected = evidence if decision == "candidate_ready" else []
        target_context = {
            "text": "\n".join(e["cleaned_quote"] for e in selected),
            "source_spans": [{k: e[k] for k in ("source", "start", "end")} for e in selected],
            "matched_names": sorted(matched_names),
            "historical_resolution": historical_audit,
            "historical_context": historical_row.get("company_context"),
            "identity_scope": "dated_registry_context" if historical_audit["context_added"] else "article_lexical_candidate_not_historical_identity_proof",
        }
        return {"version": VERSION, "article_id": original["article_id"], "ticker": original["ticker"],
                "source_input_sha256": canonical_input_sha256(original),
                "cleaned_title": cleaned["title"], "cleaned_body": cleaned["body"],
                "cleaned_text_sha256": digest({"title": cleaned["title"], "body": cleaned["body"]}),
                "date_audit": dates, "target_context": target_context,
                "materialization_decision": decision, "reasons": reasons,
                "evidence": evidence, "rejected_mentions": rejected, "unresolved_mentions": unresolved,
                "economic_evidence_candidate_count": evidence_count,
                "economic_evidence_omitted_count": max(0, evidence_count - len(evidence)),
                "cleaning_audit": cleaning, "provisional": True,
                "economic_relevance_validated": False, "price_impact_guaranteed": False,
                "b_classifier_decision_modified": False}
