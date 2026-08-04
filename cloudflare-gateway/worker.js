// ==============================================================================
// Opsora AI Gateway — Cloudflare Edge Worker (standalone, no backend needed)
// OpenAI-compatible API: routes to NVIDIA NIM (primary) + DashScope (fallback)
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

function pickProvider(model) {
  // NVIDIA hosts namespaced models (provider/model); DashScope hosts bare qwen names
  return model && model.includes("/") ? "nvidia" : "dashscope";
}

async function callProvider(env, provider, body) {
  const base = provider === "nvidia" ? NVIDIA_BASE : DASHSCOPE_BASE;
  const key = provider === "nvidia" ? env.NVIDIA_API_KEY : env.DASHSCOPE_API_KEY;
  const resp = await fetch(`${base}/chat/completions`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${key}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!resp.ok) {
    const text = await resp.text().catch(() => "");
    throw new Error(`${provider} HTTP ${resp.status}: ${text.slice(0, 200)}`);
  }
  return resp;
}

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
        endpoints: ["/v1/models", "/v1/chat/completions"],
        timestamp: new Date().toISOString(),
      });
    }

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
      let body;
      try {
        body = await request.json();
      } catch {
        return json({ error: { message: "invalid JSON body" } }, 400);
      }
      if (!body.messages) {
        return json({ error: { message: "messages field required" } }, 400);
      }

      const requested = body.model || "";
      const primary = pickProvider(requested);
      const order = primary === "nvidia" ? ["nvidia", "dashscope"] : ["dashscope", "nvidia"];
      if (!body.model) body.model = order[0] === "nvidia" ? NVIDIA_DEFAULT : DASHSCOPE_DEFAULT;

      const errors = [];
      for (const provider of order) {
        try {
          const attempt = { ...body };
          if (provider !== primary) {
            // fallback: map to that provider's default model
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

    return json({ error: { message: "not found", available: ["/health", "/v1/models", "/v1/chat/completions"] } }, 404);
  },
};
