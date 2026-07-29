'use client';

import { useState } from 'react';
import { Key, Copy, Trash2, Plus, Check } from 'lucide-react';

interface ApiKey {
  id: string;
  name: string;
  key?: string;
  created_at: string;
  last_used: string | null;
  rate_limit: number;
  status: 'active' | 'revoked';
}

function generateKey(): string {
  const chars = 'abcdefghijklmnopqrstuvwxyz0123456789';
  let result = 'opsora-sk-';
  for (let i = 0; i < 40; i++) {
    result += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return result;
}

export default function KeysPage() {
  const [keys, setKeys] = useState<ApiKey[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [newLimit, setNewLimit] = useState(60);
  const [newKey, setNewKey] = useState('');
  const [copied, setCopied] = useState('');

  const handleCreate = () => {
    if (!newName.trim()) return;
    const key = generateKey();
    const apiKey: ApiKey = {
      id: Date.now().toString(),
      name: newName,
      key,
      created_at: new Date().toISOString(),
      last_used: null,
      rate_limit: newLimit,
      status: 'active',
    };
    setKeys(prev => [apiKey, ...prev]);
    setNewKey(key);
    setShowCreate(false);
    setNewName('');
  };

  const handleRevoke = (id: string) => {
    setKeys(prev => prev.map(k => k.id === id ? { ...k, status: 'revoked' as const } : k));
  };

  const handleCopy = (text: string, id: string) => {
    navigator.clipboard.writeText(text);
    setCopied(id);
    setTimeout(() => setCopied(''), 2000);
  };

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold">API Keys</h1>
          <p className="text-sm mt-1" style={{ color: 'var(--fg-muted)' }}>
            Manage your API keys for authenticating requests
          </p>
        </div>
        <button onClick={() => setShowCreate(true)} className="btn-primary flex items-center gap-2">
          <Plus size={16} /> New Key
        </button>
      </div>

      {/* New key alert */}
      {newKey && (
        <div className="card" style={{ borderColor: 'var(--success)' }}>
          <p className="text-sm font-medium text-green-400 mb-2">✓ Key created — copy it now, you won&apos;t see it again!</p>
          <div className="flex items-center gap-2">
            <code className="flex-1 text-sm p-2 rounded" style={{ background: 'var(--bg-input)' }}>
              {newKey}
            </code>
            <button onClick={() => handleCopy(newKey, 'new')} className="btn-secondary">
              {copied === 'new' ? <Check size={16} /> : <Copy size={16} />}
            </button>
          </div>
          <button onClick={() => setNewKey('')} className="text-xs mt-2" style={{ color: 'var(--fg-muted)' }}>
            Dismiss
          </button>
        </div>
      )}

      {/* Create form */}
      {showCreate && (
        <div className="card space-y-4">
          <h3 className="font-medium">Create New API Key</h3>
          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--fg-muted)' }}>Name</label>
              <input className="input" value={newName} onChange={e => setNewName(e.target.value)} placeholder="My app" />
            </div>
            <div>
              <label className="block text-sm mb-1" style={{ color: 'var(--fg-muted)' }}>Rate Limit (RPM)</label>
              <select className="input" value={newLimit} onChange={e => setNewLimit(Number(e.target.value))}>
                <option value={60}>60 RPM (Free)</option>
                <option value={120}>120 RPM (Starter)</option>
                <option value={300}>300 RPM (Pro)</option>
                <option value={600}>600 RPM (Business)</option>
              </select>
            </div>
          </div>
          <div className="flex gap-2">
            <button onClick={handleCreate} className="btn-primary">Create</button>
            <button onClick={() => setShowCreate(false)} className="btn-secondary">Cancel</button>
          </div>
        </div>
      )}

      {/* Keys list */}
      <div className="card">
        {keys.length === 0 ? (
          <div className="text-center py-12">
            <Key size={40} className="mx-auto mb-4" style={{ color: 'var(--fg-muted)' }} />
            <p style={{ color: 'var(--fg-muted)' }}>No API keys yet. Create one to get started.</p>
          </div>
        ) : (
          <div className="space-y-3">
            {keys.map(k => (
              <div key={k.id} className="flex items-center gap-4 p-3 rounded-lg" style={{ background: 'var(--bg-elevated)' }}>
                <Key size={18} style={{ color: k.status === 'active' ? 'var(--success)' : 'var(--danger)' }} />
                <div className="flex-1 min-w-0">
                  <p className="font-medium text-sm">{k.name}</p>
                  <p className="text-xs truncate" style={{ color: 'var(--fg-muted)' }}>
                    {k.key ? `${k.key.slice(0, 20)}...` : 'opsora-sk-****'} · {k.rate_limit} RPM · {k.status}
                  </p>
                </div>
                <span className={`badge ${k.status === 'active' ? 'badge-success' : 'badge-danger'}`}>
                  {k.status}
                </span>
                {k.status === 'active' && (
                  <button onClick={() => handleRevoke(k.id)} className="text-red-400 hover:text-red-300">
                    <Trash2 size={16} />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
