// Weighted propagation paths from a selected shock source.
MATCH path = (shock {id: $shockId})-[rels:SHORTAGE_RISK|SUPPLIES_TO|OWNS_STAKE*1..4]->(affected:Company)
WITH path, affected,
     reduce(strength = $shockScore, rel IN rels | strength * rel.weight * 0.82) AS propagatedRisk
WHERE propagatedRisk >= $minimumRisk
RETURN affected.id AS companyId,
       affected.name AS company,
       propagatedRisk,
       [node IN nodes(path) | node.name] AS path
ORDER BY propagatedRisk DESC
LIMIT $topK;

// Personalized PageRank seeded by the shock node.
MATCH (source {id: $shockId})
CALL gds.pageRank.stream('supply-risk', {
  sourceNodes: [source],
  relationshipWeightProperty: 'weight',
  dampingFactor: 0.85,
  maxIterations: 30
})
YIELD nodeId, score
WITH gds.util.asNode(nodeId) AS node, score
WHERE node:Company
RETURN node.id, node.name, score
ORDER BY score DESC
LIMIT $topK;
