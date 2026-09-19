"""CPU derivation contract: frozen encoder lineage and full test denominator."""
import contextlib
import json
from pathlib import Path
import shutil
import sys
import unittest
import uuid
from unittest import mock
from types import SimpleNamespace

import numpy as np
import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import derive_grounded_training as d
import prepare_grounded_training as p


@contextlib.contextmanager
def workspace():
    root=Path(__file__).resolve().parents[1]/'artifacts';path=root/('test_grounded_derive_'+uuid.uuid4().hex);path.mkdir()
    try:yield path
    finally:
        assert path.resolve().is_relative_to(root.resolve());shutil.rmtree(path)


def fixture(path):
    parent=path/'parent';parent.mkdir();integrated=path/'integrated';integrated.mkdir()
    p.dump(integrated/'manifest.json',{'artifact_complete':True})
    docs=[{'record_id':rid,'split':split,'graph':'2023'} for rid,split in [('train_bad','train'),('train_good','train'),('validation','val'),('test_bad','test')]]
    pairs=pd.DataFrame([{'article':i,'news_id':doc['record_id'],'ticker':ticker,'split':doc['split'],'market':'KOSPI','y_1d':.02,'y_3d':-.01}
                        for i,doc in enumerate(docs) for ticker in (('A','B') if i in (0,3) else ('A',))])
    with (parent/'articles.jsonl').open('w',encoding='utf8') as stream:
        for row in docs:p.write_row(stream,row)
    pairs.to_parquet(parent/'pairs.parquet',index=False)
    pd.DataFrame({'ticker':['A','B']}).to_json(parent/'companies.json')
    np.save(parent/'feature_mapping.npy',np.array([[1,2],[3,0],[4,0],[5,6]],dtype='int64'))
    np.save(parent/'direct_mask.npy',np.array([[1,1],[1,0],[1,0],[1,1]],dtype=bool))
    np.save(parent/'mentions.npy',np.ones((4,2,5),dtype='float32'))
    np.save(parent/'target_text.npy',np.arange(7*64,dtype='float32').reshape(7,64))
    np.save(parent/'target_sentiment.npy',np.ones((7,3),dtype='float32')/3)
    (parent/'new_feature_entries.jsonl').write_text('{"immutable":"context"}\n')
    p.dump(parent/'protocol.json',{'old_preparation':True});p.dump(parent/'graphs_base.json',{'2023':{'edges':[]}})
    prepared={'version':p.VERSION,'complete':True,'source':{'original':'kg'},'new_feature_entries':6,'old_feature_bank_size':1,'legacy_val_pairs':0,
              'files':{file.name:p.sha(file) for file in parent.iterdir() if file.name not in ('target_text.npy','target_sentiment.npy')}}
    p.dump(parent/'prepared.json',prepared)
    p.dump(parent/'encoding_protocol.json',{'prepared_sha256':p.sha(parent/'prepared.json')})
    p.dump(parent/'encoded.json',{'complete':True,'encoding_protocol_sha256':p.sha(parent/'encoding_protocol.json'),
                                'files':{name:p.sha(parent/name) for name in ('target_text.npy','target_sentiment.npy')}})
    p.dump(path/'review.json',{'reviewed':True,'human_gold':False})
    policy={'schema':d.POLICY_SCHEMA,'frozen_before_training':True,'review_artifact':{'path':str(path/'review.json'),'sha256':p.sha(path/'review.json')},
            'records':[{'article_id':rid,'split':split,'reason':'fixed pre-fit inspection','evidence_type':'recommendation_heading'} for rid,split in [('train_bad','train'),('test_bad','test')]]}
    p.dump(path/'policy.json',policy)
    return parent,integrated,prepared,docs,pairs,policy


def fake_graphs(out,integrated):
    p.dump(out/'graphs_candidate.json',{'2023':{'edges':[]}})
    receipt={'complete':True,'candidate_sha256':p.sha(out/'graphs_candidate.json'),'base_sha256':p.sha(out/'graphs_base.json'),
             'integrated_manifest_sha256':p.sha(integrated/'manifest.json')}
    p.dump(out/'graphs_receipt.json',receipt);return receipt


class DerivationTests(unittest.TestCase):
    def test_train_loss_population_removed_but_test_labels_and_split_unchanged(self):
        with workspace() as path:
            _,_,_,docs,pairs,policy=fixture(path)
            changed_docs,changed_pairs,indexes,counts=d.quality_plan(docs,pairs,policy)
            self.assertEqual(indexes,[0,3]);self.assertEqual(changed_docs[0]['split'],'quality_quarantine');self.assertEqual(changed_docs[3]['split'],'test')
            self.assertEqual(int(changed_pairs.split.eq('train').sum()),1)
            pd.testing.assert_frame_equal(pairs[pairs.split.eq('test')],changed_pairs[changed_pairs.split.eq('test')])
            self.assertEqual(counts['pairs_by_original_split'],{'train':2,'test':2})
            from train_news_impact import batches
            from train_grounded_news import baseline_outputs
            self.assertEqual([int(i) for _,ids in batches(SimpleNamespace(docs=changed_docs),'train',24) for i in ids],[1])
            cls=np.ones((len(changed_pairs),2),dtype='int64');cls[:2]=2
            baseline=baseline_outputs(SimpleNamespace(pairs=changed_pairs,cls=cls,mag=np.zeros_like(cls,dtype='float32')),np.arange(len(changed_pairs)))
            self.assertTrue((baseline['majority'][0].argmax(-1)==1).all())

    def test_fixed_validation_cannot_be_quarantined(self):
        with workspace() as path:
            _,_,_,docs,pairs,policy=fixture(path)
            policy['records']=[{'article_id':'validation','split':'test','reason':'x','evidence_type':'x'}]
            with self.assertRaisesRegex(ValueError,'fixed validation'):d.quality_plan(docs,pairs,policy)

    def test_derived_snapshot_verifies_and_cannot_claim_changed_masks_were_encoded(self):
        with workspace() as path:
            parent,integrated,prepared,_,_,_=fixture(path);before={f.name:p.sha(f) for f in parent.iterdir()}
            with mock.patch.object(p,'verify_prepared',return_value=prepared),mock.patch.object(p,'add_graphs',side_effect=fake_graphs):
                result=d.derive(parent,path/'child',integrated,path/'policy.json');child=path/'child'
                verified=d.verify_dataset(child)
                self.assertEqual(result['counts']['test_pairs'],2);self.assertEqual(result['counts']['test_predictable_pairs'],0)
                for name in ('feature_mapping.npy','direct_mask.npy','mentions.npy'):
                    self.assertFalse(np.load(child/name)[[0,3]].any())
                    np.testing.assert_array_equal(np.load(child/name)[[1,2]],np.load(parent/name)[[1,2]])
                for name in ('target_text.npy','target_sentiment.npy','encoded.json','encoding_protocol.json','new_feature_entries.jsonl'):
                    self.assertEqual(p.sha(child/name),p.sha(parent/name))
                self.assertNotEqual(p.sha(child/'prepared.json'),p.sha(parent/'prepared.json'))
                self.assertEqual(verified['encoding_prepared_sha256'],p.sha(parent/'prepared.json'))
                self.assertEqual(before,{f.name:p.sha(f) for f in parent.iterdir()})
                # A non-approved input alteration is rejected even if the attacker
                # repairs the local file hash and completion marker.
                values=np.load(child/'mentions.npy');values[1,0,0]=123;np.save(child/'mentions.npy',values)
                local=json.loads((child/'prepared.json').read_text());local['files']['mentions.npy']=p.sha(child/'mentions.npy');p.dump(child/'prepared.json',local)
                receipt=json.loads((child/'derivation_complete.json').read_text());receipt['prepared_sha256']=p.sha(child/'prepared.json');p.dump(child/'derivation_complete.json',receipt)
                with self.assertRaisesRegex(ValueError,'outside approved quarantine'):d.verify_dataset(child)

    def test_changed_review_or_encoder_bank_is_rejected(self):
        with workspace() as path:
            parent,integrated,prepared,_,_,_=fixture(path)
            with mock.patch.object(p,'verify_prepared',return_value=prepared),mock.patch.object(p,'add_graphs',side_effect=fake_graphs):
                d.derive(parent,path/'child',integrated,path/'policy.json')
                (path/'review.json').write_text('{}')
                with self.assertRaisesRegex(ValueError,'reviewer artifact changed'):d.verify_dataset(path/'child')


if __name__=='__main__':unittest.main()
