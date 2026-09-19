"""Exercise the whole quality pipeline with poisoned holdout duplicates and feeds."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import prepare_news_quality as pipeline


class IsolationTest(unittest.TestCase):
    def test_holdout_poison_and_duplicate_cannot_enter_candidates(self):
        alias_bytes=(pipeline.HERE/'artifacts/news_impact_v2/aliases.csv').read_bytes()
        code_bytes={n:(pipeline.HERE/n).read_bytes() for n in ('prepare_news_quality.py','news_quality.py','target_news_features.py','news_content.py')}
        def article(rid,date,title,body,split='train'):
            return dict(record_id=rid,published_date=date,title=title,body=body,split=split,region='domestic',year=date[:4],
                available_at=date+'T15:00:00+00:00',companies=[dict(ticker='005930',n_mentions=1,first_pos='title',confidence=1.,method='dictionary')])
        def metadata(r):return dict(record_id=r['record_id'],published_date=r['published_date'],published_at_raw=r['published_date'],published_at=None,published_timezone=None,date_normalization_method='source_parser',canonical_url='https://example.test/'+r['record_id'])
        padding=' 제품과 사업의 세부 내용을 소개하는 기사 문장입니다.'*12
        old=article('old','2020-01-02','삼성전자 소개','기존 학습 자료입니다.'+padding)
        held=article('held','2023-02-01','삼성전자 실적','삼성전자는 매출 증가를 발표했다. 검증 전용 본문입니다.'+padding,'val')
        good=article('good','2019-01-02','삼성전자 실적 발표','삼성전자는 매출과 영업이익 증가를 발표했다. 새로운 기업 사건입니다.'+padding)
        duplicate=dict(good,record_id='duplicate')
        poison=dict(held,record_id='poison',published_date='2019-03-01',year='2019',split='train')
        feed=article('feed','2019-03-02','삼성전자 실적','Technology read more Other read more 삼성전자는 매출 증가를 발표했다.'+padding)
        neutral=article('neutral','2019-03-03','삼성전자 소개','삼성전자 제품의 사용 방법을 소개합니다.'+padding)
        work=(pipeline.HERE/'artifacts').resolve()
        with tempfile.TemporaryDirectory(dir=work,prefix='quality_test_') as t:
            root=Path(t).resolve();assert root.is_relative_to(work)
            legacy=root/'artifacts/news_impact_v2';legacy.mkdir(parents=True);export=root/'export';export.mkdir();out=root/'result'
            for name,content in code_bytes.items():(root/name).write_bytes(content)
            (legacy/'aliases.csv').write_bytes(alias_bytes)
            for n in ('pairs.parquet','label_config.npz','predictions_stage3.npz'):(legacy/n).write_bytes(b'not-readable-labels: must only hash')
            def save(path,rows):path.write_text(''.join(json.dumps(r,ensure_ascii=False)+'\n' for r in rows),encoding='utf8')
            save(legacy/'articles.jsonl',[old,held]);save(export/'legacy_metadata.jsonl',[metadata(old),metadata(held)])
            save(export/'training_candidates.jsonl',[dict(r,**{k:v for k,v in metadata(r).items() if k not in r}) for r in (good,duplicate,held,poison,feed,neutral)])
            # Changed body/ID at an existing training URL is not a new article.
            exported=[json.loads(l) for l in (export/'training_candidates.jsonl').read_text(encoding='utf8').splitlines()]
            for r in exported:
                if r['record_id'] in ('good','duplicate'):r['canonical_url']=metadata(old)['canonical_url']
            save(export/'training_candidates.jsonl',exported)
            with patch.object(pipeline,'HERE',root),patch.object(sys,'argv',['prepare','--export',str(export),'--out',str(out)]),contextlib.redirect_stdout(io.StringIO()):pipeline.main()
            kept=[json.loads(l) for l in (out/'training_candidates_clean.jsonl').read_text(encoding='utf8').splitlines()]
            rejected=[json.loads(l) for l in (out/'rejected_candidates.jsonl').read_text(encoding='utf8').splitlines()]
            self.assertEqual(len(kept),1);self.assertIn(kept[0]['record_id'],{'good','duplicate'})
            self.assertEqual(kept[0]['label_status'],'unlabeled');self.assertEqual(kept[0]['human_review_status'],'pending')
            self.assertTrue(kept[0]['already_in_legacy_train'])
            self.assertEqual(json.loads((out/'completion.json').read_text(encoding='utf8'))['additional_to_legacy_train'],0)
            reasons={r['record_id']:r['reason'] for r in rejected}
            self.assertEqual(reasons['held'],'holdout_overlap');self.assertEqual(reasons['poison'],'holdout_overlap')
            self.assertEqual(reasons['feed'],'feed_quarantine');self.assertEqual(reasons['neutral'],'no_same_sentence_event')
            enriched=[json.loads(l) for l in (out/'legacy_enriched.jsonl').read_text(encoding='utf8').splitlines()]
            for before,after in zip((old,held),enriched):
                for k,v in before.items():self.assertEqual(after[k],v)


if __name__=='__main__':unittest.main()
