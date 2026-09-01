// DeepSeek client (OpenAI-compatible). Ship model: deepseek-chat, temp 0, strict
// JSON. Retries with exponential backoff on 429/5xx, same shape as evaluate-video.
// DEEPSEEK_API_BASE env override lets tests point at a local mock (no real key).

import { LLM_BASE, LLM_KEY_ENV, MODEL } from "./config.ts";

const MAX_RETRIES = 2;

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function parseJson(text: string): Record<string, unknown> {
  let cleaned = text.trim();
  if (cleaned.startsWith("```json")) cleaned = cleaned.slice(7);
  else if (cleaned.startsWith("```")) cleaned = cleaned.slice(3);
  if (cleaned.endsWith("```")) cleaned = cleaned.slice(0, -3);
  return JSON.parse(cleaned.trim());
}

// Throws on failure after retries. Callers map that to a 502.
export async function callDeepSeek(
  prompt: string,
): Promise<Record<string, unknown>> {
  const key = Deno.env.get(LLM_KEY_ENV)!;
  const base = Deno.env.get("LLM_API_BASE") ?? LLM_BASE;
  let lastError = "";

  for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
    try {
      const resp = await fetch(`${base}/chat/completions`, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${key}`,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model: MODEL,
          messages: [{ role: "user", content: prompt }],
          temperature: 0,
          response_format: { type: "json_object" },
        }),
      });

      if (!resp.ok) {
        lastError = `HTTP ${resp.status}: ${(await resp.text()).slice(0, 200)}`;
        if (resp.status === 429 || resp.status >= 500) {
          await sleep(1000 * Math.pow(2, attempt));
          continue;
        }
        throw new Error(lastError);
      }

      const data = await resp.json();
      const content = data?.choices?.[0]?.message?.content;
      if (!content) throw new Error("Empty LLM response");
      return parseJson(content);
    } catch (e) {
      lastError = String(e).slice(0, 200);
      if (attempt < MAX_RETRIES) {
        await sleep(1000 * Math.pow(2, attempt));
        continue;
      }
    }
  }

  throw new Error(`DeepSeek failed after ${MAX_RETRIES + 1} attempts: ${lastError}`);
}
