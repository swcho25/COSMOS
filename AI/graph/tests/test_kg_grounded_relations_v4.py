"""CPU-only positive/negative role tests; no model or source database writes."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_grounded_relations_v3 as old
import kg_grounded_relations_v4 as new

ENTITIES = [{'ticker': 'A', 'company_name': '카카오'}, {'ticker': 'B', 'company_name': 'LG유플러스'}]


class AdditiveTests(unittest.TestCase):
    def result(self, body, entities=ENTITIES):
        return new.extract_grounded_relations('🙂 실제 원문', body, entities)

    def test_completed_alliance_relative_clause_with_exact_spans(self):
        body = '카카오와 동맹한 LG유플러스도 파주에 200MW급 AIDC를 짓고 2030년 누적 수주 5조원을 노린다.'
        self.assertEqual(old.extract_grounded_relations('', body, ENTITIES)['relationships'], [])
        result = self.result(body)
        self.assertEqual(result['new_candidate_count'], 1)
        edge = result['relationships'][0]
        self.assertEqual((edge['rel_type'], edge['src_ticker'], edge['dst_ticker']), ('PARTNER','A','B'))
        self.assertTrue(edge['contextual_review_required'])
        text = '🙂 실제 원문\n' + body
        for p in ('source', 'target'):
            self.assertEqual(text[edge[p+'_start']:edge[p+'_end']], edge[p+'_quote'])
        self.assertEqual(text[edge['sentence_start']:edge['sentence_end']], edge['sentence'])

    def test_signed_relative_alliance_roles(self):
        for body in ('카카오와 업무협약을 체결한 LG유플러스는 서비스를 시작했다.',
                     '카카오와 제휴한 LG유플러스는 서비스 판매를 시작했다.'):
            self.assertEqual(self.result(body)['new_candidate_count'], 1)

    def test_leader_participant_is_explicit_membership_only(self):
        for body in ('카카오가 이끄는 컨소시엄에 LG유플러스가 참여했다.',
                     'LG유플러스는 카카오가 이끄는 컨소시엄의 참여사다.',
                     'LG유플러스는 카카오가 주도하는 컨소시엄에 참여했다고 밝혔다.'):
            edge = self.result(body)['relationships'][0]
            self.assertEqual(edge['relation_subtype'], 'consortium_leader_participant')
            self.assertFalse(edge['directed'])

    def test_open_market_completed_acquisition_direction(self):
        for body in ('LG유플러스는 카카오 지분 3.1%를 장내 매입했다.',
                     'LG유플러스는 지난 3일 카카오의 지분 약 3.1%를 장내에서 매입했다고 밝혔다.'):
            edge = self.result(body)['relationships'][0]
            self.assertEqual((edge['rel_type'],edge['src_ticker'],edge['dst_ticker'],edge['relation_subtype']),
                             ('INVEST','B','A','stake_acquisition'))

    def test_completed_acquisition_repeat_target_resolves_same_holding(self):
        sentence = '한화시스템은 최근 한 달간 KAI 지분 3.45%를 장내 매입해 KAI 지분율이 총 15.89%로 확대된 바 있다.'
        entities = [{'ticker':'BUYER','company_name':'한화시스템'}, {'ticker':'TARGET','company_name':'한국항공우주','aliases':['KAI']}]
        edge = self.result(sentence,entities)['relationships'][0]
        self.assertEqual((edge['src_ticker'],edge['dst_ticker'],edge['rel_type']), ('BUYER','TARGET','INVEST'))
        self.assertEqual(edge['target_quote'], 'KAI')
        for bad in (sentence.replace('KAI 지분율', '다른 회사 지분율'),
                    sentence.replace('확대된 바 있다', '확대될 예정이다'),
                    sentence.replace('장내 매입해', '장내 매입하지 않아')):
            self.assertEqual(self.result(bad,entities)['relationships'], [])

    def test_no_uncertain_negative_or_personal_biography(self):
        for body in ('카카오와 동맹할 LG유플러스는 공장을 짓는다.',
                     '카카오와 동맹한 LG유플러스라는 소문이 돌았다.',
                     '카카오와 동맹한 LG유플러스가 아니다.',
                     '카카오와 동맹한 LG유플러스일까?',
                     '카카오 출신 직원이 LG유플러스와 동맹했다.',
                     'LG유플러스는 카카오 지분 3.1%를 장내 매입할 예정이다.',
                     'LG유플러스는 카카오 지분 3.1%를 장내 매입하지 않았다.',
                     '카카오가 이끄는 컨소시엄에 LG유플러스가 참여할 계획이다.'):
            with self.subTest(body=body): self.assertEqual(self.result(body)['relationships'], [])

    def test_no_joint_list_third_party_or_unresolved_identity(self):
        for body in ('카카오와 LG유플러스는 각각 컨소시엄에 참여했다.',
                     '카카오와 LG유플러스는 한 컨소시엄에 참여했다.',
                     '카카오와 LG유플러스가 제삼자와 동맹했다.',
                     '카카오와 동맹한 LG유플러스서비스는 판매를 시작했다.',
                     'LG유플러스는 카카오가 이끄는 컨소시엄과 경쟁한다.',
                     'LG유플러스는 카카오 지분을 가진 직원의 주식을 장내 매입했다.'):
            with self.subTest(body=body): self.assertEqual(self.result(body)['relationships'], [])
        entities = [*ENTITIES, {'ticker':'C','company_name':'카카오'}]
        self.assertFalse(self.result('카카오와 동맹한 LG유플러스는 판매를 시작했다.', entities)['relationships'])

    def test_existing_v3_edges_unchanged(self):
        body = '카카오는 LG유플러스에 반도체를 공급했다.'
        self.assertEqual(self.result(body)['relationships'], old.extract_grounded_relations('🙂 실제 원문', body, ENTITIES)['relationships'])
        self.assertEqual(self.result(body)['new_candidate_count'], 0)


if __name__ == '__main__': unittest.main()
