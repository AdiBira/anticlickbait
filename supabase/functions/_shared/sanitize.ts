// Defense-in-depth for prompt-injection (C2): the LLM's honest_title / verdict_line
// / reasonings are shown to other users, so anything that survived the prompt
// hardening is scrubbed before storage. Pure - no runtime access, node-testable.

const URL_PATTERNS = [
  /\bhttps?:\/\/\S+/gi, // http(s) links
  /\bwww\.\S+/gi, // www. links without scheme
  // bare domains (foo.com, foo.com/path) for a common set of TLDs
  /\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|net|org|io|co|gg|tv|xyz|info|app|dev|me|ru|cn|de|uk|us|biz|link|site)\b\S*/gi,
];

// Built from a string so no literal control chars appear in source.
const CONTROL_CHARS = new RegExp("[\\u0000-\\u001F\\u007F]", "g");

// Strip URLs and control chars, collapse whitespace, hard-cap length.
export function sanitizeText(s: string, maxLen: number): string {
  let out = s;
  for (const re of URL_PATTERNS) out = out.replace(re, "");
  out = out.replace(CONTROL_CHARS, " ");
  out = out.replace(/\s+/g, " ").trim();
  return out.slice(0, maxLen);
}
