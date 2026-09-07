import { cp, mkdir, writeFile } from "node:fs/promises";
import path from "node:path";
import { pathToFileURL } from "node:url";

const outputArg = process.argv[2];
if (!outputArg) throw new Error("Usage: node scripts/build-aws-static.mjs <new-output-directory>");

const outputDir = path.resolve(outputArg);
if (!path.basename(outputDir).startsWith("aws-static-")) throw new Error("Output directory must start with aws-static-");
await mkdir(outputDir, { recursive: false });

const workerUrl = pathToFileURL(path.resolve("dist/server/index.js"));
workerUrl.searchParams.set("aws-static", Date.now().toString());
const { default: worker } = await import(workerUrl.href);
const routes = ["/", "/news", "/network", "/companies", "/data", "/community", "/mypage"];

for (const route of routes) {
  const response = await worker.fetch(
    new Request(`https://static.local${route}`, { headers: { accept: "text/html" } }),
    { ASSETS: { fetch: async () => new Response("Not found", { status: 404 }) } },
    { waitUntil() {}, passThroughOnException() {} },
  );
  if (!response.ok) throw new Error(`${route} rendered with ${response.status}`);
  const routeDir = route === "/" ? outputDir : path.join(outputDir, route.slice(1));
  await mkdir(routeDir, { recursive: true });
  await writeFile(path.join(routeDir, "index.html"), await response.text(), "utf8");
}

await cp(path.resolve("dist/client/_next"), path.join(outputDir, "_next"), { recursive: true });
await cp(path.resolve("public/og.png"), path.join(outputDir, "og.png"));
await cp(path.resolve("public/company-logos"), path.join(outputDir, "company-logos"), { recursive: true });
await cp(path.join(outputDir, "index.html"), path.join(outputDir, "error.html"));

console.log(outputDir);
