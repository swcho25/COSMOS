"""Prepare a lossless, canonical service projection of a verified lake run.

This is a local adapter, not a database publisher. All source shards, exact
schemas and counts are checked again and bound to the HDFS verification receipt.
Canonical sentiment uses the same conservative revision policy as lake metrics:
disagreement in probabilities, status or availability remains NULL. FinBERT
probabilities are never presented as calibrated confidence or GNN price impact.

Original source edges, including quote offsets and attribution restrictions, are
retained separately. Relationship citations retain their exact source revision,
not a possibly different canonical article body. Unknown publication/collection
times and missing URLs stay unknown; the downstream DB adapter must explicitly
handle required-column constraints rather than inventing source observations.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import datetime as dt
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

from grounded_lake_features import normalize_canonical_mentions

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'spark'))
from publish_grounded_lake import validate_grounded


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf8'))


def validate_receipts(publication, digest, audit, receipt):
    counts = {d['name']: d['row_count'] for d in publication['datasets']}
    require(audit.get('status') == 'PASS' and receipt.get('status') == 'HDFS_VERIFIED_ONLY',
            'A successful HDFS receipt and independent audit are required')
    for obj in (audit, receipt):
        require(obj.get('run_id') == publication['run_id'], 'HDFS run ID mismatch')
        require(obj.get('publication_sha256') == digest, 'HDFS publication SHA mismatch')
        require(obj.get('dataset_count') == len(counts), 'HDFS dataset count mismatch')
    require(audit.get('counts') == counts, 'HDFS independent counts mismatch')
    require(audit.get('global_marker_reread') is True and audit.get('commit_metadata_reread') is True,
            'HDFS final commit was not independently checked')
    require(audit.get('zero_byte_data_files') == 0 and not receipt.get('sandbox'),
            'Empty HDFS files or sandbox-only publication')
    require({d['name']: d['row_count'] for d in receipt['datasets']} == counts,
            'HDFS receipt row counts mismatch')
    for dataset in receipt['datasets']:
        require(all(dataset['checks'].get(k) is True for k in
                    ('file_checksums', 'global_count', 'global_unique_nonnull_keys', 'shard_exact_content')),
                'Incomplete HDFS data verification')


def news_audit(row):
    url = row.get('source_url')
    issues = []
    if not url:
        issues.append('missing_original_url')
    else:
        try:
            valid = urlsplit(url).scheme in {'http', 'https'} and bool(urlsplit(url).hostname)
        except ValueError:
            valid = False
        if not valid:
            issues.append('invalid_original_url')
        if len(url) > 2000:
            issues.append('original_url_over_2000_chars')
        if len(url) > 2048:
            issues.append('original_url_over_2048_chars')
    if not row.get('published_at'):
        issues.append('publication_utc_unknown')
    if not row.get('published_date'):
        issues.append('publication_date_unknown')
    if not row.get('collected_at'):
        issues.append('collection_time_unknown')
    if len(row.get('title') or '') > 500:
        issues.append('title_over_500_chars')
    return issues


def mention_observation(row):
    edge = json.loads(row['source_edge_json'])
    sentiment = edge['sentiment']
    for field, source_field in [('sentiment_status', 'status'), ('sentiment_label', 'label'),
                                ('sentiment_score', 'score')]:
        require(row[field] == sentiment.get(source_field), 'Source sentiment differs from lake projection')
    require(edge['edge_id'] == row['edge_id'] and edge['ticker'] == row['stock_code'],
            'Source edge identity differs')
    return {'document_id': row['document_id'], 'ticker': row['stock_code'],
            'available_at': row['available_at'], 'sentiment_status': row['sentiment_status'],
            'sentiment_label': row['sentiment_label'], 'sentiment_score': row['sentiment_score'],
            'sentiment_probabilities': sentiment.get('probabilities')}


def canonicalize_mentions(observations, identities):
    canonical, report = normalize_canonical_mentions(observations)
    for row in canonical:
        refs = identities[(row['document_id'], row['ticker'])]
        require(len({x['market'] for x in refs}) == 1, 'Ambiguous canonical company market')
        row.update(market=refs[0]['market'], stock_code=row['ticker'],
                   source_edge_ids=[x['edge_id'] for x in refs],
                   source_revision_ids=sorted({x['source_revision_id'] for x in refs}),
                   sentiment_scope='target_grounded_context_financial_polarity',
                   probability_calibrated=False, is_stock_price_prediction=False,
                   impact_score=None, impact_direction=None)
    return canonical, report


def validate_quote(evidence, document):
    require(evidence['source_revision_id'] == document['source_revision_id'], 'Citation revision mismatch')
    text = document['text']
    require(hashlib.sha256(text.encode('utf8')).hexdigest() == document['text_sha256'],
            'Citation source text hash mismatch')
    start, end = evidence['quote_start'], evidence['quote_end']
    require(type(start) is int and type(end) is int and 0 <= start < end <= len(text),
            'Citation offsets out of range')
    require(text[start:end] == evidence['quote'], 'Citation quote does not match exact revision')


def run(source, verified, output):
    source, verified, output = map(lambda p: Path(p).resolve(), (source, verified, output))
    require(not output.exists(), 'Output already exists; use a new generation')
    publication = read(source / 'backfill_publication.json')
    digest = sha(source / 'backfill_publication.json')
    audit, receipt = read(verified / 'final_hdfs_audit.json'), read(verified / 'receipt.json')
    validate_receipts(publication, digest, audit, receipt)
    print('Validating every frozen source shard, exact schema and count', flush=True)
    publication, preflight = validate_grounded(source, publication['run_id'])
    require(preflight['publication_sha256'] == digest, 'Source publication changed')
    specs = {d['name']: d for d in publication['datasets']}

    def rows(name):
        for part in specs[name]['files']:
            with (source / part['local_file']).open(encoding='utf8') as stream:
                for line in stream:
                    yield json.loads(line)

    output.mkdir(parents=True)
    conn = sqlite3.connect(output / 'projection.sqlite3')
    conn.execute('PRAGMA journal_mode=DELETE')
    conn.execute('PRAGMA synchronous=FULL')
    conn.execute('PRAGMA cache_size=-65536')
    counts, issues = Counter(), Counter()
    table_keys = {
        'canonical_news': ['document_id', 'source_revision_id'],
        'canonical_company_mentions': ['document_id', 'ticker'],
        'mention_source_edges': ['edge_id', 'document_id', 'ticker', 'source_revision_id'],
        'relation_evidence': ['evidence_id', 'source_revision_id'],
        'historical_relations': ['relationship_key'],
        'relationship_scores': ['source_stock_code', 'target_stock_code', 'relationship_type_code', 'window_type'],
        'company_metrics': ['market', 'stock_code', 'window_type'],
        'company_features': ['market', 'stock_code', 'window_type'],
        'relationship_features': ['source_stock_code', 'target_stock_code', 'relationship_type_code', 'window_type'],
        'filing_mentions': ['document_id', 'ticker'],
        'companies': ['market', 'stock_code'],
        'evidence_documents': ['source_revision_id'],
    }
    primary_keys = {'canonical_news': ['document_id'], 'mention_source_edges': ['edge_id'],
                    'relation_evidence': ['evidence_id']}
    for name, keys in table_keys.items():
        fields = ','.join(k + ' TEXT NOT NULL' for k in keys)
        primary = ','.join(primary_keys.get(name, keys))
        conn.execute(f'CREATE TABLE {name} ({fields},row_json TEXT NOT NULL,PRIMARY KEY({primary}))')
    conn.execute('CREATE TABLE news_issues(document_id TEXT NOT NULL,issue TEXT NOT NULL,PRIMARY KEY(document_id,issue))')

    def insert(name, row):
        keys = table_keys[name]
        values = [row[k] for k in keys] + [encoded(row)]
        conn.execute(f'INSERT INTO {name} VALUES ({",".join("?" for _ in values)})', values)
        counts[name] += 1

    print('Projecting canonical news and original company evidence', flush=True)
    news_ids = set()
    for row in rows('cleaned_news'):
        news_ids.add(row['document_id'])
        insert('canonical_news', row)
        for issue in news_audit(row):
            issues[issue] += 1
            conn.execute('INSERT INTO news_issues VALUES (?,?)', (row['document_id'], issue))
    conn.commit()
    observations, identities = [], defaultdict(list)
    sentiment_signatures = {}
    for row in rows('news_mentions'):
        require(row['document_id'] in news_ids, 'Mention references unknown canonical news')
        observation = mention_observation(row)
        observations.append(observation)
        row['ticker'] = row['stock_code']
        insert('mention_source_edges', row)
        identities[(row['document_id'], row['ticker'])].append(
            {k: row[k] for k in ('market', 'edge_id', 'source_revision_id')})
        sentiment_signatures[row['edge_id']] = encoded(observation)
    conn.commit()
    for row in rows('news_sentiment'):
        observation = {'document_id': row['document_id'], 'ticker': row['stock_code'],
                       'available_at': row['available_at'], **{k: row[k] for k in
                       ('sentiment_status', 'sentiment_label', 'sentiment_score')}}
        observation['sentiment_probabilities'] = ({k: row[k + '_probability'] for k in
                                                 ('positive', 'negative', 'neutral')}
                                                if row['sentiment_status'] == 'analyzed' else None)
        require(sentiment_signatures.pop(row['edge_id'], None) == encoded(observation),
                'Sentiment dataset disagrees with original edge')
    require(not sentiment_signatures, 'Source sentiment rows missing')
    canonical, revision_report = canonicalize_mentions(observations, identities)
    for row in canonical:
        insert('canonical_company_mentions', row)
    status_counts = Counter(r['sentiment_status'] for r in canonical)
    label_counts = Counter(r['sentiment_label'] or 'NULL' for r in canonical)
    del observations, identities, canonical, sentiment_signatures
    conn.execute('CREATE INDEX source_edges_company_doc ON mention_source_edges(document_id,ticker)')
    conn.commit()

    print('Projecting relationships, source revisions, metrics and disclosure observations', flush=True)
    evidence = list(rows('graph_evidence'))
    by_revision = defaultdict(list)
    for row in evidence:
        insert('relation_evidence', row)
        by_revision[row['source_revision_id']].append(row)
    seen_revisions = set()
    for row in rows('graph_documents'):
        if row['source_revision_id'] in by_revision:
            for item in by_revision[row['source_revision_id']]:
                validate_quote(item, row)
            insert('evidence_documents', row)
            seen_revisions.add(row['source_revision_id'])
    require(seen_revisions == set(by_revision), 'Missing exact relationship citation document')
    for dataset, table in [('graph_relationships', 'historical_relations'),
                           ('relationship_scores', 'relationship_scores'), ('company_metrics', 'company_metrics'),
                           ('company_features', 'company_features'), ('relationship_features', 'relationship_features'),
                           ('disclosure_mentions', 'filing_mentions'), ('graph_nodes', 'companies')]:
        for row in rows(dataset):
            insert(table, row)
    conn.commit()
    require(conn.execute('PRAGMA integrity_check').fetchone()[0] == 'ok', 'SQLite integrity check failed')
    actual_counts = {name: conn.execute(f'SELECT COUNT(*) FROM {name}').fetchone()[0] for name in counts}
    require(actual_counts == dict(counts), 'Projection count mismatch')
    conn.close()
    # Inputs were read after the preflight too. Bind the finished result only if
    # every source part is still the same, so a concurrent edit cannot slip in.
    for spec in specs.values():
        for part in spec['files']:
            require(sha(source / part['local_file']) == part['sha256'], 'Frozen source changed during projection')
    require(sha(source / 'backfill_publication.json') == digest, 'Publication changed during projection')
    report = {'schema_version': 'grounded-service-projection-1', 'status': 'PASS',
              'run_id': publication['run_id'], 'snapshot_id': publication['snapshot_id'],
              'as_of': publication['as_of'], 'prepared_at': dt.datetime.now(dt.timezone.utc).isoformat(),
              'publication_sha256': digest, 'source_manifest_sha256': publication['source_manifest_sha256'],
              'hdfs_audit_sha256': sha(verified / 'final_hdfs_audit.json'),
              'hdfs_receipt_sha256': sha(verified / 'receipt.json'),
              'source_shards_verified': sum(len(d['files']) for d in publication['datasets']),
              'source_datasets': preflight['datasets'], 'counts': actual_counts,
              'canonical_revision_audit': revision_report, 'sentiment_status_counts': dict(status_counts),
              'sentiment_label_counts': dict(label_counts), 'news_metadata_issues': dict(issues),
              'projection_file': 'projection.sqlite3', 'projection_sha256': sha(output / 'projection.sqlite3'),
              'projection_bytes': (output / 'projection.sqlite3').stat().st_size,
              'adapter_sha256': sha(Path(__file__)), 'database_modified': False,
              'checks': {'all_source_hashes_schemas_counts': True, 'hdfs_receipt_bound': True,
                         'canonical_mention_conflicts_preserved': True, 'sentiment_dataset_exact_match': True,
                         'all_relationship_quotes_exact_revision': True, 'sqlite_integrity': True,
                         'no_synthetic_source_timestamps': True, 'no_gnn_impact_from_finbert': True},
              'limitations': publication['limitations'] + [
                  'Service DB identity mapping and required-column compatibility must be handled by the publisher.',
                  'Source edges are complete audit records; their probabilities are uncalibrated sentiment only.']}
    with (output / 'manifest.json').open('x', encoding='utf8') as stream:
        stream.write(encoded(report) + '\n')
    print(encoded({k: report[k] for k in ('status', 'counts', 'canonical_revision_audit', 'news_metadata_issues')}), flush=True)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--verified', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run(args.source, args.verified, args.output)


if __name__ == '__main__':
    main()
