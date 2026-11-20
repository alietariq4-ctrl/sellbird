"""The factory the host names in ``brain.transport_factory``.

``harvey_brain.router_factory:build`` reads ``brain.yaml`` (see
:mod:`harvey_brain.config`) and returns a :class:`RouterTransport`.
"""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
import os
from pathlib import Path
from typing import Any

from .config import (
    BrainSettings,
    ChainEntry,
    find_config,
    load_dotenv,
    load_settings,
    resolve_key,
)

logger = logging.getLogger(__name__)


def _env_custom_available() -> bool:
    from .spec import is_local_url

    base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
    if not base_url:
        return False
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    return bool(api_key) or is_local_url(base_url)


def _load_env_for_config(path: str | os.PathLike[str] | None) -> Path | None:
    found: Path | None = None
    try:
        found = find_config(path)
    except FileNotFoundError:
        found = None
    if found is not None:
        env_beside = found.parent / ".env"
        if env_beside.is_file():
            load_dotenv(env_beside)
        elif path is None:
            load_dotenv()
    elif path is None:
        load_dotenv()
    return found


def build_chain(settings: "BrainSettings", *, http=None) -> list[Any]:
    """Instantiate the configured chain. Skips providers without a key."""
    from .delegate import DelegateTransport
    from .openai_compat import OpenAICompatTransport
    from .spec import get_provider, is_local_url

    built: list[Any] = []
    for entry in settings.chain:
        try:
            spec = get_provider(
                entry.provider,
                base_url=entry.base_url,
                api_key_env=entry.api_key_env,
                extra_headers=entry.extra_headers,
            )
        except KeyError as exc:
            raise ValueError(str(exc)) from None
        if spec.delegates:
            built.append(DelegateTransport(
                spec.name,
                model=entry.model,
                reasoning_effort=entry.reasoning_effort,
                timeout=entry.timeout or 300.0,
            ))
            continue
        key = resolve_key(entry, spec)
        if not key and not spec.keyless and not is_local_url(spec.base_url):
            logger.info(
                "skipping %s: %s is empty", spec.name,
                entry.api_key_env or spec.api_key_env,
            )
            continue
        built.append(OpenAICompatTransport(
            spec,
            api_key=key,
            model=entry.model or spec.default_model,
            base_url=entry.base_url or spec.base_url,
            extra_headers=entry.extra_headers,
            http=http,
            max_retries=entry.max_retries,
            timeout=entry.timeout or 300.0,
            json_mode=entry.json_mode,
            reasoning_effort=entry.reasoning_effort,
            max_output_tokens=entry.max_output_tokens,
            temperature=entry.temperature,
            system_prefix=settings.system_prefix or None,
        ))

    if _env_custom_available():
        custom_url = os.environ.get("OPENAI_BASE_URL", "").strip().rstrip("/")
        already = any(
            getattr(t, "name", "") == "custom"
            or str(getattr(t, "base_url", "")).rstrip("/") == custom_url
            for t in built
        )
        if not already:
            spec = get_provider("custom")
            key = os.environ.get("OPENAI_API_KEY", "").strip()
            model = os.environ.get("OPENAI_MODEL", "").strip() or spec.default_model
            built.append(OpenAICompatTransport(
                spec,
                api_key=key,
                model=model,
                base_url=spec.base_url,
                http=http,
                system_prefix=settings.system_prefix or None,
            ))
    return built


def build(path: str | os.PathLike[str] | None = None, *,
          http=None, load_env: bool = True) -> "RouterTransport":
    """The callable named by ``brain.transport_factory``.

    ``harvey_brain.router_factory:build``. Raises only when the resulting chain
    is empty — a run with no usable provider is a configuration error, not a
    runtime failure to paper over.
    """
    from .router import RouterTransport

    if load_env:
        _load_env_for_config(path)

    try:
        settings = load_settings(path)
    except FileNotFoundError:
        if path is None and _env_custom_available():
            settings = BrainSettings(chain=[ChainEntry(provider="custom")])
        else:
            raise

    chain = build_chain(settings, http=http)
    if not chain:
        raise RuntimeError(
            "no usable provider in brain.yaml: every entry is missing its API "
            "key. Set OPENROUTER_API_KEY (or OPENAI_BASE_URL + OPENAI_API_KEY, "
            "or add a local ollama/lmstudio/vllm entry) and retry."
        )
    router = RouterTransport(
        chain,
        budget_usd_per_day=settings.max_usd_per_day,
        profiles=settings.profiles,
    )
    logger.info(
        "brain chain: %s (budget $%s/day%s)",
        " -> ".join(getattr(t, "name", "?") for t in chain),
        f"{settings.max_usd_per_day:.2f}" if settings.max_usd_per_day else "none",
        f"; {len(settings.profiles)} role profile(s)" if settings.profiles else "",
    )
    return router


def _prepare_doctor_rows(
    path: str | os.PathLike[str] | None = None,
    *,
    load_env: bool = True,
) -> tuple[list[tuple[dict[str, Any], Any, str]], str]:
    from .spec import PROVIDERS, get_provider, is_local_url

    if load_env:
        _load_env_for_config(path)

    entries: list[ChainEntry] = []
    config_error = ""
    try:
        settings = load_settings(path)
        entries = list(settings.chain)
        if _env_custom_available() and not any(e.provider == "custom" for e in entries):
            entries.append(ChainEntry(provider="custom"))
    except Exception as exc:
        config_error = f"{type(exc).__name__}: {exc}"
        entries = [ChainEntry(provider=name) for name in PROVIDERS]

    prepared: list[tuple[dict[str, Any], Any, str]] = []
    seen: set[str] = set()
    for entry in entries:
        if entry.provider in seen:
            continue
        seen.add(entry.provider)
        try:
            spec = get_provider(
                entry.provider,
                base_url=entry.base_url,
                api_key_env=entry.api_key_env,
                extra_headers=entry.extra_headers,
            )
        except KeyError as exc:
            prepared.append(({
                "name": entry.provider,
                "configured": False,
                "model": entry.model,
                "reachable": "not checked",
                "error": str(exc),
            }, None, ""))
            continue
        key = resolve_key(entry, spec)
        configured = bool(key) or spec.keyless or is_local_url(spec.base_url)
        row: dict[str, Any] = {
            "name": spec.name,
            "configured": configured,
            "model": entry.model or spec.default_model,
            "reachable": "not checked",
            "error": "",
        }
        if not configured:
            row["error"] = f"{entry.api_key_env or spec.api_key_env} is empty"
        elif spec.delegates:
            row["error"] = "host runtime: checked on first call"
        prepared.append((row, spec, key))
    return prepared, config_error


async def doctor_async(
    path: str | os.PathLike[str] | None = None,
    *,
    network: bool = False,
    http=None,
    load_env: bool = True,
) -> list[dict[str, Any]]:
    """Async per-provider configuration report (safe inside FastAPI / asyncio)."""
    prepared, config_error = _prepare_doctor_rows(path, load_env=load_env)
    rows: list[dict[str, Any]] = []
    for row, spec, key in prepared:
        if network and spec is not None:
            if row["configured"] and not spec.delegates:
                row["reachable"], row["error"] = await _probe(
                    spec, key, row["model"], http=http
                )
            elif spec.delegates:
                row["reachable"] = "host runtime"
        rows.append(row)
    if config_error and rows:
        rows[0]["error"] = rows[0]["error"] or config_error
    return rows


def doctor(
    path: str | os.PathLike[str] | None = None,
    *,
    network: bool = False,
    http=None,
    load_env: bool = True,
) -> list[dict[str, Any]]:
    """Per-provider configuration report. No network unless ``network``."""
    if not network:
        prepared, config_error = _prepare_doctor_rows(path, load_env=load_env)
        rows = [row for row, _, _ in prepared]
        if config_error and rows:
            rows[0]["error"] = rows[0]["error"] or config_error
        return rows

    try:
        asyncio.get_running_loop()
        loop_running = True
    except RuntimeError:
        loop_running = False

    if not loop_running:
        return asyncio.run(
            doctor_async(path, network=True, http=http, load_env=load_env)
        )
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(
            lambda: asyncio.run(
                doctor_async(path, network=True, http=http, load_env=load_env)
            )
        ).result()


async def _probe(spec: Any, key: str, model: str, *, http=None) -> tuple[str, str]:
    """One cheap authenticated GET. Only the models endpoint: no prompt sent."""
    import httpx

    owns = http is None
    client = http or httpx.AsyncClient(timeout=20.0)
    url = f"{spec.base_url.rstrip('/')}/models"
    headers = {**spec.headers()}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    try:
        response = await client.get(url, headers=headers)
        if 200 <= response.status_code < 300:
            return "yes", ""
        return "no", f"models returned {response.status_code}"
    except httpx.HTTPError as exc:
        return "no", type(exc).__name__
    finally:
        if owns:
            await client.aclose()
