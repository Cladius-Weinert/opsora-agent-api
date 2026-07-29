'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { healthCheck } from '@/lib/opsora';
import { Key, Play, BarChart3, ArrowRight } from 'lucide-react';

interface HealthData {
  status: string;
  models: string[];
  total_requests: number;
  total_tokens: number;
}

export default function OverviewPage() {
  const [health, setHealth] = useState<HealthData | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    healthCheck()
      .then(setHealth)
      .catch(() => null)
      .finally(() => setLoading(false));
  }, []);

  const metrics = [
    { label: 'Total Requests', value: health?.total_requests?.toLocaleString() ?? '—' },
    { label: 'Total Tokens', value: health?.total_tokens?.toLocaleString() ?? '—' },
    { label: 'Models Available', value: health?.models?.length?.toString() ?? '—' },
    { label: 'API Status', value: health?.status ?? '—' },
  ];

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold">Dashboard</h1>
        <p className="text-sm mt-1" style={{ color: 'var(--fg-muted)' }}>
          Manage your Opsora Agent API usage and settings
        </p>
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        {metrics.map(m => (
          <div key={m.label} className="metric-card">
            <p className="metric-label">{m.label}</p>
            <p className="metric-value mt-1">{loading ? '...' : m.value}</p>
          </div>
        ))}
      </div>

      {/* Quick Actions */}
      <div>
        <h2 className="text-lg font-semibold mb-4">Quick Actions</h2>
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
          <Link href="/dashboard/keys" className="card flex items-center gap-4 hover:border-indigo-500/50 transition-colors">
            <div className="p-3 rounded-lg" style={{ background: 'var(--bg-elevated)' }}>
              <Key size={20} style={{ color: 'var(--accent)' }} />
            </div>
            <div className="flex-1">
              <p className="font-medium">Create API Key</p>
              <p className="text-xs" style={{ color: 'var(--fg-muted)' }}>Generate a new key to start making requests</p>
            </div>
            <ArrowRight size={16} style={{ color: 'var(--fg-muted)' }} />
          </Link>

          <Link href="/dashboard/playground" className="card flex items-center gap-4 hover:border-indigo-500/50 transition-colors">
            <div className="p-3 rounded-lg" style={{ background: 'var(--bg-elevated)' }}>
              <Play size={20} style={{ color: 'var(--success)' }} />
            </div>
            <div className="flex-1">
              <p className="font-medium">Test Playground</p>
              <p className="text-xs" style={{ color: 'var(--fg-muted)' }}>Try models interactively before integrating</p>
            </div>
            <ArrowRight size={16} style={{ color: 'var(--fg-muted)' }} />
          </Link>

          <Link href="/dashboard/usage" className="card flex items-center gap-4 hover:border-indigo-500/50 transition-colors">
            <div className="p-3 rounded-lg" style={{ background: 'var(--bg-elevated)' }}>
              <BarChart3 size={20} style={{ color: 'var(--warning)' }} />
            </div>
            <div className="flex-1">
              <p className="font-medium">View Usage</p>
              <p className="text-xs" style={{ color: 'var(--fg-muted)' }}>Monitor requests, tokens, and costs</p>
            </div>
            <ArrowRight size={16} style={{ color: 'var(--fg-muted)' }} />
          </Link>
        </div>
      </div>

      {/* Models */}
      {health?.models && (
        <div>
          <h2 className="text-lg font-semibold mb-4">Available Models</h2>
          <div className="card">
            <div className="flex flex-wrap gap-2">
              {health.models.map(m => (
                <span key={m} className="badge" style={{ background: 'var(--bg-elevated)', color: 'var(--accent)' }}>
                  {m}
                </span>
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
