"""``python -m harvey_brain probe`` — see it work before spending money.

Default is a **dry run**: no network, no key values printed, just what each
provider would use, whether its key looks present (length only), and what one
representative call would cost from the price table.

``--live`` makes exactly ONE minimal real call per provider. It refuses to run
without ``--yes``, because that is real money.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from dataclasses import asdict, dataclass
from time import monotonic
from typing import Any, Callable, Sequence

from .base import Usage
from .pricing import estimate

# One representative call per role, from the profile defaults: research-sized
# input, short output. Only used to price the dry run.
PROBE_INPUT_TOKENS = 8_000
PROBE_OUTPUT_TOKENS = 1_200
LIVE_PROMPT = "reply with the single word ok"
LIVE_MAX_TOKENS = 16


@dataclass(slots=True)
class ProbeRow:
    provider: str
    model: str
    endpoint: str
    auth_env: str
    key_present: bool
    key_hint: str
    est_cost_usd: float
    status: str = "dry-run"
    latency_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    error: str = ""


def key_hint(value: str) -> str:
    """A length-only fingerprint. Never echoes any part of the key.

    Printing a prefix would be a leak in screenshots and pasted logs; the
    length is enough to tell "set" from "empty" from "truncated by a copy-paste".
    """
    if not value:
        return "empty"
    return f"set ({len(value)} chars)"


def resolve_entries(chain: Sequence[Any]) -> list[tuple[str, str, Any]]:
    """(provider, model, spec) for each chain member that speaks HTTP."""
    rows: list[tuple[str, str, Any]] = []
    for transport in chain:
        spec = getattr(transport, "spec", None)
        if spec is None:                      # delegate: no endpoint to probe
            continue
        rows.append((spec.name, getattr(transport, "model", "") or
                     spec.default_model, spec))
    return rows


def dry_run(*, config_path: str | None = None,
            chain: Sequence[Any] | None = None,
            role_of: Callable[[str], Any] | None = None) -> list[ProbeRow]:
    """Describe every configured provider. Zero network calls."""
    from .router_factory import build_chain
    from .config import find_config, load_dotenv, load_settings

    if chain is None:
        found = find_config(config_path)
        if found is not None:
            load_dotenv(found.parent / ".env")
        elif config_path is None:
            load_dotenv()
        settings = load_settings(config_path)
        chain = build_chain(settings)

    rows: list[ProbeRow] = []
    seen: set[tuple[str, str]] = set()
    for provider, model, spec in resolve_entries(chain):
        if (provider, model) in seen:
            continue
        seen.add((provider, model))
        env_name = spec.api_key_env
        value = os.environ.get(env_name, "").strip() if env_name else ""
        rows.append(ProbeRow(
            provider=provider,
            model=model,
            endpoint=f"{spec.base_url.rstrip('/')}/chat/completions",
            auth_env=env_name or "— (keyless)",
            key_present=bool(value) or spec.keyless,
            key_hint=key_hint(value) if env_name else "not needed",
            est_cost_usd=estimate(
                provider, model,
                input_tokens=PROBE_INPUT_TOKENS,
                output_tokens=PROBE_OUTPUT_TOKENS,
            ),
        ))
    # Delegate members cannot be probed but must not look "missing".
    for transport in chain:
        if getattr(transport, "spec", None) is None:
            rows.append(ProbeRow(
                provider=str(getattr(transport, "name", "?")),
                model=getattr(transport, "model", "") or "(host default)",
                endpoint="host runtime",
                auth_env="— (subscription)",
                key_present=True,
                key_hint="host session",
                est_cost_usd=0.0,
                status="delegated",
            ))
    return rows


async def _probe_one(transport: Any, *, timeout: float = 90.0) -> ProbeRow:
    """One minimal call **through the transport itself**.

    Deliberately not a second HTTP code path: probing must exercise the same
    retry/auth/redaction logic production uses, or it proves nothing.
    """
    spec = transport.spec
    provider, model = spec.name, getattr(transport, "model", "") or spec.default_model
    key = getattr(transport, "api_key", "") or ""
    row = ProbeRow(
        provider=provider, model=model,
        endpoint=f"{spec.base_url.rstrip('/')}/chat/completions",
        auth_env=spec.api_key_env or "— (keyless)",
        key_present=bool(key) or spec.keyless,
        key_hint=key_hint(key) if spec.api_key_env else "not needed",
        est_cost_usd=estimate(provider, model,
                              input_tokens=PROBE_INPUT_TOKENS,
                              output_tokens=PROBE_OUTPUT_TOKENS),
        status="live",
    )
    started = monotonic()
    text = await transport(
        LIVE_PROMPT, agent="probe", task="probe", timeout=timeout,
        expect_json=False, max_output_tokens=LIVE_MAX_TOKENS, temperature=0,
    )
    row.latency_ms = int((monotonic() - started) * 1000)

    usage = getattr(transport, "last_usage", None)
    if text:
        row.status = "ok"
        if isinstance(usage, Usage):
            row.input_tokens = usage.input_tokens
            row.output_tokens = usage.output_tokens
            row.cost_usd = usage.cost_usd
        else:
            row.cost_usd = estimate(provider, model,
                                    input_tokens=row.input_tokens,
                                    output_tokens=row.output_tokens)
        return row

    row.status = "failed"
    row.error = str(getattr(transport, "last_error", "") or
                    getattr(transport, "paused_reason", "") or "empty reply")[:120]
    return row


async def _close_chain(chain: Sequence[Any]) -> None:
    for transport in chain:
        close = getattr(transport, "aclose", None)
        if close is None:
            continue
        try:
            res = close()
            if inspect.isawaitable(res):
                await res
        except Exception:
            pass


async def live(chain: Sequence[Any], *, http: Any = None,
               confirm: bool = False) -> list[ProbeRow]:
    """ONE minimal real call per HTTP provider. Requires explicit confirmation.

    ``http`` is accepted for signature symmetry with the other entry points and
    is otherwise unused: the call goes through each transport, which already
    holds its own client (and which tests can inject).
    """
    if not confirm:
        raise PermissionError(
            "live probing spends real money: pass --yes to confirm"
        )
    rows: list[ProbeRow] = []
    try:
        for transport in chain:
            spec = getattr(transport, "spec", None)
            if spec is None:                       # delegate: nothing to bill here
                if not getattr(transport, "paused_reason", ""):
                    rows.append(ProbeRow(
                        provider=str(getattr(transport, "name", "?")),
                        model=getattr(transport, "model", "") or "(host default)",
                        endpoint="host runtime",
                        auth_env="— (subscription)", key_present=True,
                        key_hint="host session", est_cost_usd=0.0,
                        status="delegated",
                    ))
                continue
            if spec.api_key_env and not getattr(transport, "api_key", ""):
                continue                            # cannot authenticate: skip
            rows.append(await _probe_one(transport))
        return rows
    finally:
        await _close_chain(chain)


def render(rows: Sequence[ProbeRow], *, live_mode: bool) -> str:
    headers = ("provider", "model", "endpoint", "auth env", "key",
               "est/call", "status", "latency", "tokens", "cost")
    widths = (13, 32, 52, 22, 18, 10, 12, 9, 13, 9)
    lines = ["  ".join(_fmt(h, w) for h, w in zip(headers, widths)).rstrip(),
             "  ".join("-" * w for w in widths)]
    for row in rows:
        lines.append("  ".join(_fmt(value, width) for value, width in zip((
            row.provider,
            row.model,
            row.endpoint if not row.error else f"{row.endpoint} ({row.error})",
            row.auth_env,
            row.key_hint,
            f"${row.est_cost_usd:.4f}",
            row.status,
            f"{row.latency_ms} ms" if row.latency_ms else "—",
            f"{row.input_tokens}/{row.output_tokens}" if row.input_tokens or
            row.output_tokens else "—",
            f"${row.cost_usd:.6f}" if row.cost_usd else "—",
        ), widths)).rstrip())
    return "\n".join(lines)


def _fmt(value: Any, width: int) -> str:
    text = str(value if value is not None else "")
    if len(text) > width:
        text = text[: width - 1] + "…"
    return text.ljust(width)


def main(argv: list[str] | None = None) -> int:
    import argparse

    from .config import find_config, load_dotenv, load_settings
    from .router_factory import build_chain

    parser = argparse.ArgumentParser(
        prog="python -m harvey_brain probe",
        description="Check each configured provider without spending money",
    )
    parser.add_argument("--config", default="", help="path to brain.yaml")
    parser.add_argument("--live", action="store_true",
                        help="make one minimal real call per provider")
    parser.add_argument("--yes", action="store_true",
                        help="confirm that --live may spend money")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    path = args.config or None
    try:
        found = find_config(path)
        if found is not None:
            load_dotenv(found.parent / ".env")
        elif path is None:
            load_dotenv()
        settings = load_settings(path)
    except Exception as exc:
        print(f"  {type(exc).__name__}: {exc}")
        return 2
    chain = build_chain(settings)
    if not chain:
        print("  no usable provider: every chain entry is missing its API key.")
        return 2

    try:
        if args.live:
            if not args.yes:
                print("  --live makes real, billable calls. Re-run with --live --yes.")
                return 2
            rows = asyncio.run(live(chain, confirm=True))
        else:
            rows = dry_run(chain=chain)
    finally:
        asyncio.run(_close_chain(chain))

    if args.json:
        print(json.dumps([asdict(row) for row in rows], indent=2, default=str))
        return 0

    print()
    print(f"  brain config: {found}")
    print(f"  chain:        {' -> '.join(getattr(t, 'name', '?') for t in chain)}")
    cap = settings.max_usd_per_day
    print(f"  budget:       {f'${cap:.2f}/day' if cap else 'none'}")
    print(f"  mode:         {'LIVE (one minimal call each)' if args.live else 'dry run (no network)'}")
    print()
    print(render(rows, live_mode=args.live))
    print()
    if settings.profiles:
        print("  per-role models (agent= label from the host):")
        for role in sorted(settings.profiles):
            profile = settings.profiles[role]
            print(f"    {role:<10} {profile.provider + '/' if profile.provider else ''}"
                  f"{profile.model or '(chain)'}"
                  f"  max_tokens={profile.max_output_tokens or 'default'}"
                  f"  json={profile.json_mode}")
        print()
    if not args.live:
        print(f"  Estimated cost of one {PROBE_INPUT_TOKENS:,}-in/"
              f"{PROBE_OUTPUT_TOKENS:,}-out call, from the price table.")
        print("  Add --live --yes to make one real minimal call per provider.")
    print()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
