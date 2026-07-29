'use client';

import { useEffect, useState } from 'react';
import { CreditCard, TrendingUp, AlertTriangle } from 'lucide-react';

interface Plan {
  id: string;
  name: string;
  monthly_price_idr: number | null;
  monthly_quota_tokens: number | null;
  features: string[];
}

const PLANS: Plan[] = [
  { id: 'free', name: 'Free', monthly_price_idr: 0, monthly_quota_tokens: 100_000, features: ['All models', 'Community support'] },
  { id: 'starter', name: 'Starter', monthly_price_idr: 490_000, monthly_quota_tokens: 5_000_000, features: ['All models', 'Email support', 'Webhooks'] },
  { id: 'pro', name: 'Pro', monthly_price_idr: 990_000, monthly_quota_tokens: 20_000_000, features: ['All models', 'Priority support', 'Webhooks', 'Analytics'] },
  { id: 'business', name: 'Business', monthly_price_idr: 2_490_000, monthly_quota_tokens: 100_000_000, features: ['All models', 'Dedicated support', 'SLA', 'Teams'] },
];

export default function BillingPage() {
  const [currentPlan] = useState('free');
  const [tokensUsed] = useState(0);
  const [quota] = useState(100_000);

  const percentage = quota > 0 ? Math.min(100, (tokensUsed / quota) * 100) : 0;

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold">Billing</h1>
        <p className="text-sm mt-1" style={{ color: 'var(--fg-muted)' }}>
          Manage your subscription and view usage
        </p>
      </div>

      {/* Current Plan */}
      <div className="card" style={{ borderColor: 'var(--accent)' }}>
        <div className="flex items-center gap-3 mb-4">
          <CreditCard size={20} style={{ color: 'var(--accent)' }} />
          <h2 className="font-semibold text-lg">Current Plan: <span style={{ color: 'var(--accent)' }}>{PLANS.find(p => p.id === currentPlan)?.name}</span></h2>
        </div>

        <div className="space-y-3">
          <div className="flex justify-between text-sm">
            <span style={{ color: 'var(--fg-muted)' }}>Tokens Used</span>
            <span>{tokensUsed.toLocaleString()} / {quota.toLocaleString()}</span>
          </div>
          <div className="progress-bar">
            <div
              className="progress-fill"
              style={{
                width: `${percentage}%`,
                background: percentage > 80 ? 'var(--danger)' : percentage > 50 ? 'var(--warning)' : 'var(--success)',
              }}
            />
          </div>
          <div className="flex justify-between text-xs" style={{ color: 'var(--fg-muted)' }}>
            <span>{percentage.toFixed(1)}% used</span>
            <span>{(quota - tokensUsed).toLocaleString()} remaining</span>
          </div>
        </div>

        {percentage > 80 && (
          <div className="mt-4 flex items-center gap-2 text-sm text-yellow-400">
            <AlertTriangle size={16} />
            <span>Usage is getting high. Consider upgrading your plan.</span>
          </div>
        )}
      </div>

      {/* Plans */}
      <div>
        <h2 className="text-lg font-semibold mb-4">Upgrade Plan</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
          {PLANS.map(plan => {
            const isCurrent = plan.id === currentPlan;
            const isPopular = plan.id === 'pro';
            return (
              <div
                key={plan.id}
                className={`card relative ${isPopular ? 'ring-2 ring-indigo-500' : ''}`}
              >
                {isPopular && (
                  <span className="absolute -top-2 right-4 badge text-xs" style={{ background: 'var(--accent)', color: 'white' }}>
                    Popular
                  </span>
                )}
                <h3 className="font-semibold">{plan.name}</h3>
                <p className="text-2xl font-bold mt-2">
                  {plan.monthly_price_idr === 0 ? 'Free' : `Rp ${plan.monthly_price_idr?.toLocaleString()}`}
                  <span className="text-sm font-normal" style={{ color: 'var(--fg-muted)' }}>/month</span>
                </p>
                <p className="text-xs mt-1" style={{ color: 'var(--fg-muted)' }}>
                  {plan.monthly_quota_tokens?.toLocaleString()} tokens
                </p>
                <ul className="mt-4 space-y-1">
                  {plan.features.map(f => (
                    <li key={f} className="text-xs flex items-center gap-1">
                      <span style={{ color: 'var(--success)' }}>✓</span> {f}
                    </li>
                  ))}
                </ul>
                <button
                  className={`w-full mt-4 ${isCurrent ? 'btn-secondary' : 'btn-primary'}`}
                  disabled={isCurrent}
                >
                  {isCurrent ? 'Current' : 'Upgrade'}
                </button>
              </div>
            );
          })}
        </div>
      </div>

      {/* Payment Methods */}
      <div>
        <h2 className="text-lg font-semibold mb-4">Payment Methods</h2>
        <div className="card">
          <div className="flex flex-wrap gap-3">
            {['QRIS', 'Bank Transfer', 'GoPay', 'OVO', 'DANA'].map(m => (
              <span key={m} className="badge" style={{ background: 'var(--bg-elevated)' }}>{m}</span>
            ))}
          </div>
          <p className="text-xs mt-3" style={{ color: 'var(--fg-muted)' }}>
            Powered by Midtrans. All payments in Indonesian Rupiah (IDR).
          </p>
        </div>
      </div>
    </div>
  );
}
