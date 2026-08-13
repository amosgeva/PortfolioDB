# Advisor LLM providers

> **Not investment advice.** The advisor is a language model reading your ledger
> and your own written rules. It will sometimes be confidently wrong, and it has
> no duty of care to you. Treat its output as a prompt to think, never as a
> recommendation to act on — and never let it place a trade, which is why this
> project has no broker credentials at all.

The advisor works with any of five provider modes. Provider, model, and base
URL are set from the dashboard (**Manage → Settings**) or via env vars; the
**API key always lives in the repo-root `.env`** — it is never stored in the
database or shown in the UI.

Resolution order for every value: Settings page → env var → default.

| Provider | Default model | Key env var(s) | Base URL |
|---|---|---|---|
| `anthropic` (default) | `claude-sonnet-5` | `LLM_API_KEY` or `ANTHROPIC_API_KEY` | — (native SDK) |
| `openai` | `gpt-5` | `LLM_API_KEY` or `OPENAI_API_KEY` | default |
| `openrouter` | `anthropic/claude-sonnet-5` | `LLM_API_KEY` or `OPENROUTER_API_KEY` | `https://openrouter.ai/api/v1` |
| `ollama` | `llama3.3` | none needed | `http://localhost:11434/v1` |
| `custom` | (set your own) | `LLM_API_KEY` (if the server wants one) | set your own |

Only two code paths exist (`app/llm.py`): the native `anthropic` SDK, and one
OpenAI-compatible path that covers everything else — OpenRouter proxies 300+
models, and the same dialect is spoken by Ollama, LM Studio, Groq, Mistral,
DeepSeek, etc.

## Examples

**OpenRouter** (any hosted model, one key):

```env
LLM_PROVIDER=openrouter
LLM_MODEL=deepseek/deepseek-chat
OPENROUTER_API_KEY=sk-or-...
```

**Fully local with Ollama** — your financial data never leaves your machine,
LLM included:

```env
LLM_PROVIDER=ollama
LLM_MODEL=llama3.3
```

Running the dashboard in Docker with Ollama on the host? The host is not
`localhost` from inside a container — set the base URL to
`http://host.docker.internal:11434/v1` (and on Linux add
`extra_hosts: ["host.docker.internal:host-gateway"]` to the service).

## Weaker / local models and the daily brief

The structured brief asks the model for JSON. Strong models comply; smaller
local models sometimes return prose or malformed JSON. The advisor never
errors on this: the raw text is kept as the brief's markdown (which is what
you actually read), the structured insight/suggestion lists come back empty,
and the payload is flagged with `parse_error: true`. Chat streaming works
with any provider regardless.
