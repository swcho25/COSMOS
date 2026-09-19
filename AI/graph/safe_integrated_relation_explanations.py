"""Recommended local relationship explanation: typed introduction + exact quotes.

ALL four relation types use deterministic extraction. No LLM, NLI, generated
paraphrase, numerical interpretation, current ownership or causal assertion is
accepted. A real CPU Qwen/NLI probe falsely accepted an alliance as joint AIDC
construction. The previous generators and their rejected outputs are retained
as research; NLI acceptance must never be treated as semantic accuracy.

The immutable integrated reader verifies source spans, metadata, hashes, source
availability and typed directions. This adapter preserves that evidence and
adds only a fixed Korean description of the queried graph relation. Company
tickers are displayed directly; no undated company-name dictionary is needed.
This is the recommended current local adapter, not a service deployment.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
from pathlib import Path
import json

from integrated_relation_graph import IntegratedRelationRetriever, require, sha, timestamp

HERE = Path(__file__).resolve().parent
DEFAULT_GRAPH = HERE / 'artifacts/kg_integrated_20260919_v2'
VERSION = 'typed-introduction-exact-relation-evidence-1'
TYPES = {'SUPPLY': '공급', 'INVEST': '투자·지분', 'PARTNER': '협력', 'COMPETE': '경쟁'}
POLICY_REASON = 'freeform_paraphrase_disabled_after_observed_role_attribution_false_accept'
NOTE = ('원문에 기록된 관계의 근거입니다. 사건 발생일, 현재 계약·소유관계의 유효성, '
        '기업별 지분율 또는 주가 영향을 새로 판단하지 않습니다.')


class SafeIntegratedRelationExplainer:
    """Same explain signature, deliberately without a text-generation pathway.

    ``generator`` is accepted solely to make non-invocation testable and prevent
    callers from accidentally enabling legacy paraphrases. It is never stored,
    inspected or called. Use a new reader after changing graph artifacts.
    """

    def __init__(self, retriever=None, *, directory=DEFAULT_GRAPH, generator=None):
        self.retriever = retriever if retriever is not None else IntegratedRelationRetriever(directory)
        self.generator_argument_ignored = generator is not None
        self.provenance = {
            'version': VERSION, 'adapter_code_sha256': sha(Path(__file__)),
            'company_names_source': 'query_tickers_only_no_name_dictionary',
            'generation_policy': 'disabled_for_all_relation_types',
            'nli_acceptance_is_accuracy': False,
            'semantic_guard': 'no_freeform_generated_assertions_to_accept',
        }

    def explain(self, src_ticker, dst_ticker, rel_type, *, as_of=None, limit=5):
        require(rel_type in TYPES, 'Unknown relation type')
        require(type(limit) is int and 1 <= limit <= 20, 'limit must be 1..20')
        require(all(isinstance(t, str) and t and t.strip() == t for t in (src_ticker, dst_ticker))
                and src_ticker != dst_ticker, 'Two distinct company identifiers required')
        symmetric = rel_type in {'PARTNER', 'COMPETE'}
        expected = {'src_ticker': src_ticker, 'dst_ticker': dst_ticker,
                    'rel_type': rel_type, 'directed': not symmetric}
        result = deepcopy(self.retriever.retrieve(src_ticker, dst_ticker, rel_type, as_of=as_of, limit=limit))
        require(result['relationship'] == expected, 'Reader returned a different relationship')
        require(result['as_of'] == as_of, 'Reader changed requested cutoff')
        require(result['disclosures_searched'] is True and result['source_kind'] == 'NEWS_AND_DISCLOSURE',
                'Both evidence sources must be searched')
        evidence = result['evidence']
        require(isinstance(evidence, list) and len(evidence) <= limit, 'Invalid evidence count')
        require(len({e['id'] for e in evidence}) == len(evidence), 'Duplicate citation identifiers')
        cutoff = timestamp(as_of) if as_of is not None else None
        for e in evidence:
            require(e['rel_type'] == rel_type and e['directed'] == (not symmetric), 'Evidence direction/type differs')
            endpoints = (e['src_ticker'], e['dst_ticker'])
            require(set(endpoints) == {src_ticker, dst_ticker} if symmetric else endpoints == (src_ticker, dst_ticker),
                    'Evidence belongs to another company pair')
            require(e['source_kind'] in {'NEWS', 'DISCLOSURE'}, 'Unknown evidence source')
            require(isinstance(e['quote'], str) and e['quote'] and e['text'] == e['quote'], 'Missing exact quotation')
            require(e['citation'] == '[' + e['id'] + ']', 'Citation identifier mismatch')
            require(isinstance(e['source_url'], str) and e['source_url'], 'Missing original source URL')
            require(cutoff is None or timestamp(e['available_at']) < cutoff, 'Evidence is not before cutoff')
        require(result['sources_returned'] == sorted({e['source_kind'] for e in evidence}), 'Source summary differs')

        # Drop any explanation-like fields supplied by an alternative reader.
        response = {key: result[key] for key in (
            'schema', 'relationship', 'as_of', 'evidence', 'eligible_unique_evidence',
            'source_kind', 'disclosures_searched', 'sources_returned',
            'excluded_unknown_or_after_cutoff', 'retrieval_provenance')}
        response.update(
            status='EXTRACTIVE_FALLBACK' if evidence else 'NO_EVIDENCE',
            mode='extractive', extraction_mode='typed_relation_introduction_and_literal_evidence',
            introduction=None, explanation=None, claims=[], evidence_ids=[],
            generator_used=False, generator_argument_ignored=self.generator_argument_ignored,
            generator_provenance=None, generated_verified=False, nli_used=False,
            verification_scores=[], fallback_reason=POLICY_REASON if evidence else None,
            explanation_provenance=self.provenance,
            production_approved=False, graph_quality_validated=False, price_effect_established=False,
            structured_financial_claims_generated=False, semantic_accuracy_estimated=False,
            note=NOTE)
        if not evidence:
            return response
        arrow = '↔' if symmetric else '→'
        intro = f'다음 자료는 {src_ticker} {arrow} {dst_ticker} {TYPES[rel_type]} 관계로 연결한 근거입니다.'
        claims = [{'evidence_id': e['id'], 'quote': e['quote']} for e in evidence]
        pieces = [intro]
        for e in evidence:
            kind = '뉴스' if e['source_kind'] == 'NEWS' else '공시'
            # Source times are explicitly availability, not inferred event time.
            metadata = f"{e['citation']} {kind} · 이용 가능 시각 {e['available_at']}"
            if e['source_kind'] == 'DISCLOSURE':
                require(bool(e.get('filing_ticker')) and bool(e.get('filing_corp_code')), 'Missing verified filing identity')
                metadata += f" · 공시 보고기업 {e['filing_ticker']} · 법인코드 {e['filing_corp_code']}"
            pieces.append(metadata + '\n' + e['quote'] + '\n' + e['source_url'])
        response.update(introduction=intro, explanation='\n\n'.join(pieces), claims=claims,
                        evidence_ids=[e['id'] for e in evidence])
        return response


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--graph', type=Path, default=DEFAULT_GRAPH)
    parser.add_argument('--src', required=True)
    parser.add_argument('--dst', required=True)
    parser.add_argument('--type', dest='rel_type', choices=sorted(TYPES), required=True)
    parser.add_argument('--as-of')
    parser.add_argument('--limit', type=int, default=5)
    args = parser.parse_args()
    result = SafeIntegratedRelationExplainer(directory=args.graph).explain(
        args.src, args.dst, args.rel_type, as_of=args.as_of, limit=args.limit)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
