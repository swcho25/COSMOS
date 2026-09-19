"""Refresh SQLite planner statistics in a PAUSED, unfinished local KG output.

The builder code/model/protocol and all logical rows remain unchanged. This
maintenance only issues ANALYZE for existing pairs/articles indexes, after
acquiring the builder lock. It records before/after query plans, checkpoint
counters, DB hashes, and a bounded query-output equivalence check. Never run
against the immutable merged input or a completed, checksummed output.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sqlite3
import time

import build_grounded_kg as build

QUERY = "SELECT payload FROM pairs WHERE article_id=? AND decision='candidate_ready'"


def snapshot(db):
    return {
        'meta':dict(db.execute('SELECT key,value FROM meta ORDER BY key')),
        'counts':{name:db.execute('SELECT COUNT(*) FROM '+name).fetchone()[0]
                  for name in ('articles','pairs','sentiments','relations','relation_reviews')},
    }


def probes(db, articles):
    results=[]
    for article in articles:
        start=time.perf_counter()
        rows=db.execute(QUERY,(article,)).fetchall()
        results.append({'article_id':article,'seconds':time.perf_counter()-start,
                        'rows':len(rows),'payload_multiset_sha256':build.digest(sorted(rows))})
    return results


def run(out):
    out=out.resolve()
    artifacts=(build.HERE/'artifacts').resolve()
    build.preview.require(out.parent==artifacts and out.name.startswith('kg_grounded_'),
                          'Only local kg_grounded output directories are supported')
    build.preview.require((out/'STOP').is_file() and not (out/'manifest.json').exists(),
                          'Require a stopped, unfinished output')
    state=build.read(out/'status.json'); protocol=build.read(out/'protocol.json')
    build.preview.require(state.get('schema')==build.SCHEMA and state.get('state')=='paused_stop_file',
                          'Builder has not acknowledged STOP')
    audit_path=out/'query_optimization_20260919.json'
    build.preview.require(not audit_path.exists(),'Optimization receipt already exists')
    with build.run_lock(out):
        db=sqlite3.connect(out/'state.sqlite3',timeout=10)
        try:
            db.execute('PRAGMA synchronous=FULL')
            build.preview.require(build.meta(db,'stage')=='relations','Expected paused relations stage')
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            before=snapshot(db)
            article_ids=list(dict.fromkeys(r[0] for r in db.execute(
                "SELECT article_id FROM pairs WHERE decision='candidate_ready' LIMIT 12")))[:6]
            build.preview.require(bool(article_ids),'No eligible articles to compare')
            old_plan=db.execute('EXPLAIN QUERY PLAN '+QUERY,(article_ids[0],)).fetchall()
            old_probes=probes(db,article_ids)
            old_sha=build.sha(out/'state.sqlite3')
            started=build.now(); timer=time.perf_counter()
            with db:
                db.execute('ANALYZE pairs')
                db.execute('ANALYZE articles')
            elapsed=time.perf_counter()-timer
        finally:
            db.close()
        # A fresh connection must choose the improved plan used on resume.
        db=sqlite3.connect(out/'state.sqlite3',timeout=10)
        try:
            new_plan=db.execute('EXPLAIN QUERY PLAN '+QUERY,(article_ids[0],)).fetchall()
            build.preview.require(any('article_id=?' in r[3] for r in new_plan),
                                  'Planner still does not use the article index')
            new_probes=probes(db,article_ids)
            after=snapshot(db)
            build.preview.require(before==after,'Logical counters/checkpoint changed')
            keys=('article_id','rows','payload_multiset_sha256')
            build.preview.require([{k:r[k] for k in keys} for r in old_probes]==
                                  [{k:r[k] for k in keys} for r in new_probes],
                                  'Sample query results changed')
            build.validate_checkpoint(db,protocol['expected_pairs'])
            statistics=db.execute('SELECT tbl,idx,stat FROM sqlite_stat1 ORDER BY tbl,idx').fetchall()
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            receipt={
                'schema':'cosmos-grounded-query-optimization-1','started_at':started,'finished_at':build.now(),
                'statements':['ANALYZE pairs','ANALYZE articles'],'analyze_seconds':elapsed,
                'builder_code_changed':False,'model_or_protocol_changed':False,'logical_rows_modified':False,
                'checkpoint':after,'before_query_plan':old_plan,'after_query_plan':new_plan,
                'before_probes':old_probes,'after_probes':new_probes,'sqlite_stat1':statistics,
                'before_db_sha256':old_sha,'after_db_sha256':build.sha(out/'state.sqlite3'),
                'maintenance_code_sha256':build.sha(Path(__file__)),
                'verification_scope':'SQLite quick_check, logical counters/references/cursors and six sampled query multisets; ANALYZE only updates planner statistics',
            }
            build.atomic(audit_path,receipt)
            print(build.canonical({'audit':str(audit_path),'query_plan':new_plan,
                                  'before_seconds':sum(r['seconds'] for r in old_probes),
                                  'after_seconds':sum(r['seconds'] for r in new_probes),
                                  'checkpoint':after['meta']}),flush=True)
        finally:
            db.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out',type=Path,required=True)
    run(parser.parse_args().out)
