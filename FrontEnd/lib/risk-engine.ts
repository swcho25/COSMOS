import type { RiskEdge, RiskNode, Scenario } from "./risk-data";

export type NodeRisk = { node: RiskNode; score: number; depth: number; direct: boolean };

const relationFactor: Record<RiskEdge["relation"], number> = {
  USES_MATERIAL: 1,
  SUPPLIES_TO: .92,
  OWNS_STAKE: .74,
  BELONGS_TO: .3,
  SHORTAGE_RISK: 1,
  NEWS_MENTIONS: 1,
  EXTRACTED_RELATION: .96,
  MODEL_EXPOSURE: .84,
};

export function diffuseRisk(scenario: Scenario, attenuation = .82): Map<string, NodeRisk> {
  const result = new Map<string, NodeRisk>();
  const byId = new Map(scenario.nodes.map((node) => [node.id, node]));
  const queue: Array<{ id: string; score: number; depth: number }> = [
    { id: scenario.shockSource, score: scenario.severityScore, depth: 0 },
  ];

  while (queue.length) {
    const current = queue.shift()!;
    const previous = result.get(current.id);
    if (previous && previous.score >= current.score) continue;
    const node = byId.get(current.id);
    if (!node) continue;
    result.set(current.id, { node, score: Math.round(current.score), depth: current.depth, direct: current.depth === 1 });
    if (current.depth >= scenario.maxDepth + 1) continue;

    scenario.edges
      .filter((edge) => edge.source === current.id)
      .forEach((edge) => {
        const propagated = current.score * edge.weight * relationFactor[edge.relation] * attenuation;
        if (propagated >= 8) queue.push({ id: edge.target, score: propagated, depth: current.depth + 1 });
      });
  }

  return result;
}

export function rankCompanies(scenario: Scenario): NodeRisk[] {
  return [...diffuseRisk(scenario).values()]
    .filter(({ node }) => node.kind === "company")
    .sort((a, b) => b.score - a.score);
}

export function strongestPath(scenario: Scenario): string[] {
  const risks = rankCompanies(scenario);
  if (!risks.length) return [scenario.shockSource];
  const target = risks.find((risk) => risk.depth === Math.min(3, scenario.maxDepth))?.node.id ?? risks[0].node.id;
  const best = new Map<string, { strength: number; path: string[] }>();
  best.set(scenario.shockSource, { strength: 1, path: [scenario.shockSource] });

  for (let pass = 0; pass < scenario.nodes.length; pass += 1) {
    scenario.edges.forEach((edge) => {
      const source = best.get(edge.source);
      if (!source || source.path.includes(edge.target)) return;
      const strength = source.strength * edge.weight * relationFactor[edge.relation];
      const current = best.get(edge.target);
      if (!current || strength > current.strength) best.set(edge.target, { strength, path: [...source.path, edge.target] });
    });
  }

  return best.get(target)?.path ?? [scenario.shockSource, target];
}

export function controllingEntities(scenario: Scenario): Array<{ owner: string; target: string; confidence: number }> {
  const byId = new Map(scenario.nodes.map((node) => [node.id, node.label]));
  return scenario.edges
    .filter((edge) => edge.relation === "OWNS_STAKE" && edge.weight > .5)
    .map((edge) => ({ owner: byId.get(edge.source) ?? edge.source, target: byId.get(edge.target) ?? edge.target, confidence: Math.round(edge.weight * 100) }));
}
