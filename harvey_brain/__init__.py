"""Provider-agnostic, cost-aware brain transports for Sellbird's host seam.

Two entry points matter:

* :class:`harvey_brain.router.RouterTransport` — an ordered chain with a daily
  dollar cap, satisfying ``async transport(prompt, *, agent, task, timeout,
  expect_json) -> str`` plus ``paused_reason``.
* :func:`harvey_brain.router_factory.build` — reads ``brain.yaml`` and returns
  that object; name it in the host's ``brain.transport_factory``.

Claude and Codex are kept as chain members that delegate to the host's own
isolated runtimes (see :mod:`harvey_brain.delegate`).
"""

from .base import Usage
from .config import BrainSettings, ChainEntry, load_settings
from .router import RouterTransport
from .router_factory import build, build_chain, doctor, doctor_async
from .spec import PROVIDERS, ProviderSpec, get_provider

__all__ = [
    "BrainSettings",
    "ChainEntry",
    "PROVIDERS",
    "ProviderSpec",
    "RouterTransport",
    "Usage",
    "build",
    "build_chain",
    "doctor",
    "doctor_async",
    "get_provider",
    "load_settings",
]
