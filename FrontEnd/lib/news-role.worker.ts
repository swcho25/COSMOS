/// <reference lib="webworker" />

import { pipeline } from "@huggingface/transformers";
import { buildCompanyRolePrompt, NEWS_ROLE_MODEL_ID, parseCompanyRoleOutput } from "./news-role-analysis";

type AnalyzeMessage = {
  type: "analyze";
  id: string;
  title: string;
  summary: string;
  candidates: string[];
};

type TextGenerator = (input: string, options: Record<string, unknown>) => Promise<unknown>;
const workerScope = self as unknown as DedicatedWorkerGlobalScope;
let generatorPromise: Promise<TextGenerator> | null = null;
let queue = Promise.resolve();
let modelFailed = false;
let downloadProgress = 0;

function postStatus(state: "idle" | "loading" | "ready" | "analyzing" | "error", detail?: Record<string, unknown>) {
  workerScope.postMessage({ type: "status", state, ...detail });
}

function getGenerator() {
  if (!generatorPromise) {
    postStatus("loading", { progress: 0 });
    generatorPromise = pipeline("text-generation", NEWS_ROLE_MODEL_ID, {
      device: "webgpu",
      dtype: "q4f16",
      progress_callback: (event: unknown) => {
        const progress = typeof event === "object" && event && "progress" in event ? Number((event as { progress?: unknown }).progress) : NaN;
        const file = typeof event === "object" && event && "file" in event ? String((event as { file?: unknown }).file ?? "") : "";
        const rounded = Math.round(progress);
        if (file.endsWith(".onnx") && Number.isFinite(progress) && rounded >= downloadProgress) {
          downloadProgress = rounded;
          postStatus("loading", { progress: rounded });
        }
      },
    }).then((generator) => {
      postStatus("ready");
      return generator as unknown as TextGenerator;
    });
  }
  return generatorPromise;
}

function outputText(output: unknown) {
  if (!Array.isArray(output) || !output.length) return "";
  const generated = (output[0] as { generated_text?: unknown }).generated_text;
  if (typeof generated === "string") return generated;
  if (Array.isArray(generated)) {
    const last = generated.at(-1) as { content?: unknown } | undefined;
    return typeof last?.content === "string" ? last.content : "";
  }
  return "";
}

async function analyze(message: AnalyzeMessage) {
  if (modelFailed) return;
  try {
    const generator = await getGenerator();
    postStatus("analyzing", { id: message.id });
    const prompt = buildCompanyRolePrompt(message);
    const formattedPrompt = `<|im_start|>system\n당신은 금융 뉴스 기업 역할 분류기입니다. 설명을 덧붙이지 말고 유효한 JSON만 출력합니다.<|im_end|>\n<|im_start|>user\n${prompt}<|im_end|>\n<|im_start|>assistant\n`;
    const output = await generator(formattedPrompt, {
      max_new_tokens: Math.min(420, 100 + message.candidates.length * 70),
      do_sample: false,
      return_full_text: false,
    });
    const companies = parseCompanyRoleOutput(output, message.candidates);
    workerScope.postMessage({ type: "result", id: message.id, companies, raw: companies ? undefined : outputText(output).slice(0, 500) });
    postStatus("ready");
  } catch (error) {
    generatorPromise = null;
    modelFailed = true;
    postStatus("error", { id: message.id, message: error instanceof Error ? error.message : String(error) });
  }
}

workerScope.addEventListener("message", (event: MessageEvent<AnalyzeMessage>) => {
  if (event.data?.type !== "analyze") return;
  queue = queue.then(() => analyze(event.data));
});

postStatus("idle");

export {};
