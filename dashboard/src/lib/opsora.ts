const OPSORA_API_URL = process.env.NEXT_PUBLIC_OPSORA_API_URL || 'http://localhost:8080';

interface Model {
  id: string;
  label: string;
  description: string;
  speed: string;
}

interface UsageStats {
  all_time: { requests: number; total_tokens: number; input_tokens: number; output_tokens: number };
  last_24h: { requests: number; total_tokens: number; input_tokens: number; output_tokens: number };
  last_1h: { requests: number; total_tokens: number; input_tokens: number; output_tokens: number };
  top_models: { model: string; requests: number; total_tokens: number; avg_latency_ms: number }[];
}

interface BillingSummary {
  plan: string;
  plan_name: string;
  monthly_quota_tokens: number;
  tokens_used_this_cycle: number;
  remaining_tokens: number;
  percentage_used: number;
  total_cost_idr: number;
  days_remaining_in_cycle: number;
}

async function opsoraFetch(path: string, options: RequestInit = {}) {
  const res = await fetch(`${OPSORA_API_URL}${path}`, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
  });
  if (!res.ok) {
    const error = await res.json().catch(() => ({ error: { message: 'Request failed' } }));
    throw new Error(error.error?.message || `HTTP ${res.status}`);
  }
  return res.json();
}

export async function fetchModels(): Promise<{ data: Model[] }> {
  return opsoraFetch('/v1/models');
}

export async function fetchUsage(): Promise<UsageStats> {
  return opsoraFetch('/v1/usage');
}

export async function fetchBilling(): Promise<BillingSummary> {
  return opsoraFetch('/v1/billing');
}

export async function chatCompletion(
  model: string,
  messages: { role: string; content: string }[],
  stream = false
) {
  return opsoraFetch('/v1/chat/completions', {
    method: 'POST',
    body: JSON.stringify({ model, messages, stream }),
  });
}

export async function healthCheck() {
  return opsoraFetch('/health');
}

export type { Model, UsageStats, BillingSummary };
