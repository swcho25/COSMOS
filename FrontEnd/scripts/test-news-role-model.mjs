import { pipeline } from "@huggingface/transformers";

const modelId = "onnx-community/Qwen3-0.6B-Instruct-ONNX";
let lastProgress = -1;
const generator = await pipeline("text-generation", modelId, {
  dtype: "q4",
  progress_callback: (progress) => {
    if (progress.status === "progress" && Number.isFinite(progress.progress)) {
      const rounded = Math.round(progress.progress);
      if (rounded !== lastProgress) {
        lastProgress = rounded;
        process.stderr.write(`\rmodel ${rounded}%`);
      }
    }
  },
});
process.stderr.write("\n");

const article = {
  title: "한빛증권, 삼성전자 목표주가 11만원으로 상향",
  summary: "한빛증권 연구원은 삼성전자의 HBM 매출 증가를 전망하며 투자의견 매수를 유지했다.",
  candidates: ["한빛증권", "삼성전자"],
};
const userPrompt = `/no_think
금융 뉴스에서 각 후보 기업의 문맥상 역할을 판별하세요.
affected=실제 사건·실적·계약·가격·위험의 대상 기업, source=목표주가·투자의견을 제시한 증권사나 연구기관, mentioned=비교·배경·단순 언급.
"A증권, B 목표주가 상향"이면 A증권은 source, B는 affected입니다. 후보를 모두 한 번씩 분류하세요.
${JSON.stringify(article)}
confidence는 0.00~1.00 사이의 실제 확신도를 추정하세요.
JSON만 출력: {"companies":[{"name":"후보명 그대로","role":"affected|source|mentioned","confidence":0.95,"reason":"짧은 근거"}]}`;
const formattedPrompt = `<|im_start|>system\n너는 한국 금융뉴스 기업 역할 분류기다. 설명 없이 JSON만 출력한다.<|im_end|>\n<|im_start|>user\n${userPrompt}<|im_end|>\n<|im_start|>assistant\n`;

const output = await generator(formattedPrompt, {
  max_new_tokens: 140,
  do_sample: false,
  return_full_text: false,
  repetition_penalty: 1.05,
});
console.log(JSON.stringify(output, null, 2));

const text = output[0]?.generated_text ?? "";
const jsonStart = text.indexOf("{");
const jsonEnd = text.lastIndexOf("}");
const parsed = JSON.parse(text.slice(jsonStart, jsonEnd + 1));
const source = parsed.companies.find((company) => company.name === "한빛증권")?.role;
const affected = parsed.companies.find((company) => company.name === "삼성전자")?.role;
if (source !== "source" || affected !== "affected") {
  throw new Error(`role_mismatch:${source}:${affected}`);
}
console.log("PASS: 한빛증권=source, 삼성전자=affected");
