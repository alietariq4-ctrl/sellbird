"""``brain.yaml`` schema, discovery, and loading.

Kept separate from :mod:`harvey_brain.router_factory` so the file format can be
read, validated, and printed without constructing a single transport (that is
what ``doctor`` does).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

logger = logging.getLogger(__name__)

DEFAULT_CONFIG_NAMES = ("brain.yaml", "brain.yml")
DEFAULT_BUDGET_USD = 5.0
_DOTENV_LOADED: dict[str, str] = {}


@dataclass(slots=True)
class ChainEntry:
    provider: str
    model: str = ""
    api_key_env: str = ""
    base_url: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
    json_mode: str = "auto"
    reasoning_effort: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    max_retries: int = 2
    timeout: float | None = None

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> "ChainEntry":
        if not isinstance(data, Mapping):
            raise ValueError("each chain entry must be a mapping")
        provider = str(data.get("provider") or "").strip()
        if not provider:
            raise ValueError("each chain entry needs a 'provider'")
        known = {
            "provider", "model", "api_key_env", "base_url", "extra_headers",
            "json_mode", "reasoning_effort", "max_output_tokens", "temperature",
            "max_retries", "timeout",
        }
        extra = set(data) - known
        if extra:
            raise ValueError(
                f"unknown keys in chain entry for {provider!r}: "
                f"{', '.join(sorted(extra))}"
            )
        raw_headers = data.get("extra_headers")
        if raw_headers is None:
            headers: dict[str, str] = {}
        elif isinstance(raw_headers, Mapping):
            headers = {str(k).strip(): str(v) for k, v in raw_headers.items() if str(k).strip()}
        else:
            raise ValueError(f"'extra_headers' for {provider!r} must be a mapping")

        return cls(
            provider=provider,
            model=str(data.get("model") or "").strip(),
            api_key_env=str(data.get("api_key_env") or "").strip(),
            base_url=str(data.get("base_url") or "").strip(),
            extra_headers=headers,
            json_mode=str(data.get("json_mode") or "auto").strip(),
            reasoning_effort=(str(data["reasoning_effort"]).strip()
                              if data.get("reasoning_effort") else None),
            max_output_tokens=(int(data["max_output_tokens"])
                               if data.get("max_output_tokens") is not None else None),
            temperature=(float(data["temperature"])
                         if data.get("temperature") is not None else None),
            max_retries=int(data.get("max_retries", 2)),
            timeout=(float(data["timeout"]) if data.get("timeout") is not None else None),
        )


@dataclass(slots=True)
class BrainSettings:
    chain: list[ChainEntry] = field(default_factory=list)
    max_usd_per_day: float | None = DEFAULT_BUDGET_USD
    system_prefix: str = ""
    # role -> TaskProfile. Empty means "no per-role routing": every call walks
    # the chain. Profiles are merged over the built-in defaults when the key
    # ``profiles:`` is present at all.
    profiles: dict[str, "TaskProfile"] = field(default_factory=dict)
    use_default_profiles: bool = False

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "BrainSettings":
        data = data or {}
        if not isinstance(data, Mapping):
            raise ValueError("brain.yaml must be a mapping")
        raw_chain = data.get("chain")
        if not isinstance(raw_chain, list) or not raw_chain:
            raise ValueError("brain.yaml needs a non-empty 'chain:' list")
        budget = data.get("budget") or {}
        if not isinstance(budget, Mapping):
            raise ValueError("'budget' must be a mapping")
        cap = budget.get("max_usd_per_day", DEFAULT_BUDGET_USD)
        profiles, use_defaults = _profiles_from(data.get("profiles"))
        return cls(
            chain=[ChainEntry.from_mapping(item) for item in raw_chain],
            max_usd_per_day=(float(cap) if cap is not None else None),
            system_prefix=str(data.get("system_prefix") or "").strip(),
            profiles=profiles,
            use_default_profiles=use_defaults,
        )


def _profiles_from(raw: Any) -> tuple[dict[str, "TaskProfile"], bool]:
    """Parse the ``profiles:`` block.

    ``profiles: false`` disables per-role routing outright. A mapping is merged
    over ``DEFAULT_PROFILES`` so an operator only spells out the roles they
    want to change.
    """
    from .profile import DEFAULT_PROFILES, TaskProfile

    if raw is None:
        return {}, False
    if raw is False:
        return {}, False
    if raw is True or not isinstance(raw, Mapping):
        raise ValueError(
            "'profiles' must be a mapping of role -> settings, or false to "
            "disable per-role routing"
        )
    merged = dict(DEFAULT_PROFILES)
    for role, item in raw.items():
        key = str(role).strip().lower()
        if not key:
            raise ValueError("a profile needs a role name")
        if item is False:
            merged.pop(key, None)       # explicitly switch one role off
            continue
        merged[key] = TaskProfile.from_mapping(item)
    return merged, True


def _search_roots() -> list[Path]:
    """Ordered directories to search for ``brain.yaml`` when no path is given."""
    roots: list[Path] = []

    sellbird_home = os.environ.get("SELLBIRD_HOME", "").strip()
    if sellbird_home:
        roots.append(Path(sellbird_home))

    try:
        from sellbird.workspace_home import current_home
        home_root = getattr(current_home(), "root", None)
        if home_root is not None:
            roots.append(Path(home_root))
    except Exception:
        pass

    try:
        from sellbird.paths import PROJECT_ROOT
        if PROJECT_ROOT is not None:
            roots.append(Path(PROJECT_ROOT))
    except Exception:
        pass

    roots.append(Path.cwd())
    return roots


def find_config(path: str | os.PathLike[str] | None = None) -> Path | None:
    """Explicit path -> $HARVEY_BRAIN_CONFIG -> $SELLBIRD_HOME -> current_home -> PROJECT_ROOT -> cwd."""
    if path:
        candidate = Path(path)
        if not candidate.is_file():
            raise FileNotFoundError(f"brain config not found: {candidate}")
        return candidate
    from_env = os.environ.get("HARVEY_BRAIN_CONFIG", "").strip()
    if from_env:
        candidate = Path(from_env)
        if not candidate.is_file():
            raise FileNotFoundError(
                f"HARVEY_BRAIN_CONFIG points at a missing file: {candidate}")
        return candidate

    seen: set[Path] = set()
    for root in _search_roots():
        try:
            resolved = root.resolve()
        except OSError:
            resolved = root
        if resolved in seen:
            continue
        seen.add(resolved)
        for name in DEFAULT_CONFIG_NAMES:
            candidate = root / name
            if candidate.is_file():
                return candidate
    return None


def load_settings(path: str | os.PathLike[str] | None = None) -> BrainSettings:
    """Read and validate ``brain.yaml``. Raises with an actionable message."""
    found = find_config(path)
    if found is None:
        raise FileNotFoundError(
            "no brain.yaml found: copy brain.example.yaml to brain.yaml next to "
            "sellbird.yaml, or set HARVEY_BRAIN_CONFIG"
        )
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover - yaml ships with the host
        raise ImportError(
            "reading brain.yaml needs PyYAML (pip install pyyaml); "
            "or build the chain in code with router_factory.build_chain"
        ) from exc
    raw = found.read_text(encoding="utf-8")
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"invalid YAML in {found}: {exc}") from exc
    if data is None:
        raise ValueError(f"{found} is empty")
    return BrainSettings.from_mapping(data)


def _parse_dotenv_value(raw_value: str) -> str:
    """Parse a .env RHS: respect quotes and strip inline ``#`` comments on unquoted values."""
    val = raw_value.strip()
    if not val:
        return ""
    if val[0] in ("'", '"'):
        quote = val[0]
        end = val.find(quote, 1)
        if end != -1:
            return val[1:end]
        return val.strip("'\"")
    if "#" in val:
        val = val.split("#", 1)[0]
    return val.strip()


def load_dotenv(
    path: str | os.PathLike[str] | None = None,
    *,
    override: bool = False,
) -> None:
    """Best-effort .env loader.

    Strips inline ``#`` comments on unquoted values. Never clobbers a non-empty
    shell/monkeypatched variable unless ``override=True``, while ALWAYS updating
    variables that are missing, empty placeholders (``""``), or previously set
    by ``load_dotenv`` itself so editing ``.env`` takes effect immediately.
    """
    candidates: list[Path] = []
    if path:
        candidates.append(Path(path))
    else:
        candidates.append(Path.cwd() / ".env")
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, raw_val = line.partition("=")
            key = key.strip()
            if key.startswith("export "):
                key = key[len("export "):].strip()
            if not key:
                continue
            value = _parse_dotenv_value(raw_val)
            existing = os.environ.get(key)
            if existing is None:
                os.environ[key] = value
                _DOTENV_LOADED[key] = value
            elif existing == "":
                if value:
                    os.environ[key] = value
                    _DOTENV_LOADED[key] = value
            elif override or _DOTENV_LOADED.get(key) == existing:
                if value or override:
                    os.environ[key] = value
                    _DOTENV_LOADED[key] = value
        return


def resolve_key(entry: ChainEntry, spec: Any) -> str:
    """The key for this entry: explicit env name, else the provider default."""
    name = (entry.api_key_env or spec.api_key_env or "").strip()
    return os.environ.get(name, "").strip() if name else ""
