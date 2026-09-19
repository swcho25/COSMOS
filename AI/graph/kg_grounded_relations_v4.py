"""Additive literal alliance/consortium/equity grammar for development review.

Frozen v3 remains a dependency and is never mutated. Its assertions are retained
verbatim; only previously unsupported sentences can gain a v4 assertion. New
rules establish explicit grammatical roles, not semantic similarity or proximity.
Consortium edges mean an explicit leader/participant affiliation, not a direct
bilateral contract, investment, completed project, or verified current alliance.
Merely appearing together in a consortium list never establishes an edge.

Dates and legal-issuer identity remain caller responsibilities. In particular,
an approved lexical alias does not establish a historical legal-entity mapping.
The scan must apply the same immutable parent identity gates as v3. Every new
candidate requires contextual review (including adjacent sentences) before any
integration. Neither v3 nor this module certifies factual or financial effects.
"""
from __future__ import annotations

import re
import kg_grounded_relations_v3 as base

SCHEMA = 'kg-grounded-relations-4-additive'
POLICY_VERSION = 'explicit-alliance-consortium-stake-role-grammar-4'
RELATION_TYPES = base.RELATION_TYPES
OFFSET_BASIS = base.OFFSET_BASIS
A, B = base.MARK_A, base.MARK_B
_ENTITY_END = r'(?=$|[\s,.!?。！？]|(?:은|는|이|가|도|와|과|를|을|의|에|로|처럼|조차)(?=\s|$))'
_FINISHED = r'(?:했다|하였다|한\s+바\s+있다|해왔다|해\s+왔다)'
_SHARE = r'(?:약\s+)?[0-9]+(?:\.[0-9]+)?%'
_PERIOD = r'(?:최근\s+(?:한|두|세|[0-9]+)\s*(?:달|개월|년|주)(?:간|동안)\s+)?'
_RULES = (
    ('v4_relative_completed_alliance', 'PARTNER', 'symmetric', 'business_alliance', 'search',
     rf'{A}(?:와|과)\s+(?:동맹한|제휴한|동맹을\s+맺은|제휴를\s+맺은)\s+{B}{_ENTITY_END}'),
    ('v4_relative_signed_agreement', 'PARTNER', 'symmetric', 'business_cooperation', 'search',
     rf'{A}(?:와|과)\s+(?:업무협약|양해각서|MOU|협력\s+협약|제휴\s+계약)(?:을|를)\s+체결한\s+{B}{_ENTITY_END}'),
    ('v4_consortium_leader_participant', 'PARTNER', 'symmetric', 'consortium_leader_participant', 'fullmatch',
     rf'{A}(?:가|이)\s+(?:이끄는|주도하는)\s+컨소시엄에\s+{B}{base.KO_SUBJECT}참여{_FINISHED}'),
    ('v4_consortium_participant_leader', 'PARTNER', 'symmetric', 'consortium_leader_participant', 'fullmatch',
     rf'{A}{base.KO_SUBJECT}{B}(?:가|이)\s+(?:이끄는|주도하는)\s+컨소시엄(?:의\s+참여사(?:다|이다)|에\s+참여{_FINISHED})'),
    ('v4_stake_acquisition_open_market', 'INVEST', 'ab', 'stake_acquisition', 'fullmatch',
     rf'{A}{base.KO_SUBJECT}{_PERIOD}{B}(?:의)?\s+지분\s+{_SHARE}(?:를|을)\s+장내(?:에서)?\s+매입{_FINISHED}'),
    ('v4_stake_acquisition_confirmed_holding_change', 'INVEST', 'ab', 'stake_acquisition', 'fullmatch',
     rf'{A}{base.KO_SUBJECT}{_PERIOD}{B}(?:의)?\s+지분\s+{_SHARE}(?:를|을)\s+장내(?:에서)?\s+매입해\s+{B}(?:의)?\s+지분율이\s+(?:총\s+)?{_SHARE}로\s+확대(?:됐다|된\s+바\s+있다)'),
)
RULES = tuple((*r[:5], re.compile(r[5], re.I)) for r in _RULES)


def extract_grounded_relations(title, body, accepted_entities, *, affiliate_pairs=()):
    """Preserve v3; append exact-span v4 candidates requiring contextual review."""
    result = base.extract_grounded_relations(title, body, accepted_entities, affiliate_pairs=affiliate_pairs)
    text = title + '\n' + body
    mentions = base._mentions(text, base._entity_aliases(accepted_entities))
    occupied = {(e['sentence_start'], e['sentence_end']) for e in result['relationships']}
    added, resolved = [], set()
    for start, end, sentence in base._sentences(text, mentions):
        if (start, end) in occupied:
            continue
        hits = [m for m in mentions if start <= m['start'] and m['end'] <= end]
        if len(hits) < 2 or any(m['ambiguous'] or m['extended'] for m in hits):
            continue
        if len({m['tickers'][0] for m in hits}) != 2:
            continue
        first = hits[0]
        second = next(m for m in hits if m['tickers'] != first['tickers'])
        if '?' in sentence or any(p.search(sentence) for p in (
                base.INSTRUCTION, base.NEGATION, base.UNCERTAINTY, base.BIOGRAPHY, base.COMPARISON)):
            continue
        pieces, previous = [], start
        for hit in hits:
            pieces.extend((text[previous:hit['start']], A if hit['tickers'] == first['tickers'] else B))
            previous = hit['end']
        pieces.append(text[previous:end])
        masked = base._clause_surface(''.join(pieces))
        matches = [r for r in RULES if (len(hits) == 2 or r[4] == 'fullmatch') and getattr(r[5], r[4])(masked)]
        if not matches or len({(r[1], r[2], r[3]) for r in matches}) != 1:
            continue
        pattern, kind, direction, subtype, _, _ = matches[0]
        src, dst = first, second
        if direction == 'symmetric' and src['tickers'][0] > dst['tickers'][0]:
            src, dst = dst, src
        added.append({'sentence': sentence, 'sentence_start': start, 'sentence_end': end,
                      'offset_basis': OFFSET_BASIS, 'evidence_source': 'title' if end <= len(title) else 'body',
                      'src_ticker': src['tickers'][0], 'dst_ticker': dst['tickers'][0], 'rel_type': kind,
                      'directed': direction != 'symmetric', 'relation_subtype': subtype,
                      'direction_semantics': 'investor_to_investee' if kind == 'INVEST' else 'symmetric',
                      'source_start': src['start'], 'source_end': src['end'], 'source_quote': src['quote'],
                      'target_start': dst['start'], 'target_end': dst['end'], 'target_quote': dst['quote'],
                      'pattern': pattern, 'assertion_status': 'asserted_in_article',
                      'temporal_validity': 'not_established', 'economic_effect_verified': False,
                      'contextual_review_required': True})
        resolved.add((start, end))
    reviews = [r for r in result['review_candidates'] if (r['sentence_start'], r['sentence_end']) not in resolved]
    return {**result, 'schema': SCHEMA, 'policy_version': POLICY_VERSION,
            'relationships': [*result['relationships'], *added], 'review_candidates': reviews,
            'flags': sorted({r['reason'] for r in reviews}), 'new_candidate_count': len(added)}
