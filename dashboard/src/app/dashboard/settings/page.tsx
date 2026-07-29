'use client';

import { useState } from 'react';
import { Save, Bell } from 'lucide-react';

export default function SettingsPage() {
  const [name, setName] = useState('');
  const [email] = useState('user@example.com');
  const [notifications, setNotifications] = useState({
    quota80: true,
    quota95: true,
    quotaExhausted: true,
    weeklyDigest: false,
  });

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-bold">Settings</h1>
        <p className="text-sm mt-1" style={{ color: 'var(--fg-muted)' }}>
          Manage your account and preferences
        </p>
      </div>

      {/* Profile */}
      <div className="card space-y-4">
        <h2 className="font-semibold">Profile</h2>
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label className="block text-sm mb-1" style={{ color: 'var(--fg-muted)' }}>Name</label>
            <input className="input" value={name} onChange={e => setName(e.target.value)} placeholder="Your name" />
          </div>
          <div>
            <label className="block text-sm mb-1" style={{ color: 'var(--fg-muted)' }}>Email</label>
            <input className="input" value={email} disabled />
          </div>
        </div>
        <button className="btn-primary flex items-center gap-2">
          <Save size={16} /> Save Changes
        </button>
      </div>

      {/* Notifications */}
      <div className="card space-y-4">
        <div className="flex items-center gap-2">
          <Bell size={18} />
          <h2 className="font-semibold">Notifications</h2>
        </div>
        {Object.entries({
          quota80: 'Alert when 80% quota used',
          quota95: 'Alert when 95% quota used',
          quotaExhausted: 'Alert when quota exhausted',
          weeklyDigest: 'Weekly usage digest email',
        }).map(([key, label]) => (
          <label key={key} className="flex items-center gap-3 cursor-pointer">
            <input
              type="checkbox"
              checked={notifications[key as keyof typeof notifications]}
              onChange={e => setNotifications(prev => ({ ...prev, [key]: e.target.checked }))}
              className="w-4 h-4 rounded"
            />
            <span className="text-sm">{label}</span>
          </label>
        ))}
      </div>

      {/* Danger Zone */}
      <div className="card" style={{ borderColor: 'var(--danger)' }}>
        <h2 className="font-semibold text-red-400">Danger Zone</h2>
        <p className="text-sm mt-2" style={{ color: 'var(--fg-muted)' }}>
          Permanently delete your account and all associated data.
        </p>
        <button className="mt-4 px-4 py-2 rounded text-sm font-medium text-white" style={{ background: 'var(--danger)' }}>
          Delete Account
        </button>
      </div>
    </div>
  );
}
