"""CPU checks that the comparison removes messages, not nodes or model weights."""
import sys
import unittest
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from target_news_model import TargetNewsGAT


class GraphMessageIsolationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        torch.set_num_threads(1)

    def setUp(self):
        # Both tests instantiate identical weights. No checkpoint or GPU is used.
        torch.manual_seed(314159)
        self.model = TargetNewsGAT(n_companies=3, static_dim=3, text_dim=4).eval()
        self.text = torch.tensor([[[.1, -.2, .3, -.4], [0., 0., 0., 0.], [0., 0., 0., 0.]]])
        self.changed = self.text.clone()
        self.changed[0, 0] += torch.tensor([1.2, -3.2, 4.4, -1.6])
        self.sentiment = torch.zeros((1, 3, 3))
        self.mentions = torch.zeros((1, 3, 5))
        self.mentions[0, 0, 0] = 1.  # Only source node 0 receives article text.
        self.static = torch.tensor([[1., 0., 0.], [0., 1., 0.], [0., 0., 1.]])

    def compare(self, edge_index, edge_attr):
        before = {name: value.clone() for name, value in self.model.state_dict().items()}
        parameter_count = sum(p.numel() for p in self.model.parameters())
        with torch.inference_mode():
            original = self.model(self.text, self.sentiment, self.mentions, self.static, edge_index, edge_attr)
            changed = self.model(self.changed, self.sentiment, self.mentions, self.static, edge_index, edge_attr)
        self.assertEqual(original[0].shape, (1, 3, 2, 3))
        self.assertEqual(original[1].shape, (1, 3, 2))
        self.assertEqual(parameter_count, sum(p.numel() for p in self.model.parameters()))
        for name, value in self.model.state_dict().items():
            torch.testing.assert_close(value, before[name], rtol=0., atol=0.)
        self.assertGreater(float((original[0][0, 0] - changed[0][0, 0]).abs().max()), 1e-5)
        return original, changed

    def test_no_edges_keeps_self_response_and_isolates_other_nodes(self):
        # GATv2 automatically supplies self-loops; all three candidate nodes stay.
        edges = torch.empty((2, 0), dtype=torch.long)
        attributes = torch.empty((0, 11), dtype=torch.float32)
        original, changed = self.compare(edges, attributes)
        for initial, altered in zip(original, changed):
            torch.testing.assert_close(initial[:, 1:], altered[:, 1:], rtol=0., atol=0.)

    def test_directed_edge_carries_news_to_recipient_only(self):
        # A source -> recipient edge must carry a text change to node 1. Node 2
        # is still disconnected, so retaining its output detects global mixing.
        edges = torch.tensor([[0], [1]], dtype=torch.long)
        attributes = torch.zeros((1, 11), dtype=torch.float32)
        attributes[0, 0] = 1.
        original, changed = self.compare(edges, attributes)
        self.assertGreater(float((original[0][0, 1] - changed[0][0, 1]).abs().max()), 1e-5)
        for initial, altered in zip(original, changed):
            torch.testing.assert_close(initial[:, 2], altered[:, 2], rtol=0., atol=0.)


if __name__ == '__main__':
    unittest.main()
