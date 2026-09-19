"""Context selection preserves literal spans, late mentions, and exact token limits."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from news_company_context import select_context,merge_spans


class CharacterTokenizer:
    def num_special_tokens_to_add(self,pair=False):return 2
    def __call__(self,text,add_special_tokens=True,**kwargs):
        return {"input_ids":list(range(len(text)+(2 if add_special_tokens else 0))),
                "offset_mapping":[(i,i+1) for i in range(len(text))]}


class CompanyContextTests(unittest.TestCase):
    def test_late_company_is_visible_with_same_budget(self):
        title="Business update";body="lead "*200+"Krafton signed an agreement."
        original=title+"\n"+body;pos=original.index("Krafton")
        hits=[{"ticker":"259960","start":pos,"end":pos+7}]
        text,audit=select_context(title,body,hits,CharacterTokenizer(),128)
        self.assertIn("Krafton",text);self.assertIn("259960",audit["covered"])
        self.assertLessEqual(audit["tokens"],128)
        self.assertEqual(text,"\n".join(original[a:b] for a,b in audit["spans"]))

    def test_multiple_companies_and_duplicate_mentions(self):
        title="News";body="x "*80+"Alpha agrees. "+"y "*80+"Beta supplies."
        original=title+"\n"+body
        hits=[{"ticker":name,"start":original.index(name),"end":original.index(name)+len(name)} for name in ("Alpha","Beta")]
        text,audit=select_context(title,body,hits+hits,CharacterTokenizer(),128)
        self.assertEqual(audit["covered"],["Alpha","Beta"])
        self.assertEqual((text,audit),select_context(title,body,list(reversed(hits)),CharacterTokenizer(),128))
        self.assertLessEqual(audit["tokens"],128)

    def test_empty_and_many_mentions_respect_budget(self):
        for text in ("", "a"*1000):
            _,audit=select_context("",text,[],CharacterTokenizer(),128)
            self.assertLessEqual(audit["tokens"],128)
        body=" ".join(f"Company{i}" for i in range(50));original="\n"+body
        hits=[{"ticker":str(i),"start":original.index(f"Company{i} "),"end":original.index(f"Company{i} ")+len(f"Company{i}")}
              for i in range(49)]
        _,audit=select_context("",body,hits,CharacterTokenizer(),128)
        self.assertLessEqual(audit["tokens"],128)
        self.assertLess(len(audit["covered"]),len(audit["matched"]))

    def test_overlap_merge_keeps_source_order(self):
        self.assertEqual(merge_spans([(9,12),(0,4),(3,6),(9,12)]),[(0,6),(9,12)])


if __name__=="__main__":unittest.main()
