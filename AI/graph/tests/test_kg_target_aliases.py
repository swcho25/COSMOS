from pathlib import Path
import sys
import io
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import kg_target_aliases as aliases


class AliasTests(unittest.TestCase):
    def test_real_korean_and_acronym_names_are_hints(self):
        self.assertIn("AMD", aliases.matched_aliases({"ticker":"AMD", "title":"AMD의 신제품", "body":""}))
        self.assertIn("테슬라", aliases.matched_aliases({"ticker":"TSLA", "title":"테슬라 판매 증가", "body":""}))
        self.assertEqual(aliases.matched_aliases({"ticker":"TSLA", "title":"TSLA 주가", "body":""}), [])

    def test_word_boundaries_and_unknown_target(self):
        self.assertNotIn("AMD", aliases.matched_aliases({"ticker":"AMD", "title":"XAMDfoo", "body":""}))
        self.assertEqual(aliases.matched_aliases({"ticker":"not-a-company", "title":"삼성전자", "body":""}), [])
        self.assertFalse(aliases.provenance()["historical_identity_verified"])

    def test_shared_group_product_and_ticker_aliases_are_not_authority(self):
        text=("alias,ticker,alias_type,ambiguity\nAlpha,AAA,official,safe\n"
              "Shared,AAA,en_rule,safe\nShared,BBB,en_rule,safe\n"
              "Group,AAA,group,needs_context\nWidget,AAA,product,product\n"
              "AAA,AAA,ticker,ticker_only\n")
        with patch.object(aliases,"sha",return_value="fixture"), patch.object(Path,"open",return_value=io.StringIO(text)):
            self.assertEqual(aliases.matched_aliases({"ticker":"AAA", "title":"Alpha Shared Group Widget AAA", "body":""}),["Alpha"])
            with self.assertRaisesRegex(ValueError,"changed"):
                aliases._read(str(aliases.ALIAS_FILE),"0"*64)


if __name__ == "__main__":
    unittest.main()
