"""Build typed versioned-service rows from an immutable verified projection.

Public stable IDs are reused by canonical URL/hash or filing identity. Analysis
rows never borrow old predictions. Unknown source timestamps, sentiment and
model confidence stay NULL; created_at is explicitly an administrative payload
creation time. News sentiment is not a GNN price prediction. Historical typed
relations and their literal citations are not assertions of current contracts.

This module writes local JSONL only. Publication, legacy capture, active-version
switching and rollback are deliberately separate database responsibilities.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import uuid
from urllib.parse import urlsplit

from prepare_grounded_service import encoded, read, require, sha
from safe_integrated_relation_explanations import SafeIntegratedRelationExplainer

HERE = Path(__file__).resolve().parent
VERSION = 'grounded-service-v1'
SENTIMENT_VERSION = 'kg-target-context-finbert-canonical-v1'
RELATION_VERSION = 'grounded-integrated-relation-v3'
NS = uuid.UUID('5cb50a33-5979-5261-b718-9b0734da05a8')
TABLES = ['source_document', 'news_article', 'disclosure', 'company_document', 'document_evidence',
          'company_relationship', 'relationship_evidence', 'relationship_score_current',
          'relationship_score_history', 'company_metric_history', 'graph_snapshot', 'graph_snapshot_load']
CONTEXTS = {
    'document_context': ['document_id', 'source_revision_id', 'sort_at', 'published_at_raw', 'publication_precision', 'payload'],
    'relationship_context': ['relationship_id', 'explanation', 'payload'],
    'evidence_context': ['evidence_id', 'source_revision_id', 'source_kind', 'available_at', 'published_at_raw', 'publication_precision', 'payload'],
}
KEYS = {'source_document': ['document_id'], 'news_article': ['document_id'], 'disclosure': ['document_id'],
        'company_document': ['document_id', 'company_id'], 'document_evidence': ['evidence_id'],
        'company_relationship': ['relationship_id'], 'relationship_evidence': ['relationship_evidence_id'],
        'relationship_score_current': ['relationship_id', 'window_type'], 'relationship_score_history': ['score_history_id'],
        'company_metric_history': ['company_id', 'measured_at', 'window_type'], 'graph_snapshot': ['snapshot_id'],
        'graph_snapshot_load': ['snapshot_id'], 'document_context': ['document_id'],
        'relationship_context': ['relationship_id'], 'evidence_context': ['evidence_id'], 'data_sources': ['source_id']}


def stable(*parts):
    return str(uuid.uuid5(NS, encoded(parts)))


def text_sha(value):
    return hashlib.sha256(value.encode('utf8')).hexdigest()


def relation_identity(source, target, kind):
    if kind in {'PARTNER', 'COMPETE'}:
        source, target = sorted((source, target), key=lambda x: uuid.UUID(x).int)
    return source, target, kind


def filing_identity(row):
    if row['source'] == 'DART':
        require(re.fullmatch(r'\d{14}', row['report_id']) is not None, 'Invalid DART receipt number')
        return 'DART', row['report_id']
    require(row['source'] == 'SEC', 'Unsupported filing source')
    match = re.search(r'/Archives/edgar/data/(\d+)/', row['source_url'])
    require(match is not None, 'SEC CIK is not present in the original source URL')
    cik = match.group(1).zfill(10)
    require(re.fullmatch(r'\d{10}-\d{2}-\d{6}', row['report_id']) is not None, 'Invalid SEC accession')
    return 'SEC', cik, row['report_id']


def existing_filing_identity(row):
    if row['filing_system'] == 'DART':
        return 'DART', row['dart_receipt_no']
    require(row['filing_system'] == 'SEC' and row['sec_cik'] and row['sec_accession_no'],
            'Invalid existing filing identity')
    return 'SEC', str(row['sec_cik']).zfill(10), row['sec_accession_no']


def validate_typed_row(table, row, columns):
    require(set(row) == {c['column_name'] for c in columns}, 'Unexpected typed columns: ' + table)
    for column in columns:
        value, name, kind = row[column['column_name']], column['column_name'], column['data_type']
        if value is None:
            continue  # analysis version tables intentionally allow source NULLs
        if kind == 'uuid':
            uuid.UUID(value)
        elif kind in {'smallint', 'integer', 'bigint'}:
            require(type(value) is int, 'Expected integer: ' + table + '.' + name)
        elif kind == 'boolean':
            require(type(value) is bool, 'Expected bool: ' + table + '.' + name)
        elif kind == 'numeric':
            require(type(value) in {int, float}, 'Expected number: ' + table + '.' + name)
        elif kind in {'text', 'character', 'character varying'}:
            require(isinstance(value, str) and '\x00' not in value, 'Invalid SQL text: ' + table + '.' + name)
            maximum = column.get('character_maximum_length')
            require(maximum is None or len(value) <= maximum, 'Text exceeds column: ' + table + '.' + name)
        elif kind == 'timestamp with time zone':
            require(dt.datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is not None,
                    'Timestamp requires timezone: ' + table + '.' + name)
        elif kind == 'date':
            dt.date.fromisoformat(value)
        elif kind == 'ARRAY':
            require(isinstance(value, list), 'Expected array: ' + table + '.' + name)
        else:
            raise ValueError('Unknown PostgreSQL type: ' + kind)


class Writer:
    def __init__(self, output, references):
        self.output, self.references = output, references
        self.columns = {t: [c for c in references['columns'] if c['table_name'] == t]
                        for t in TABLES + ['data_source']}
        self.streams, self.counts, self.seen = {}, Counter(), defaultdict(set)

    def write(self, table, row):
        if table in CONTEXTS:
            require(set(row) == set(CONTEXTS[table]), 'Wrong context columns: ' + table)
        else:
            columns = self.columns['data_source' if table == 'data_sources' else table]
            require(columns, 'Missing DB column metadata: ' + table)
            require(not set(row) - {c['column_name'] for c in columns}, 'Unknown DB output fields: ' + table)
            row = {c['column_name']: row.get(c['column_name']) for c in columns}
            validate_typed_row(table, row, columns)
        key = tuple(row[k] for k in KEYS[table])
        require(all(x is not None for x in key) and key not in self.seen[table], 'Duplicate/NULL key: ' + table)
        self.seen[table].add(key)
        if table not in self.streams:
            self.streams[table] = (self.output / (table + '.jsonl')).open('x', encoding='utf8', newline='\n')
        self.streams[table].write(encoded(row) + '\n')
        self.counts[table] += 1

    def finish(self):
        for stream in self.streams.values():
            stream.close()
        return {table: {'path': table + '.jsonl', 'rows': self.counts[table],
                        'bytes': (self.output / (table + '.jsonl')).stat().st_size,
                        'sha256': sha(self.output / (table + '.jsonl'))}
                for table in self.streams}


def run(projection, references_path, output, prepared, graph):
    projection, references_path, output, prepared, graph = map(lambda p: Path(p).resolve(),
                                                              (projection, references_path, output, prepared, graph))
    require(not output.exists(), 'Output exists; use a new generation')
    manifest, references = read(projection / 'manifest.json'), read(references_path)
    require(manifest['status'] == 'PASS', 'Projection is not verified')
    require(sha(projection / 'projection.sqlite3') == manifest['projection_sha256'], 'Projection SQLite changed')
    require(sha(prepared / 'backfill_publication.json') == manifest['publication_sha256'], 'Wrong source lake publication')
    publication = read(prepared / 'backfill_publication.json')
    specs = {d['name']: d for d in publication['datasets']}
    conn = sqlite3.connect((projection / 'projection.sqlite3').as_uri() + '?mode=ro&immutable=1', uri=True)
    def rows(table):
        for (raw,) in conn.execute('SELECT row_json FROM ' + table):
            yield json.loads(raw)
    def source_rows(dataset):
        for part in specs[dataset]['files']:
            digest = hashlib.sha256()
            with (prepared / part['local_file']).open('rb') as stream:
                for number, line in enumerate(stream, 1):
                    digest.update(line)
                    yield json.loads(line), {'local_file': part['local_file'], 'line_1based': number,
                                            'file_sha256': part['sha256'], 'line_sha256': hashlib.sha256(line).hexdigest(),
                                            'dataset': dataset, 'publication_sha256': manifest['publication_sha256']}
            require(digest.hexdigest() == part['sha256'], 'Original source shard changed: ' + part['local_file'])

    companies = {(r['market'], r['stock_code']): r['company_id'] for r in references['companies']}
    require(len(companies) == len(references['companies']) == 202, 'Ambiguous/changed company universe')
    require({(r['market'], r['stock_code']) for r in rows('companies')} == set(companies), 'Company universe differs')
    types = {r['code']: r for r in references['relationship_types']}
    require(set(types) == {'SUPPLY', 'INVEST', 'PARTNER', 'COMPETE'}, 'Wrong relationship type universe')
    for code, spec in types.items():
        require(spec['directionality'] == ('UNDIRECTED' if code in {'PARTNER', 'COMPETE'} else 'DIRECTED'),
                'Wrong relation direction contract')
    type_codes = {r['relationship_type_id']: r['code'] for r in types.values()}
    existing_news, existing_news_ids = {}, {}
    for row in references['news']:
        require(text_sha(row['canonical_url']) == row['canonical_url_hash'], 'Existing news URL SHA mismatch')
        require(row['canonical_url_hash'] not in existing_news, 'Duplicate existing canonical URL hash')
        existing_news[row['canonical_url_hash']] = row
        existing_news_ids[row['document_id']] = row['canonical_url']
    existing_filings = {}
    for row in references['disclosures']:
        key = existing_filing_identity(row)
        require(key not in existing_filings, 'Ambiguous existing filing identity')
        existing_filings[key] = row
    existing_relations = {}
    for row in references['relationships']:
        key = relation_identity(row['source_company_id'], row['target_company_id'], type_codes[row['relationship_type_id']])
        require(key not in existing_relations, 'Both directions of an undirected relationship already exist')
        existing_relations[key] = row

    output.mkdir(parents=True)
    writer = Writer(output, references)
    mappings = (output / 'mapping.jsonl').open('x', encoding='utf8', newline='\n')
    def mapping(kind, original, service, **extra):
        mappings.write(encoded({'kind': kind, 'source_id': original, 'service_id': service, **extra}) + '\n')
    admin_at = dt.datetime.now(dt.timezone.utc).isoformat()
    hdfs_graph = '/data-lake/snapshots/graph/run_id=' + manifest['run_id']
    hdfs_news = '/data-lake/cleaned/news/run_id=' + manifest['run_id'] + '/data'
    hdfs_mentions = '/data-lake/analyzed/news/company-mentions/run_id=' + manifest['run_id'] + '/data'
    data_sources = {r['name']: r for r in references['sources']}
    new_sources = {}
    def source_id(url, kind):
        name = kind if kind in {'DART', 'SEC'} else 'news:grounded:' + (urlsplit(url).hostname or '')
        require(len(name) <= 100 and name != 'news:grounded:', 'Invalid data-source name')
        if name in data_sources:
            return data_sources[name]['source_id']
        if name not in new_sources:
            new_sources[name] = {'source_id': stable('data-source', name), 'name': name,
                                 'source_type': 'NEWS' if kind == 'NEWS' else 'DISCLOSURE',
                                 'base_url': urlsplit(url).scheme + '://' + urlsplit(url).netloc, 'is_active': True}
        return new_sources[name]['source_id']
    doc_ids, doc_times, doc_original = {}, {}, {}
    reused = Counter()
    print('Building canonical news identities and typed document rows', flush=True)
    for row, pointer in source_rows('cleaned_news'):
        canonical = row['canonical_url']
        require(bool(canonical), 'Canonical news URL missing')
        digest = text_sha(canonical)
        old = existing_news.get(digest)
        if old:
            require(old['canonical_url'] == canonical, 'Canonical hash collision')
        if row['document_id'] in existing_news_ids:
            require(existing_news_ids[row['document_id']] == canonical, 'Existing news UUID belongs to another URL')
        document = old['document_id'] if old else row['document_id']
        require(document not in doc_original, 'Two canonical news mapped to one service UUID')
        doc_ids[row['document_id']] = document
        doc_original[document] = row['document_id']
        doc_times[document] = {k: row[k] for k in ('available_at', 'published_at_raw', 'published_at', 'availability_policy')}
        require(row['available_at'] is not None, 'Unknown news sort/availability time')
        reused['news_existing' if old else 'news_new'] += 1
        mapping('news', row['document_id'], document, reused=bool(old), canonical_url_hash=digest)
        writer.write('source_document', {'document_id': document, 'source_id': old['source_id'] if old else source_id(row['source_url'], 'NEWS'),
                    'document_type': 'NEWS', 'title': row['title'], 'summary': row['body'][:500], 'original_url': row['source_url'],
                    'published_at': row['published_at'], 'first_collected_at': None, 'last_collected_at': None,
                    'content_hash': row['cleaned_text_sha256'], 'hdfs_clean_uri': hdfs_news,
                    'status': 'ANALYZED', 'analysis_version': VERSION, 'created_at': admin_at, 'updated_at': admin_at})
        writer.write('news_article', {'document_id': document, 'publisher': urlsplit(row['source_url']).hostname,
                    'canonical_url': canonical, 'canonical_url_hash': digest})
        writer.write('document_context', {'document_id': document, 'source_revision_id': row['source_revision_id'],
                    'sort_at': row['available_at'], 'published_at_raw': row['published_at_raw'],
                    'publication_precision': 'timestamp' if row['published_at'] else 'date',
                    'payload': {'source_document_id': row['document_id'], 'published_date': row['published_date'],
                                'availability_policy': row['availability_policy'], 'source_collected_at': None,
                                'administrative_created_at': admin_at, 'summary_method': 'cleaned_body_first_500_characters',
                                'source_pointer': pointer, 'source_revision_id': row['source_revision_id'],
                                'cleaned_text_sha256': row['cleaned_text_sha256'], 'scope': row['scope'],
                                'graph_quality_validated': False, 'price_effect_established': False}})

    filing_groups = defaultdict(list)
    for row in rows('filing_mentions'):
        filing_groups[filing_identity(row)].append(row)
    for key, observations in sorted(filing_groups.items()):
        first = observations[0]
        require(len({(r['source_url'], r['available_at'], r['report_id']) for r in observations}) == 1,
                'Conflicting filing metadata for one identity')
        metadata = json.loads(first['source_document_json'])
        old = existing_filings.get(key)
        document = old['document_id'] if old else stable('filing', *key)
        issuers = sorted({companies[(r['market'], r['ticker'])] for r in observations}, key=lambda v: uuid.UUID(v).int)
        issuer = old['filing_company_id'] if old else issuers[0]
        require(issuer in issuers, 'Existing filing company is not a verified observed issuer')
        require(document not in doc_original, 'Filing identity collides with another document')
        doc_original[document] = first['document_id']
        for row in observations:
            doc_ids[row['document_id']] = document
        doc_times[document] = {k: first[k] for k in ('available_at', 'published_at_raw', 'published_at', 'availability_policy')}
        require(first['available_at'] is not None, 'Unknown filing sort/availability time')
        reused['filing_existing' if old else 'filing_new'] += 1
        mapping('disclosure', first['document_id'], document, reused=bool(old), filing_key=key,
                observed_company_ids=issuers, representative_company_id=issuer)
        title = metadata['report_name']
        revision = 'filing-metadata:' + text_sha(first['source_document_json'])
        writer.write('source_document', {'document_id': document, 'source_id': source_id(first['source_url'], first['source']),
                    'document_type': 'DISCLOSURE', 'title': title, 'summary': None, 'original_url': first['source_url'],
                    'published_at': first['published_at'], 'first_collected_at': None, 'last_collected_at': None,
                    'content_hash': metadata.get('original_report_sha256'), 'hdfs_raw_uri': metadata.get('source_path'),
                    'hdfs_clean_uri': '/data-lake/analyzed/disclosures/company-mentions/run_id=' + manifest['run_id'] + '/data',
                    'status': 'ANALYZED', 'analysis_version': VERSION, 'created_at': admin_at, 'updated_at': admin_at})
        writer.write('disclosure', {'document_id': document, 'filing_company_id': issuer,
                    'dart_receipt_no': key[1] if key[0] == 'DART' else None,
                    'report_code': metadata.get('report_code') if key[0] == 'DART' else title,
                    'report_name': title, 'filing_date': first['published_date'],
                    'disclosure_type': metadata.get('disclosure_type') if key[0] == 'DART' else title,
                    'correction_status': metadata.get('correction_status'), 'filing_system': key[0],
                    'sec_accession_no': key[2] if key[0] == 'SEC' else None, 'sec_cik': key[1] if key[0] == 'SEC' else None})
        writer.write('document_context', {'document_id': document, 'source_revision_id': revision,
                    'sort_at': first['available_at'], 'published_at_raw': first['published_at_raw'],
                    'publication_precision': 'timestamp' if first['published_at'] else 'date',
                    'payload': {'source_document_id': first['document_id'], 'source_collected_at': None,
                                'source_observed_at': metadata.get('observed_at'), 'administrative_created_at': admin_at,
                                'availability_policy': first['availability_policy'], 'source_metadata': metadata,
                                'observed_company_ids': issuers, 'representative_company_policy': 'reuse_existing_otherwise_lowest_uuid',
                                'original_filing_mentions': observations, 'price_effect_established': False}})

    company_docs = set()
    def company_document(document, company, *, visible, sentiment=None, mention='DIRECT', model=SENTIMENT_VERSION):
        key = document, company
        if key in company_docs:
            return
        company_docs.add(key)
        writer.write('company_document', {'document_id': document, 'company_id': company, 'mention_type': mention,
                    'relevance_score': None, 'sentiment': sentiment, 'impact_score': None, 'confidence': None,
                    'model_version': model, 'is_service_visible': visible, 'analyzed_at': admin_at})
    for row in rows('canonical_company_mentions'):
        company_document(doc_ids[row['document_id']], companies[(row['market'], row['ticker'])],
                         visible=True, sentiment=row['sentiment_label'])
    for row in rows('filing_mentions'):
        company_document(doc_ids[row['document_id']], companies[(row['market'], row['ticker'])],
                         visible=False, mention='REPORTING_COMPANY', model=VERSION)

    print('Building original news citation rows and source offset contexts', flush=True)
    sentence_orders = Counter()
    for row, pointer in source_rows('news_mentions'):
        edge = json.loads(row['source_edge_json'])
        document, company = doc_ids[row['document_id']], companies[(row['market'], row['stock_code'])]
        require((document, company) in company_docs, 'Missing visible company observation')
        for index, evidence in enumerate(edge['evidence']):
            require(bool(evidence['quote']), 'Empty original news evidence')
            evidence_id = stable('news-company-evidence', row['edge_id'], row['source_revision_id'], index)
            order = sentence_orders[(document, company)]
            sentence_orders[(document, company)] += 1
            writer.write('document_evidence', {'evidence_id': evidence_id, 'document_id': document, 'company_id': company,
                        'sentence_text': evidence['quote'], 'sentence_order': order, 'confidence': None,
                        'model_version': 'grounded-news-original-evidence-v1'})
            writer.write('evidence_context', {'evidence_id': evidence_id, 'source_revision_id': row['source_revision_id'],
                        'source_kind': 'NEWS', 'available_at': row['available_at'], 'published_at_raw': row['published_at_raw'],
                        'publication_precision': 'timestamp' if row['published_at'] else 'date',
                        'payload': {'source_edge_id': row['edge_id'], 'source_document_id': row['document_id'],
                                    'article_id': row['article_id'], 'original_evidence': evidence,
                                    'quote_sha256': text_sha(evidence['quote']),
                                    'source_edge_json_sha256': text_sha(row['source_edge_json']), 'source_pointer': pointer,
                                    'hdfs_source_dataset': hdfs_mentions, 'hdfs_revision_documents': hdfs_graph + '/documents',
                                    'offset_basis': 'original_' + evidence['source'] + '_unicode_codepoints',
                                    'source_url': row['source_url'], 'sentiment_status': row['sentiment_status'],
                                    'sentiment_scope': 'target_grounded_context_financial_polarity',
                                    'economic_relevance_validated': False, 'graph_quality_validated': False,
                                    'price_effect_established': False, 'is_stock_price_prediction': False}})

    print('Building relationship identities and literal GraphRAG explanations', flush=True)
    explainer = SafeIntegratedRelationExplainer(directory=graph)
    relations, relation_rows = {}, list(rows('historical_relations'))
    score_groups = defaultdict(list)
    for row in rows('relationship_scores'):
        score_groups[(row['source_stock_code'], row['target_stock_code'], row['relationship_type_code'])].append(row)
    for row in relation_rows:
        source = companies[(row['source_market'], row['source_stock_code'])]
        target = companies[(row['target_market'], row['target_stock_code'])]
        key = relation_identity(source, target, row['relationship_type_code'])
        old = existing_relations.get(key)
        relation_id = old['relationship_id'] if old else stable('relationship', *key)
        relations[key] = relation_id
        reused['relation_existing' if old else 'relation_new'] += 1
        mapping('relationship', row['relationship_key'], relation_id, reused=bool(old), canonical_identity=key)
        # Reuse the original orientation alongside an existing UUID; the lookup
        # key is normalized for symmetric relationships, not the historical row.
        writer.write('company_relationship', {'relationship_id': relation_id,
                    'source_company_id': old['source_company_id'] if old else key[0],
                    'target_company_id': old['target_company_id'] if old else key[1],
                    'relationship_type_id': types[key[2]]['relationship_type_id'],
                    'created_at': old['created_at'] if old else admin_at})
        explanation = explainer.explain(row['source_stock_code'], row['target_stock_code'],
                                        row['relationship_type_code'], as_of=manifest['as_of'], limit=5)
        require(explanation['status'] == 'EXTRACTIVE_FALLBACK' and explanation['evidence'],
                'Historical relation has no validated retrieved explanation')
        require({e['evidence_id'] for e in explanation['evidence']} <= set(json.loads(row['evidence_ids_json'])),
                'GraphRAG cited evidence outside the frozen relationship')
        writer.write('relationship_context', {'relationship_id': relation_id,
                    'explanation': {'summary': explanation['explanation'], 'scope': 'historical',
                                    'mode': explanation['mode'], 'generator_used': False,
                                    'note': explanation['note'], 'evidence': explanation['evidence'],
                                    'retrieval_provenance': explanation['retrieval_provenance']},
                    'payload': {'source_relationship': row, 'source_window_scores': score_groups[
                                (row['source_stock_code'], row['target_stock_code'], row['relationship_type_code'])],
                                'confidence_policy': 'document_support_weight_is_not_model_probability',
                                'source_direction_preserved': True}})
    revision_meta = {row['source_revision_id']: {key: value for key, value in row.items() if key != 'text'}
                     for row in rows('evidence_documents')}
    for row, pointer in source_rows('graph_evidence'):
        document = doc_ids[row['document_id']]
        src = companies[(row['source_market'], row['src_ticker'])]
        dst = companies[(row['target_market'], row['dst_ticker'])]
        key = relation_identity(src, dst, row['rel_type'])
        relation_id = relations[key]
        company_document(document, src, visible=False, mention='RELATION_EVIDENCE', model=RELATION_VERSION)
        company_document(document, dst, visible=False, mention='RELATION_EVIDENCE', model=RELATION_VERSION)
        evidence_id = stable('relationship-document-evidence', row['evidence_id'])
        order = sentence_orders[(document, src)]
        sentence_orders[(document, src)] += 1
        writer.write('document_evidence', {'evidence_id': evidence_id, 'document_id': document, 'company_id': src,
                    'sentence_text': row['quote'], 'sentence_order': order, 'confidence': None, 'model_version': RELATION_VERSION})
        writer.write('evidence_context', {'evidence_id': evidence_id, 'source_revision_id': row['source_revision_id'],
                    'source_kind': row['source_kind'], 'available_at': row['available_at'],
                    'published_at_raw': row['published_at_raw'], 'publication_precision': 'timestamp' if row['published_at'] else 'date',
                    'payload': {'source_evidence': row, 'hdfs_revision_documents': hdfs_graph + '/documents',
                                'hdfs_evidence': hdfs_graph + '/evidence', 'publication_sha256': manifest['publication_sha256'],
                                'source_pointer': pointer, 'quote_sha256': text_sha(row['quote']),
                                'source_revision_metadata': revision_meta[row['source_revision_id']],
                                'quote_start': row['quote_start'], 'quote_end': row['quote_end'], 'offset_basis': row['offset_basis'],
                                'contribution_meaning': 'document_support_weight_not_probability'}})
        writer.write('relationship_evidence', {'relationship_evidence_id': stable('relationship-evidence', row['evidence_id']),
                    'relationship_id': relation_id, 'document_id': document, 'evidence_id': evidence_id,
                    'contribution_score': row['support_weight'], 'model_version': RELATION_VERSION,
                    'formula_version': 'rel-v0.3-components'})

    for row in rows('relationship_scores'):
        key = relation_identity(companies[(row['source_market'], row['source_stock_code'])],
                                companies[(row['target_market'], row['target_stock_code'])], row['relationship_type_code'])
        relation = relations[key]
        shared = {'relationship_id': relation, 'window_type': row['window_type'], 'snapshot_id': manifest['snapshot_id'],
                  'score': row['score'], 'impact_direction': row['impact_direction'], 'confidence': None,
                  'formula_version': row['formula_version'], 'news_score': row['news_score'], 'disclosure_score': row['disclosure_score']}
        writer.write('relationship_score_current', {**shared, 'evidence_count': row['evidence_count'], 'as_of_at': row['as_of_at']})
        writer.write('relationship_score_history', {**shared, 'score_history_id': stable('score-history', manifest['snapshot_id'], relation, row['window_type']),
                    'period_start': row['period_start'], 'period_end': row['period_end']})
    for row in rows('company_metrics'):
        company = companies[(row['market'], row['stock_code'])]
        writer.write('company_metric_history', {'company_id': company, **{k: row[k] for k in
                    ('measured_at', 'news_mention_count', 'positive_count', 'negative_count', 'sentiment_score',
                     'relationship_count', 'metric_version', 'window_type')}})
    writer.write('graph_snapshot', {'snapshot_id': manifest['snapshot_id'], 'as_of_at': manifest['as_of'],
                'formula_version': 'rel-v0.3-components', 'model_version': VERSION, 'status': 'PUBLISHED',
                'hdfs_uri': hdfs_graph, 'published_at': admin_at})
    writer.write('graph_snapshot_load', {'snapshot_id': manifest['snapshot_id'], 'manifest_sha256': manifest['publication_sha256'],
                'record_count': writer.counts['relationship_score_current'], 'stored_count': writer.counts['relationship_score_current'],
                'windows': ['7D', '30D', '90D'], 'loaded_at': admin_at})
    for row in new_sources.values():
        writer.write('data_sources', row)
    require(writer.counts['news_article'] == 219662 and writer.counts['disclosure'] == 797,
            'Frozen document counts differ')
    require(writer.counts['company_relationship'] == 119 and writer.counts['relationship_evidence'] == 541,
            'Frozen relationship counts differ')
    require(writer.counts['relationship_score_current'] == 67 and writer.counts['company_metric_history'] == 606,
            'Frozen window counts differ')
    for table in TABLES + list(CONTEXTS):
        require(writer.counts[table] > 0, 'Missing mandatory payload table: ' + table)
    files = writer.finish()
    mappings.close()
    files['mapping'] = {'path': 'mapping.jsonl', 'sha256': sha(output / 'mapping.jsonl'),
                        'bytes': (output / 'mapping.jsonl').stat().st_size}
    require(sha(projection / 'projection.sqlite3') == manifest['projection_sha256'], 'Projection changed during payload build')
    result = {'schema_version': 'grounded-service-payload-1', 'status': 'PASS', 'run_id': manifest['run_id'],
              'snapshot_id': manifest['snapshot_id'], 'as_of': manifest['as_of'], 'publication_sha256': manifest['publication_sha256'],
              'projection_sha256': manifest['projection_sha256'], 'review_references_sha256': sha(references_path),
              'adapter_sha256': sha(Path(__file__)), 'created_at': admin_at, 'counts': dict(writer.counts),
              'identity_reuse': dict(reused), 'files': files, 'visible_news_company_pairs': 246117,
              'filing_observations': 800, 'unique_filing_documents': 797,
              'source_collection_time_policy': 'unknown_NULL_administrative_created_at_is_separate',
              'service_confidence_policy': 'NULL_uncalibrated_support_preserved_in_context',
              'relationship_explanation_policy': 'verified_graph_retrieval_typed_intro_literal_quotes_no_generation',
              'database_modified': False,
              'checks': {'typed_columns_and_lengths': True, 'canonical_identity_uniqueness': True,
                         'source_shard_hashes': True, 'relation_explanations_only_known_evidence': True,
                         'confidence_and_gnn_impact_not_fabricated': True}}
    with (output / 'manifest.json').open('x', encoding='utf8') as stream:
        stream.write(encoded(result) + '\n')
    print(encoded({'status': 'PASS', 'counts': result['counts'], 'identity_reuse': result['identity_reuse']}), flush=True)
    conn.close()
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--projection', type=Path, required=True)
    parser.add_argument('--references', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prepared', type=Path, default=HERE / 'artifacts/lake_grounded_20260919_v1/prepared_v1')
    parser.add_argument('--graph', type=Path, default=HERE / 'artifacts/kg_integrated_20260919_v3')
    args = parser.parse_args()
    run(args.projection, args.references, args.output, args.prepared, args.graph)
