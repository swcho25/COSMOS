import subprocess
import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from matcher import CompanyMatcher  # noqa: E402

ALIASES = HERE / "data" / "aliases.csv"


def tickers(res):
    return {m.ticker for m in res.mentions}


class MatcherRules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not ALIASES.exists():
            subprocess.run([sys.executable, str(HERE / "build_aliases.py")], check=True)
        cls.m = CompanyMatcher.from_csv(ALIASES)

    def test_basic_and_particle(self):
        r = self.m.match("삼성전자, HBM 투자 확대", "삼성전자는 이날 밝혔다.")
        self.assertEqual(tickers(r), {"005930"})
        self.assertEqual(r.by_ticker()[0]["n_mentions"], 2)
        self.assertEqual(r.by_ticker()[0]["first_pos"], "title")

    def test_preferred_stock_blocked(self):
        r = self.m.match("", "삼성전자우 강세, 삼성전자우B도 상승")
        self.assertEqual(tickers(r), set())
        self.assertTrue(any(b[0].startswith("삼성전자우") for b in r.blocked))

    def test_hybrid_not_hybe(self):
        r = self.m.match("", "하이브리드 차량 판매가 늘었다.")
        self.assertEqual(tickers(r), set())
        r = self.m.match("", "하이브가 신인 그룹을 데뷔시켰다.")
        self.assertEqual(tickers(r), {"352820"})

    def test_latin_boundary(self):
        r = self.m.match("", "SKT와 KTX 요금 인상")
        self.assertNotIn("030200", tickers(r))          # KT inside SKT / KTX must not match KT
        self.assertIn("017670", tickers(r))             # but SKT itself is an SK텔레콤 alias
        r = self.m.match("", "KT는 통신비 할인을 발표했다.")
        self.assertEqual(tickers(r), {"030200"})

    def test_skt_alias(self):
        r = self.m.match("", "SKT, 5G 요금제 개편")
        self.assertEqual(tickers(r), {"017670"})

    def test_sports_team_blocked(self):
        r = self.m.match("한화 이글스 4연승", "한화 이글스가 LG 트윈스를 꺾었다.")
        self.assertEqual(tickers(r), set())

    def test_group_resolution(self):
        # group name + member present -> attributed to member only
        r = self.m.match("", "삼성이 발표했다. 삼성전자는 실적을 공개했다.")
        self.assertEqual(tickers(r), {"005930"})
        # group name alone with holding ticker -> low confidence holding
        r = self.m.match("", "LG가 신사업을 발표했다.")
        self.assertEqual(tickers(r), {"003550"})
        self.assertEqual(r.mentions[0].confidence, 0.5)
        # group name alone without holding ticker -> unresolved
        r = self.m.match("", "삼성이 발표했다.")
        self.assertEqual(tickers(r), set())
        self.assertEqual([u.alias for u in r.unresolved], ["삼성"])

    def test_unlisted_subsidiary_blocked(self):
        r = self.m.match("", "SK온은 배터리 공장을 짓는다. GS칼텍스도 참여한다.")
        self.assertEqual(tickers(r), set())

    def test_longest_match(self):
        r = self.m.match("", "LG에너지솔루션과 LG전자, 포스코퓨처엠이 참여")
        self.assertEqual(tickers(r), {"373220", "066570", "003670"})

    def test_ticker_context(self):
        r = self.m.match("", "삼성전자(005930)는 3% 올랐다.")
        self.assertEqual(tickers(r), {"005930"})
        r = self.m.match("", "황반변성(AMD) 치료제 개발")            # acronym, not the chip maker
        self.assertEqual(tickers(r), set())
        r = self.m.match("", "엔비디아(NVDA) 주가 급등")
        self.assertEqual(tickers(r), {"NVDA"})
        r = self.m.match("", "$NVDA is up 5% while NASDAQ: AAPL fell.")
        self.assertEqual(tickers(r), {"NVDA", "AAPL"})

    def test_nasdaq_korean_names(self):
        r = self.m.match("", "애플과 마이크로소프트, 테슬라가 하락했다.")
        self.assertEqual(tickers(r), {"AAPL", "MSFT", "TSLA"})

    def test_english_news(self):
        r = self.m.match("Nvidia beats estimates", "Samsung Electronics and SK hynix supply HBM to Nvidia.")
        self.assertEqual(tickers(r), {"NVDA", "005930", "000660"})

    def test_short_ascii_group_needs_korean_doc(self):
        r = self.m.match("", "Goldman Sachs ( GS ) and Morgan Stanley ( MS ) rose. KB Home fell.")
        self.assertEqual(tickers(r), set())
        r = self.m.match("", "GS는 신사업을 발표했다.")
        self.assertEqual(tickers(r), {"078930"})

    def test_sports_gate(self):
        r = self.m.match("류현진 4실점", "KBO 플레이오프 4차전에서 한화의 선발투수로 등판했다. 삼성과의 경기에서 3이닝을 던졌다.")
        self.assertTrue(r.is_sports)
        self.assertEqual(tickers(r), set())
        self.assertIn("한화", [u.alias for u in r.unresolved])

    def test_alphabet_korean_word(self):
        r = self.m.match("", "알파벳 순서로 정렬한다.")
        self.assertEqual(tickers(r), set())          # unresolved, not a company
        r = self.m.match("", "구글 모회사 알파벳이 실적을 발표했다.")
        self.assertEqual(tickers(r), {"GOOGL"})
        self.assertEqual(r.by_ticker()[0]["n_mentions"], 1)  # 알파벳 absorbed into 구글

    def test_product_names_need_company(self):
        r = self.m.match("", "김 의원은 페이스북에 글을 올렸다. 아이폰으로 촬영했다.")
        self.assertEqual(tickers(r), set())
        self.assertEqual({u.alias for u in r.unresolved}, {"페이스북", "아이폰"})
        r = self.m.match("", "메타는 페이스북과 인스타그램에 새 기능을 넣었다.")
        self.assertEqual(tickers(r), {"META"})
        self.assertEqual(r.by_ticker()[0]["n_mentions"], 3)
        self.assertIn("product", r.by_ticker()[0]["method"])

    def test_industry_hits_without_company(self):
        r = self.m.match("반도체 수출 30% 증가", "반도체 업황 개선으로 조선업계도 기대감이 커졌다. 조선일보는 이를 보도했다.")
        self.assertEqual(tickers(r), set())
        inds = {i["industry_id"] for i in r.by_industry()}
        self.assertEqual(inds, {"SEMI", "TRANSPORT"})
        self.assertEqual([i for i in r.by_industry() if i["industry_id"] == "SEMI"][0]["first_pos"], "title")
        self.assertTrue(any(b[0] == "조선일보" for b in r.blocked))

    def test_industry_with_company_both_reported(self):
        r = self.m.match("", "삼성전자가 HBM 공급을 늘린다.")
        self.assertEqual(tickers(r), {"005930"})
        self.assertEqual({i["industry_id"] for i in r.by_industry()}, {"SEMI"})

    def test_name_equals_ticker(self):
        r = self.m.match("", "엔비디아의 H100, AMD의 MI300 시리즈가 HBM과 결합된다.")
        self.assertEqual(tickers(r), {"NVDA", "AMD"})

    def test_person_name_not_kt(self):
        r = self.m.match("", "케이티 맥낼리(미국) 조를 상대로 승리했다.")
        self.assertEqual(tickers(r), set())

    def test_sk_compound_not_sk(self):
        r = self.m.match("", "에브리봇이 SK인텔릭스 웰니스 로봇에 AI 플랫폼을 이식한다.")
        self.assertEqual(tickers(r), set())

    def test_hanwha_corp_itself(self):
        r = self.m.match("", "김승연 회장이 ㈜한화 지분 11.32%를 증여했다. 한화에어로스페이스도 언급됐다.")
        self.assertEqual(tickers(r), {"000880", "012450"})

    def test_boilerplate_trimmed(self):
        body = "GM 실버라도 EV가 신기록을 세웠다. " * 20 + "#전기픽업트럭 #전기차기술 #맥스레인지 #전기차효율\n\n● 현대차, 2026 싼타페 출시\nblog.naver.com"
        r = self.m.match("GM 실버라도 EV 신기록", body)
        self.assertEqual(tickers(r), set())
        self.assertIsNotNone(r.trimmed_at)
        body = "Universal Health Services reported results. " * 30 + "Stocks that made our list in 2020 include Nvidia (+1,545%)."
        r = self.m.match("Analyst questions", body)
        self.assertEqual(tickers(r), set())
        # a genuine mention early in the text is kept even when a marker appears later
        r = self.m.match("엔비디아 실적", "엔비디아가 실적을 발표했다. " * 20 + "관련기사 삼성전자 …")
        self.assertEqual(tickers(r), {"NVDA"})

    def test_sports_sponsor_downgraded(self):
        body = "김연경 감독이 IBK 기업은행 알토스전 첫 패배 후 선수들과 미팅했다. 우리가 리시브는 기업은행보다 잘했다. 세터들이 안 좋았다."
        r = self.m.match("김연경, 창단 첫 패배 후 독설", body)
        self.assertTrue(r.is_sports)
        strong = {m.ticker for m in r.mentions if m.confidence >= 0.5}
        self.assertEqual(strong, set())
        r = self.m.match("", "기업은행이 중소기업 대출 금리를 내렸다.")
        self.assertEqual(tickers(r), {"024110"})

    def test_inserted_headlines_masked(self):
        body = ("Onsemi announced a buyback. " * 10 + "\n→ Intel's Black Friday Breakout: Apple Rumors Fuel a Holiday Rally\n"
                + "The program starts in 2026. " * 10 + "\nIs Meta Stock a Buy After the Dip?\n" + "Analysts were split. " * 10)
        r = self.m.match("onsemi buyback", body)
        self.assertEqual(tickers(r), set())
        r = self.m.match("", "Intel reported record data-center revenue on Thursday.")
        self.assertEqual(tickers(r), {"INTC"})

    def test_kia_and_hyundai(self):
        r = self.m.match("", "현대차·기아는 EV 판매를 늘렸다. 현대적인 디자인이다.")
        self.assertEqual(tickers(r), {"005380", "000270"})


    def test_krw_conversion_is_not_hanwha(self):
        # 韓貨: a won conversion of a foreign amount, not the Hanwha group
        for body in ("영국에서 커피를 버린 여성이 150파운드(한화 약 29만원)의 과태료를 부과받았다.",
                     "지난해 K푸드 수출액은 70억2000만달러(한화 약 10조849억원)로 집계됐다.",
                     "총가격 2,200엔 (한화 약 2만 원대) 수준이었다.",
                     "이 회사의 상반기 매출은 10억6471만달러로 한화 1조4780억원에 이른다."):
            r = self.m.match("", body)
            self.assertNotIn("000880", tickers(r), body)

    def test_hanwha_company_still_matches(self):
        r = self.m.match("", "한화는 계열사 엣지코어피에프브이의 PF 대출을 위해 담보를 제공했다.")
        self.assertIn("000880", tickers(r))
        r = self.m.match("", "한화가 3조원 규모 수주를 따냈다.")
        self.assertIn("000880", tickers(r))

    def test_strategy_is_not_microstrategy(self):
        # "Strategy, Inc." must stay one alias; a bare "Strategy" would match everyday English
        for body in ("AI CIC장에 유경상 전 전사전략(Corp. Strategy)센터장이 선임됐다.",
                     "ESG(Enterprise Strategy Group)의 검증을 받았다.",
                     "투자 회수(Exit Strategy)를 검토 중이다."):
            r = self.m.match("", body)
            self.assertNotIn("MSTR", tickers(r), body)

    def test_stadium_name_does_not_erase_the_company(self):
        # one team name with no other sports signal must not downgrade the whole document
        r = self.m.match("KT, 갤럭시 Z 폴더블7 AI 체험존 운영",
                         "KT가 체험존을 연다. " * 20 + "수원 KT 위즈파크점, KT플라자 동성로점 등 네 곳에서 운영한다. "
                         + "KT는 MZ세대를 겨냥했다고 밝혔다. " * 20)
        strong = {t["ticker"] for t in r.by_ticker() if t["confidence"] >= 0.5}
        self.assertIn("030200", strong)

    def test_match_recap_still_downgraded(self):
        r = self.m.match("GS칼텍스 개막전 쾌승…기업은행 3-1 제압 [V-리그]",
                         "여자부 개막 경기에서 GS칼텍스가 기업은행을 세트스코어 3-1로 제압했다. "
                         "기업은행은 리시브가 흔들렸다. 감독은 선수 기용을 아쉬워했다.")
        strong = {t["ticker"] for t in r.by_ticker() if t["confidence"] >= 0.5}
        self.assertNotIn("024110", strong)

if __name__ == "__main__":
    unittest.main()
