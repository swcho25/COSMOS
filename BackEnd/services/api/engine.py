from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from math import prod

from .models import GraphEdge, GraphNode, NodeExposure, RelationKind, RuleInference


RELATION_FACTOR = {
    RelationKind.USES_MATERIAL: 1.00,
    RelationKind.SUPPLIES_TO: 0.92,
    RelationKind.OWNS_STAKE: 0.74,
    RelationKind.BELONGS_TO: 0.30,
    RelationKind.SHORTAGE_RISK: 1.00,
}


@dataclass(frozen=True)
class _State:
    node_id: str
    score: float
    depth: int
    path: tuple[str, ...]


def propagate(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    shock_node_id: str,
    shock_score: float = 100,
    attenuation: float = 0.82,
    max_depth: int = 4,
) -> tuple[list[NodeExposure], list[str]]:
    """Run deterministic weighted diffusion and return ranking + strongest path."""
    node_map = {node.id: node for node in nodes}
    if shock_node_id not in node_map:
        raise ValueError(f"Unknown shock node: {shock_node_id}")

    outgoing: dict[str, list[GraphEdge]] = defaultdict(list)
    for edge in edges:
        if edge.source not in node_map or edge.target not in node_map:
            raise ValueError(f"Edge references unknown node: {edge.source}->{edge.target}")
        outgoing[edge.source].append(edge)

    best: dict[str, _State] = {}
    queue: deque[_State] = deque([_State(shock_node_id, shock_score, 0, (shock_node_id,))])

    while queue:
        current = queue.popleft()
        previous = best.get(current.node_id)
        if previous and previous.score >= current.score:
            continue
        best[current.node_id] = current
        if current.depth >= max_depth:
            continue

        for edge in outgoing[current.node_id]:
            if edge.target in current.path:
                continue
            next_score = current.score * edge.weight * RELATION_FACTOR[edge.relation] * attenuation
            if next_score >= 5:
                queue.append(_State(edge.target, next_score, current.depth + 1, current.path + (edge.target,)))

    exposures = [
        NodeExposure(
            node_id=state.node_id,
            label=node_map[state.node_id].label,
            score=round(state.score, 2),
            depth=state.depth,
            direct=state.depth == 1,
        )
        for state in best.values()
        if state.node_id != shock_node_id
    ]
    exposures.sort(key=lambda item: item.score, reverse=True)

    terminal = max(
        (state for state in best.values() if state.node_id != shock_node_id),
        key=lambda state: (state.depth, state.score),
        default=best[shock_node_id],
    )
    return exposures, list(terminal.path)


def infer_controlling_entities(edges: list[GraphEdge]) -> list[RuleInference]:
    """Apply a Cypher/SWRL-equivalent majority-control rule."""
    return [
        RuleInference(
            source=edge.source,
            target=edge.target,
            confidence=round(edge.weight, 4),
            rule="OWNS_STAKE.weight >= 0.50 -> CONTROLLING_ENTITY",
        )
        for edge in edges
        if edge.relation == RelationKind.OWNS_STAKE and edge.weight >= 0.50
    ]


def path_strength(path: list[str], edges: list[GraphEdge]) -> float:
    weights = {(edge.source, edge.target): edge.weight * RELATION_FACTOR[edge.relation] for edge in edges}
    return round(prod(weights[(a, b)] for a, b in zip(path, path[1:])), 6) if len(path) > 1 else 1.0
