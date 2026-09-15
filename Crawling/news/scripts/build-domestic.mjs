import { build } from "esbuild";
import { fileURLToPath } from "node:url";

await build({
  absWorkingDir: fileURLToPath(new URL("..", import.meta.url)),
  entryPoints: ["services/news_pipeline/domestic_discover.mjs"],
  outfile: "services/news_pipeline/domestic.bundle.mjs",
  bundle: true, platform: "node", format: "esm", target: "node22",
  banner: { js: 'import { createRequire as __createRequire } from "node:module"; const require = __createRequire(import.meta.url);' },
});
