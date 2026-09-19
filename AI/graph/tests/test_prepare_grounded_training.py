"""CPU-only checks of conservative time, immutable context and no truncation."""
import copy
import datetime as dt
import sys
import unittest
import json
import sqlite3
import shutil
import uuid
from unittest import mock
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import prepare_grounded_training as prep
from news_impact_data import Labels


class FakeTokenizer:
    def encode(self, text, **kwargs):
        assert kwargs == {'add_special_tokens': False, 'truncation': False}
        return list(range(len(text)))
    def num_special_tokens_to_add(self, pair=False):
        return 2


class PreparationTests(unittest.TestCase):
    def test_naive_and_date_only_use_worldwide_day_end(self):
        self.assertEqual(prep.available_time('2022-12-31')[0], '2023-01-01T12:00:00+00:00')
        self.assertEqual(prep.available_time('2022-12-31 09:01')[0], '2023-01-01T12:00:00+00:00')

    def test_aware_publication_is_not_reinterpreted_by_company_market(self):
        self.assertEqual(prep.available_time('2023-01-01T09:00:00+09:00')[0], '2023-01-01T00:00:00+00:00')

    def test_graph_is_strictly_before_article(self):
        graphs = {'old': {'available_at': '2019-01-01T12:00:00Z'}, 'new': {'available_at': '2020-01-01T12:00:00Z'}}
        self.assertEqual(prep.choose_graph(graphs, '2020-01-01T12:00:00Z'), 'old')
        self.assertEqual(prep.choose_graph(graphs, '2020-01-01T12:00:01Z'), 'new')

    def test_primary_periods_do_not_include_legacy_2017_training(self):
        self.assertIsNone(prep.split_for('2019-12-31'))
        self.assertEqual(prep.split_for('2022-12-31'), 'train')
        self.assertEqual(prep.split_for('2023-01-01'), 'val')
        self.assertEqual(prep.split_for('2024-01-01'), 'test')

    def test_windows_cover_every_token_once(self):
        windows = prep.token_windows(FakeTokenizer(), 'a'*1100)
        self.assertEqual([n for _, n in windows], [254,254,254,254,84])
        self.assertEqual([x for ids,_ in windows for x in ids], list(range(1100)))

    def fixture(self):
        article = {'article_id':'a', 'title':'Acme wins contract.', 'body':'', 'cleaned_title':'Acme wins contract.', 'cleaned_body':'', 'cleaned_text_sha256':'s'}
        pair = {'article_id':'a', 'ticker':'ACME', 'materialization_decision':'candidate_ready', 'cleaned_text_sha256':'s', 'source_input_sha256':'i',
                'target_context':{'text':'Acme wins contract.'}, 'evidence':[{'source':'title','start':0,'end':19,'quote':'Acme wins contract.', 'cleaned_start':0,'cleaned_end':19,'cleaned_quote':'Acme wins contract.'}]}
        sentiment = {'article_id':'a','ticker':'ACME','input_sha256':'i','status':'analyzed','probabilities':{'negative':.1,'neutral':.2,'positive':.7}}
        return article,pair,sentiment

    def test_reuses_company_probabilities_in_explicit_order(self):
        article,pair,sentiment = self.fixture(); before = copy.deepcopy((article,pair,sentiment))
        self.assertEqual(prep.validate_target(article,pair,sentiment)[1], [.1,.2,.7])
        self.assertEqual((article,pair,sentiment), before)

    def test_missing_sentiment_is_not_neutral(self):
        article,pair,sentiment = self.fixture(); sentiment.update(status='ambiguous_target_context', probabilities=None)
        self.assertEqual(prep.validate_target(article,pair,sentiment)[1], [0.,0.,0.])

    def test_original_cleaned_and_sentiment_identity_must_match(self):
        for change in ('quote', 'cleaned_quote', 'input_identity'):
            article,pair,sentiment = self.fixture()
            if change == 'input_identity': sentiment['input_sha256'] = 'wrong'
            else: pair['evidence'][0][change] = 'Fabricated earnings.'
            with self.assertRaises(ValueError): prep.validate_target(article,pair,sentiment)

    def test_context_cannot_omit_evidence(self):
        article,pair,sentiment = self.fixture(); pair['target_context']['text'] = 'Unrelated.'
        with self.assertRaises(ValueError): prep.validate_target(article,pair,sentiment)

    def test_label_end_purges_split_boundary(self):
        labels = object.__new__(Labels); labels.meta = pd.DataFrame({'market':['KOSPI']},index=['x'])
        dates = pd.to_datetime(['2022-12-28','2022-12-29','2022-12-30','2023-01-02','2023-01-03'])
        p = pd.DataFrame({'x':np.ones(5)},index=dates); residual = p*.01
        closes = pd.DatetimeIndex(dates).tz_localize('UTC')+pd.Timedelta(hours=6)
        labels.panels = {'KOSPI':(p,residual,closes)}
        self.assertIsNone(labels.label('x', pd.Timestamp('2022-12-27T12:00Z'), 'train'))

    def test_url_fragment_dedup_keeps_article_query_identity(self):
        self.assertEqual(prep.url_key('HTTPS://EXAMPLE.COM/news?id=12#top'), 'https://example.com/news?id=12')
        self.assertNotEqual(prep.url_key('https://example.com/news?id=12'), prep.url_key('https://example.com/news?id=13'))

    def test_future_label_availability_changes_loss_mask_not_news_input(self):
        root=Path(__file__).resolve().parents[1]/'artifacts';work=root/('test_grounded_label_gate_'+uuid.uuid4().hex);work.mkdir()
        try:
            original=work/'original';features=work/'features';source=work/'source'
            for path in (original,features,source):path.mkdir()
            meta=pd.DataFrame({'ticker':['A','B'],'market':['KOSPI','KOSPI'],'industry_id':['x','x']});meta.to_json(original/'companies.json')
            legacy={'record_id':'legacy_only','split':'val','published_date':'2023-05-01','available_at':'2023-05-02T12:00Z','graph':'2023','title':'Legacy news','body':'Legacy body','companies':[]}
            (original/'articles.jsonl').write_text(json.dumps(legacy)+'\n',encoding='utf8')
            pd.DataFrame([{'article':0,'news_id':'legacy_only','ticker':'A','split':'val','graph':'2023','is_direct':True,'path_length':0,
                           'y_1d':.01,'y_3d':.02,'anchor_date':'2023-05-03','label_end':'2023-05-08','market':'KOSPI'}]).to_parquet(original/'pairs.parquet')
            np.savez(original/'label_config.npz',thresholds=[.01,.01],scales=[.1,.1])
            for name,value in [('target_text',np.zeros((3,64),dtype='float32')),('target_sentiment',np.zeros((3,3),dtype='float32')),
                               ('feature_mapping',np.array([[1,0]],dtype='int64')),('direct_mask',np.array([[True,False]])),('mentions',np.zeros((1,2,5),dtype='float32'))]:
                np.save(features/(name+'.npy'),value)
            graph_path=work/'graphs.json';graph_path.write_text(json.dumps({str(y):{'as_of':f'{y}-01-01','available_at':f'{y}-01-01T12:00Z','industry_available':False,'edges':[]} for y in range(2017,2024)}))
            article={'article_id':'current','published_at':'2021-06-01','title':'A supplies B.','body':'new contract details','cleaned_title':'A supplies B.','cleaned_body':'new contract details','cleaned_text_sha256':'clean','source_url':'https://example.com/unique'}
            connection=sqlite3.connect(source/'state.sqlite3');connection.executescript('CREATE TABLE articles(article_id TEXT,payload TEXT);CREATE TABLE pairs(article_id TEXT,ticker TEXT,decision TEXT,payload TEXT);CREATE TABLE sentiments(article_id TEXT,ticker TEXT,payload TEXT);')
            connection.execute('INSERT INTO articles VALUES (?,?)',('current',json.dumps(article)))
            for ticker in ('A','B'):
                pair={'article_id':'current','ticker':ticker,'company_name':ticker,'materialization_decision':'candidate_ready','cleaned_text_sha256':'clean','source_input_sha256':ticker,
                      'target_context':{'text':'A supplies B.'},'evidence':[{'source':'title','start':0,'end':13,'quote':'A supplies B.','cleaned_start':0,'cleaned_end':13,'cleaned_quote':'A supplies B.'}]}
                connection.execute('INSERT INTO pairs VALUES (?,?,?,?)',('current',ticker,'candidate_ready',json.dumps(pair)))
            connection.commit();connection.close()
            class FakeLabels:
                second=True
                def label(self,ticker,published,split):
                    if ticker=='B' and not self.second:return None
                    return {'y_1d':.01,'y_3d':.02,'anchor_date':'2021-06-03','label_end':'2021-06-08','market':'KOSPI'}
            labels=FakeLabels();first={t:pd.Timestamp('2020-01-01T00:00Z') for t in ('A','B')}
            with mock.patch.multiple(prep,ORIGINAL=original,FEATURES=features,BASE_GRAPHS=graph_path),mock.patch.object(prep,'source_contract',return_value={}),mock.patch.object(prep,'input_assets',return_value={}),mock.patch('experiment_news_scale.labeler',return_value=(labels,first,meta)):
                prep.prepare(source,work/'with_label');labels.second=False;prep.prepare(source,work/'without_label')
            for filename in ('feature_mapping.npy','mentions.npy'):
                np.testing.assert_array_equal(np.load(work/'with_label'/filename),np.load(work/'without_label'/filename))
            self.assertTrue(np.load(work/'with_label/direct_mask.npy')[0,1]);self.assertFalse(np.load(work/'without_label/direct_mask.npy')[0,1])
            self.assertEqual((work/'with_label/new_feature_entries.jsonl').read_text(),(work/'without_label/new_feature_entries.jsonl').read_text())
        finally:
            assert work.resolve().is_relative_to(root.resolve());shutil.rmtree(work)


if __name__ == '__main__': unittest.main()
