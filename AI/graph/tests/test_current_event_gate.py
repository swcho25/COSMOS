"""Subject/time adversarial checks: no outcome files and no human-gold claims."""
import copy
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from current_event_gate import evaluate_article
from news_quality import financial_evidence


def article(title, body='', names=None, published='2020-10-16'):
    names = names or {'AAPL': 'Apple'}
    full = title + '\n' + body
    hits = [dict(ticker=ticker, start=m.start(), end=m.end())
            for ticker, name in names.items() for m in re.finditer(re.escape(name), full)]
    return dict(title=title, body=body, published_date=published,
                literal_mentions=hits, event_evidence=financial_evidence(title, body, hits))


class CurrentEventGateTest(unittest.TestCase):
    def test_direct_actions_in_both_languages(self):
        cases = [
            ('Apple reported quarterly revenue of $50 billion today.', {'AAPL': 'Apple'}, 'earnings'),
            ("Apple’s quarterly revenue rose 12 percent.", {'AAPL': 'Apple'}, 'earnings'),
            ('삼성전자는 3분기 매출 50조원을 기록했다고 발표했다.', {'005930': '삼성전자'}, 'earnings'),
            ('Apple signed a supply agreement today.', {'AAPL': 'Apple'}, 'contract'),
            ('삼성전자는 3조원 규모의 공급계약을 체결했다고 밝혔다.', {'005930': '삼성전자'}, 'contract'),
            ('Apple acquired Example Corp for $1 billion.', {'AAPL': 'Apple'}, 'merger_acquisition'),
            ('LG전자가 오스트리아에 본사를 둔 ZKW를 인수한다.', {'066570': 'LG전자'}, 'merger_acquisition'),
            ('Apple announced a $10 billion share repurchase.', {'AAPL': 'Apple'}, 'capital_return'),
            ('삼성전자는 주당 100원의 배당을 결정했다.', {'005930': '삼성전자'}, 'capital_return'),
            ('Apple recalled defective batteries today.', {'AAPL': 'Apple'}, 'legal_operational'),
            ('삼성전자는 결함이 발견된 제품의 리콜을 실시한다고 밝혔다.', {'005930': '삼성전자'}, 'legal_operational'),
        ]
        for text, names, kind in cases:
            with self.subTest(text=text):
                result = evaluate_article(article(text, names=names))
                self.assertEqual(result['accepted_tickers'], list(names))
                self.assertIn(kind, [e['event_type'] for e in result['decisions'][0]['evidence']])

    def test_attribution_does_not_turn_supplier_into_subject(self):
        cases = [
            ("Here's what analysts expect, based on a survey of analysts by Thomson Reuters: Earnings per share: 97 cents.", {'TRI': 'Thomson Reuters'}),
            ("Sourcepoint cites Google research that suggests publishers earn 52% less revenue.", {'GOOGL': 'Google'}),
            ("According to Apple research, industry revenue rose 12 percent.", {'AAPL': 'Apple'}),
        ]
        for text, names in cases:
            with self.subTest(text=text):
                result = evaluate_article(article(text, names=names))
                self.assertEqual(result['accepted_tickers'], [])
                self.assertIn('source_or_research_attribution', result['decisions'][0]['reasons'])
        own = evaluate_article(article('Thomson Reuters reported quarterly revenue of $1 billion.', names={'TRI': 'Thomson Reuters'}))
        self.assertEqual(own['accepted_tickers'], ['TRI'])

    def test_client_product_rank_and_other_subject_abstain(self):
        for text, names in [
            ('고객의 매출대금을 우리은행 통장으로 수취하면 수수료를 면제한다.', {'316140': '우리은행'}),
            ("Apple's supplier reported revenue of $2 billion.", {'AAPL': 'Apple'}),
            ("Apple's products ranked first in sales revenue.", {'AAPL': 'Apple'}),
            ('이 같은 실적을 바탕으로 테슬라와도 공동 기술 개발을 진행 중이다.', {'TSLA': '테슬라'}),
            ('삼성전자는 LG전자가 실적을 발표했다고 밝혔다.', {'005930': '삼성전자', '066570': 'LG전자'}),
        ]:
            with self.subTest(text=text):
                result = evaluate_article(article(text, names=names))
                # In the two-company statement Samsung must never own LG results.
                self.assertNotIn(next(iter(names)), result['accepted_tickers'])

    def test_historical_acquisition_and_month_abstain(self):
        for text in [
            'Apple acquired Example in 2011 for $1 billion.',
            'Apple had acquired Example for $1 billion.',
            'Four years after the deal, Apple acquired Example.',
            'In July, Apple acquired Example for $1 billion.',
        ]:
            with self.subTest(text=text):
                result = evaluate_article(article(text))
                self.assertEqual(result['accepted_tickers'], [])
                self.assertTrue(set(result['decisions'][0]['reasons']) &
                                {'historical_reference', 'historical_year', 'different_month_reference'})
        self.assertEqual(evaluate_article(article('Apple reported annual revenue for fiscal 2019 today.'))['accepted_tickers'], ['AAPL'])

    def test_revenue_sharing_is_contract(self):
        for text in [
            "Microsoft's Xbox deal with GameStop includes a revenue share agreement.",
            'Microsoft confirmed the revenue sharing agreement in a statement.',
        ]:
            result = evaluate_article(article(text, names={'MSFT': 'Microsoft'}))
            self.assertEqual(result['accepted_tickers'], ['MSFT'])
            self.assertEqual({e['event_type'] for e in result['decisions'][0]['evidence']}, {'contract'})

    def test_negation_uncertainty_and_missing_date_fail_closed(self):
        for text in [
            'Apple reported revenue estimates of $50 billion.',
            'Apple announced an acquisition but denied the claim.',
            'Apple reportedly acquired Example.',
            'Apple may report quarterly earnings tomorrow.',
        ]:
            self.assertEqual(evaluate_article(article(text))['accepted_tickers'], [])
        self.assertEqual(evaluate_article(article('Apple acquired Example.', published='unknown'))['accepted_tickers'], [])

    def test_literal_spans_mutation_and_outcome_invariance(self):
        data = article('Apple reported revenue of $50 billion.', 'Apple announced a dividend.')
        original = copy.deepcopy(data)
        result = evaluate_article(data)
        self.assertEqual(data, original)
        injected = copy.deepcopy(data)
        injected.update(price=999999, target=-1, direction='up', prediction=.9999,
                        metadata_json='{"sentiment": "negative"}')
        self.assertEqual(evaluate_article(injected), result)
        full = data['title'] + '\n' + data['body']
        for decision in result['decisions']:
            for e in decision['evidence'] + decision['excluded_evidence']:
                self.assertEqual(full[e['start']:e['end']], e['quote'])
                self.assertEqual(full[e['mention_start']:e['mention_end']], 'Apple')
                if 'event_start' in e:
                    self.assertEqual(full[e['event_start']:e['event_end']], e['event_quote'])
            self.assertNotIn('confidence', decision)
        self.assertEqual(result['human_review_status'], 'pending')
        self.assertEqual(result['label_status'], 'unlabeled')

    def test_late_unbounded_and_invalid_mentions_abstain(self):
        late = article('Profile', ('Background sentence. ' * 130) + 'Apple acquired Example.')
        self.assertEqual(evaluate_article(late)['accepted_tickers'], [])
        huge = article('Apple acquired Example ' + 'and background ' * 70)
        self.assertEqual(evaluate_article(huge)['accepted_tickers'], [])
        invalid = article('Apple acquired Example.')
        invalid['literal_mentions'][0]['end'] = 99999
        self.assertIn('invalid_literal_mention', evaluate_article(invalid)['decisions'][0]['reasons'])

    def test_v2_parenthetical_acquisition_object_and_relative_history(self):
        exact = '롯데는 지난해 3조원을 들여 삼성의 화학사(삼성SDI 화학사업, 삼성정밀화학, 삼성BP화학)를 인수했다.'
        result = evaluate_article(article(exact, names={'006400': '삼성SDI'}, published='2017-02-03'))
        self.assertEqual(result['accepted_tickers'], [])
        self.assertIn('company_mention_inside_parenthetical', result['decisions'][0]['reasons'])
        self.assertIn('historical_relative_period', result['decisions'][0]['reasons'])
        # Removing the old-date marker does not turn a parenthetical object into a subject.
        self.assertEqual(evaluate_article(article(exact.replace('지난해', '오늘'), names={'006400': '삼성SDI'}))['accepted_tickers'], [])
        for period in ('지난해', '작년', '지난달'):
            self.assertEqual(evaluate_article(article(f'삼성SDI는 {period} 회사를 인수했다.', names={'006400': '삼성SDI'}))['accepted_tickers'], [])
        self.assertEqual(evaluate_article(article('삼성SDI는 오늘 회사를 인수했다.', names={'006400': '삼성SDI'}))['accepted_tickers'], ['006400'])
        prior_results = 'LG화학 역시 지난해 1조9919억원의 영업이익을 기록하면서 최근 5년래 최대 실적을 달성했다.'
        self.assertEqual(evaluate_article(article(prior_results, names={'051910': 'LG화학'}, published='2017-02-03'))['accepted_tickers'], ['051910'])


if __name__ == '__main__':
    unittest.main()
