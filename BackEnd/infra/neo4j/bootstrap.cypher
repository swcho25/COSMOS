CREATE CONSTRAINT company_id IF NOT EXISTS FOR (n:Company) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT shareholder_id IF NOT EXISTS FOR (n:Shareholder) REQUIRE n.id IS UNIQUE;
CREATE CONSTRAINT material_id IF NOT EXISTS FOR (n:Material) REQUIRE n.id IS UNIQUE;
CREATE INDEX company_ticker IF NOT EXISTS FOR (n:Company) ON (n.ticker);
CREATE INDEX event_observed_at IF NOT EXISTS FOR (n:Event) ON (n.observedAt);

// Majority-control inference. Keep inferred provenance separate from raw edges.
MATCH (owner)-[stake:OWNS_STAKE]->(company:Company)
WHERE stake.stakeRatio > 0.50
MERGE (owner)-[control:CONTROLLING_ENTITY]->(company)
SET control.inferred = true,
    control.rule = 'majority_stake_v1',
    control.confidence = stake.stakeRatio,
    control.updatedAt = datetime();

// Exclusive supplier inference based on concentration and substitutability.
MATCH (supplier:Company)-[s:SUPPLIES_TO]->(buyer:Company)
WHERE s.revenueShare >= 0.60 AND coalesce(s.substitutability, 0.0) <= 0.20
MERGE (supplier)-[exclusive:EXCLUSIVE_SUPPLIER]->(buyer)
SET exclusive.inferred = true,
    exclusive.rule = 'concentrated_supply_v1',
    exclusive.confidence = s.revenueShare * (1 - coalesce(s.substitutability, 0.0));

// GDS projection for weighted Personalized PageRank.
CALL gds.graph.project(
  'supply-risk',
  ['Company', 'Material', 'Shareholder'],
  {
    SUPPLIES_TO: { properties: 'weight', orientation: 'NATURAL' },
    USES_MATERIAL: { properties: 'weight', orientation: 'REVERSE' },
    SHORTAGE_RISK: { properties: 'weight', orientation: 'NATURAL' },
    OWNS_STAKE: { properties: 'weight', orientation: 'NATURAL' }
  }
);
