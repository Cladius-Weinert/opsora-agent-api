import { NextResponse } from 'next/server';

const OPSORA_API_URL = process.env.NEXT_PUBLIC_OPSORA_API_URL || 'http://localhost:8080';

export async function POST(request: Request) {
  const body = await request.json();
  const { model, messages } = body;

  try {
    const res = await fetch(`${OPSORA_API_URL}/v1/chat/completions`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ model, messages, stream: false }),
    });

    if (!res.ok) {
      const error = await res.json().catch(() => ({ error: { message: 'Upstream error' } }));
      return NextResponse.json(error, { status: res.status });
    }

    const data = await res.json();
    return NextResponse.json(data);
  } catch {
    return NextResponse.json(
      { error: { message: 'Failed to connect to Opsora API', type: 'connection_error' } },
      { status: 502 }
    );
  }
}
