// ==============================================================================
// Opsora AI Gateway — Cloudflare Edge Worker (standalone, no backend needed)
// OpenAI-compatible API: routes to NVIDIA NIM (primary) + DashScope (fallback)
// Auth: register/login with JWT (HS256) + PBKDF2 password hashing, D1 storage
// ==============================================================================

const NVIDIA_BASE = "https://integrate.api.nvidia.com/v1";
const DASHSCOPE_BASE = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1";

const NVIDIA_DEFAULT = "meta/llama-3.1-8b-instruct";
const DASHSCOPE_DEFAULT = "qwen-turbo";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Authorization, Content-Type",
};

function json(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "Content-Type": "application/json", ...CORS },
  });
}

// ---------- Crypto helpers (Web Crypto, Workers-native) ----------

const enc = new TextEncoder();

function b64url(buf) {
  return btoa(String.fromCharCode(...new Uint8Array(buf)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function b64urlDecode(str) {
  str = str.replace(/-/g, "+").replace(/_/g, "/");
  while (str.length % 4) str += "=";
  return Uint8Array.from(atob(str), (c) => c.charCodeAt(0));
}

async function hmacKey(secret) {
  return crypto.subtle.importKey("raw", enc.encode(secret), { name: "HMAC", hash: "SHA-256" }, false, ["sign", "verify"]);
}

async function signJWT(payload, secret) {
  const header = { alg: "HS256", typ: "JWT" };
  const h = b64url(enc.encode(JSON.stringify(header)));
  const p = b64url(enc.encode(JSON.stringify(payload)));
  const key = await hmacKey(secret);
  const sig = await crypto.subtle.sign("HMAC", key, enc.encode(`${h}.${p}`));
  return `${h}.${p}.${b64url(sig)}`;
}

async function verifyJWT(token, secret) {
  try {
    const [h, p, s] = token.split(".");
    if (!h || !p || !s) return null;
    const key = await hmacKey(secret);
    const ok = await crypto.subtle.verify("HMAC", key, b64urlDecode(s), enc.encode(`${h}.${p}`));
    if (!ok) return null;
    const payload = JSON.parse(new TextDecoder().decode(b64urlDecode(p)));
    if (payload.exp && payload.exp < Math.floor(Date.now() / 1000)) return null;
    return payload;
  } catch {
    return null;
  }
}

async function hashPassword(password, salt) {
  const keyMaterial = await crypto.subtle.importKey("raw", enc.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt: enc.encode(salt), iterations: 100000, hash: "SHA-256" },
    keyMaterial, 256
  );
  return b64url(bits);
}

function newSalt() {
  return b64url(crypto.getRandomValues(new Uint8Array(16)));
}

// ---------- Auth handlers ----------

async function handleRegister(request, env) {
  let body;
  try { body = await request.json(); } catch { return json({ error: "invalid JSON" }, 400); }
  const email = String(body.email || "").toLowerCase().trim();
  const password = String(body.password || "");
  const name = String(body.name || "").trim() || email.split("@")[0];

  if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) return json({ error: "invalid email" }, 400);
  if (password.length < 6) return json({ error: "password must be at least 6 characters" }, 400);

  const existing = await env.DB.prepare("SELECT id FROM users WHERE email = ?").bind(email).first();
  if (existing) return json({ error: "email already registered" }, 409);

  const salt = newSalt();
  const passwordHash = await hashPassword(password, salt);
  const res = await env.DB.prepare(
    "INSERT INTO users (email, name, password_hash, salt) VALUES (?, ?, ?, ?)"
  ).bind(email, name, passwordHash, salt).run();

  const userId = res.meta.last_row_id;
  const token = await signJWT(
    { sub: userId, email, name, plan: "free", exp: Math.floor(Date.now() / 1000) + 7 * 86400 },
    env.JWT_SECRET
  );
  return json({ token, user: { id: userId, email, name, plan: "free" } }, 201);
}

async function handleLogin(request, env) {
  let body;
  try { body = await request.json(); } catch { return json({ error: "invalid JSON" }, 400); }
  const email = String(body.email || "").toLowerCase().trim();
  const password = String(body.password || "");
  if (!email || !password) return json({ error: "email and password required" }, 400);

  const user = await env.DB.prepare("SELECT * FROM users WHERE email = ?").bind(email).first();
  if (!user) return json({ error: "invalid credentials" }, 401);

  const hash = await hashPassword(password, user.salt);
  if (hash !== user.password_hash) return json({ error: "invalid credentials" }, 401);

  const token = await signJWT(
    { sub: user.id, email: user.email, name: user.name, plan: user.plan, exp: Math.floor(Date.now() / 1000) + 7 * 86400 },
    env.JWT_SECRET
  );
  return json({ token, user: { id: user.id, email: user.email, name: user.name, plan: user.plan } });
}

async function handleMe(request, env) {
  const auth = request.headers.get("Authorization") || "";
  const token = auth.startsWith("Bearer ") ? auth.slice(7) : null;
  if (!token) return json({ error: "missing bearer token" }, 401);
  const payload = await verifyJWT(token, env.JWT_SECRET);
  if (!payload) return json({ error: "invalid or expired token" }, 401);
  return json({ user: { id: payload.sub, email: payload.email, name: payload.name, plan: payload.plan } });
}

// ---------- AI routing ----------

function pickProvider(model) {
  return model && model.includes("/") ? "nvidia" : "dashscope";
}

async function callProvider(env, provider, body) {
  const base = provider === "nvidia" ? NVIDIA_BASE : DASHSCOPE_BASE;
  const key = provider === "nvidia" ? env.NVIDIA_API_KEY : env.DASHSCOPE_API_KEY;
  const resp = await fetch(`${base}/chat/completions`, {
    method: "POST",
    headers: { "Authorization": `Bearer ${key}`, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`${provider} HTTP ${resp.status}: ${text.slice(0, 200)}`);
  }
  return resp;
}

async function handleChat(request, env) {
  let body;
  try { body = await request.json(); } catch { return json({ error: { message: "invalid JSON body" } }, 400); }
  if (!body.messages) return json({ error: { message: "messages field required" } }, 400);

  const requested = body.model || "";
  const primary = pickProvider(requested);
  const order = primary === "nvidia" ? ["nvidia", "dashscope"] : ["dashscope", "nvidia"];
  if (!body.model) body.model = order[0] === "nvidia" ? NVIDIA_DEFAULT : DASHSCOPE_DEFAULT;

  const errors = [];
  for (const provider of order) {
    try {
      const attempt = { ...body };
      if (provider !== primary) {
        attempt.model = provider === "nvidia" ? NVIDIA_DEFAULT : DASHSCOPE_DEFAULT;
      }
      const resp = await callProvider(env, provider, attempt);
      const data = await resp.json();
      data.gateway = "opsora-edge";
      data.provider = provider;
      return json(data);
    } catch (err) {
      errors.push(`${provider}: ${err.message}`);
    }
  }
  return json({ error: { message: "all providers failed", details: errors } }, 502);
}

// ---------- Main ----------

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const path = url.pathname;

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: CORS });
    }

    if (path === "/" || path === "/health") {
      return json({
        status: "ok",
        service: "opsora-gateway",
        edge: "cloudflare",
        providers: ["nvidia", "dashscope"],
        endpoints: ["/v1/models", "/v1/chat/completions", "/auth/register", "/auth/login", "/auth/me"],
        timestamp: new Date().toISOString(),
      });
    }

    // Auth routes
    if (path === "/auth/register" && request.method === "POST") return handleRegister(request, env);
    if (path === "/auth/login" && request.method === "POST") return handleLogin(request, env);
    if (path === "/auth/me" && request.method === "GET") return handleMe(request, env);

    if (path === "/v1/models") {
      return json({
        object: "list",
        data: [
          { id: "meta/llama-3.1-8b-instruct", object: "model", owned_by: "nvidia" },
          { id: "meta/llama-3.1-70b-instruct", object: "model", owned_by: "nvidia" },
          { id: "nvidia/nemotron-mini-4b-instruct", object: "model", owned_by: "nvidia" },
          { id: "nvidia/nemotron-3-super-120b-a12b", object: "model", owned_by: "nvidia" },
          { id: "deepseek-ai/deepseek-v4-flash", object: "model", owned_by: "nvidia" },
          { id: "qwen-turbo", object: "model", owned_by: "dashscope" },
          { id: "qwen-plus", object: "model", owned_by: "dashscope" },
          { id: "qwen-max", object: "model", owned_by: "dashscope" },
          { id: "qwen3-coder-flash", object: "model", owned_by: "dashscope" },
        ],
        gateway: "opsora-edge",
      });
    }

    if (path === "/v1/chat/completions" && request.method === "POST") {
      return handleChat(request, env);
    }

    return json({ error: { message: "not found", available: ["/health", "/v1/models", "/v1/chat/completions", "/auth/register", "/auth/login", "/auth/me"] } }, 404);
  },
};
