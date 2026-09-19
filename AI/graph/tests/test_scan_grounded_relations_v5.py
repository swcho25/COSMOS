"""Exact cross-sentence evidence and immutable endpoint policy at scan boundary."""
from copy import deepcopy
from pathlib import Path
import sys
import sqlite3
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scan_grounded_relations_v5 import validate_edge, verify_checkpoint, canonical, digest


class CandidateBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.article = {'article_id':'n1','title':'계약 소식','body':'삼성전자는 계약을 발표했다. 이 회사는 현대차에 반도체를 공급했다.',
                        'published_at':'2025-01-01T00:00:00Z','source_url':'https://example.invalid/a'}
        self.article.update(cleaned_title=self.article['title'],cleaned_body=self.article['body'])
        text=self.article['title']+'\n'+self.article['body']; begin=len(self.article['title'])+1
        self.edge=dict(sentence_start=begin,sentence_end=len(text),sentence=text[begin:],
                       src_ticker='S',dst_ticker='H',rel_type='SUPPLY',directed=True,pattern='fixture_cross_sentence')
        for prefix,name in [('source','삼성전자'),('target','현대차')]:
            pos=text.index(name); self.edge.update({prefix+'_start':pos,prefix+'_end':pos+len(name),prefix+'_quote':name})
        self.pairs={}
        for ticker,name in [('S','삼성전자'),('H','현대차')]:
            self.pairs[ticker]=dict(company_name=name,source_input_sha256='s',upstream_result_sha256='u',
                materialization_decision='candidate_ready',reasons=[],
                target_context={'identity_scope':'literal','historical_resolution':None})
        self.registry={'S':['삼성전자'],'H':['현대차']}

    def check(self,edge=None,pairs=None,article=None,blocked=frozenset()):
        return validate_edge(article or self.article,edge or self.edge,pairs or self.pairs,self.registry,'source','clean',blocked)

    def test_contiguous_two_sentence_quote_retains_literal_anchors(self):
        row,reason=self.check()
        self.assertIsNone(reason)
        self.assertEqual(row['sentence'],self.article['body'])
        self.assertEqual(row['source_quote'],'삼성전자')
        self.assertTrue(row['endpoint_validation']['source']['parent_news_decision_unchanged'])
        self.assertTrue(row['contextual_review_required'])
        self.assertFalse(row['production_approved'])

    def test_spliced_quote_is_rejected(self):
        edge={**self.edge,'sentence':self.edge['sentence'].replace('계약을 발표했다. ','')}
        with self.assertRaises(AssertionError): self.check(edge=edge)

    def test_endpoint_outside_support_is_rejected(self):
        edge={**self.edge,'sentence_start':self.edge['target_start'],'sentence':self.edge['sentence'][self.edge['target_start']-self.edge['sentence_start']:]}
        with self.assertRaises(AssertionError): self.check(edge=edge)

    def test_news_removed_by_cleaning_cannot_be_restored_by_relation(self):
        article={**self.article,'cleaned_body':'삼성전자는 계약을 발표했다.'}
        self.assertEqual(self.check(article=article)[1],'evidence_not_retained_in_cleaned_article')

    def test_historical_identity_block_survives_cross_sentence(self):
        pairs=deepcopy(self.pairs); pairs['S']['reasons']=['historical_identity_time_unverified']
        self.assertEqual(self.check(pairs=pairs)[1],'endpoint_identity_block:historical_issuer_identity_pending')

    def test_frozen_article_company_exclusion_survives(self):
        self.assertEqual(self.check(blocked={('n1','S')})[1],'frozen_endpoint_identity_block')

    def test_no_unseen_counterparty_or_wrong_direction(self):
        with self.assertRaises(AssertionError): self.check(edge={**self.edge,'dst_ticker':'UNKNOWN'})
        with self.assertRaises(AssertionError): self.check(edge={**self.edge,'directed':False})

    def checkpoint(self):
        state=sqlite3.connect(':memory:'); source=sqlite3.connect(':memory:')
        self.addCleanup(state.close); self.addCleanup(source.close)
        state.executescript('CREATE TABLE done(article_id TEXT,counters TEXT,receipt TEXT); CREATE TABLE edges(edge_id TEXT,article_id TEXT,payload TEXT);')
        source.executescript('CREATE TABLE articles(article_id TEXT,source_sha TEXT,cleaned_sha TEXT,payload TEXT); CREATE TABLE pairs(article_id TEXT,ticker TEXT,payload TEXT);')
        source.execute('INSERT INTO articles VALUES (?,?,?,?)',('n1','source','clean',canonical(self.article)))
        for ticker,pair in self.pairs.items(): source.execute('INSERT INTO pairs VALUES (?,?,?)',('n1',ticker,canonical(pair)))
        row,_=self.check(); counts={'new:SUPPLY':1,'pattern:fixture_cross_sentence':1}
        receipt=digest({'article_id':'n1','source_sha':'source','counters':counts,'edges':[row]})
        state.execute('INSERT INTO done VALUES (?,?,?)',('n1',canonical(counts),receipt))
        state.execute('INSERT INTO edges VALUES (?,?,?)',(row['edge_id'],'n1',canonical(row)))
        return state,source,row

    def test_valid_checkpoint_can_be_resumed(self):
        state,source,_=self.checkpoint()
        verify_checkpoint(state,source,self.registry,set())

    def test_changed_checkpoint_payload_and_counters_fail(self):
        for changed in ('edge','counts'):
            state,source,row=self.checkpoint()
            if changed=='edge': state.execute('UPDATE edges SET payload=?',(canonical({**row,'src_ticker':'H'}),))
            else: state.execute('UPDATE done SET counters=?',(canonical({'new:SUPPLY':2}),))
            with self.assertRaises(AssertionError): verify_checkpoint(state,source,self.registry,set())

    def test_orphan_checkpoint_edge_fails(self):
        state,source,row=self.checkpoint()
        state.execute('INSERT INTO edges VALUES (?,?,?)',('orphan','unprocessed',canonical(row)))
        with self.assertRaises(AssertionError): verify_checkpoint(state,source,self.registry,set())


if __name__=='__main__': unittest.main()
