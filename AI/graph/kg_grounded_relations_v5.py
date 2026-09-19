"""Additive, bounded explicit-role relation candidates with original evidence.

V4 is immutable. This revision adds independent local clauses, explicit
counterparty lists, and adjacent-sentence pronouns whose grammatical antecedent
is unique. It is a grammar, not a learned semantic classifier. Shared industry,
customers, price comparison and mere co-mentions never create relationships.

Lists are expanded only across explicit roles (supplier -> listed customers,
listed investors -> investment target, or one party -> named counterparties).
Co-investors and consortium members are not connected to one another. Adjacent
coreference requires a recognized company business-subject anchor, or an exact
joint two-company subject, in the immediately preceding body sentence of the
same paragraph. Arbitrary pronouns, third parties, plans, negation, biographies
and unresolved subsidiary/group identities remain review candidates.

All new candidates require contextual review. Offsets refer to untouched
title + '\\n' + body. For coreference, ``sentence`` spans both original sentences;
``anchor_spans`` identifies the literal company mentions and
``relation_clause_span`` identifies the pronoun-bearing predicate sentence.
No synthetic matcher text is returned as an original quote. No price effects,
current relation validity, or legal/historical issuer identity are established.
"""
from __future__ import annotations

import re

import kg_grounded_relations_v3 as grammar
import kg_grounded_relations_v4 as previous

SCHEMA = "kg-grounded-relations-5-additive"
POLICY_VERSION = "bounded-explicit-roles-lists-adjacent-coreference-5"
RELATION_TYPES = previous.RELATION_TYPES
OFFSET_BASIS = previous.OFFSET_BASIS
A, B = grammar.MARK_A, grammar.MARK_B
N = r"⟦[0-9]+⟧"
LIST = rf"{N}(?:(?:\s*(?:,|·|및|와|과|and|&)\s*){N}){{0,5}}"
TOKEN = re.compile(N)
END = r"(?:했다|하였다|한다|해왔다|하고\s+있다|한\s+바\s+있다)"
COMPLETED = r"(?:했다|하였다|해왔다|한\s+바\s+있다)"
SUBJECT = grammar.KO_SUBJECT
OBJECT = r"[가-힣A-Za-z0-9%$€₩ +/·()_–—:-]{1,120}?"
EN_OBJECT = r"[A-Za-z0-9%$€£ +/()_-]{1,100}?"
AGREEMENT = r"(?:업무협약|협력\s*(?:협약|계약)|제휴\s*계약|양해각서|MOU|공동개발\s*계약)"
FUTURE_END = re.compile(r"(?:할|될|맺을|체결할|공급할|투자할)\s*(?:것|계획|예정)|(?:[?？])|\b(?:seeks?\s+to|aims?\s+to|expected\s+to)\b", re.I)
CONTESTED = re.compile(r"거짓|허위|부인|반박|사실무근|\b(?:false|disputed|unfounded|fabricated|denied|denial|hoax)\b", re.I)
UNNAMED_ACTOR = re.compile(r"자회사|계열사|(?:두|다른|해당|미공개)\s*(?:기업|회사)|\b(?:subsidiar(?:y|ies)|affiliates?|unnamed|manufacturer|another\s+(?:firm|company)|a\s+company|two\s+companies)\b", re.I)
UNKNOWN_ACTOR = re.compile(r"(?:[가-힣A-Za-z0-9]+(?:와|과|은|는|이|가))\s|\b(?:and|but|while|which|who|that|said|says)\b", re.I)
PRONOUN = re.compile(r"^(?P<p>이\s*회사|해당\s*회사|동사|양사|두\s*회사|두\s*기업|It|This\s+company|The\s+company|They|The\s+two\s+companies)(?P<rest>(?:는|은|가|이)?\s+.*)$", re.I)
# Recognized finite subject anchors intentionally omit people, subsidiaries,
# quotes, pronouns, and another grammatical subject.
KO_BUSINESS = rf"(?:신제품|제품|서비스|실적|매출|영업이익|분기\s*실적)(?:을|를)?\s+(?:발표|공개|출시|보고){COMPLETED}"
EN_BUSINESS = r"(?:reported|announced|released|launched)\s+(?:its\s+)?(?:earnings|results|revenue|a\s+new\s+product|a\s+new\s+service)"
KO_ANCHOR = re.compile(rf"^{A}(?:는|은|가|이)\s+{KO_BUSINESS}$")
EN_ANCHOR = re.compile(rf"^{A}\s+{EN_BUSINESS}$", re.I)
JOINT_ANCHOR = re.compile(rf"^(?:{A}(?:와|과|·)\s*{B}(?:는|은|가|이)\s+{KO_BUSINESS}|{A}\s+and\s+{B}\s+{EN_BUSINESS})$", re.I)


def _rule(name, kind, direction, subtype, expression):
    return name, kind, direction, subtype, re.compile(expression, re.I)


PAIR_RULES = (
    _rule("v5_ko_postposed_component_supplier", "SUPPLY", "ba", "component_supply_for_named_product",
          rf"{A}\s+[가-힣A-Za-z0-9 -]{{1,40}}의\s+경우\s+[가-힣A-Za-z0-9() /·-]{{1,160}}(?:을|를)\s+{B}(?:가|이)\s+공급하고\s+있다"),
    _rule("v5_ko_completed_dated_supply_contract", "SUPPLY", "ab", "supply_contract",
          rf"{A}{SUBJECT}(?:(?:올해|[0-9]{{4}}년)부터\s+[0-9]{{4}}년까지\s+[0-9]+년간\s+)?{B}에\s+(?:약\s+)?[0-9.,조억만천원달러$ ]+\s+규모의\s+{OBJECT}(?:을|를)\s+공급하는\s+계약(?:을|를)\s+(?:[0-9]{{1,2}}일\s+)?체결{COMPLETED}"),
    _rule("v5_ko_signed_purpose_agreement", "PARTNER", "symmetric", "business_cooperation",
          rf"{A}{SUBJECT}{B}(?:와|과)\s+(?:(?:{OBJECT})(?:을|를)\s*위한\s+)?{AGREEMENT}(?:을|를)\s+체결{END}"),
    _rule("v5_ko_supply_agreement", "SUPPLY", "ab", "supplier_customer",
          rf"{A}{SUBJECT}{B}(?:에게|에)\s+{OBJECT}(?:을|를)\s+공급하는\s+계약(?:을|를)\s+체결{END}"),
    _rule("v5_ko_supplier_contract_nominal", "SUPPLY", "ab", "supplier_customer",
          rf"{A}(?:는|은)?\s*{B}에\s+{OBJECT}\s+공급\s*계약(?:을|를)?\s+체결(?:했다|하였다)?"),
    _rule("v5_en_supply_agreement", "SUPPLY", "ab", "supplier_customer",
          rf"{A}\s+(?:signed|has\s+signed|entered\s+into)\s+(?:a|an)\s+(?:supply\s+)?(?:agreement|contract)\s+to\s+supply\s+{EN_OBJECT}\s+to\s+{B}"),
    _rule("v5_en_partnership_agreement", "PARTNER", "symmetric", "business_cooperation",
          rf"{A}\s+(?:signed|has\s+signed|entered\s+into)\s+(?:a|an)\s+(?:strategic\s+)?(?:partnership|cooperation)\s+agreement\s+with\s+{B}"),
    _rule("v5_en_has_partnered", "PARTNER", "symmetric", "business_cooperation",
          rf"{A}\s+has\s+(?:partnered|collaborated)\s+with\s+{B}(?:\s+(?:on|to\s+develop)\s+{EN_OBJECT})?"),
    _rule("v5_en_passive_supply", "SUPPLY", "ba", "supplier_customer",
          rf"{A}\s+(?:is|was)\s+supplied\s+with\s+{EN_OBJECT}\s+by\s+{B}"),
)

ROLE_RULES = (
    _rule("v5_en_announced_partnership", "PARTNER", "symmetric", "business_cooperation",
          rf"(?P<left>{N})\s+(?:announced|has\s+announced)\s+(?:a\s+)?(?:new\s+|strategic\s+)?partnership\s+with\s+(?P<right>{N})(?:\s+within\s+the\s+(?P<platform>{N})\s+[A-Za-z0-9®™ -]{{1,60}}\s+platform)?"),
    _rule("v5_en_expanded_joint_partnership", "PARTNER", "symmetric", "business_cooperation",
          rf"(?P<left>{N})\s+and\s+(?P<right>{N})\s+(?:expanded|extended|renewed)\s+their\s+partnership(?:\s+to\s+(?:roll\s+out|develop|deliver|provide)\s+[A-Za-z0-9 -]{{1,80}}\s+across\s+the\s+(?:wider\s+)?(?:[A-Za-z0-9 -]{{1,50}}|{N})\s+Group)?"),
)

LIST_RULES = (
    _rule("v5_ko_supplier_to_list", "SUPPLY", "ab", "supplier_customer",
          rf"(?P<left>{N}){SUBJECT}(?P<right>{LIST})(?:에게|에)\s+{OBJECT}(?:을|를)\s+(?:공급|납품){END}"),
    _rule("v5_ko_investor_list", "INVEST", "ab", "investment",
          rf"(?P<left>{LIST})(?:는|은|가|이)\s+(?:공동으로\s+)?(?P<right>{N})에\s+(?:[0-9.,조억만천원달러$% ]+(?:을|를)\s+)?투자{END}"),
    _rule("v5_ko_partner_counterparty_list", "PARTNER", "symmetric", "explicit_collective_counterparties",
          rf"(?P<left>{N}){SUBJECT}(?P<right>{LIST})(?:와|과)\s+(?:{OBJECT}\s+)?{AGREEMENT}(?:을|를)\s+체결{COMPLETED}"),
    _rule("v5_en_supplier_to_list", "SUPPLY", "ab", "supplier_customer",
          rf"(?P<left>{N})\s+(?:supplies|supplied|provides|provided)\s+{EN_OBJECT}\s+to\s+(?P<right>{LIST})"),
    _rule("v5_en_investor_list", "INVEST", "ab", "investment",
          rf"(?P<left>{LIST})\s+(?:jointly\s+)?(?:invested|have\s+invested)\s+(?:[0-9.,$€£ ]+(?:million|billion|dollars)?\s+)?in\s+(?P<right>{N})"),
    _rule("v5_en_partner_counterparty_list", "PARTNER", "symmetric", "explicit_collective_counterparties",
          rf"(?P<left>{N})\s+(?:signed|has\s+signed)\s+(?:a|an)\s+(?:partnership|cooperation)\s+agreement\s+with\s+(?P<right>{LIST})"),
)


def _guard(sentence, hits):
    if TOKEN.search(sentence) or A in sentence or B in sentence:
        return "reserved_matcher_marker_in_source"
    if any(hit["ambiguous"] for hit in hits):
        return "ambiguous_alias_identity"
    if any(hit["extended"] for hit in hits):
        return "group_or_other_entity_extension"
    if any(re.search(re.escape(hit["quote"]) + r"(?:의|['’]s)?\s+(?:자회사|계열사|그룹|subsidiar(?:y|ies)|affiliate)\b", sentence, re.I) for hit in hits):
        return "unsupported_subsidiary_or_group_identity"
    for reason, pattern in (
        ("instruction_like_text", grammar.INSTRUCTION),
        ("contested_or_false_assertion", CONTESTED),
        ("negated_or_ended_relation", grammar.NEGATION),
        ("uncertain_or_planned_relation", grammar.UNCERTAINTY),
        ("uncertain_or_planned_relation", FUTURE_END),
        ("biography_context", grammar.BIOGRAPHY),
        ("comparison_context", grammar.COMPARISON),
    ):
        if pattern.search(sentence):
            return reason
    return None


def _masked(text, start, end, hits):
    parts, previous_position = [], start
    for index, hit in enumerate(hits):
        parts.extend((text[previous_position:hit["start"]], f"⟦{index}⟧"))
        previous_position = hit["end"]
    parts.append(text[previous_position:end])
    return "".join(parts)


def _subtype(pattern, kind):
    if pattern.endswith("acquires_company"):
        return "company_acquisition"
    if "stake_acquisition" in pattern:
        return "stake_acquisition"
    if "holds_stake" in pattern or "majority_shareholder" in pattern:
        return "shareholding"
    return {"SUPPLY": "supplier_customer", "INVEST": "investment",
            "PARTNER": "business_cooperation", "COMPETE": "market_competition"}[kind]


def _pair_match(surface, hits):
    if len(hits) != 2 or hits[0]["tickers"] == hits[1]["tickers"]:
        return []
    surface = surface.replace("⟦0⟧", A).replace("⟦1⟧", B)
    matches = [(name, kind, direction, _subtype(name, kind))
               for name, kind, direction, pattern in grammar.RULES if pattern.fullmatch(surface)]
    matches += [(name, kind, direction, subtype) for name, kind, direction, subtype, mode, pattern in previous.RULES
                if mode == "fullmatch" and pattern.fullmatch(surface)]
    matches += [(name, kind, direction, subtype) for name, kind, direction, subtype, pattern in PAIR_RULES
                if pattern.fullmatch(surface)]
    return [(name, kind, direction, subtype, [0], [1]) for name, kind, direction, subtype in matches]


def _matches(surface, hits, lists=True):
    matches = _pair_match(surface, hits)
    if lists and 3 <= len(hits) <= 7 and len({h["tickers"][0] for h in hits}) == len(hits):
        for name, kind, direction, subtype, pattern in LIST_RULES:
            matched = pattern.fullmatch(surface)
            if matched:
                left, right = [[int(token[1:-1]) for token in TOKEN.findall(matched[group])] for group in ("left", "right")]
                if len(left) == 1 or len(right) == 1:
                    matches.append((name, kind, direction, subtype, left, right))
    role_matches = []
    for name, kind, direction, subtype, pattern in ROLE_RULES:
        matched = pattern.fullmatch(surface)
        if not matched: continue
        left, right = int(matched["left"][1:-1]), int(matched["right"][1:-1])
        if hits[left]["tickers"] == hits[right]["tickers"]: continue
        platform = matched.groupdict().get("platform")
        if platform is not None and hits[int(platform[1:-1])]["tickers"] != hits[left]["tickers"]:
            continue
        role_matches.append((name, kind, direction, subtype, [left], [right]))
    safe = []
    for matched in matches:
        remainder = surface
        # Entity-role connectors are consumed. Any remaining subject or
        # conjoined actor in an object/purpose slot is unsupported.
        remainder = re.sub(N + r"(?:은|는|이|가|와|과|도|의|에|에게|로부터|으로부터)?", "", remainder)
        if len(hits) > 2:
            remainder = re.sub(r"^\s*(?:and\s*)+|(?:,|·|및|&)\s*", "", remainder, flags=re.I)
            remainder = re.sub(r"\band\b", "", remainder, flags=re.I)
        else:
            remainder = re.sub(r"^\s*and\s+", "", remainder, flags=re.I)
        if matched[0] == "v5_ko_postposed_component_supplier":
            # A coordinated product noun (카메라와 후방카메라), not a third
            # actor: this finite construction ends in the explicit supplier.
            remainder = remainder.replace("볼 수 있는", "")
            if re.search(r"(?:은|는|이|가)\s+|\b(?:said|says|and|who|which)\b", remainder, re.I):
                continue
            safe.append(matched)
        else:
            if matched[0] in {"v5_ko_completed_dated_supply_contract", "v5_ko_supply_agreement", "ko_supplier_to_contract"}:
                remainder = remainder.replace("공급하는", "공급")
            if not UNKNOWN_ACTOR.search(remainder): safe.append(matched)
    return [*safe, *role_matches]


def _clause_spans(start, end, text, hits):
    """Only punctuation-separated clauses; never split arbitrary keyword gaps."""
    interior = [match.end() for match in re.finditer(r"[;；,]", text[start:end])
                if not any(hit["start"] <= start + match.start() < hit["end"] for hit in hits)
                and not (match.start() > 0 and match.end() < end - start
                         and text[start + match.start() - 1].isdigit() and text[start + match.end()].isdigit())]
    cuts = [start, *interior, end]
    # Interior offsets above are local to this sentence.
    cuts = [cuts[0], *[start + offset for offset in cuts[1:-1]], cuts[-1]]
    for lo, hi in zip(cuts, cuts[1:]):
        while lo < hi and text[lo].isspace(): lo += 1
        while hi > lo and (text[hi - 1].isspace() or text[hi - 1] in ",;；"): hi -= 1
        if lo < hi and (lo, hi) != (start, end):
            yield lo, hi


def _surface(masked):
    # Exact exchange annotations are metadata, not actors. Original offsets
    # and raw evidence remain unchanged; only the matcher sees this removal.
    masked = re.sub(r"(" + N + r")\s*\((?:NASDAQ|NasdaqGS|NYSE|AMEX)\s*:\s*[A-Z0-9.:-]{1,16}\)", r"\1", masked, flags=re.I)
    result = grammar._clause_surface(masked)
    result = re.sub(r"^(?:한편|또한|또|이와\s*함께)\s+", "", result)
    # Completed predicates with an independent following punctuation boundary.
    return re.sub(r"(?:했으며|하였으며|했고|하였고)$", "했다", result)


def _edge(text, title, start, end, source, target, match, mode, clause_span, anchors=(), participants=()):
    name, kind, direction, subtype, _, _ = match
    if direction == "ba": source, target = target, source
    if direction == "symmetric" and source["tickers"][0] > target["tickers"][0]: source, target = target, source
    edge = {"sentence": text[start:end], "sentence_start": start, "sentence_end": end,
            "offset_basis": OFFSET_BASIS, "evidence_source": "title" if end <= len(title) else "body",
            "src_ticker": source["tickers"][0], "dst_ticker": target["tickers"][0],
            "rel_type": kind, "directed": direction != "symmetric", "relation_subtype": subtype,
            "direction_semantics": "buyer_to_target" if subtype == "company_acquisition" else
                {"SUPPLY": "supplier_to_customer", "INVEST": "investor_to_investee", "PARTNER": "symmetric", "COMPETE": "symmetric"}[kind],
            "pattern": name, "assertion_status": "asserted_in_article", "temporal_validity": "not_established",
            "economic_effect_verified": False, "contextual_review_required": True,
            "expansion_mode": mode, "relation_clause_span": {"start": clause_span[0], "end": clause_span[1], "quote": text[clause_span[0]:clause_span[1]]},
            "anchor_spans": [{"start": hit["start"], "end": hit["end"], "quote": hit["quote"], "ticker": hit["tickers"][0]} for hit in anchors]}
    for label, hit in (("source", source), ("target", target)):
        edge.update({label + "_start": hit["start"], label + "_end": hit["end"], label + "_quote": hit["quote"]})
    if participants:
        edge["collective_role_participants"] = list(participants)
        edge["assertion_scope"] = "explicit_role_cross_product_not_bilateral_contract_or_independent_event"
    return edge


def extract_grounded_relations(title, body, accepted_entities, *, affiliate_pairs=()):
    """Return unchanged v4 assertions plus exact-span reviewed-only candidates."""
    result = previous.extract_grounded_relations(title, body, accepted_entities, affiliate_pairs=affiliate_pairs)
    text = title + "\n" + body
    mentions = grammar._mentions(text, grammar._entity_aliases(accepted_entities))
    sentences = list(grammar._sentences(text, mentions))
    edges, reviews = list(result["relationships"]), list(result["review_candidates"])
    initial_count = len(edges)
    seen = {(edge["sentence_start"], edge["sentence_end"], edge["src_ticker"], edge["dst_ticker"], edge["rel_type"]) for edge in edges}
    affiliates = {frozenset(pair) for pair in affiliate_pairs}

    def review(start, end, hits, reason, mode):
        reviews.append({"sentence": text[start:end], "sentence_start": start, "sentence_end": end,
            "offset_basis": OFFSET_BASIS, "evidence_source": "title" if end <= len(title) else "body",
            "candidate_tickers": sorted({ticker for hit in hits for ticker in hit["tickers"]}),
            "decision": "review", "reason": reason, "is_asserted_relationship": False, "expansion_mode": mode})

    def add_matches(start, end, hits, matches, mode, clause_span, anchors=()):
        signatures = {(kind, direction, subtype, tuple(left), tuple(right)) for _, kind, direction, subtype, left, right in matches}
        if len(signatures) > 1:
            review(start, end, hits, "ambiguous_relation_or_direction", mode)
            return
        if not matches: return
        chosen = matches[0]
        participants = sorted({hit["tickers"][0] for hit in hits}) if "list" in mode else ()
        for left in chosen[4]:
            for right in chosen[5]:
                edge = _edge(text, title, start, end, hits[left], hits[right], chosen, mode, clause_span, anchors, participants)
                if edge["src_ticker"] == edge["dst_ticker"]: continue
                if edge["rel_type"] == "COMPETE" and frozenset((edge["src_ticker"], edge["dst_ticker"])) in affiliates:
                    review(start, end, hits, "competition_conflicts_with_affiliate_evidence", mode)
                    continue
                key = (start, end, edge["src_ticker"], edge["dst_ticker"], edge["rel_type"])
                if key not in seen:
                    edges.append(edge); seen.add(key)

    for index, (start, end, sentence) in enumerate(sentences):
        hits = [hit for hit in mentions if start <= hit["start"] and hit["end"] <= end]
        if len(hits) >= 2:
            blocked = _guard(sentence, hits)
            if blocked:
                review(start, end, hits, blocked, "local_or_list_deferred")
            elif len(sentence) > 1200:
                review(start, end, hits, "sentence_length_outside_bounded_grammar", "local_or_list_deferred")
            else:
                surface = _surface(_masked(text, start, end, hits))
                add_matches(start, end, hits, _matches(surface, hits),
                            "explicit_counterparty_list" if len(hits) > 2 else "explicit_pair_phrasing", (start, end))
                for lo, hi in _clause_spans(start, end, text, hits):
                    local_hits = [hit for hit in hits if lo <= hit["start"] and hit["end"] <= hi]
                    if len(local_hits) < 2: continue
                    local = _surface(_masked(text, lo, hi, local_hits))
                    add_matches(start, end, local_hits, _matches(local, local_hits),
                                "bounded_local_clause_list" if len(local_hits) > 2 else "bounded_local_clause", (lo, hi))

        pronoun = PRONOUN.match(sentence)
        if not pronoun: continue
        if not index:
            review(start, end, hits, "coreference_missing_adjacent_anchor", "adjacent_coreference")
            continue
        previous_start, previous_end, previous_sentence = sentences[index - 1]
        anchors = [hit for hit in mentions if previous_start <= hit["start"] and hit["end"] <= previous_end]
        combined = [*anchors, *hits]
        if previous_start <= len(title) or start <= len(title) or "\n" in text[previous_end:start] or len(text[previous_start:end]) > 600:
            review(previous_start, end, combined, "coreference_outside_same_body_paragraph", "adjacent_coreference")
            continue
        blocked = _guard(text[previous_start:end], combined)
        if blocked:
            review(previous_start, end, combined, blocked, "adjacent_coreference")
            continue
        if UNNAMED_ACTOR.search(previous_sentence):
            review(previous_start, end, combined, "coreference_unnamed_entity_antecedent", "adjacent_coreference")
            continue
        p = pronoun["p"].casefold()
        plural = p in {"양사", "두 회사", "두회사", "두 기업", "두기업", "they", "the two companies"}
        anchor_surface = _surface(_masked(text, previous_start, previous_end, anchors))
        anchor_surface = anchor_surface.replace("⟦0⟧", A).replace("⟦1⟧", B)
        if plural:
            anchor_ok = len(anchors) == 2 and len({h["tickers"][0] for h in anchors}) == 2 and not hits and JOINT_ANCHOR.fullmatch(anchor_surface)
            remainder = JOINT_ANCHOR.fullmatch(anchor_surface).group() if anchor_ok else ""
            # A second unknown subject/list or a person invalidates a unique joint antecedent.
            remainder = re.sub(rf"^{A}(?:와|과|·)\s*{B}(?:는|은|가|이)\s+|^{A}\s+and\s+{B}\s+", "", remainder, flags=re.I)
            anchor_ok = bool(anchor_ok and not UNKNOWN_ACTOR.search(remainder))
            if not anchor_ok:
                review(previous_start, end, combined, "coreference_ambiguous_plural_antecedent", "adjacent_coreference")
                continue
            if re.match(r"(?:양사|두\s*(?:회사|기업))", pronoun["p"]):
                synthetic = "⟦0⟧와 ⟦1⟧" + pronoun["rest"]
            else:
                synthetic = "⟦0⟧ and ⟦1⟧" + pronoun["rest"]
            resolved_hits = anchors
        else:
            anchor_ok = len(anchors) == 1 and (KO_ANCHOR.fullmatch(anchor_surface) or EN_ANCHOR.fullmatch(anchor_surface))
            tail = re.sub(rf"^{A}(?:는|은|가|이)?\s+", "", anchor_surface)
            if not anchor_ok or UNKNOWN_ACTOR.search(tail) or len(hits) != 1 or hits[0]["tickers"] == anchors[0]["tickers"]:
                review(previous_start, end, combined, "coreference_ambiguous_single_antecedent", "adjacent_coreference")
                continue
            current_surface = _masked(text, start, end, hits)
            current_surface = current_surface.replace("⟦0⟧", "⟦1⟧")
            synthetic = "⟦0⟧" + current_surface[pronoun.end("p"):]
            resolved_hits = [anchors[0], hits[0]]
        matches = _matches(_surface(synthetic), resolved_hits, lists=False)
        if not matches:
            review(previous_start, end, combined, "coreference_predicate_outside_supported_roles", "adjacent_coreference")
        else:
            add_matches(previous_start, end, resolved_hits, matches, "adjacent_coreference", (start, end), anchors)

    unique_reviews = {}
    for item in reviews:
        key = (item["sentence_start"], item["sentence_end"], item["reason"], tuple(item["candidate_tickers"]))
        unique_reviews.setdefault(key, item)
    reviews = list(unique_reviews.values())
    return {**result, "schema": SCHEMA, "policy_version": POLICY_VERSION,
            "relationships": edges, "review_candidates": reviews,
            "flags": sorted({row["reason"] for row in reviews}),
            "new_candidate_count": len(edges) - initial_count,
            "new_candidates_require_contextual_review": True}
