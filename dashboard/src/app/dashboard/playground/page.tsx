'use client';

import { useState, useEffect, useRef } from 'react';
import { Send, Copy, Code, RotateCcw } from 'lucide-react';

interface Message {
  role: 'user' | 'assistant';
  content: string;
}

const MODELS = [
  { id: 'opsora-fast', label: 'Fast (DeepSeek V4 Flash)' },
  { id: 'opsora-brain', label: 'Brain (Llama 3.1 70B)' },
  { id: 'opsora-code', label: 'Code (Nemotron 49B)' },
  { id: 'opsora-vision', label: 'Vision (Llama 3.2 90B)' },
  { id: 'opsora-reason', label: 'Reason (DeepSeek V4 Pro)' },
  { id: 'opsora-max', label: 'Max (Nemotron 550B)' },
  { id: 'opsora-agent', label: 'Agent (Auto-route)' },
];

const PRESETS = [
  'Explain recursion like I\'m a beginner',
  'Write a Python function to check if a number is prime',
  'What are the pros and cons of microservices vs monoliths?',
];

export default function PlaygroundPage() {
  const [model, setModel] = useState('opsora-fast');
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(false);
  const [showCode, setShowCode] = useState(false);
  const outputRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
    }
  }, [messages]);

  const handleSend = async () => {
    if (!input.trim() || loading) return;

    const userMsg: Message = { role: 'user', content: input };
    const newMessages = [...messages, userMsg];
    setMessages(newMessages);
    setInput('');
    setLoading(true);

    try {
      const res = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ model, messages: newMessages }),
      });
      const data = await res.json();
      const content = data.choices?.[0]?.message?.content ?? 'Error: No response';
      setMessages([...newMessages, { role: 'assistant', content }]);
    } catch {
      setMessages([...newMessages, { role: 'assistant', content: 'Error: Could not reach the API. Check your configuration.' }]);
    } finally {
      setLoading(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  };

  const codeExport = `curl ${process.env.NEXT_PUBLIC_OPSORA_API_URL || 'https://api.opsora.id'}/v1/chat/completions \\
  -H "Content-Type: application/json" \\
  -H "Authorization: Bearer YOUR_API_KEY" \\
  -d '{
    "model": "${model}",
    "messages": [{"role": "user", "content": "${messages[messages.length - 2]?.content || 'Hello'}"}]
  }'`;

  return (
    <div className="space-y-4 h-[calc(100vh-10rem)]">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Playground</h1>
        <div className="flex items-center gap-2">
          <select className="input w-auto" value={model} onChange={e => setModel(e.target.value)}>
            {MODELS.map(m => (
              <option key={m.id} value={m.id}>{m.label}</option>
            ))}
          </select>
          <button onClick={() => setShowCode(!showCode)} className={`btn-secondary ${showCode ? 'border-indigo-500' : ''}`}>
            <Code size={16} />
          </button>
          <button onClick={() => setMessages([])} className="btn-secondary">
            <RotateCcw size={16} />
          </button>
        </div>
      </div>

      {/* Preset prompts */}
      {messages.length === 0 && (
        <div className="flex flex-wrap gap-2">
          {PRESETS.map((p, i) => (
            <button key={i} onClick={() => setInput(p)} className="btn-secondary text-xs">
              {p.slice(0, 40)}...
            </button>
          ))}
        </div>
      )}

      {/* Code export */}
      {showCode && (
        <div className="card">
          <div className="flex items-center justify-between mb-2">
            <p className="text-sm font-medium">cURL Example</p>
            <button onClick={() => navigator.clipboard.writeText(codeExport)} className="btn-secondary text-xs">
              <Copy size={12} /> Copy
            </button>
          </div>
          <pre className="text-xs overflow-x-auto p-3 rounded" style={{ background: 'var(--bg-input)' }}>
            {codeExport}
          </pre>
        </div>
      )}

      {/* Chat messages */}
      <div ref={outputRef} className="flex-1 overflow-y-auto space-y-4 min-h-0" style={{ maxHeight: 'calc(100vh - 22rem)' }}>
        {messages.length === 0 ? (
          <div className="flex items-center justify-center h-full">
            <p style={{ color: 'var(--fg-muted)' }}>Send a message to start chatting</p>
          </div>
        ) : (
          messages.map((msg, i) => (
            <div key={i} className={`flex ${msg.role === 'user' ? 'justify-end' : 'justify-start'}`}>
              <div
                className="max-w-[80%] rounded-lg p-3 text-sm whitespace-pre-wrap"
                style={{
                  background: msg.role === 'user' ? 'var(--accent)' : 'var(--bg-card)',
                  color: msg.role === 'user' ? 'white' : 'var(--fg)',
                  border: msg.role === 'assistant' ? '1px solid var(--border)' : 'none',
                }}
              >
                {msg.content}
              </div>
            </div>
          ))
        )}
        {loading && (
          <div className="flex justify-start">
            <div className="rounded-lg p-3 text-sm" style={{ background: 'var(--bg-card)', border: '1px solid var(--border)' }}>
              <span className="animate-pulse">Thinking...</span>
            </div>
          </div>
        )}
      </div>

      {/* Input */}
      <div className="flex gap-2">
        <input
          className="input flex-1"
          value={input}
          onChange={e => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Type your message..."
          disabled={loading}
        />
        <button onClick={handleSend} className="btn-primary flex items-center gap-2" disabled={loading || !input.trim()}>
          <Send size={16} /> Send
        </button>
      </div>
    </div>
  );
}
