"""USD per 1M tokens, keyed by ``(provider, model_prefix)``.

Rules:
* Longest matching prefix wins, so ``anthropic/claude-haiku-5.5`` beats
  ``anthropic/claude-*``.
* OpenRouter ids ending in ``:free`` cost 0 (doc fact: the ``:free`` variant is
  a separate catalog entry with its own rate limits).
* Anything unknown returns 0.0 and logs once. Never guess: a wrong price is
  worse than a missing one, because a budget gate trusts the number.
* Every row carries a ``# source:`` URL. Rows whose price could not be read
  from the provider's own page say ``UNVERIFIED`` and are 0.

OpenRouter prices are the per-token strings from ``GET /api/v1/models``
multiplied by 1e6 (that endpoint's ``pricing.prompt`` is USD per token), read
2026-11-16.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_WARNED: set[tuple[str, str]] = set()


@dataclass(frozen=True, slots=True)
class Price:
    input_per_m: float = 0.0
    cached_input_per_m: float = 0.0
    output_per_m: float = 0.0
    source: str = ""
    verified: bool = True


# (provider, model_prefix, Price)
# Longest prefix wins. Keep the table sorted by provider for readability.
PRICES: tuple[tuple[str, str, Price], ...] = (
    # ── OpenRouter ────────────────────────────────────────────────────────
    # source: https://openrouter.ai/api/v1/models (pricing.prompt/completion/
    # input_cache_read, USD per token), read 2026-11-16.
    ("openrouter", "anthropic/claude-sonnet", Price(
        2.00, 0.10, 10.00, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "anthropic/claude-opus", Price(
        4.00, 0.20, 20.00, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "anthropic/claude-haiku-5.5", Price(
        0.10, 0.01, 0.50, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "anthropic/claude-fable", Price(
        8.00, 0.20, 40.00, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "openai/gpt-6-luna", Price(
        0.10, 0.01, 0.50, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "openai/gpt-6.1-sol", Price(
        2.00, 0.10, 10.00, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "openai/gpt-6.1-terra", Price(
        1.00, 0.05, 5.00, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "openai/gpt-", Price(
        2.00, 0.20, 10.00, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "google/gemini-4-hydrogen", Price(
        1.50, 0.075, 7.50, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "google/gemini-4-carbon", Price(
        2.00, 0.10, 10.00, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "google/gemini-", Price(
        1.50, 0.075, 7.50, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "x-ai/grok-4.7", Price(
        2.00, 0.50, 6.00, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "x-ai/grok-", Price(
        2.00, 0.50, 6.00, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "deepseek/", Price(
        0.036, 0.01, 0.50, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "meta-llama/llama-4-maverick", Price(
        0.35, 0.17, 1.00, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "meta-llama/llama-4-scout", Price(
        0.10, 0.0, 0.30, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "meta-llama/llama-3.3-70b-instruct", Price(
        0.71, 0.71, 0.71, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "meta-llama/", Price(
        0.35, 0.17, 1.00, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "qwen/qwen3.7-plus", Price(
        0.32, 0.064, 1.28, "https://openrouter.ai/api/v1/models")),
    ("openrouter", "moonshotai/kimi-k3", Price(
        0.192, 0.182, 16.00, "https://openrouter.ai/api/v1/models")),

    # ── xAI direct ────────────────────────────────────────────────────────
    # source: https://docs.x.ai/developers/pricing (< 200k prompt tokens;
    # >= 200k is $4/$1/$12 and is deliberately not modelled per row — see
    # estimate_cost for the note).
    ("xai", "grok-4.7", Price(
        2.00, 0.50, 6.00, "https://docs.x.ai/developers/pricing")),
    ("xai", "grok-", Price(
        2.00, 0.50, 6.00, "https://docs.x.ai/developers/pricing")),

    # ── OpenAI direct ─────────────────────────────────────────────────────
    # source: https://developers.openai.com/api/docs/pricing (standard tier,
    # short context).
    ("openai", "gpt-6-luna", Price(
        0.10, 0.01, 0.50, "https://developers.openai.com/api/docs/pricing")),
    ("openai", "gpt-6.1-sol", Price(
        2.00, 0.10, 10.00, "https://developers.openai.com/api/docs/pricing")),
    ("openai", "gpt-6.1-terra", Price(
        1.00, 0.05, 5.00, "https://developers.openai.com/api/docs/pricing")),
    ("openai", "gpt-4.1-mini", Price(
        0.40, 0.10, 1.60, "https://developers.openai.com/api/docs/pricing")),
    ("openai", "gpt-4.1", Price(
        2.00, 0.50, 8.00, "https://developers.openai.com/api/docs/pricing")),
    ("openai", "gpt-4o-mini", Price(
        0.15, 0.075, 0.60, "https://developers.openai.com/api/docs/pricing")),
    ("openai", "gpt-", Price(
        2.00, 0.20, 10.00, "https://developers.openai.com/api/docs/pricing")),

    # ── Gemini direct ─────────────────────────────────────────────────────
    # source: https://ai.google.dev/gemini-api/docs/pricing (standard tier).
    ("gemini", "gemini-4-hydrogen", Price(
        1.50, 0.075, 7.50, "https://ai.google.dev/gemini-api/docs/pricing")),
    ("gemini", "gemini-4-carbon", Price(
        2.00, 0.10, 10.00, "https://ai.google.dev/gemini-api/docs/pricing")),
    ("gemini", "gemini-4-", Price(
        1.50, 0.075, 7.50, "https://ai.google.dev/gemini-api/docs/pricing")),

    # ── DeepSeek direct ───────────────────────────────────────────────────
    # source: https://api-docs.deepseek.com/quick_start/pricing (PEAK rate;
    # off-peak is half). Cache-hit input included.
    ("deepseek", "deepseek-flash", Price(
        0.30, 0.006, 1.20, "https://api-docs.deepseek.com/quick_start/pricing")),
    ("deepseek", "deepseek-v4-pro", Price(
        1.32, 0.044, 3.96, "https://api-docs.deepseek.com/quick_start/pricing")),

    # ── Mistral direct ────────────────────────────────────────────────────
    # source: https://docs.mistral.ai/inference/pricing
    ("mistral", "mistral-large-4", Price(
        0.70, 0.07, 2.10, "https://docs.mistral.ai/inference/pricing")),
    # `mistral-large-latest` is Mistral's own stable alias for the same model.
    ("mistral", "mistral-large-latest", Price(
        0.70, 0.07, 2.10, "https://docs.mistral.ai/inference/pricing")),
    ("mistral", "mistral-small-4", Price(
        0.15, 0.015, 0.60, "https://docs.mistral.ai/inference/pricing")),
    ("mistral", "codestral", Price(
        0.30, 0.03, 0.90, "https://docs.mistral.ai/inference/pricing")),
    ("mistral", "deepseek-v4-flash", Price(
        0.30, 0.007, 1.20, "https://docs.mistral.ai/inference/pricing")),

    # ── Together direct ───────────────────────────────────────────────────
    # source: https://www.together.ai/pricing
    # Together's slug is capitalised with no dots: deepseek-ai/DeepSeek-V4.1-Flash-*.
    ("together", "deepseek-ai/DeepSeek-V4.1-Flash", Price(
        0.30, 0.006, 1.20, "https://www.together.ai/pricing")),
    ("together", "deepseek-ai/DeepSeek-V4-Pro", Price(
        1.32, 0.13, 3.96, "https://www.together.ai/pricing")),
    ("together", "glm-5-3", Price(
        1.40, 0.26, 4.40, "https://www.together.ai/pricing")),
    ("together", "qwen3.7-plus", Price(
        # No cached rate published for this row: charging the full input rate
        # keeps a budget gate conservative rather than optimistic.
        0.32, 0.32, 1.28, "https://www.together.ai/pricing")),

    # ── Groq ──────────────────────────────────────────────────────────────
    # UNVERIFIED: console.groq.com/docs/pricing returned 404 and groq.com/
    # pricing is a JS app with no readable table (checked 2026-11-16 twice).
    # Defaulted to 0 so a budget gate never invents a charge.
    ("groq", "llama-3.3-70b-versatile", Price(
        0.0, 0.0, 0.0, "UNVERIFIED: console.groq.com/docs/pricing (404)",
        verified=False)),
    ("groq", "llama-", Price(
        0.0, 0.0, 0.0, "UNVERIFIED: console.groq.com/docs/pricing (404)",
        verified=False)),

    # ── Fireworks & Cerebras ──────────────────────────────────────────────
    ("fireworks", "", Price(
        0.0, 0.0, 0.0, "UNVERIFIED: fireworks.ai/pricing (tier-dependent)",
        verified=False)),
    ("cerebras", "", Price(
        0.0, 0.0, 0.0, "UNVERIFIED: cerebras.ai/pricing (tier-dependent)",
        verified=False)),

    # ── Local & Custom ────────────────────────────────────────────────────
    ("ollama", "", Price(0.0, 0.0, 0.0, "local model: no invoice", True)),
    ("lmstudio", "", Price(0.0, 0.0, 0.0, "local model: no invoice", True)),
    ("vllm", "", Price(0.0, 0.0, 0.0, "local model: no invoice", True)),
    ("custom", "", Price(0.0, 0.0, 0.0, "custom OpenAI-compatible endpoint: user-defined", True)),
)


def lookup(provider: str, model: str) -> Price | None:
    """The most specific row for this provider+model, or None."""
    provider = (provider or "").strip().lower()
    model = (model or "").strip()
    if model.endswith(":free"):
        # doc fact: the :free variant is its own catalog entry, priced at 0.
        return Price(0.0, 0.0, 0.0, "https://openrouter.ai/docs/guides/routing/model-variants/free")
    best: tuple[int, Price] | None = None
    for prov, prefix, price in PRICES:
        if prov != provider:
            continue
        if prefix and not model.startswith(prefix):
            continue
        size = len(prefix)
        if best is None or size > best[0]:
            best = (size, price)
    return best[1] if best else None


def estimate(provider: str, model: str, *, input_tokens: int = 0,
             cached_input_tokens: int = 0, output_tokens: int = 0) -> float:
    """Cost from the table. ``input_tokens`` counts cached tokens too."""
    price = lookup(provider, model)
    if price is None:
        key = (provider, model)
        if key not in _WARNED:
            _WARNED.add(key)
            logger.warning(
                "no price row for %s/%s; recording cost 0 (add a row to pricing.py)",
                provider or "?", model or "?",
            )
        return 0.0
    cached = min(max(0, cached_input_tokens), max(0, input_tokens))
    uncached = max(0, input_tokens) - cached
    total = (
        uncached * price.input_per_m
        + cached * price.cached_input_per_m
        + max(0, output_tokens) * price.output_per_m
    ) / 1_000_000
    return round(total, 6)
