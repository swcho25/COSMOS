import { writeFile } from "node:fs/promises";
import path from "node:path";

const sourceUrl = "https://kind.krx.co.kr/corpgeneral/corpList.do?method=download";
const response = await fetch(sourceUrl, { headers: { "user-agent": "NexusRiskRegistry/1.0" } });
if (!response.ok) throw new Error(`KRX registry download failed: ${response.status}`);

const html = new TextDecoder("euc-kr").decode(await response.arrayBuffer());
const rows = [...html.matchAll(/<tr[^>]*>([\s\S]*?)<\/tr>/gi)].flatMap((row) => {
  const cells = [...row[1].matchAll(/<td[^>]*>([\s\S]*?)<\/td>/gi)].map((cell) => cleanCell(cell[1]));
  if (cells.length < 10 || !/^\d{6}$/.test(cells[2])) return [];
  return [{
    name: cells[0],
    market: normalizeMarket(cells[1]),
    code: cells[2],
    industry: cells[3],
  }];
}).filter((company) => company.name && company.industry);

const unique = rows
  .filter((company, index) => rows.findIndex((candidate) => candidate.code === company.code) === index)
  .sort((a, b) => a.name.localeCompare(b.name, "ko"));

if (unique.length < 2_000) throw new Error(`KRX registry is unexpectedly small: ${unique.length}`);
if (!unique.some((company) => company.name === "LG전자" && company.code === "066570")) throw new Error("LG전자 is missing from KRX registry");

const generatedAt = new Date().toISOString().slice(0, 10);
const contents = `// Generated from the official KRX KIND listed-company registry. Do not edit manually.\n` +
  `export type KrxListedCompany = { name: string; code: string; market: string; industry: string };\n` +
  `export const krxRegistrySource = ${JSON.stringify(sourceUrl)};\n` +
  `export const krxRegistryAsOf = ${JSON.stringify(generatedAt)};\n` +
  `export const krxListedCompanies: KrxListedCompany[] = ${JSON.stringify(unique, null, 2)};\n`;

const outputPath = path.resolve("lib/krx-company-registry.generated.ts");
await writeFile(outputPath, contents, "utf8");
console.log(JSON.stringify({ outputPath, count: unique.length, generatedAt }));

function cleanCell(value) {
  return decodeEntities(value.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim());
}

function decodeEntities(value) {
  const named = { amp: "&", apos: "'", quot: '"', lt: "<", gt: ">", nbsp: " " };
  return value
    .replace(/&([a-z]+);/gi, (match, name) => named[name.toLowerCase()] ?? match)
    .replace(/&#(\d+);/g, (_, code) => String.fromCodePoint(Number(code)))
    .replace(/&#x([\da-f]+);/gi, (_, code) => String.fromCodePoint(Number.parseInt(code, 16)));
}

function normalizeMarket(value) {
  if (value.includes("코스닥")) return "KOSDAQ";
  if (value.includes("코넥스")) return "KONEX";
  if (value.includes("유가")) return "KOSPI";
  return value || "KRX";
}
