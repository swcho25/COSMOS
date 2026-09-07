from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from .engine import infer_controlling_entities, path_strength, propagate
from .models import PropagationRequest, PropagationResponse


app = FastAPI(
    title="NEXUS RISK API",
    description="Financial supply-chain shock propagation and governance inference API",
    version="0.1.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://localhost:3001"],
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "nexus-risk-api", "graph_engine": "ready"}


@app.post("/v1/analysis/propagate", response_model=PropagationResponse)
def analyze_propagation(payload: PropagationRequest) -> PropagationResponse:
    try:
        ranking, critical_path = propagate(
            nodes=payload.nodes,
            edges=payload.edges,
            shock_node_id=payload.shock_node_id,
            shock_score=payload.shock_score,
            attenuation=payload.attenuation,
            max_depth=payload.max_depth,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PropagationResponse(
        shock_node_id=payload.shock_node_id,
        ranking=ranking,
        critical_path=critical_path,
    )


@app.post("/v1/analysis/rules")
def analyze_rules(payload: PropagationRequest) -> dict[str, object]:
    inferred = infer_controlling_entities(payload.edges)
    return {"count": len(inferred), "relations": [item.model_dump() for item in inferred]}


@app.post("/v1/analysis/path-strength")
def analyze_path_strength(payload: PropagationRequest) -> dict[str, object]:
    try:
        _, critical_path = propagate(
            payload.nodes,
            payload.edges,
            payload.shock_node_id,
            payload.shock_score,
            payload.attenuation,
            payload.max_depth,
        )
        strength = path_strength(critical_path, payload.edges)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"critical_path": critical_path, "multiplicative_strength": strength}
