// ==============================================================================
// Opsora Agent API — Cloudflare Edge Worker
// Provides: health check proxy, model config cache, usage stats cache
// ==============================================================================

const API_ORIGIN = "https://opsora-agent-api.onrender.com";

// KV namespace bindings (configured in wrangler.toml)
// CONFIG_CACHE — caches /v1/models and /v1/billing/pricing
// USAGE_CACHE — caches /v1/usage stats

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    // CORS headers
    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Authorization, Content-Type",
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    // --- Health Check Proxy ---
    if (path === "/health" || path === "/") {
      return handleHealthCheck(env, corsHeaders);
    }

    // --- Model Config (cached) ---
    if (path === "/v1/models" && request.method === "GET") {
      return handleCachedGet("/v1/models", env, request, corsHeaders, 300);
    }

    // --- Pricing Table (cached) ---
    if (path === "/v1/billing/pricing" && request.method === "GET") {
      return handleCachedGet("/v1/billing/pricing", env, request, corsHeaders, 600);
    }

    // --- Usage Stats (cached) ---
    if (path === "/v1/usage" && request.method === "GET") {
      return handleCachedGet("/v1/usage", env, request, corsHeaders, 60);
    }

    // --- Pass-through for everything else ---
    return fetch(API_ORIGIN + url.pathname + url.search, {
      method: request.method,
      headers: request.headers,
      body: request.body,
    });
  },
};

// --- Handlers ---

async function handleHealthCheck(env, corsHeaders) {
  const startTime = Date.now();
  try {
    const resp = await fetch(`${API_ORIGIN}/health`, {
      cf: { cacheTtl: 5 },
    });
    const data = await resp.json();
    const latency = Date.now() - startTime;

    return new Response(
      JSON.stringify({
        ...data,
        edge: "cloudflare",
        edge_latency_ms: latency,
        cached: false,
      }),
      {
        headers: {
          "Content-Type": "application/json",
          ...corsHeaders,
        },
      }
    );
  } catch (err) {
    return new Response(
      JSON.stringify({
        status: "error",
        edge: "cloudflare",
        error: err.message,
        edge_latency_ms: Date.now() - startTime,
      }),
      {
        status: 502,
        headers: {
          "Content-Type": "application/json",
          ...corsHeaders,
        },
      }
    );
  }
}

async function handleCachedGet(apiPath, env, request, corsHeaders, ttlSeconds) {
  const cacheKey = `opsora:${apiPath}`;

  // Try KV cache first
  if (env.CONFIG_CACHE) {
    const cached = await env.CONFIG_CACHE.get(cacheKey, "json");
    if (cached) {
      return new Response(JSON.stringify({ ...cached, _cached: true, _ttl: ttlSeconds }), {
        headers: {
          "Content-Type": "application/json",
          "X-Cache": "HIT",
          ...corsHeaders,
        },
      });
    }
  }

  // Fetch from origin
  try {
    const resp = await fetch(`${API_ORIGIN}${apiPath}`, {
      headers: {
        Authorization: request.headers.get("Authorization") || "",
      },
    });
    const data = await resp.json();

    // Store in KV cache
    if (env.CONFIG_CACHE) {
      await env.CONFIG_CACHE.put(cacheKey, JSON.stringify(data), {
        expirationTtl: ttlSeconds,
      });
    }

    return new Response(JSON.stringify(data), {
      headers: {
        "Content-Type": "application/json",
        "X-Cache": "MISS",
        ...corsHeaders,
      },
    });
  } catch (err) {
    return new Response(
      JSON.stringify({ error: err.message }),
      {
        status: 502,
        headers: { "Content-Type": "application/json", ...corsHeaders },
      }
    );
  }
}
