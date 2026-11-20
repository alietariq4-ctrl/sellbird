"""Provider registry: endpoint, auth, capabilities, and price-table key.

Every field here is a doc fact unless the comment says otherwise. Sources are
the provider's own API reference; see ``pricing.py`` for the numbers.

OpenRouter's attribution headers are ``HTTP-Referer`` (required by OpenRouter
for attribution) and ``X-OpenRouter-Title`` (``X-Title`` is still accepted for
backwards compatibility), per https://openrouter.ai/docs/app-attribution
(fetched 2026-11-16).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Mapping
from urllib.parse import urlparse

LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal"})


def is_local_url(url: str) -> bool:
    """True when ``url`` points to a local inference server (no API key required)."""
    raw = (url or "").strip()
    if not raw:
        return False
    try:
        host = (urlparse(raw).hostname or "").strip().lower()
    except ValueError:
        return False
    return host in LOCAL_HOSTS or host.endswith(".localhost")

# Attribution values we send on every OpenRouter request. Attribution is
# optional for correctness; these exist so usage is attributable in a fork.
OPENROUTER_HEADERS: dict[str, str] = {
    "HTTP-Referer": "https://github.com/ethanplusai/sellbird",
    "X-OpenRouter-Title": "Sellbird",
}


@dataclass(slots=True)
class ProviderSpec:
    """One provider's fixed facts."""

    name: str
    base_url: str
    api_style: str                      # "openai" | "delegate"
    default_model: str = ""
    api_key_env: str = ""
    supports_json_schema: bool = False
    supports_json_object: bool = False
    supports_tools: bool = False
    extra_headers: dict[str, str] = field(default_factory=dict)
    pricing: str = ""                   # prefix keys used by pricing.py
    note: str = ""

    @property
    def keyless(self) -> bool:
        return not self.api_key_env

    @property
    def delegates(self) -> bool:
        return self.api_style == "delegate"

    def headers(self) -> dict[str, str]:
        return dict(self.extra_headers)


def _spec(**kwargs: Any) -> ProviderSpec:
    return ProviderSpec(**kwargs)


# JSON-schema support: OpenRouter decides per model+endpoint, not per account.
# Its docs say structured outputs depend on the endpoint serving the request
# (https://openrouter.ai/docs/guides/features/structured-outputs), and every
# model we list there advertises the ``structured_outputs`` supported parameter
# in GET /api/v1/models. We therefore report schema support optimistically and
# let ``json_mode="auto"`` fall back to json_object/none when the API rejects it
# — see openai_compat._response_format. json_object is universally accepted on
# the OpenAI-compatible endpoints (doc fact for OpenAI, Gemini, DeepSeek,
# Mistral, Together, Groq, Ollama).
PROVIDERS: dict[str, ProviderSpec] = {
    "openrouter": _spec(
        name="openrouter",
        base_url="https://openrouter.ai/api/v1",
        api_style="openai",
        # verified in GET /api/v1/models 2026-11-16 (tests/fixtures/
        # openrouter_models.json); flagship Claude Sonnet on OpenRouter.
        default_model="anthropic/claude-sonnet-5.5",
        api_key_env="OPENROUTER_API_KEY",
        supports_json_schema=True,
        supports_json_object=True,
        supports_tools=True,
        extra_headers=OPENROUTER_HEADERS,
        pricing="openrouter",
        note="One key, every model. Usage carries `cost` in USD.",
    ),
    "xai": _spec(
        name="xai",
        base_url="https://api.x.ai/v1",
        api_style="openai",
        # verified: https://docs.x.ai/developers/models/grok-4.7 ("Model name: grok-4.7")
        default_model="grok-4.7",
        api_key_env="XAI_API_KEY",
        supports_json_schema=True,
        supports_json_object=True,
        supports_tools=True,
        pricing="xai",
        note="Chat Completions has function tools only; server-side "
             "web_search/x_search need the Responses API.",
    ),
    "openai": _spec(
        name="openai",
        base_url="https://api.openai.com/v1",
        api_style="openai",
        # verified: https://developers.openai.com/api/docs/models lists gpt-6-luna
        # (and gpt-6.1-sol / gpt-6.1-terra / gpt-6-astra) as current model ids
        default_model="gpt-6-luna",
        api_key_env="OPENAI_API_KEY",
        supports_json_schema=True,
        supports_json_object=True,
        supports_tools=True,
        pricing="openai",
    ),
    "gemini": _spec(
        name="gemini",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        api_style="openai",
        # verified: https://ai.google.dev/gemini-api/docs/openai uses
        # model="gemini-4-hydrogen" in its own OpenAI-compat example
        default_model="gemini-4-hydrogen",
        api_key_env="GEMINI_API_KEY",
        supports_json_schema=True,
        supports_json_object=True,
        supports_tools=True,
        pricing="gemini",
        note="OpenAI-compatible endpoint: Bearer auth, /chat/completions.",
    ),
    "deepseek": _spec(
        name="deepseek",
        base_url="https://api.deepseek.com/v1",
        api_style="openai",
        # verified: https://api-docs.deepseek.com/quick_start/pricing --
        # "Use `deepseek-flash` as the model name"
        default_model="deepseek-flash",
        api_key_env="DEEPSEEK_API_KEY",
        supports_json_schema=False,     # JSON output mode only, per docs
        supports_json_object=True,
        supports_tools=True,
        pricing="deepseek",
        note="Half-price off-peak; table lists the peak rate.",
    ),
    "groq": _spec(
        name="groq",
        base_url="https://api.groq.com/openai/v1",
        api_style="openai",
        # id verified in https://console.groq.com/docs/models (llama-3.3-70b-versatile);
        # the PRICE for it is UNVERIFIED -- see pricing.py
        default_model="llama-3.3-70b-versatile",
        api_key_env="GROQ_API_KEY",
        supports_json_schema=True,
        supports_json_object=True,
        supports_tools=True,
        pricing="groq",
        note="Prices UNVERIFIED (console pricing page was unavailable).",
    ),
    "mistral": _spec(
        name="mistral",
        base_url="https://api.mistral.ai/v1",
        api_style="openai",
        # verified: https://docs.mistral.ai/models/mistral-large-4-0 lists the API
        # names ["mistral-large-4", "mistral-large-4-0", "mistral-large-latest"];
        # `-latest` is the stable alias the docs recommend for production
        default_model="mistral-large-latest",
        api_key_env="MISTRAL_API_KEY",
        supports_json_schema=True,
        supports_json_object=True,
        supports_tools=True,
        pricing="mistral",
    ),
    "together": _spec(
        name="together",
        base_url="https://api.together.xyz/v1",
        api_style="openai",
        # verified: https://docs.together.ai/docs/serverless-models -- Together's slug
        # is capitalised and has no dots: deepseek-ai/DeepSeek-V4.1-Flash-1017
        default_model="deepseek-ai/DeepSeek-V4.1-Flash-1017",
        api_key_env="TOGETHER_API_KEY",
        supports_json_schema=True,
        supports_json_object=True,
        supports_tools=True,
        pricing="together",
    ),
    "fireworks": _spec(
        name="fireworks",
        base_url="https://api.fireworks.ai/inference/v1",
        api_style="openai",
        # verified: https://docs.fireworks.ai/getting-started/quickstart
        default_model="accounts/fireworks/models/llama-v3p3-70b-instruct",
        api_key_env="FIREWORKS_API_KEY",
        supports_json_schema=True,
        supports_json_object=True,
        supports_tools=True,
        pricing="fireworks",
    ),
    "cerebras": _spec(
        name="cerebras",
        base_url="https://api.cerebras.ai/v1",
        api_style="openai",
        # verified: https://inference-docs.cerebras.ai/models/llama-33-70b
        default_model="llama-3.3-70b",
        api_key_env="CEREBRAS_API_KEY",
        supports_json_schema=True,
        supports_json_object=True,
        supports_tools=True,
        pricing="cerebras",
    ),
    "ollama": _spec(
        name="ollama",
        base_url="http://localhost:11434/v1",
        api_style="openai",
        # UNVERIFIED: local model availability depends on what the operator pulled.
        # `llama3.2` is Ollama's documented example tag; any tag you have works.
        default_model="llama3.2",
        api_key_env="",                 # local, no key
        supports_json_schema=True,
        supports_json_object=True,
        supports_tools=False,
        pricing="ollama",
        note="Local. Costs nothing on your bill; priced at 0.",
    ),
    "lmstudio": _spec(
        name="lmstudio",
        base_url="http://localhost:1234/v1",
        api_style="openai",
        default_model="local-model",
        api_key_env="",                 # local, no key
        supports_json_schema=True,
        supports_json_object=True,
        supports_tools=True,
        pricing="lmstudio",
        note="Local LM Studio server. Costs nothing on your bill; priced at 0.",
    ),
    "vllm": _spec(
        name="vllm",
        base_url="http://localhost:8000/v1",
        api_style="openai",
        default_model="local-model",
        api_key_env="",                 # local, no key
        supports_json_schema=True,
        supports_json_object=True,
        supports_tools=True,
        pricing="vllm",
        note="Local vLLM OpenAI-compatible server. Priced at 0.",
    ),
    "custom": _spec(
        name="custom",
        base_url="https://api.openai.com/v1",
        api_style="openai",
        default_model="gpt-4o-mini",
        api_key_env="OPENAI_API_KEY",
        supports_json_schema=True,
        supports_json_object=True,
        supports_tools=True,
        pricing="custom",
        note="Universal OpenAI-compatible endpoint (OPENAI_BASE_URL / OPENAI_API_KEY / OPENAI_MODEL).",
    ),
    "claude_cli": _spec(
        name="claude_cli",
        base_url="",
        api_style="delegate",
        default_model="",
        api_key_env="",
        pricing="",
        note="Keeps Claude: delegates to the host's own isolated Claude CLI.",
    ),
    "codex_cli": _spec(
        name="codex_cli",
        base_url="",
        api_style="delegate",
        default_model="",
        api_key_env="",
        pricing="",
        note="Keeps Codex: delegates to the host's own Codex runtime.",
    ),
}

DEFAULT_PROVIDER = "openrouter"
DELEGATE_PROVIDERS = frozenset({"claude_cli", "codex_cli"})


def get_provider(
    name: str,
    *,
    base_url: str = "",
    api_key_env: str = "",
    extra_headers: Mapping[str, str] | None = None,
) -> ProviderSpec:
    clean = (name or "").strip().lower()
    explicit_url = (base_url or "").strip()
    explicit_key_env = (api_key_env or "").strip()
    spec = PROVIDERS.get(clean)

    if spec is not None:
        if clean == "custom":
            env_url = os.environ.get("OPENAI_BASE_URL", "").strip()
            env_model = os.environ.get("OPENAI_MODEL", "").strip()
            eff_url = explicit_url or env_url or spec.base_url
            eff_model = env_model or spec.default_model
            eff_key_env = explicit_key_env or spec.api_key_env
            if is_local_url(eff_url) and not explicit_key_env and not os.environ.get(eff_key_env, "").strip():
                eff_key_env = ""
            headers = {**spec.extra_headers, **(extra_headers or {})}
            return replace(
                spec,
                base_url=eff_url,
                default_model=eff_model,
                api_key_env=eff_key_env,
                extra_headers=headers,
            )
        if explicit_url or explicit_key_env or extra_headers:
            eff_url = explicit_url or spec.base_url
            eff_key_env = explicit_key_env or spec.api_key_env
            if explicit_url and is_local_url(eff_url) and not explicit_key_env and not os.environ.get(eff_key_env, "").strip():
                eff_key_env = ""
            headers = {**spec.extra_headers, **(extra_headers or {})}
            return replace(
                spec,
                base_url=eff_url,
                api_key_env=eff_key_env,
                extra_headers=headers,
            )
        return spec

    if explicit_url:
        eff_key_env = explicit_key_env
        if not eff_key_env and not is_local_url(explicit_url):
            cand = f"{clean.upper().replace('-', '_')}_API_KEY"
            if os.environ.get(cand, "").strip():
                eff_key_env = cand
            elif os.environ.get("OPENAI_API_KEY", "").strip():
                eff_key_env = "OPENAI_API_KEY"
        return ProviderSpec(
            name=clean or "custom",
            base_url=explicit_url,
            api_style="openai",
            default_model=os.environ.get("OPENAI_MODEL", "").strip() or "gpt-4o-mini",
            api_key_env=eff_key_env,
            supports_json_schema=True,
            supports_json_object=True,
            supports_tools=True,
            extra_headers=dict(extra_headers or {}),
            pricing=clean or "custom",
            note="Dynamic OpenAI-compatible endpoint.",
        )

    raise KeyError(
        f"unknown provider {name!r}; known: {', '.join(sorted(PROVIDERS))}"
    )


def provider_names() -> list[str]:
    return list(PROVIDERS)


def env_var(spec: ProviderSpec, override: str | None = None) -> str:
    """Which env var holds this provider's key (``override`` wins)."""
    return (override or spec.api_key_env).strip()


def as_dict(spec: ProviderSpec) -> Mapping[str, Any]:
    """A plain description, for `doctor` output."""
    return {
        "name": spec.name,
        "base_url": spec.base_url,
        "api_style": spec.api_style,
        "default_model": spec.default_model,
        "api_key_env": spec.api_key_env,
        "supports_json_schema": spec.supports_json_schema,
        "supports_json_object": spec.supports_json_object,
        "supports_tools": spec.supports_tools,
        "note": spec.note,
    }
