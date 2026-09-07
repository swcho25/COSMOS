import unittest

from services.api.engine import infer_controlling_entities, propagate
from services.api.models import GraphEdge, GraphNode, NodeKind, RelationKind


class RiskEngineTest(unittest.TestCase):
    def setUp(self) -> None:
        self.nodes = [
            GraphNode(id="m", label="Material", kind=NodeKind.MATERIAL),
            GraphNode(id="a", label="Supplier", kind=NodeKind.COMPANY),
            GraphNode(id="b", label="Buyer", kind=NodeKind.COMPANY),
        ]
        self.edges = [
            GraphEdge(source="m", target="a", relation=RelationKind.USES_MATERIAL, weight=0.9),
            GraphEdge(source="a", target="b", relation=RelationKind.SUPPLIES_TO, weight=0.8),
        ]

    def test_diffusion_decreases_over_hops(self) -> None:
        ranking, path = propagate(self.nodes, self.edges, "m", 100, 0.8, 3)
        by_id = {item.node_id: item for item in ranking}
        self.assertGreater(by_id["a"].score, by_id["b"].score)
        self.assertEqual(path, ["m", "a", "b"])

    def test_majority_stake_infers_control(self) -> None:
        edge = GraphEdge(source="a", target="b", relation=RelationKind.OWNS_STAKE, weight=0.51)
        inferred = infer_controlling_entities([edge])
        self.assertEqual(len(inferred), 1)
        self.assertEqual(inferred[0].target, "b")


if __name__ == "__main__":
    unittest.main()
