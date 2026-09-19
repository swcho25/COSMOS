"""Export frozen grounded news and reviewed relations as a new HDFS publication.

This adapter never reruns the selector, sentiment model, relation extractor or
GNN. Original article revisions remain separate from canonical document IDs so
deduplication cannot redirect a citation to another text. All source decisions,
unknown sentiment states and raw timestamps remain available in provenance.
It writes a new local generation only; Spark publication and DB serving are
separate actions. A completion marker is written after local validation.
"""
from __future__ import annotations

import argparse
from collections import Counter
import csv
import datetime as dt
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import uuid

from integrated_relation_graph import available_time, validate_evidence
from lake_news_normalize import canonicalize_url


HERE = Path(__file__).resolve().parent
PRIOR_SHA = '5bcd1b61aaf31ea811f9b450cfdbf4a75611b9ff9ac00ed97e00891c51c54783'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(',', ':'))


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def rows(path):
    with Path(path).open(encoding='utf8') as stream:
        for line in stream:
            if line.strip():
                yield json.loads(line)


def save(path, value):
    with Path(path).open('x', encoding='utf8', newline='\n') as stream:
        stream.write(encoded(value) + '\n')


def source_file(directory, manifest, key):
    spec = manifest['files'][key]
    path = (directory / spec['path']).resolve()
    require(path.is_relative_to(directory.resolve()), 'Source path escapes artifact')
    require(sha(path) == spec['sha256'], 'Frozen source changed: ' + key)
    return path


def publication_time(raw, precision=None, audit=None):
    value = raw[:10] if precision == 'date' and raw else raw
    if audit is not None:
        if not audit.get('usable_day'):
            return dict(published_at=None, published_at_raw=raw, published_date=audit.get('published_date'),
                        available_at=None, availability_policy='source_date_audit_unusable')
        if audit.get('status') == 'explicit_source_time' and audit.get('published_at_utc'):
            value = audit['published_at_utc']
        elif audit.get('published_date'):
            value = audit['published_date']
    try:
        available, policy = available_time(value)
    except (ValueError, TypeError):
        return dict(published_at=None, published_at_raw=raw, published_date=None,
                    available_at=None, availability_policy='unknown_excluded_from_windows')
    return dict(published_at=available if policy == 'timezone_aware_publication' else None,
                published_at_raw=raw, published_date=value[:10], available_at=available,
                availability_policy=policy)


def primitive_schema(fields):
    return {'type': 'struct', 'fields': [dict(name=k, type=v, nullable=True, metadata={})
                                       for k, v in sorted(fields.items())]}


def infer_flat_schema(records):
    fields = {}
    for row in records:
        for key, value in row.items():
            if isinstance(value, (dict, list)):
                raise ValueError('Nested fields require explicit JSON encoding: ' + key)
            if value is None:
                continue
            kind = 'boolean' if isinstance(value, bool) else 'long' if isinstance(value, int) else 'double' if isinstance(value, float) else 'string'
            previous = fields.get(key)
            require(previous in (None, kind) or {previous, kind} == {'long', 'double'}, 'Mixed types: ' + key)
            fields[key] = 'double' if 'double' in (previous, kind) else kind
    for row in records:
        for key in row:
            if key not in fields:
                fields[key] = 'double' if key.endswith(('_score', '_confidence', '_probability', '_return', '_volatility', '_correlation', '_close')) or key in {'score', 'confidence', 'weight'} else 'long' if key.endswith('_count') else 'string'
    return fields


class DatasetWriter:
    """Bounded JSONL shards with explicit field set, scalar types and hashes."""
    def __init__(self, root, name, target, keys, fields, path='data', shard_bytes=48 * 1024 * 1024):
        self.root, self.name, self.fields = root, name, fields
        self.spec = dict(name=name, target_root=target, dataset_path=path,
                         schema=primitive_schema(fields), key_columns=keys, row_count=0, files=[])
        self.limit = shard_bytes
        self.stream = None
        self.keys = set()
        self.open()

    def open(self):
        self.path = self.root / 'parts' / f'{self.name}_{len(self.spec["files"]):05d}.jsonl'
        self.stream = self.path.open('xb')
        self.digest, self.count, self.size = hashlib.sha256(), 0, 0

    def add(self, row):
        require(not set(row) - set(self.fields), 'Unknown fields: ' + self.name)
        row = {k: row.get(k) for k in self.fields}
        identity = tuple(row[k] for k in self.spec['key_columns'])
        require(all(x is not None for x in identity), 'NULL key: ' + self.name)
        require(identity not in self.keys, 'Duplicate key: ' + self.name + str(identity))
        self.keys.add(identity)
        line = (encoded(row) + '\n').encode('utf8')
        if self.size and self.size + len(line) > self.limit:
            self.close_part()
            self.open()
        self.stream.write(line)
        self.digest.update(line)
        self.size += len(line)
        self.count += 1

    def close_part(self):
        self.stream.close()
        self.spec['files'].append(dict(local_file=self.path.relative_to(self.root).as_posix(),
            sha256=self.digest.hexdigest(), row_count=self.count, bytes=self.size))
        self.spec['row_count'] += self.count
        self.stream = None

    def finish(self):
        if self.stream:
            self.close_part()
        return self.spec


TIME_FIELDS = {k: 'string' for k in ('published_at', 'published_at_raw', 'published_date', 'available_at', 'availability_policy')}


def assert_span(text, start, end, quote, label):
    require(type(start) is int and type(end) is int and 0 <= start < end <= len(text)
            and text[start:end] == quote, 'Source quote mismatch: ' + label)


def validate_company_edge(edge, article):
    require(edge['article_id'] == article['article_id'], 'Edge source article mismatch')
    original_sha = hashlib.sha256(encoded({k: article[k] for k in ('title', 'body')}).encode('utf8')).hexdigest()
    cleaned_sha = hashlib.sha256(encoded({k: article['cleaned_' + k] for k in ('title', 'body')}).encode('utf8')).hexdigest()
    require(article['cleaned_text_sha256'] == cleaned_sha and edge['cleaned_text_sha256'] == cleaned_sha,
            'Actual cleaned fields differ from source hash')
    require(article['cleaning_audit']['original_text_sha256'] == original_sha,
            'Actual original fields differ from source hash')
    evidence_keys = {}
    for item in edge['evidence']:
        field = item['source']
        require(field in {'title', 'body'}, 'Unknown quote field')
        assert_span(article[field], item['start'], item['end'], item['quote'], edge['edge_id'])
        assert_span(article['cleaned_' + field], item['cleaned_start'], item['cleaned_end'], item['cleaned_quote'], edge['edge_id'])
        evidence_keys[(field, item['start'], item['end'], item['quote'], item['cleaned_quote'])] = item
    sentiment = edge['sentiment']
    require(sentiment['ticker'] == edge['ticker'] and sentiment['article_id'] == edge['article_id'], 'Sentiment identity mismatch')
    require(sentiment['cleaned_content_sha256'] == cleaned_sha and sentiment['source_content_sha256'] == original_sha,
            'Sentiment original/cleaned source differs')
    require(sentiment['input_sha256'] == edge['source_input_sha256'], 'Sentiment source input identity differs')
    contexts, unique_contexts = sentiment.get('contexts', []), set()
    def probabilities(value, label):
        require(isinstance(value, dict) and set(value) == {'positive', 'negative', 'neutral'}, 'Missing probabilities: ' + label)
        require(all(type(v) in (float, int) and math.isfinite(v) and 0 <= v <= 1 for v in value.values()), 'Invalid probabilities: ' + label)
        require(abs(sum(value.values()) - 1) <= 1e-5, 'Probability sum differs: ' + label)
        winners = [k for k, v in value.items() if v == max(value.values())]
        return winners[0].upper() if len(winners) == 1 else None
    for context in contexts:
        field = context['field']
        require(field in {'title', 'body'}, 'Unknown sentiment field')
        assert_span(article[field], context['start'], context['end'], context['quote'], 'sentiment original')
        context_key = (field, context['start'], context['end'], context['quote'], context['actual_input'])
        require(context_key in evidence_keys, 'Sentiment context not grounded in this edge evidence')
        require(context_key not in unique_contexts, 'Repeated sentiment context')
        unique_contexts.add(context_key)
        # Frozen v1 contexts predate source_validation. Their exact matching
        # edge evidence still supplies verified cleaned offsets; no model
        # rerun or fabricated validation metadata is added to the source.
        evidence_item = evidence_keys[context_key]
        assert_span(article['cleaned_' + field], evidence_item['cleaned_start'], evidence_item['cleaned_end'], context['actual_input'], 'sentiment cleaned')
        validation = context.get('source_validation')
        if validation is not None:
            require(validation['method'] in {'full_article_cleaned_slice_and_original_offset_map_v1', 'legacy_excerpt_cleanup_substring_v1'},
                    'Unknown sentiment source validation')
            if validation['method'] == 'full_article_cleaned_slice_and_original_offset_map_v1':
                require(validation['cleaned_start'] == evidence_item['cleaned_start']
                        and validation['cleaned_end'] == evidence_item['cleaned_end'], 'Sentiment cleaned offsets differ from evidence')
        require(context['input_sha256'] == hashlib.sha256(context['actual_input'].encode('utf8')).hexdigest(), 'Sentiment context input hash differs')
        if context.get('status') == 'analyzed':
            require(context['label'] == probabilities(context.get('probabilities'), 'context'), 'Sentiment context label differs')
    if sentiment['status'] == 'analyzed':
        require(sentiment.get('label') in {'POSITIVE', 'NEGATIVE', 'NEUTRAL'}, 'Unknown sentiment label')
        require(contexts and all(x['status'] == 'analyzed' for x in contexts), 'Analyzed aggregate contains deferred contexts')
        require(not {'POSITIVE', 'NEGATIVE'}.issubset({x['label'] for x in contexts}), 'Conflicting contexts must remain deferred')
        values = sentiment.get('probabilities')
        require(sentiment['label'] == probabilities(values, 'aggregate'), 'Aggregate sentiment label differs')
        for key, value in values.items():
            require(abs(value - sum(x['probabilities'][key] for x in contexts) / len(contexts)) < 1e-8, 'Aggregate probabilities differ from contexts')
        require(type(sentiment.get('score')) in (float, int) and math.isfinite(sentiment['score'])
                and abs(sentiment['score'] - (values['positive'] - values['negative'])) < 1e-8,
                'Sentiment score differs from positive minus negative probability')
        require(abs(sentiment['model_max_probability'] - max(values.values())) < 1e-8, 'Sentiment model maximum differs')
    else:
        require(all(sentiment.get(k) is None for k in ('label', 'score', 'probabilities', 'model_max_probability')),
                'Deferred sentiment must remain NULL')


def build(args):
    from grounded_lake_identity import GroundedIdentityIndex
    from grounded_lake_features import (build_grounded_features, load_verified_price_snapshot,
                                       normalize_canonical_mentions)
    out = args.out.resolve()
    require(not out.exists(), 'Use a new local preparation directory; incomplete runs are retained')
    (out / 'parts').mkdir(parents=True)
    kg, graph, disclosures = args.kg.resolve(), args.graph.resolve(), args.disclosures.resolve()
    parent, integrated, filing_manifest = (read(p / 'manifest.json') for p in (kg, graph, disclosures))
    require(all(m.get('artifact_complete') is True for m in (parent, integrated, filing_manifest)), 'Incomplete input')
    require(integrated['provenance']['parent_kg_manifest_sha256'] == sha(kg / 'manifest.json'), 'Integrated graph parent differs')
    require(integrated['provenance']['disclosure_manifest_sha256'] == sha(disclosures / 'manifest.json'), 'Disclosure lineage differs')
    require(sha(args.prior_map) == PRIOR_SHA, 'Existing canonical ID map changed')
    print(encoded({'stage': 'checking_frozen_sources'}), flush=True)
    news_path = source_file(kg, parent, 'news_nodes')
    edges_path = source_file(kg, parent, 'news_company_edges')
    integrated_db = source_file(graph, integrated, 'database')
    temporal_path = source_file(graph, integrated, 'temporal_evidence')
    expected_path = source_file(graph, integrated, 'relationship_scores')
    require(sha(kg / 'state.sqlite3') == parent['state_sqlite_sha256'], 'Frozen news database changed')
    companies_path = HERE.parent / 'ner/data/company_industry.csv'
    require(sha(companies_path) == integrated['provenance']['company_dictionary_sha256'], 'Company dictionary differs')
    with companies_path.open(encoding='utf-8-sig', newline='') as stream:
        companies = list(csv.DictReader(stream))
    markets = {r['ticker']: r['market'] for r in companies}
    source_db = sqlite3.connect((kg / 'state.sqlite3').as_uri() + '?mode=ro&immutable=1', uri=True)
    relation_db = sqlite3.connect(integrated_db.as_uri() + '?mode=ro&immutable=1', uri=True)

    @lru_cache(maxsize=128)
    def article(article_id):
        value = source_db.execute('SELECT payload FROM articles WHERE article_id=?', (article_id,)).fetchone()
        require(value is not None, 'Missing original article')
        return json.loads(value[0])

    identity = GroundedIdentityIndex(args.identity.resolve())
    # Identity source bindings are checked against the current frozen inputs.
    identity_receipt = read(args.identity.with_suffix('.receipt.json'))
    require(identity_receipt['parent_manifest_sha256'] == sha(kg / 'manifest.json')
            and identity_receipt['integrated_manifest_sha256'] == sha(graph / 'manifest.json')
            and identity_receipt['prior_map_sha256'] == PRIOR_SHA
            and identity_receipt['identity_sha256'] == sha(args.identity), 'Identity index lineage differs')
    as_of = integrated['as_of']
    provenance = dict(parent_manifest_sha256=sha(kg / 'manifest.json'),
        integrated_manifest_sha256=sha(graph / 'manifest.json'),
        disclosure_manifest_sha256=sha(disclosures / 'manifest.json'),
        prior_document_id_map_sha256=PRIOR_SHA, identity_sha256=sha(args.identity))
    bound_inputs = {str(p.resolve()):sha(p) for p in [kg/'manifest.json',kg/'protocol.json',
        kg/'state.sqlite3',news_path,edges_path,graph/'manifest.json',integrated_db,temporal_path,
        expected_path,disclosures/'manifest.json',companies_path,args.prior_map,args.identity,
        args.identity.with_suffix('.receipt.json'),HERE/'data/prices_daily.parquet',HERE/'data/prices_manifest.json']}
    require(bound_inputs[str((kg/'protocol.json').resolve())] == parent['protocol_sha256'], 'Parent protocol differs')
    for key in ['disclosure_documents','sec_company_observations']:
        path = source_file(disclosures,filing_manifest,key)
        bound_inputs[str(path)] = sha(path)
    specs = []
    def writer(name, target, keys, fields, path='data'):
        return DatasetWriter(out, name, target, keys, fields, path)
    text_fields = {k: 'string' for k in ['document_id','source_revision_id','record_id','title','body','source_url','canonical_url','cleaned_text_sha256','date_audit_json','cleaning_audit_json']}
    clean = writer('cleaned_news', '/data-lake/cleaned/news', ['document_id'], {**text_fields, **TIME_FIELDS, 'duplicate_count': 'long', 'scope': 'string'})
    graph_documents = writer('graph_documents', '/data-lake/snapshots/graph', ['source_revision_id'],
        {**{k:'string' for k in ['document_id','source_revision_id','record_id','source_kind','source_url','title','text','text_sha256','source_metadata_json']}, **TIME_FIELDS, 'base_news_candidate':'boolean'}, 'documents')
    seen_articles, seen_revisions, canonical_documents = set(), set(), set()
    def emit_news(node, base):
        aid = node['article_id']
        if aid in seen_articles:
            return
        seen_articles.add(aid)
        mapping = identity.get(aid)
        require(mapping is not None and mapping.get('document_id'), 'Unresolved canonical news ID')
        document_id, revision_id = mapping['document_id'], mapping['source_revision_id']
        clock = publication_time(node.get('published_at'),audit=node.get('date_audit'))
        original = node['title'] + '\n' + node['body']
        digest = hashlib.sha256(original.encode('utf8')).hexdigest()
        require(digest == mapping['original_text_sha256'], 'Revision original hash mismatch')
        graph_documents.add(dict(document_id=document_id,source_revision_id=revision_id,record_id=aid,
            source_kind='NEWS',source_url=node.get('source_url'),title=node['title'],text=original,text_sha256=digest,
            source_metadata_json=encoded({k:node.get(k) for k in ['date_audit','cleaning_audit','scope','production_approved']}),base_news_candidate=base,**clock))
        seen_revisions.add(revision_id)
        if mapping['representative_article_id'] == aid:
            canonical_documents.add(document_id)
            clean.add(dict(document_id=document_id,source_revision_id=revision_id,record_id=aid,
                title=node['cleaned_title'],body=node['cleaned_body'],source_url=node.get('source_url'),
                canonical_url=canonicalize_url(node.get('source_url')),cleaned_text_sha256=node['cleaned_text_sha256'],
                date_audit_json=encoded(node.get('date_audit')),cleaning_audit_json=encoded(node.get('cleaning_audit')),
                duplicate_count=mapping['duplicate_count'],scope='grounded_selected_and_reviewed_relation_sources',**clock))
    for n, node in enumerate(rows(news_path), 1):
        emit_news(node, True)
        if n % 50000 == 0:
            print(encoded({'stage':'news_export','rows':n}), flush=True)
    base_news_count = len(seen_articles)
    evidence_rows = list(rows(temporal_path))
    availability = {r['document_id']:r['available_at'] for r in evidence_rows}
    precision_by_document = {}
    for row in evidence_rows:
        item = (row['available_at'], row.get('published_at_precision'))
        require(row['document_id'] not in precision_by_document or precision_by_document[row['document_id']] == item,
                'One relation document has inconsistent publication precision')
        precision_by_document[row['document_id']] = item
    integrated_documents = {}
    for document_id, payload in relation_db.execute('SELECT document_id,payload FROM documents ORDER BY document_id'):
        doc = json.loads(payload)
        if doc['source_kind'] == 'NEWS':
            node = article(doc['record_id'])
            require(doc['text'] == node['title'] + '\n' + node['body'], 'Integrated news revision differs')
            emit_news(node, doc['record_id'] in seen_articles)
            mapping = identity.get(doc['record_id'])
            integrated_documents[document_id] = (mapping['document_id'], mapping['source_revision_id'])
        else:
            revision_id = 'disclosure-revision:' + hashlib.sha256((document_id+'\n'+doc['text_sha256']).encode()).hexdigest()
            normalized_id = 'disclosure:' + doc['record_id']
            require(hashlib.sha256(doc['text'].encode()).hexdigest() == doc['text_sha256'], 'Disclosure source hash differs')
            clock = publication_time(doc['published_at'],precision_by_document[document_id][1])
            require(clock['available_at'] == availability[document_id], 'Disclosure availability projection differs')
            graph_documents.add(dict(document_id=normalized_id,source_revision_id=revision_id,record_id=doc['record_id'],
                source_kind='DISCLOSURE',source_url=doc['source_url'],title=doc['title'],text=doc['text'],text_sha256=doc['text_sha256'],
                source_metadata_json=encoded({k:v for k,v in doc.items() if k != 'text'}),base_news_candidate=False,**clock))
            seen_revisions.add(revision_id)
            integrated_documents[document_id] = (normalized_id, revision_id)
    specs.extend([clean.finish(), graph_documents.finish()])
    mention_fields = {k:'string' for k in ['edge_id','document_id','source_revision_id','article_id','market','stock_code','company_name','source_url','sentiment_label','sentiment_status','source_edge_json']}
    mention_fields.update(TIME_FIELDS)
    mention_fields.update(confidence='double',sentiment_score='double',economic_relevance_validated='boolean',price_effect_established='boolean')
    mentions_out = writer('news_mentions','/data-lake/analyzed/news/company-mentions',['edge_id'], mention_fields)
    sentiments_out = writer('news_sentiment','/data-lake/analyzed/news/sentiment',['edge_id'],
        {k:v for k,v in mention_fields.items() if k not in {'source_edge_json','confidence','economic_relevance_validated','price_effect_established'}} |
        {'positive_probability':'double','negative_probability':'double','neutral_probability':'double','sentiment_json':'string'})
    mentions, sentiment_counts = [], Counter()
    for n, edge in enumerate(rows(edges_path), 1):
        aid, ticker = edge['article_id'], edge['ticker']
        require(aid in seen_articles and ticker in markets, 'Mention foreign key missing')
        node = article(aid)
        validate_company_edge(edge, node)
        mapping = identity.get(aid)
        require(mapping['document_id'] in canonical_documents, 'Mention has no canonical news document')
        sentiment = edge['sentiment']; status = sentiment['status']
        clock = publication_time(edge.get('published_at'),audit=edge.get('date_audit'))
        common = dict(edge_id=edge['edge_id'],document_id=mapping['document_id'],source_revision_id=mapping['source_revision_id'],
            article_id=aid,market=markets[ticker],stock_code=ticker,company_name=edge['company_name'],source_url=edge.get('source_url'),
            sentiment_label=sentiment.get('label'),sentiment_score=sentiment.get('score'),sentiment_status=status,**clock)
        mentions_out.add(dict(**common,confidence=None,economic_relevance_validated=False,price_effect_established=False,source_edge_json=encoded(edge)))
        probabilities = sentiment.get('probabilities') or {}
        sentiments_out.add(dict(**common,positive_probability=probabilities.get('positive'),negative_probability=probabilities.get('negative'),
            neutral_probability=probabilities.get('neutral'),sentiment_json=encoded(sentiment)))
        mentions.append(dict(document_id=mapping['document_id'],ticker=ticker,available_at=clock['available_at'],
            sentiment_status=status,sentiment_label=sentiment.get('label'),sentiment_score=sentiment.get('score'),sentiment_probabilities=sentiment.get('probabilities')))
        sentiment_counts[status] += 1
        if n % 50000 == 0:
            print(encoded({'stage':'mention_export_and_full_quote_validation','rows':n}), flush=True)
    specs.extend([mentions_out.finish(), sentiments_out.finish()])
    require(specs[-2]['row_count'] == parent['files']['news_company_edges']['rows'], 'Mention row count changed')
    relation_fields = {k:'string' for k in ['evidence_id','document_id','source_revision_id','source_document_id','record_id','source_kind','source_market','target_market','src_ticker','dst_ticker','rel_type','quote','offset_basis','source_url','source_evidence_json']}
    relation_fields.update(TIME_FIELDS)
    relation_fields.update(quote_start='long',quote_end='long',support_weight='double',directed='boolean',price_effect_established='boolean')
    relation_outputs = {kind:writer(name, target, ['evidence_id'],relation_fields,path) for kind,name,target,path in [
        ('NEWS','news_relationships','/data-lake/analyzed/news/relationships','data'),
        ('DISCLOSURE','disclosure_relationships','/data-lake/analyzed/disclosures/relationships','data'),
        ('ALL','graph_evidence','/data-lake/snapshots/graph','evidence')]}
    normalized_relations = []
    for row in evidence_rows:
        doc = json.loads(relation_db.execute('SELECT payload FROM documents WHERE document_id=?',(row['document_id'],)).fetchone()[0])
        validate_evidence(row, doc)
        canonical, revision = integrated_documents[row['document_id']]
        require(revision in seen_revisions and all(row[x] in markets for x in ['src_ticker','dst_ticker']), 'Relation foreign key missing')
        projected = {k:row.get(k) for k in relation_fields}
        relation_clock = publication_time(row['published_at'],row.get('published_at_precision'))
        require(relation_clock['available_at'] == row['available_at'], 'Relation availability projection differs')
        projected.update(document_id=canonical,source_revision_id=revision,source_document_id=row['document_id'],
            source_market=markets[row['src_ticker']],target_market=markets[row['dst_ticker']],source_evidence_json=encoded(row),**relation_clock)
        relation_outputs[row['source_kind']].add(projected)
        relation_outputs['ALL'].add(projected)
        # Preserve frozen article/report support identities; no event-count claim.
        normalized_relations.append({**row,'canonical_document_id':canonical})
    specs.extend(w.finish() for w in relation_outputs.values())
    filing_fields = {k:'string' for k in ['document_id','source','ticker','market','source_url','report_id','source_document_json']}
    filing_fields.update(TIME_FIELDS)
    filing_out = writer('disclosure_mentions','/data-lake/analyzed/disclosures/company-mentions',['document_id','ticker'],filing_fields)
    filing_mentions = []
    for key in ['disclosure_documents','sec_company_observations']:
        for doc in rows(source_file(disclosures, filing_manifest,key)):
            ticker = doc['ticker']; require(ticker in markets,'Disclosure company not in universe')
            clock = publication_time(doc.get('published_at'),doc.get('published_at_precision'))
            document_id = 'disclosure:' + doc['document_id']
            filing_out.add(dict(document_id=document_id,source=doc['source'],ticker=ticker,market=markets[ticker],
                source_url=doc.get('source_url'),report_id=doc.get('report_id'),source_document_json=encoded(doc),**clock))
            filing_mentions.append(dict(document_id=document_id,ticker=ticker,available_at=clock['available_at']))
    specs.append(filing_out.finish())
    prices, price_provenance = load_verified_price_snapshot(HERE/'data/prices_daily.parquet', HERE/'data/prices_manifest.json', companies, as_of)
    provenance['price_provenance_json'] = encoded(price_provenance)
    mentions, duplicate_mention_report = normalize_canonical_mentions(mentions)
    features = build_grounded_features(companies,mentions,normalized_relations,filing_mentions,prices,as_of,
        run_id=args.run_id,provenance=provenance,expected_scores=list(rows(expected_path)))
    targets = {'company_features':('/data-lake/features/company','data',['market','stock_code','window_type']),
        'company_metrics':('/data-lake/aggregated/company-metrics','data',['market','stock_code','window_type']),
        'relationship_features':('/data-lake/features/relationship','data',['source_stock_code','target_stock_code','relationship_type_code','window_type']),
        'relationship_scores':('/data-lake/aggregated/relationship-scores','data',['source_stock_code','target_stock_code','relationship_type_code','window_type']),
        'graph_nodes':('/data-lake/snapshots/graph','nodes',['market','stock_code']),
        'graph_edges':('/data-lake/snapshots/graph','edges',['relationship_key','window_type']),
        'graph_relationships':('/data-lake/snapshots/graph','historical_relationships',['relationship_key'])}
    for name, values in features['datasets'].items():
        target,path,keys = targets[name]
        fields = infer_flat_schema(values)
        destination = writer(name,target,keys,fields,path)
        for row in values:
            destination.add(row)
        specs.append(destination.finish())
    id_map_path = out/'identity_map.jsonl'
    identity.export_id_map(id_map_path)
    id_writer = writer('document_id_map','/data-lake/metadata/id-maps/news',['identity_key'],
        {k:'string' for k in ['identity_key','document_id','representative_record_id','representative_source_dataset','available_at']})
    for row in rows(id_map_path):
        id_writer.add(row)
    specs.append(id_writer.finish())
    print(encoded({'stage':'rechecking_all_frozen_input_hashes'}),flush=True)
    for path,digest in bound_inputs.items():
        require(sha(path) == digest, 'Source changed while exporting: '+path)
    require(base_news_count == parent['files']['news_nodes']['rows'], 'Base news row count changed')
    require(len(canonical_documents) == identity.summary['representative_count'], 'Canonical representative count differs')
    require(len(seen_articles) == identity.summary['unique_source_records'], 'Original revision count differs')
    source_manifest = dict(schema='grounded-lake-sources-1',run_id=args.run_id,as_of=as_of,
        sources=provenance,parent_manifest=parent,integrated_manifest=integrated,
        identity_receipt=identity_receipt,price_provenance=price_provenance,bound_inputs=bound_inputs,
        builder_sha256=sha(Path(__file__)),feature_code_sha256=sha(HERE/'grounded_lake_features.py'),
        limitations=['Fixed retrospective partial-source snapshot, not all news or latest live data.',
            'Candidate news relevance is provisional; no human-gold accuracy guarantee.',
            'Target context sentiment is not a GNN stock-price prediction.',
            'Historical relationships do not prove current contract validity.',
            'The earlier HDFS run and service database are unchanged.'])
    save(out/'source_manifest.json',source_manifest)
    summary = dict(status='PASS',source_manifest_sha256=sha(out/'source_manifest.json'),
        checks={k:True for k in ['source_lineage','canonical_document_ids','foreign_keys','evidence_quotes','window_aggregates','null_semantics']},
        base_news_revisions=base_news_count,total_news_revisions=len(seen_articles),canonical_news=len(canonical_documents),
        original_news_company_edges=specs[2]['row_count'],sentiment_counts=dict(sentiment_counts),
        relation_evidence=len(evidence_rows),features=features['coverage'],identity=identity_receipt,
        canonical_mention_deduplication=duplicate_mention_report,
        dataset_counts={d['name']:d['row_count'] for d in specs})
    save(out/'grounded_validation.json',summary)
    publication = dict(schema_version='lake-backfill-publication-1',run_id=args.run_id,as_of=as_of,
        snapshot_id=features['snapshot_id'],source_manifest_sha256=sha(out/'source_manifest.json'),datasets=specs,
        grounded_contract=dict(schema_version='grounded-lake-1',validation_file='grounded_validation.json',validation_sha256=sha(out/'grounded_validation.json')),
        limitations=source_manifest['limitations'])
    save(out/'backfill_publication.json',publication)
    save(out/'finish.done.json',dict(publication_sha256=sha(out/'backfill_publication.json'),status='LOCAL_VALIDATED_NOT_PUBLISHED'))
    source_db.close(); relation_db.close()
    print(encoded({'stage':'local_preparation_complete','summary':summary}),flush=True)
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--kg',type=Path,default=HERE/'artifacts/kg_grounded_full_20260918_v1')
    parser.add_argument('--graph',type=Path,default=HERE/'artifacts/kg_integrated_20260919_v3')
    parser.add_argument('--disclosures',type=Path,default=HERE/'artifacts/kg_disclosures_20260919_v1/result')
    parser.add_argument('--identity',type=Path,required=True)
    parser.add_argument('--prior-map',type=Path,required=True)
    parser.add_argument('--out',type=Path,required=True)
    parser.add_argument('--run-id',required=True)
    build(parser.parse_args())
