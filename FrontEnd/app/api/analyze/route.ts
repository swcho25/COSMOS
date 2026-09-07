import { NextResponse } from "next/server";
import { controllingEntities, diffuseRisk, rankCompanies, strongestPath } from "../../../lib/risk-engine";
import { scenarios } from "../../../lib/risk-data";

export async function GET(request: Request) {
  const url = new URL(request.url);
  const scenarioId = url.searchParams.get("scenario") ?? scenarios[0].id;
  const scenario = scenarios.find((item) => item.id === scenarioId);

  if (!scenario) {
    return NextResponse.json(
      { error: "unknown_scenario", available: scenarios.map((item) => item.id) },
      { status: 404 },
    );
  }

  const risk = diffuseRisk(scenario);
  return NextResponse.json({
    scenario: { id: scenario.id, title: scenario.title, severityScore: scenario.severityScore },
    ranking: rankCompanies(scenario).map((item) => ({
      companyId: item.node.id,
      company: item.node.label,
      score: item.score,
      depth: item.depth,
    })),
    criticalPath: strongestPath(scenario),
    controllingEntities: controllingEntities(scenario),
    nodeScores: Object.fromEntries([...risk.entries()].map(([id, value]) => [id, value.score])),
    model: { algorithm: "weighted_graph_diffusion", attenuation: .82, maxDepth: scenario.maxDepth },
    disclaimer: "Source-linked facts with modeled propagation weights — not investment advice.",
  });
}
