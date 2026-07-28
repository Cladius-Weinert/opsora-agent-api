# Opsora Copy Improvement Prompts

## Prompt 1: Hero Copy (DeepSeek V4 Pro)
```
You write developer-focused marketing copy. Product: Opsora, an OpenAI-compatible AI API gateway with 102 models.

Give 3 alternative hero headlines (max 6 words each). Then 3 alternative subheadlines (max 20 words). Be specific, use numbers. No buzzwords. Format as numbered list.
```

## Prompt 2: Section Descriptions (Llama 3.1 70B)
```
Rewrite these landing page section descriptions for an AI API product called Opsora. Target: developers globally. Tone: peer-to-peer, technical, concise.

Current:
1. "how it works" — "3 steps to start sending requests"
2. "models" — "6 curated models, each optimized for different tasks"
3. "pricing" — "Start free, scale when you need more"
4. "why switch" — "Concrete reasons to choose Opsora over alternatives"

For each, provide 2 alternatives that are more specific and compelling.
```

## Prompt 3: Model Descriptions (CodeLlama 70B)
```
Write a one-line technical description for each AI model in this catalog:

1. opsora-fast (DeepSeek V4 Flash) — for real-time applications
2. opsora-brain (Llama 3.1 70B) — general purpose
3. opsora-code (CodeLlama 70B) — code generation
4. opsora-vision (Llama 3.2 90B Vision) — multimodal
5. opsora-reason (DeepSeek V4 Pro) — complex reasoning
6. opsora-max (Nemotron Ultra 253B) — maximum quality

Each description should be exactly one sentence, mention the parameter count, key strength, and ideal use case. Technical but accessible.
```

## Prompt 4: Error Messages (DeepSeek V4 Flash)
```
Write 5 user-friendly API error messages for an AI API gateway called Opsora. Cover these errors:
1. 401 Unauthorized
2. 429 Rate Limited
3. 503 Model Unavailable
4. 504 Upstream Timeout
5. 400 Invalid Request

Each should be: JSON format with "error" object containing "message", "type", and "suggestion" fields. Professional but helpful tone.
```

## Prompt 5: Changelog/Release Notes (Llama 3.1 70B)
```
Write release notes for the Opsora Agent API v1.0 launch. Include:
- 3-4 new features (API gateway, model routing, usage tracking, playground)
- 2 performance improvements
- 1 known limitation

Format as markdown with ## headings. Keep each item to one line.
```
