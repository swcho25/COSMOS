import { readdir, unlink } from "node:fs/promises";
import path from "node:path";

const serverStaticRoots = [
  path.resolve("dist/server/_next/static"),
  path.resolve("dist/server/ssr/_next/static"),
];

let removed = 0;
for (const root of serverStaticRoots) {
  const entries = await readdir(root, { withFileTypes: true }).catch(() => []);
  for (const entry of entries) {
    if (!entry.isFile() || !/^ort-wasm.*\.wasm$/i.test(entry.name)) continue;
    await unlink(path.join(root, entry.name));
    removed += 1;
  }
}

console.log(`Pruned ${removed} server-only ONNX Runtime WASM copies; client asset retained.`);
