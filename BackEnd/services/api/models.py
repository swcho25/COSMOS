from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class NodeKind(str, Enum):
    COMPANY = "Company"
    SHAREHOLDER = "Shareholder"
    INDUSTRY = "Industry"
    MATERIAL = "Material"
    EVENT = "Event"


class RelationKind(str, Enum):
    OWNS_STAKE = "OWNS_STAKE"
    SUPPLIES_TO = "SUPPLIES_TO"
    BELONGS_TO = "BELONGS_TO"
    USES_MATERIAL = "USES_MATERIAL"
    SHORTAGE_RISK = "SHORTAGE_RISK"


class GraphNode(BaseModel):
    id: str
    label: str
    kind: NodeKind
    ticker: str | None = None
    credit_rating: str | None = None
    market_cap_krw: int | None = None


class GraphEdge(BaseModel):
    source: str
    target: str
    relation: RelationKind
    weight: float = Field(ge=0, le=1)
    label: str | None = None
    evidence: str | None = None


class PropagationRequest(BaseModel):
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    shock_node_id: str
    shock_score: float = Field(default=100, ge=0, le=100)
    attenuation: float = Field(default=0.82, gt=0, le=1)
    max_depth: int = Field(default=4, ge=1, le=10)


class NodeExposure(BaseModel):
    node_id: str
    label: str
    score: float
    depth: int
    direct: bool


class PropagationResponse(BaseModel):
    shock_node_id: str
    ranking: list[NodeExposure]
    critical_path: list[str]
    algorithm: Literal["weighted_graph_diffusion"] = "weighted_graph_diffusion"


class RuleInference(BaseModel):
    relation: Literal["CONTROLLING_ENTITY"] = "CONTROLLING_ENTITY"
    source: str
    target: str
    confidence: float
    rule: str
