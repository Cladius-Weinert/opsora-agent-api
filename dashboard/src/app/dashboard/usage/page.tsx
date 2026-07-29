'use client';

import { useEffect, useState } from 'react';
import { fetchUsage } from '@/lib/opsora';
import type { UsageStats } from '@/lib/opsora';

export default function UsagePage() {
  const [stats, setStats] = useState<UsageStats | null>(null);
  const [period, setPeriod] = useState<'1h' | '24h' | 'all'>('24h');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    fetchUsage().then(setStats).catch(() => null).finally(() => setLoading(false));
  }, []);

  const currentStats = stats
    ? period === '1h' ? stats.last_1h
    : period === '24h' ? stats.last_24h
    : stats.all_time
    : null;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">Usage</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--fg-muted)' }}>
            Monitor your API requests and token consumption
          </p>
        </div>
        <div className="flex gap-1 rounded-lg p-1" style={{ background: 'var(--bg-elevated)' }}>
          {(['1h', '24h', 'all'] as const).map(p => (
            <button
              key={p}
              onClick={() => setPeriod(p)}
              className={`px-3 py-1 rounded text-sm ${period === p ? 'text-white' : ''}`}
              style={{
                background: period === p ? 'var(--accent)' : 'transparent',
                color: period === p ? 'white' : 'var(--fg-muted)',
              }}
            >
              {p === 'all' ? 'All Time' : p === '1h' ? 'Last Hour' : 'Last 24h'}
            </button>
          ))}
        </div>
      </div>

      {/* Metrics */}
      <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
        <div className="metric-card">
          <p className="metric-label">Requests</p>
          <p className="metric-value">{loading ? '...' : currentStats?.requests?.toLocaleString() ?? '0'}</p>
        </div>
        <div className="metric-card">
          <p className="metric-label">Total Tokens</p>
          <p className="metric-value">{loading ? '...' : currentStats?.total_tokens?.toLocaleString() ?? '0'}</p>
        </div>
        <div className="metric-card">
          <p className="metric-label">Input Tokens</p>
          <p className="metric-value">{loading ? '...' : currentStats?.input_tokens?.toLocaleString() ?? '0'}</p>
        </div>
        <div className="metric-card">
          <p className="metric-label">Output Tokens</p>
          <p className="metric-value">{loading ? '...' : currentStats?.output_tokens?.toLocaleString() ?? '0'}</p>
        </div>
      </div>

      {/* Top Models */}
      {stats?.top_models && stats.top_models.length > 0 && (
        <div>
          <h2 className="text-lg font-semibold mb-4">Top Models (Last 30 Days)</h2>
          <div className="card overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr style={{ color: 'var(--fg-muted)' }}>
                  <th className="text-left py-2 px-3">Model</th>
                  <th className="text-right py-2 px-3">Requests</th>
                  <th className="text-right py-2 px-3">Tokens</th>
                  <th className="text-right py-2 px-3">Avg Latency</th>
                </tr>
              </thead>
              <tbody>
                {stats.top_models.map(m => (
                  <tr key={m.model} className="border-t" style={{ borderColor: 'var(--border)' }}>
                    <td className="py-2 px-3 font-medium">{m.model}</td>
                    <td className="text-right py-2 px-3">{m.requests.toLocaleString()}</td>
                    <td className="text-right py-2 px-3">{m.total_tokens.toLocaleString()}</td>
                    <td className="text-right py-2 px-3">{m.avg_latency_ms}ms</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
