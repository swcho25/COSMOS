"""Selection contracts for replayed news, duplicate filings and price revisions."""
import copy
import sys
import unittest
from pathlib import Path

HERE=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(HERE))
sys.path.insert(0,str(HERE.parent/'ner'))
from prepare_lake_recent import (map_news,merge_news_receptions,select_dart_filings,
                                 select_sec_filings,select_price_history,analyze_articles)
from lake_news_normalize import normalize_article
from lake_document_ids import assign_document_ids


class PrepareLakeRecentTests(unittest.TestCase):
    def setUp(self):
        self.as_of='2026-09-16T00:00:00Z'
        self.companies=[{'ticker':'A','market':'KOSPI','name_official':'발행회사','dart_corp_code':'1.0'},
                        {'ticker':'B','market':'KOSPI','name_official':'보유회사','dart_corp_code':'2.0'},
                        {'ticker':'GOOG','market':'NASDAQ','name_official':'Alphabet C','cik':'1652044.0'},
                        {'ticker':'GOOGL','market':'NASDAQ','name_official':'Alphabet A','cik':'1652044'}]
        self.table=('최대주주 및 그 특수관계인의 주식소유 현황\n기준일 : 2026년 06월 30일\n'
                    '보유회사 | 최대주주 본인 | 보통주 | 1,000 | 5.0 | 1,500 | 7.5\n'
                    '계 | 보통주 | 1,000 | 5.0 | 1,500 | 7.5')
        self.dart={'corp_code':'00000001','rcept_no':'20260814000123','rcept_dt':'20260814',
                   'report_nm':'반기보고서 (2026.06)','source_path':'/dart/full',
                   'files':[{'filename':'report.xml','text':self.table}]}
        self.sec={'cik':'1652044','accessionNumber':'0001652044-26-000100','symbols':['GOOG'],
                  'acceptanceDateTime':'2026-09-14T21:30:00Z','filingDate':'2026-09-14',
                  'form':'10-Q','source_path':'/sec/full'}
        self.old='/datasets/prices/snapshots/20260911-krx-nasdaq-daily/data/prices_daily.parquet'
        self.new='/data-lake/raw/stock-prices/daily/market=KOSPI/as_of=2026-09-15/snapshot=20260916T010000Z-run/prices_daily.parquet'
        self.source_manifest={'sources':{'prices':[{'path':self.old},{'path':self.new}]}}

    def news(self,**fields):
        return normalize_article({'source_dataset':'realtime_news_raw','record_id':'event-1',
                                  'title':'회사 계약 발표','body':'기업이 신규 공급 계약을 발표했다. '*10,
                                  'published_at':'2026-09-15T01:00:00Z','region':'domestic','language':'ko',
                                  'url':'https://example.com/news/1','source_path':'/batch/1',
                                  'collected_at':'2026-09-15T03:00:00Z',**fields})

    def price(self,ticker='A',date='2026-09-14',source=None,value=100):
        market=next(c['market'] for c in self.companies if c['ticker']==ticker)
        return {'ticker':ticker,'market':market,'date':date,'source_path':source or self.old,
                'close_price':value,'adj_close':value,'trading_volume':0}

    def test_all_live_publication_field_variants_map_to_normalizer(self):
        for field in ('published_at','published_date','pub_date','publish_date','publishedAt'):
            row=map_news({'news_id':123,'source_path':'/live','title':'Company filing',
                          'body':'Company published results.',field:'2026-09-15'},'news_live')
            self.assertEqual(row['published_at'],'2026-09-15')
            self.assertEqual(normalize_article(row)['published_at'],'2026-09-15T23:59:59.999999Z')

    def test_replayed_identity_merges_paths_earliest_collection_and_flags(self):
        first=self.news()
        replay=self.news(source_path='/batch/2',collected_at='2026-09-15T10:00:00+09:00')
        replay['quality_flags']+=['replayed']
        merged,summary=merge_news_receptions([first,replay])
        self.assertEqual((merged,summary),merge_news_receptions([replay,first]))
        self.assertEqual(merged[0]['source_paths'],['/batch/1','/batch/2'])
        self.assertEqual(merged[0]['collected_at'],'2026-09-15T01:00:00Z')
        self.assertEqual(merged[0]['source_reception_count'],2)
        self.assertEqual(summary['merged_receptions'],1)
        self.assertIn('replayed',merged[0]['quality_flags'])
        self.assertEqual(len(assign_document_ids(merged)[0]),1)

    def test_semantic_replay_conflicts_fail_and_naive_collection_not_guessed(self):
        for field,value in [('body','changed'),('published_at','2026-09-15T02:00:00Z'),
                            ('url','https://example.com/other'),('language','en')]:
            with self.assertRaises(ValueError):
                merge_news_receptions([self.news(),self.news(**{field:value})])
        row=self.news(collected_at='2026-09-15 03:00:00')
        merged,summary=merge_news_receptions([row])
        self.assertEqual(merged[0]['collected_at'],'2026-09-15 03:00:00')
        self.assertEqual(merged[0]['collected_at_precision'],'unknown_timezone')
        self.assertEqual(merged[0]['collected_at_values'],['2026-09-15 03:00:00'])
        self.assertEqual(summary['invalid_collection_times'],1)

    def test_observed_live_schema_keeps_unknown_language_and_naive_source_times(self):
        mapped=map_news({'news_id':456,'source_path':'/live','source':'fundus',
                         'organization':'Observed Publisher','title':'A company reports',
                         'content':'Company results are available.',
                         'published_at':'2026-09-15 13:30:00','crawled_at':'2026-09-15 14:30:00'},'news_live')
        self.assertIsNone(mapped['language'])
        self.assertEqual(mapped['collected_at'],'2026-09-15 14:30:00')
        self.assertEqual(mapped['publisher'],'Observed Publisher')
        self.assertEqual(mapped['organization'],'Observed Publisher')
        self.assertEqual(mapped['provider'],'fundus')
        cleaned=normalize_article(mapped)
        self.assertEqual(cleaned['published_at'],'2026-09-15T23:59:59.999999Z')
        self.assertIn('publication_time_without_valid_timezone',cleaned['quality_flags'])

    def test_dart_best_copy_document_and_evidence_stay_together(self):
        incomplete={**self.dart,'source_path':'/dart/first','files':[{'text':'no recognized table '*100}]}
        chosen,summary=select_dart_filings([incomplete,self.dart],self.companies,[],as_of=self.as_of)
        self.assertEqual((chosen,summary),select_dart_filings([self.dart,incomplete],self.companies,[],as_of=self.as_of))
        normalized,=chosen
        self.assertEqual(normalized['document']['ownership_parse_status'],'parsed')
        self.assertEqual(normalized['document']['source_path'],'/dart/full')
        self.assertEqual(normalized['document']['source_paths'],['/dart/first','/dart/full'])
        self.assertTrue(normalized['relationships'])
        self.assertTrue(all(r['source_path']=='/dart/full' for r in normalized['relationships']))
        self.assertEqual(summary['merged_receptions'],1)

    def test_dart_most_complete_parsed_copy_wins_and_identity_conflicts_fail(self):
        longer=copy.deepcopy(self.dart)
        longer['source_path']='/dart/more_complete'
        longer['files'][0]['text']=self.table+'\nAdditional complete financial-report text.'
        result,_=select_dart_filings([self.dart,longer],self.companies,[],as_of=self.as_of)
        self.assertEqual(result[0]['document']['source_path'],'/dart/more_complete')
        for update in ({'rcept_dt':'20260813'},{'corp_code':'00000002'}):
            with self.assertRaises(ValueError):
                select_dart_filings([self.dart,{**self.dart,**update}],self.companies,[],as_of=self.as_of)

    def test_sec_exact_timestamp_overrides_date_fallback_and_unions_share_symbols(self):
        fallback={**self.sec,'source_path':'/sec/first','acceptanceDateTime':None,'symbols':['GOOGL']}
        selected,summary=select_sec_filings([fallback,self.sec],self.companies,as_of=self.as_of)
        self.assertEqual((selected,summary),select_sec_filings([self.sec,fallback],self.companies,as_of=self.as_of))
        self.assertEqual([r['ticker'] for r in selected],['GOOG','GOOGL'])
        for row in selected:
            self.assertEqual(row['published_at'],'2026-09-14T21:30:00Z')
            self.assertEqual(row['source_path'],'/sec/full')
        self.assertEqual(len({r['document_id'] for r in selected}),1)

    def test_sec_equivalent_timezones_agree_conflicts_fail(self):
        same={**self.sec,'acceptanceDateTime':'2026-09-14T17:30:00-04:00'}
        self.assertTrue(select_sec_filings([same,self.sec],self.companies,as_of=self.as_of)[0])
        different={**self.sec,'acceptanceDateTime':'2026-09-14T21:31:00Z'}
        with self.assertRaises(ValueError):
            select_sec_filings([different,self.sec],self.companies,as_of=self.as_of)

    def test_sec_future_acceptance_blocks_older_fallback(self):
        fallback={**self.sec,'acceptanceDateTime':None}
        future={**self.sec,'acceptanceDateTime':self.as_of}
        selected,summary=select_sec_filings([fallback,future],self.companies,as_of=self.as_of)
        self.assertEqual(selected,[])
        self.assertEqual(summary['future_acceptance_accessions'],1)

    def test_one_latest_history_per_market_never_backfills_older_adjustments(self):
        records=[self.price(date='2026-09-12',value=200),self.price(date='2026-09-14',value=300),
                 self.price(date='2026-09-14',source=self.new,value=150),self.price(ticker='GOOG',value=500)]
        selected,summary=select_price_history(records,self.companies,self.source_manifest,as_of=self.as_of)
        self.assertEqual((selected,summary),select_price_history(list(reversed(records)),self.companies,self.source_manifest,as_of=self.as_of))
        a=[r for r in selected if r['ticker']=='A']
        self.assertEqual([(r['date'],r['adjusted_close']) for r in a],[('2026-09-14',150)])
        self.assertEqual(summary['selected_files']['KOSPI'],self.new)
        self.assertEqual(summary['selected_files']['NASDAQ'],self.old)
        self.assertEqual(summary['dates_only_available_in_older_sources']['A'],['2026-09-12'])
        self.assertIn('B',summary['window_missing_known_tickers']['7D'])
        self.assertNotIn('A',summary['window_missing_known_tickers']['7D'])

    def test_latest_complete_file_without_window_rows_does_not_use_older_data(self):
        selected,summary=select_price_history([self.price()],self.companies,self.source_manifest,as_of=self.as_of)
        self.assertEqual(selected,[])
        self.assertEqual(summary['selected_files']['KOSPI'],self.new)
        self.assertIn('A',summary['window_missing_known_tickers']['90D'])
        self.assertEqual(summary['dates_only_available_in_older_sources']['A'],['2026-09-14'])

    def test_price_duplicates_future_dates_unpinned_paths_and_market_conflicts(self):
        selected,summary=select_price_history([self.price(source=self.new)]*2+[self.price(source=self.new,date='2026-09-16')],
                                               self.companies,self.source_manifest,as_of=self.as_of)
        self.assertEqual(len(selected),1)
        self.assertEqual(summary['duplicate_rows'],1)
        for records in ([self.price(source=self.new),self.price(source=self.new,value=999)],
                        [self.price(source='/unpinned')],
                        [self.price(source=self.new,ticker='GOOG')]):
            with self.assertRaises(ValueError):
                select_price_history(records,self.companies,self.source_manifest,as_of=self.as_of)

    def test_spawn_two_workers_preserve_serial_results_and_input_order(self):
        aliases=HERE.parent/'ner'/'data'/'aliases.csv'
        # More than two chunks ensures work is distributed rather than putting
        # the entire fixture in one chunk. Each document has a distinct ID.
        articles=[{'record_id':f'record-{i:03d}','document_id':f'document-{i:03d}',
                   'published_at':'2026-09-15T00:00:00Z','published_date':'2026-09-15',
                   'title':'삼성전자와 SK하이닉스가 공동 연구 협약을 체결했다.',
                   'body':'삼성전자와 SK하이닉스는 새로운 기술을 함께 개발하기로 했다.'}
                  for i in range(129)]
        affiliate_pairs=[('005930','000660')]
        serial=list(analyze_articles(articles,aliases,affiliate_pairs,workers=1))
        parallel=list(analyze_articles(articles,aliases,affiliate_pairs,workers=2,mp_context='spawn'))
        self.assertEqual(parallel,serial)
        self.assertTrue(all(result['mentions'] for result in parallel))
        self.assertTrue(any(result['relationships'] for result in parallel))
        self.assertEqual([result['mentions'][0]['record_id'] for result in parallel],
                         [article['record_id'] for article in articles])

    def test_worker_count_is_bounded_before_loading_dictionary(self):
        for workers in (0,3,-1,True,'2'):
            with self.assertRaises(ValueError):
                list(analyze_articles([],Path('/nonexistent/aliases.csv'),[],workers=workers))


if __name__=='__main__':
    unittest.main()
