import assert from "node:assert/strict";
import { fileURLToPath } from "node:url";
import test from "node:test";
import { build } from "esbuild";

const root = new URL("../", import.meta.url);
const bundled = await build({
  entryPoints: [fileURLToPath(new URL("lib/news-crawler.ts", root))],
  bundle: true,
  write: false,
  platform: "node",
  format: "esm",
});
const moduleUrl = `data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].contents).toString("base64")}`;
const { naverNewsApiUrl, parseNaverNewsApi } = await import(moduleUrl);

test("uses the original publisher URL for body extraction when Naver provides both links", () => {
  const [item] = parseNaverNewsApi({
    items: [{
      title: "테스트 기사",
      originallink: "https://publisher.example.com/articles/1",
      link: "https://n.news.naver.com/mnews/article/001/1",
      pubDate: "Tue, 01 Sep 2026 09:00:00 +0900",
      description: "기사 요약",
    }],
  });

  assert.equal(item.link, "https://publisher.example.com/articles/1");
  assert.equal(item.articleUrl, "https://publisher.example.com/articles/1");
});

test("builds bounded Naver news pagination URLs for historical backfill", () => {
  const url = new URL(naverNewsApiUrl("semiconductor", { display: 100, start: 901 }));
  assert.equal(url.searchParams.get("display"), "100");
  assert.equal(url.searchParams.get("start"), "901");

  const bounded = new URL(naverNewsApiUrl("semiconductor", { display: 500, start: 5_000 }));
  assert.equal(bounded.searchParams.get("display"), "100");
  assert.equal(bounded.searchParams.get("start"), "1000");
});
