// CORS + JSON response helpers + JWT auth, shared by the three Edge Functions.

export const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
  "Access-Control-Allow-Methods": "POST, OPTIONS",
};

export function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

export type Auth =
  | { kind: "service" }
  | { kind: "user"; userId: string }
  | { kind: "invalid" };

// Service-role bearer skips quota. User JWT is verified against auth/v1/user
// (same pattern as api/evaluate.py). Anything else is invalid.
export async function authenticate(req: Request): Promise<Auth> {
  const token = (req.headers.get("Authorization") ?? "").replace(/^Bearer\s+/i, "").trim();
  if (!token) return { kind: "invalid" };

  if (token === Deno.env.get("SUPABASE_SERVICE_ROLE_KEY")!) return { kind: "service" };
  // Explicit crawler/seed secret. Supabase injects SUPABASE_SERVICE_ROLE_KEY as the
  // new sb_secret value (not the legacy JWT), so the crawler can't match on that;
  // it authenticates with this dedicated shared secret instead.
  const crawlerSecret = Deno.env.get("CRAWLER_SECRET");
  if (crawlerSecret && token === crawlerSecret) return { kind: "service" };

  const url = Deno.env.get("SUPABASE_URL")!;
  const anon = Deno.env.get("SUPABASE_ANON_KEY")!;
  const resp = await fetch(`${url}/auth/v1/user`, {
    headers: { Authorization: `Bearer ${token}`, apikey: anon },
  });
  if (!resp.ok) return { kind: "invalid" };
  const user = await resp.json();
  if (!user?.id) return { kind: "invalid" };
  return { kind: "user", userId: user.id };
}

export async function sha256(text: string): Promise<string> {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf)).map((b) => b.toString(16).padStart(2, "0")).join("");
}
