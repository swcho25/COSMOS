"""Actual local CPU tokenizer regression; no model/GPU or network access."""
import sys
import unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from transformers import AutoTokenizer
from prepare_grounded_training import BUNDLE
from encode_grounded_features import model_windows


class EncodingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer=AutoTokenizer.from_pretrained(BUNDLE/'models/embedding',local_files_only=True,trust_remote_code=False)

    def test_real_backend_all_tokens_beyond_model_limit_are_covered(self):
        text=('삼성전자는 공급 계약을 발표했습니다. Company reported earnings. '*150)+'Distinct final sentence.'
        windows=model_windows(self.tokenizer,text)
        self.assertGreater(len(windows),2)
        joined=[token for row,_ in windows for token,flag in zip(row['input_ids'],row['special_tokens_mask']) if not flag]
        self.assertEqual(joined,self.tokenizer.encode(text,add_special_tokens=False,truncation=False))
        self.assertTrue(all(len(row['input_ids'])<=256 for row,_ in windows))
        self.assertEqual(sum(weight for _,weight in windows),len(joined))

    def test_short_context_is_one_window_with_no_special_token_weight(self):
        windows=model_windows(self.tokenizer,'Apple reported higher earnings.')
        self.assertEqual(len(windows),1)
        row,weight=windows[0];self.assertEqual(weight,len(row['input_ids'])-sum(row['special_tokens_mask']))

    def test_dropped_tail_is_rejected(self):
        real=self.tokenizer
        class BrokenBackend:
            def __getattr__(self,key):return getattr(real.backend_tokenizer,key)
            def post_process(self,*args,**kwargs):
                encoded=real.backend_tokenizer.post_process(*args,**kwargs)
                encoded.truncate(2,stride=0)
                return encoded
        class Broken:
            model_input_names=real.model_input_names
            backend_tokenizer=BrokenBackend()
            def num_special_tokens_to_add(self,**kwargs):return real.num_special_tokens_to_add(**kwargs)
        with self.assertRaisesRegex(ValueError,'Invalid post-processed content tokens'):
            model_windows(Broken(),'Business news. '*700)

    def test_backend_mutable_truncation_and_padding_are_restored(self):
        backend=self.tokenizer.backend_tokenizer
        backend.enable_truncation(7,stride=1);backend.enable_padding(length=9)
        truncation,padding=backend.truncation,backend.padding
        try:
            windows=model_windows(self.tokenizer,'Business news. '*700)
            self.assertGreater(len(windows),2)
            self.assertEqual(backend.truncation,truncation);self.assertEqual(backend.padding,padding)
        finally:backend.no_truncation();backend.no_padding()


if __name__=='__main__':unittest.main()
