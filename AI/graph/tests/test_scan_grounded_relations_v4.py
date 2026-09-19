"""Small complete CPU fixture for bounded v4 scan and frozen source checks."""
from pathlib import Path
import json
import sqlite3
import sys
import unittest
import uuid

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import scan_grounded_relations_v4 as scan


class ScanTests(unittest.TestCase):
    def test_new_artifact_preserves_source_and_calendar_boundary(self):
        root=Path(__file__).resolve().parents[1]/'artifacts'/('v4_scan_test_'+uuid.uuid4().hex)
        root.mkdir();parent=root/'parent';parent.mkdir();baseline=root/'baseline';baseline.mkdir();identity=root/'identity';identity.mkdir()
        (parent/'protocol.json').write_text('{}',encoding='utf8')
        (parent/'manifest.json').write_text('{"artifact_complete":true}',encoding='utf8')
        binding={'directory':str(parent),'manifest_sha256':scan.sha(parent/'manifest.json'),'protocol_sha256':scan.sha(parent/'protocol.json')}
        db=sqlite3.connect(str(parent/'state.sqlite3'))
        db.executescript('CREATE TABLE articles(article_id TEXT PRIMARY KEY,source_sha TEXT,cleaned_sha TEXT,payload TEXT); CREATE TABLE pairs(article_id TEXT,ticker TEXT,payload TEXT);')
        for aid,date in [('inside','2026-08-28T09:15Z'),('outside','2026-09-16T09:15Z')]:
            original={'article_id':aid,'title':'뉴스','body':'카카오와 동맹한 LG유플러스도 사업을 시작했다.','published_at':date,'source_url':'https://example/'+aid}
            clean={'title':original['title'],'body':original['body']}
            article={**original,'cleaned_title':original['title'],'cleaned_body':original['body'],'date_audit':{'usable_day':True},'cleaning_audit':{'quarantine':False}}
            db.execute('INSERT INTO articles VALUES (?,?,?,?)',(aid,scan.digest(original),scan.digest(clean),scan.canonical(article)))
            for ticker,name in [('035720','카카오'),('032640','LG유플러스')]:
                pair={'article_id':aid,'ticker':ticker,'company_name':name,'upstream_decision':'include',
                      'source_input_sha256':scan.digest({**original,'ticker':ticker,'company_name':name}),
                      'upstream_result_sha256':'test','materialization_decision':'review','reasons':['no_grounded_target_economic_candidate'],
                      'target_context':{'identity_scope':'lexical','historical_resolution':{'reason':'ticker_not_in_registry'}}}
                db.execute('INSERT INTO pairs VALUES (?,?,?)',(aid,ticker,scan.canonical(pair)))
        db.commit();db.close();before=scan.sha(parent/'state.sqlite3')
        (baseline/'evidence.jsonl').write_text('',encoding='utf8')
        (baseline/'manifest.json').write_text(scan.canonical({'artifact_complete':True,'parent_kg':binding,
            'files':{'relationship_evidence':{'path':'evidence.jsonl','sha256':scan.sha(baseline/'evidence.jsonl'),'rows':0}}}),encoding='utf8')
        (identity/'protocol.json').write_text(scan.canonical({'parent_kg':binding,'endpoint_policy':{'article_companies':[]}}),encoding='utf8')
        result=scan.run(baseline,identity,root/'out','2026-06-18','2026-09-16')
        self.assertEqual(result['new_evidence'],1);self.assertEqual(result['window_articles'],1)
        self.assertFalse(result['ready_for_gnn_training']);self.assertTrue(result['contextual_review_required'])
        self.assertEqual(scan.sha(parent/'state.sqlite3'),before)
        edge=json.loads((root/'out/new_relationship_evidence.jsonl').read_text(encoding='utf8'))
        self.assertEqual(edge['endpoint_validation']['source']['parent_decision'],'review')


if __name__=='__main__':unittest.main()
