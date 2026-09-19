"""CPU contract-parser regressions; no model, network or source mutations."""
import datetime as dt
from pathlib import Path
import sys
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from build_grounded_disclosures import supply_tables

class SupplyTests(unittest.TestCase):
    def report(self,party='현대자동차 주식회사',extra='',date='2026-07-28',title='단일판매ㆍ공급계약체결'):
        text='현대오토에버/단일판매ㆍ공급계약체결\n\n단일판매ㆍ공급계약 체결\n\n'+extra+'1. 판매ㆍ공급계약 구분\n기타 판매ㆍ공급계약\n3. 계약상대\n'+party+'\n4. 판매ㆍ공급지역\n국내\n7. 계약(수주)일자\n'+date+'\n'
        return {'report_nm':title,'files':[{'text':text}]}
    def parse(self,r):return supply_tables(r,{'현대자동차':'005380','현대로템':'064350','POSCO':'005490'},dt.date(2026,9,16))
    def test_signed_direction_counterparty_literal_offset(self):
        r=self.report();rows,issue=self.parse(r);self.assertIsNone(issue);self.assertEqual(rows[0]['dst_ticker'],'005380')
        e=rows[0];self.assertEqual(r['files'][0]['text'][e['evidence_start']:e['evidence_end']],e['evidence_text'])
    def test_english_translation_parentheses(self):
        rows,_=self.parse(self.report(party='현대로템 주식회사(Hyundai-Rotem Co.)'));self.assertEqual(rows[0]['dst_ticker'],'064350')
    def test_no_parent_promotion_even_when_title_missing_subsidiary(self):
        rows,issue=self.parse(self.report(extra='자회사인\nLS전선㈜\n의 주요경영사항신고\n'));self.assertEqual(rows,[]);self.assertIn('subsidiary',issue)
    def test_termination_not_positive_relation(self):
        rows,_=self.parse(self.report(title='단일판매ㆍ공급계약해지'));self.assertEqual(rows,[])
    def test_future_signed_date_rejected(self):
        rows,issue=self.parse(self.report(date='2026-09-30'));self.assertEqual(rows,[]);self.assertIn('future',issue)
    def test_compound_counterparty_not_guessed(self):
        rows,_=self.parse(self.report(party='현대자동차, 현대로템'));self.assertEqual(rows,[])
    def test_posco_does_not_become_holding_company(self):
        rows,issue=self.parse(self.report(party='POSCO'));self.assertEqual(rows,[]);self.assertIn('holding',issue)
    def test_historical_signed_contract_is_valid_disclosure(self):
        rows,_=self.parse(self.report(date='2020-07-28'));self.assertEqual(len(rows),1)
    def test_wrong_numbered_form_is_review(self):
        r=self.report();r['files'][0]['text']=r['files'][0]['text'].replace('7. 계약(수주)일자','계약 예정일')
        rows,issue=self.parse(r);self.assertEqual(rows,[]);self.assertIn('unrecognized',issue)

if __name__=='__main__':unittest.main()
