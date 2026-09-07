import { extractNewsArticle } from "../lib/news-article-extractor.ts";

const defaultUrls = [
  "https://n.news.naver.com/mnews/article/003/0014160491",
  "https://www.gukjenews.com/news/articleView.html?idxno=3606281",
  "https://news.skhynix.com/en/sk-hynix-announces-3q25-financial-results/",
  "https://news.samsung.com/global/samsung-electronics-announces-fourth-quarter-and-fy-2025-results",
];
const urls = process.argv.slice(2).filter((value) => !value.startsWith("--"));
const targets = urls.length ? urls : defaultUrls;
let successCount = 0;

console.log(`실제 기사 본문 추출 테스트: ${targets.length}건`);
for (const url of targets) {
  try {
    const article = await extractNewsArticle(url);
    successCount += 1;
    console.log(JSON.stringify({
      ok: true,
      url,
      finalUrl: article.finalUrl,
      title: article.title,
      method: article.method,
      charCount: article.charCount,
      wordCount: article.wordCount,
      publishedTime: article.publishedTime ?? null,
      preview: `${article.textContent.slice(0, 180).replace(/\s+/g, " ")}${article.charCount > 180 ? "…" : ""}`,
    }, null, 2));
  } catch (error) {
    console.log(JSON.stringify({
      ok: false,
      url,
      error: error instanceof Error ? error.message : String(error),
    }, null, 2));
  }
}

console.log(`성공 ${successCount}/${targets.length}건`);
if (successCount === 0) process.exitCode = 1;
