"""Direct loss must not vanish when the candidate count increases."""
import sys
import unittest
from pathlib import Path
import torch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from train_balanced_news_gat import group_mean_loss,balanced_loss


class BalancedLossTests(unittest.TestCase):
    def test_duplicate_indirect_nodes_do_not_dilute_direct_loss(self):
        for n in (1,10,30):
            x=torch.tensor([[2.]+[6.]*n],requires_grad=True)
            direct=torch.tensor([[True]+[False]*n]);mask=torch.ones_like(direct)
            loss=group_mean_loss(x,mask,direct);self.assertAlmostEqual(loss.item(),4.)
            loss.backward();self.assertAlmostEqual(x.grad[0,0].item(),.5)
            self.assertAlmostEqual(x.grad[0,1:].sum().item(),.5,places=6)
    def test_single_group_fallback_and_masked_extremes(self):
        x=torch.tensor([[2.,4.,999.],[7.,999.,999.]])
        mask=torch.tensor([[True,True,False],[True,False,False]])
        for direct in (torch.ones_like(mask),torch.zeros_like(mask)):
            self.assertAlmostEqual(group_mean_loss(x,mask,direct).item(),5.)
    def test_groups_still_average_articles_equally(self):
        x=torch.tensor([[2.,6.,6.],[10.,2.,2.]])
        mask=torch.ones_like(x,dtype=torch.bool)
        direct=torch.tensor([[True,False,False],[True,False,False]])
        self.assertAlmostEqual(group_mean_loss(x,mask,direct).item(),5.)
    def test_masked_labels_have_no_gradient_and_both_tasks_train(self):
        logits=torch.zeros((1,3,2,3),requires_grad=True)
        pred=torch.zeros((1,3,2),requires_grad=True)
        labels=torch.tensor([[[0,2],[1,1],[2,2]]]);mag=torch.ones((1,3,2))
        mask=torch.tensor([[True,True,False]]);direct=torch.tensor([[True,False,True]])
        loss=balanced_loss(logits,pred,labels,mag,mask,direct);loss.backward()
        self.assertEqual(logits.grad[0,2].abs().sum().item(),0)
        self.assertEqual(pred.grad[0,2].abs().sum().item(),0)
        self.assertGreater(logits.grad[0,:2].abs().sum().item(),0)
        self.assertGreater(pred.grad[0,:2].abs().sum().item(),0)


if __name__=="__main__":unittest.main()
