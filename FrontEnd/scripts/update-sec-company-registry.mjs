import { writeFile } from "node:fs/promises";
import path from "node:path";

const sourceUrl = "https://www.sec.gov/files/company_tickers_exchange.json";
const response = await fetch(sourceUrl, {
  headers: {
    accept: "application/json",
    "user-agent": "NexusRiskIntelligence/1.0 contact:auddnjs0708@ewha.ac.kr",
  },
});
if (!response.ok) throw new Error(`SEC registry download failed: ${response.status}`);

const payload = await response.json();
if (!payload || !Array.isArray(payload.data)) throw new Error("SEC registry response is invalid");

const byCik = new Map();
for (const row of payload.data) {
  if (!Array.isArray(row) || row.length < 4) continue;
  const [cik, name, ticker, exchange] = row;
  if (!Number.isInteger(cik) || typeof name !== "string" || typeof ticker !== "string") continue;
  const key = String(cik);
  const existing = byCik.get(key) ?? { cik: key, name: cleanName(name), tickers: [], exchanges: [] };
  if (!existing.tickers.includes(ticker)) existing.tickers.push(ticker);
  if (typeof exchange === "string" && exchange && !existing.exchanges.includes(exchange)) existing.exchanges.push(exchange);
  byCik.set(key, existing);
}

const companies = [...byCik.values()]
  .filter((company) => company.name && company.tickers.length)
  .map((company) => ({
    ...company,
    aliases: companyAliases(company.name),
    tickers: company.tickers.sort(),
    exchanges: company.exchanges.sort(),
  }))
  .sort((a, b) => a.name.localeCompare(b.name, "en"));

if (companies.length < 7_000) throw new Error(`SEC registry is unexpectedly small: ${companies.length}`);
if (!companies.some((company) => company.tickers.includes("NVDA") && company.aliases.includes("NVIDIA"))) throw new Error("NVIDIA is missing from SEC registry");
if (!companies.some((company) => company.tickers.includes("AAPL") && company.aliases.includes("Apple"))) throw new Error("Apple is missing from SEC registry");

const generatedAt = new Date().toISOString().slice(0, 10);
const contents = `// Generated from the official SEC company ticker/exchange associations. Do not edit manually.\n` +
  `export type SecListedCompany = { cik: string; name: string; aliases: string[]; tickers: string[]; exchanges: string[] };\n` +
  `export const secRegistrySource = ${JSON.stringify(sourceUrl)};\n` +
  `export const secRegistryAsOf = ${JSON.stringify(generatedAt)};\n` +
  `export const secListedCompanies: SecListedCompany[] = ${JSON.stringify(companies, null, 2)};\n`;

const outputPath = path.resolve("lib/sec-company-registry.generated.ts");
await writeFile(outputPath, contents, "utf8");
console.log(JSON.stringify({ outputPath, companies: companies.length, tickerAssociations: payload.data.length, generatedAt }));

function cleanName(value) {
  return value.replace(/\s+/g, " ").trim();
}

function companyAliases(name) {
  const aliases = [name];
  const simplified = name
    .replace(/\s*[,.]?\s+(?:INCORPORATED|INC\.?|CORPORATION|CORP\.?|COMPANY|CO\.?|LIMITED|LTD\.?|PLC|LLC|N\.V\.?|S\.A\.?|SE)$/i, "")
    .replace(/[,.]$/, "")
    .trim();
  if (simplified && simplified.toLocaleLowerCase("en-US") !== name.toLocaleLowerCase("en-US")) aliases.push(simplified);
  return [...new Set(aliases)];
}
