"""CPU fixture: label-independent priors, abstention denominators, epoch replay."""
import contextlib
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace
import unittest
import uuid
from unittest import mock

import numpy as np
import pandas as pd
import torch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import train_grounded_news as train


@contextlib.contextmanager
def workspace():
    root=Path(__file__).resolve().parents[1]/'artifacts'
    path=root/('test_grounded_training_'+uuid.uuid4().hex);path.mkdir()
    try:yield path
    finally:
        assert path.resolve().is_relative_to(root.resolve())
        shutil.rmtree(path)


def small_data():
    frame=pd.DataFrame([{'article':i,'news_id':str(i),'ticker':'A' if i%2==0 else 'B',
        'market':'KOSPI','split':'train' if i<4 else 'val','anchor_date':f'2023-01-{i+1:02d}'} for i in range(8)])
    cls=np.array([[i%3,(i+1)%3] for i in range(8)],dtype='int64');mag=np.ones((8,2),dtype='float32')*.3
    return SimpleNamespace(pairs=frame,cls=cls,mag=mag)


class TrainingTests(unittest.TestCase):
    def test_priors_cannot_see_validation_labels(self):
        data=small_data();indices=np.arange(4,8);before=train.baseline_outputs(data,indices)
        data.cls[4:]=2;data.mag[4:]=.99;after=train.baseline_outputs(data,indices)
        for name in before:
            for a,b in zip(before[name],after[name]):np.testing.assert_array_equal(a,b)

    def test_abstention_is_wrong_not_a_reduced_denominator(self):
        data=small_data();indices=np.arange(4,8)
        probability=np.eye(3,dtype='float32')[data.cls[indices]];covered=np.array([True,False,True,True]);probability[1]=0
        magnitude=np.ones((4,2),dtype='float32')*.3;magnitude[1]=0
        outputs={name:(probability.copy(),magnitude.copy(),covered.copy()) for name in ('candidate','no_news','no_new_graph','majority','market_prior','ticker_prior')}
        metrics,contrasts=train.evaluate_metrics(data,indices,outputs,'val')
        row=metrics[0];self.assertEqual(row['pairs'],4);self.assertEqual(row['covered_pairs'],3)
        self.assertEqual(row['pair_accuracy'],.75);self.assertEqual(row['session_accuracy'],.75)
        self.assertEqual(sum(sum(r) for r in row['confusion_true_rows_pred_columns']),4)
        self.assertTrue(all(row['p']==1 for row in contrasts))

    def test_real_cpu_gat_checkpoint_reload_and_completed_skip(self):
        torch.set_num_threads(1)
        with workspace() as folder:
            count=4;nodes=2
            np.save(folder/'feature_mapping.npy',np.array([[1,0],[0,2],[1,0],[0,2]],dtype='int64'))
            np.save(folder/'target_text.npy',np.array([[0]*64,[.1]*64,[.2]*64],dtype='float32'))
            np.save(folder/'target_sentiment.npy',np.zeros((3,3),dtype='float32'))
            mentions=np.zeros((count,nodes,5),dtype='float32');mentions[np.arange(count),np.arange(count)%2,0]=1
            data=SimpleNamespace(docs=[{'split':'train' if i<2 else 'val','graph':'2020'} for i in range(count)],
                tickers=['A','B'],static={'2020':np.zeros((nodes,3),dtype='float32')},
                graph_arrays={'2020':(np.array([[0,1],[1,0]],dtype='int64'),np.zeros((2,11),dtype='float32'))},
                mentions=mentions,dense_cls=np.zeros((count,nodes,2),dtype='int64'),dense_mag=np.zeros((count,nodes,2),dtype='float32'),mask=mentions[:,:,0].astype(bool))
            runtime=train.Runtime(data,folder,'candidate','cpu');settings=dict(train.SETTINGS,epochs=2,batch_size=2)
            done=train.fit_seed(runtime,folder/'fit',0,'protocol',settings=settings)
            self.assertEqual(done['completed_epochs'],2)
            checkpoint=torch.load(folder/'fit/model.pt',map_location='cpu',weights_only=False)
            model=train.TargetNewsGAT(**checkpoint['config']);model.load_state_dict(checkpoint['state_dict'])
            model.eval()
            with torch.inference_mode():logits,mag=runtime.forward(model,'2020',np.array([2,3]))
            self.assertEqual(tuple(logits.shape),(2,2,2,3));self.assertTrue(torch.isfinite(mag).all())
            self.assertEqual(train.fit_seed(runtime,folder/'fit',0,'protocol',settings=settings),done)
            with self.assertRaises(ValueError):train.fit_seed(runtime,folder/'fit',0,'changed',settings=settings)
            # Interrupt after epoch 0 is committed, then replay the real epoch 1
            # with optimizer, dropout and shuffle random states restored.
            calls=0;real_epoch=runtime.epoch
            def interrupted(*args,**kwargs):
                nonlocal calls
                calls+=1
                if calls==3:raise InterruptedError('power-off simulation')
                return real_epoch(*args,**kwargs)
            with mock.patch.object(runtime,'epoch',side_effect=interrupted):
                with self.assertRaises(InterruptedError):train.fit_seed(runtime,folder/'resumed',0,'protocol',settings=settings)
            replay=train.fit_seed(runtime,folder/'resumed',0,'protocol',settings=settings)
            recovered=torch.load(folder/'resumed/model.pt',map_location='cpu',weights_only=False)
            self.assertEqual(replay['best_epoch'],done['best_epoch'])
            for key in checkpoint['state_dict']:
                torch.testing.assert_close(checkpoint['state_dict'][key],recovered['state_dict'][key],rtol=0,atol=0)
            runtime.mapping._mmap.close()

    def test_uncommitted_stop_never_creates_completed_fit(self):
        with workspace() as folder:
            class Runtime:
                config=dict(n_companies=2,static_dim=3,text_dim=64,edge_dim=11)
                device='cpu'
                def epoch(self,*args,**kwargs):raise InterruptedError('stop')
            with self.assertRaises(InterruptedError):train.fit_seed(Runtime(),folder,0,'p',settings=dict(train.SETTINGS,epochs=1))
            self.assertFalse((folder/'fit_complete.json').exists());self.assertFalse((folder/'epoch_state.json').exists())


if __name__=='__main__':unittest.main()
