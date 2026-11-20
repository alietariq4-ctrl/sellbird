# harvey_brain

A provider-agnostic brain for Sellbird, with **OpenRouter as the primary**,
**Claude kept as an option**, and nine more providers behind one contract.

Every transport satisfies the seam already proven against the real
`sellbird.brain.Brain`:

```python
async transport(prompt, *, agent, task, timeout, expect_json) -> str   # "" on failure
transport.paused_reason -> str   # non-empty makes the host skip calls
```

Never raises. Returns `""` on failure. Logs one line per failure with the status
and a ≤200-character body excerpt, with credentials masked. Runtime dependency:
`httpx` only (+ PyYAML to read `brain.yaml`; the host already ships it).

## Quick start

```bash
cp harvey_brain/brain.example.yaml brain.yaml      # beside sellbird.yaml
printf 'OPENROUTER_API_KEY=sk-or-v1-...\n' >> .env
python -m harvey_brain doctor
```

Then wire it into the host — this is the whole integration:

```yaml
# sellbird.yaml
brain:
  transport_factory: "harvey_brain.router_factory:build"
```

`Brain(...)` resolves that dotted path in its constructor, so every existing
construction site (heartbeat, dashboard, CLI, setup wizard, trainer) picks it up
with no other change.

### Switching providers is one line

```yaml
chain:
  - provider: openrouter
    model: anthropic/claude-haiku-5.5
```

Change `provider`/`model`; nothing else moves. Order is the fallback order.

## Config examples

### (a) OpenRouter only

```yaml
budget: { max_usd_per_day: 5.0 }
chain:
  - provider: openrouter
    model: anthropic/claude-sonnet-5.5
    max_output_tokens: 4096
```

### (b) OpenRouter, then xAI, then Gemini

```yaml
budget: { max_usd_per_day: 10.0 }
chain:
  - provider: openrouter
    model: anthropic/claude-sonnet-5.5
  - provider: xai
    model: grok-4.7
    reasoning_effort: high
  - provider: gemini
    model: gemini-4-hydrogen
```

A provider with no key in the environment is skipped at startup (logged once),
so this works with only `OPENROUTER_API_KEY` set today and gains the others the
moment their keys appear.

### (c) Fully local (no invoice at all)

```yaml
budget: { max_usd_per_day: null }   # null, not 0: 0 would refuse every call
chain:
  - provider: ollama
    model: llama3.2
    timeout: 180
```

`ollama` is keyless, so the chain builds with no credentials at all.

### (d) Keep Claude (and Codex) as chain members

```yaml
chain:
  - provider: openrouter
    model: anthropic/claude-haiku-5.5    # cheap first pass
  - provider: claude_cli                 # subscription, $0 per token
  - provider: codex_cli
```

`claude_cli` / `codex_cli` do **not** re-implement anything: they delegate to
`sellbird.claude_runtime.build_command` (the isolated `--tools ""`
`--strict-mcp-config --safe-mode` CLI call) and `sellbird.codex.CodexClient.run`.
If the host package is not importable they report themselves unavailable
(`paused_reason`) instead of crashing, and the router simply skips them.

## Providers

| provider | `api_style` | key env | where to get it |
|---|---|---|---|
| `openrouter` **(default)** | openai | `OPENROUTER_API_KEY` | https://openrouter.ai/settings/keys |
| `xai` | openai | `XAI_API_KEY` | https://console.x.ai |
| `openai` | openai | `OPENAI_API_KEY` | https://platform.openai.com/api-keys |
| `gemini` | openai (compat endpoint) | `GEMINI_API_KEY` | https://aistudio.google.com/api-keys |
| `deepseek` | openai | `DEEPSEEK_API_KEY` | https://platform.deepseek.com/api_keys |
| `groq` | openai | `GROQ_API_KEY` | https://console.groq.com/keys |
| `mistral` | openai | `MISTRAL_API_KEY` | https://console.mistral.ai/api-keys |
| `together` | openai | `TOGETHER_API_KEY` | https://api.together.ai/settings/api-keys |
| `ollama` | openai (local) | — none — | https://ollama.com |
| `claude_cli` | delegate | — host subscription — | the host's own `claude` CLI |
| `codex_cli` | delegate | — host subscription — | the host's own `codex` runtime |

Gemini's row is the OpenAI-compatibility endpoint
(`/v1beta/openai`), confirmed in its docs — not the native
`generateContent` API, which this package does not speak.

## Per-role models (the biggest cost lever after the fetcher)

The host labels every call with `agent=`/`task=`, so each job can use a model
sized for it. Labels verified in the host: `scout.py:1288`
(`agent="scout", task="score_contacts"`), `writer.py:616`
(`agent="writer", task="personal_email"`), `writer.py:718`
(`task="write_sequence"`), `handler.py:1352` (`task="classify_intent"`),
`handler.py:1552` (`task="generate_response"`), `humanize.py:127`
(`task="humanize"`).

```yaml
profiles:
  scout:                                  # 1,000 calls/mo, JSON: cheap wins
    provider: openrouter
    model: anthropic/claude-haiku-5.5
    max_output_tokens: 1024
    temperature: 0
    json_mode: json_schema
  writer:                                 # 300 calls/mo, output IS the product
    provider: openrouter
    model: anthropic/claude-sonnet-5.5
    max_output_tokens: 2048
    temperature: 0.7
    json_mode: json_schema
  handler:
    provider: openrouter
    model: anthropic/claude-haiku-5.5
    max_output_tokens: 1024
    temperature: 0.2
    json_mode: json_schema
```

Rules:
* Matching is **agent first, then task**, so a new task label under a known
  agent still lands on the right profile.
* `profiles:` is **optional**. Absent or `false` means no per-role routing and
  every call walks the chain — a caller that has not thought about roles never
  silently gets a different model than it configured.
* Profiles are merged over the built-in defaults, so you only spell out what
  you want to change; `role: false` switches one role back to the chain.
* A profile naming a provider that has no key (not in the chain) is **skipped
  with one log line** and the call falls through to the chain. It is never an
  error: an optional provider going missing must not stop the pipeline.
* A role's model that fails also falls through to the chain, so a profile can
  only make a call cheaper, never less likely to succeed.
* Overrides travel as **call arguments**, not mutated attributes: a scout call
  and a writer call can be in flight on the same transport without seeing each
  other's temperature or token limit (tested).

What this saves, on the 1,000-companies workload below: all-sonnet is $33.10;
scout-1,000×haiku ($1.40) + writer-300×sonnet ($5.10) is **$6.50** — a 5.1×
reduction for the same output, because the cheap model only does the JSON
scoring. (Arithmetic on the doc-fact prices below.)

## Behaviour that matters

| Concern | Behaviour |
|---|---|
| JSON output | `json_mode: auto` picks `json_schema` when the provider supports it, else `json_object`, else nothing (the host already appends "Respond ONLY with valid JSON"). Override per entry with `json_mode: json_schema | json_object | none`. |
| Schema rejected | One automatic downgrade to `json_object` when the endpoint refuses the schema — OpenRouter's structured-output support varies per endpoint. |
| Retries | 429, 5xx, `httpx.TransportError`, exponential backoff with jitter. Never on 400/401/403/422. |
| Timeout | A hard wall-clock bound for the whole call, retries included; each attempt gets the remaining budget. |
| Auth failure | 401/403 pauses that provider with an actionable sentence naming the env var. No retry. |
| Credits | 402, or a 429/400 body containing a quota marker, pauses with a credits message. |
| Budget | `budget.max_usd_per_day`, checked **before** each provider is tried. Spend comes from the provider's own `usage.cost` when reported (OpenRouter does), else the price table. |
| Chain | Strictly the configured order. No shuffling. A paused provider is skipped **without a network call**. |
| All failed | `""`, and `paused_reason` = `all N providers failed (last: <name> <error>)`, held for 60 s. |
| Secrets | Error bodies are redacted (`sk-…`, `sk-or-v1-…`, `xai-…`, `gsk_…`, `AIza…`) before logging. The prompt is never logged. |

Usage callbacks: pass `on_usage=` to a transport, or read `router.last_usage`
after a call. `Usage` carries `input_tokens`, `cached_input_tokens`,
`output_tokens`, `reasoning_tokens`, `total_tokens`, `cost_usd`, `model`,
`provider`, `duration_ms`, `agent`, `task`.

Reasoning/thinking fields (`reasoning`, `reasoning_content`) are counted in
usage but **never returned as the answer** — they are billed output, not text.

## Cost per 1,000 companies

Assumption (the arithmetic base — same workload as the sibling plan):
1,000 companies/month; 1,000 research calls at 8,000 in / 1,200 out tokens;
300 writing calls at 4,000 in / 900 out. No caching, no tool fees.

| Provider + model | research | writing | **total / 1,000** | per company |
|---|---|---|---|---|
| `openrouter` `anthropic/claude-haiku-5.5` | $1.40 | $0.26 | **$1.65** | $0.0017 |
| `openrouter` `deepseek/deepseek-v4.1-flash` | $0.89 | $0.18 | **$1.07** | $0.0011 |
| `openrouter` `openai/gpt-6-luna` | $1.40 | $0.26 | **$1.65** | $0.0017 |
| `openrouter` `google/gemini-4-hydrogen` | $21.00 | $3.83 | **$24.82** | $0.0248 |
| `openrouter` `x-ai/grok-4.7` | $23.20 | $4.02 | **$27.22** | $0.0272 |
| `openrouter` `anthropic/claude-sonnet-5.5` | $28.00 | $5.10 | **$33.10** | $0.0331 |

**Doc-fact columns:** the per-1M-token prices behind every row —
`anthropic/claude-haiku-5.5` $0.10 in / $0.01 cached / $0.50 out;
`claude-sonnet-5.5` $2 / $0.10 / $10; `openai/gpt-6-luna` $0.10 / $0.01 / $0.50;
`google/gemini-4-hydrogen` $1.50 / $0.075 / $7.50; `x-ai/grok-4.7` $2 / $0.50 / $6
(below a 200k-token prompt); `deepseek/deepseek-v4.1-flash` $0.036 / $0.01 / $0.50.
All read from `GET https://openrouter.ai/api/v1/models` on 2026-11-16 and
cross-checked against each vendor's own pricing page where one exists
(Anthropic, Google, xAI, DeepSeek, OpenAI, Mistral, Together).

**Arithmetic columns:** every dollar figure after that. Worked example for
`claude-sonnet-5.5`: research = 8.0M × $2.00/M + 1.2M × $10.00/M = $28.00;
writing = 1.2M × $2.00/M + 0.27M × $10.00/M = $5.10; total $33.10; ÷1,000 =
$0.0331 per company. `pricing.estimate()` reproduces these exactly (tested).

Two honest caveats:
* OpenRouter charges a **5.5% ($0.80 minimum) fee when buying credits**
  (doc fact, OpenRouter FAQ), so the cash you deposit costs a little more than
  the inference priced above. Prices themselves are passed through with no
  markup.
* `usage.cost` from OpenRouter is the authoritative per-call number; the table
  is only a fallback for endpoints that report no cost.

## Seeing it work before spending money

### `probe` — the default is a dry run

```bash
python -m harvey_brain probe                    # no network, no spend
python -m harvey_brain probe --json             # same, machine-readable
python -m harvey_brain probe --live --yes       # ONE minimal real call each
```

The dry run prints the resolved endpoint, model, auth env name, whether the key
**looks present (length only — never the value)**, the price of one
representative 8k-in/1.2k-out call, and the per-role models. `--live` is refused
without `--yes`, because that is real money; it calls the transports themselves
(not a parallel code path), so it exercises the same retry, auth and redaction
logic production uses.

```
provider       model                             endpoint                                      auth env                key                 est/call    status        latency    tokens         cost
--------------  --------------------------------  --------------------------------------------  ----------------------  ------------------  ----------  ------------  ---------  -------------  ---------
openrouter     anthropic/claude-sonnet-5.5       https://openrouter.ai/api/v1/chat/completions  OPENROUTER_API_KEY      set (41 chars)      $0.0280     dry-run       —          —              —
ollama         llama3.2                          http://localhost:11434/v1/chat/completions    — (keyless)             not needed          $0.0000     dry-run       —          —              —
```

### `doctor` — configuration only

```bash
python -m harvey_brain doctor              # config + table, no network
python -m harvey_brain doctor --network    # also one GET /models per provider
python -m harvey_brain doctor --config path/to/brain.yaml
```

No keys, no config, no network — it still prints every known provider and
exits non-zero when none is configured.

## Default model ids are tested, not assumed

A wrong id fails on the first real call with 404 `model_not_found`, so the
defaults are pinned two ways:

* `tests/fixtures/openrouter_models.json` — the real catalogue, committed, so
  `test_model_ids.py` checks every OpenRouter id we ship **offline**.
* `-m network` re-fetches the live catalogue and fails if an id we ship has been
  retired. Run it before a release:

  ```bash
  python -m pytest harvey_brain/tests/test_model_ids.py -q -m network
  ```

Direct-provider ids are each cited in `spec.py` next to the constant, and a test
fails if any provider ships a `default_model` without a doc citation (only
`ollama`, whose tags are local, is exempt).

## UNVERIFIED facts

Everything below is marked in `pricing.py` / `spec.py` where it applies.

| Item | Status | Why |
|---|---|---|
| Groq prices (`llama-3.3-70b-versatile`, `llama-*`) | **UNVERIFIED**, 0.0 | `console.groq.com/docs/pricing` returned 404 and `groq.com/pricing` is a JS app with no readable table (checked twice, 2026-11-16). The model **id** is verified (`console.groq.com/docs/models`); only the price is unknown. A budget gate that invents a price is worse than one that under-reports. |
| Together's default slug casing | verified | `deepseek-ai/DeepSeek-V4.1-Flash-1017` — capitalised, no dots, per `docs.together.ai/docs/serverless-models`. An OpenRouter-style lowercase slug would 404 here. |
| Mistral default | verified | `mistral-large-latest` (the alias Mistral's own model page lists as `mistral-large-4`, `mistral-large-4-0`, `mistral-large-latest`). |
| OpenRouter structured-output support per model | optimistic | Support is per **endpoint**, not per model, and can change. Mitigated by the one-shot `json_object` downgrade. |
| `deepseek` `json_schema` support | **not claimed** (`supports_json_schema=False`) | DeepSeek documents JSON output mode, not schema-constrained output. |
| `ollama` model names/prices | 0.0, `llama3.2` default | Local; no invoice. Model availability depends on what you pulled. |
| `codex_cli` usage→cost | 0.0 always | ChatGPT plans are not billed per token (host's own note, `brain.py:518`). |

## Tests

```bash
python -m pytest harvey_brain/tests -q
```

80 tests, offline. `httpx.MockTransport` for every request, no API keys, no
retries that sleep (`retry_base_delay=0`). `tests/test_host_seam.py` drives a
real `sellbird.brain.Brain` + `StateManager` from `_ref/sellbird` when that
checkout is present, and skips otherwise.
