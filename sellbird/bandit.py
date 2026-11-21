"""Framework selection by Thompson sampling (Phase 4, ROADMAP.md).

Cold email frameworks (PAS, BAB, direct offer, ...) perform differently
per product and per audience, and the only honest way to find out is to
watch real reply rates. This is a multi-armed bandit: each campaign is
written in one framework, a reply from anyone it reached is a success,
and a finished sequence with no reply is a failure. Thompson sampling
(Beta-Bernoulli) picks the next framework by sampling each one's
posterior reply probability and taking the highest draw — exploration
early, exploitation once the data settles, no extra dependencies.

The counters live in the campaigns table itself (framework column +
prospect outcomes), so there is no separate state to keep consistent.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("sellbird.bandit")

# The built-in shape (prompts/writer.md) is always available; the named
# frameworks are alternative structures the Writer can be told to use.
BUILT_IN = "built_in"
DEFAULT_FRAMEWORKS = (BUILT_IN,)

# One-line instruction per framework, injected into the writer prompt. Each
# must stay compatible with the strict email rules (short, plain, one ask).
FRAMEWORK_INSTRUCTIONS = {
    "pas": (
        "FRAMEWORK: Problem, Agitate, Solve. Open with the problem in their "
        "words, one sentence on what it costs, then the offer as the fix."
    ),
    "bab": (
        "FRAMEWORK: Before, After, Bridge. Open with today's reality, one "
        "sentence of the after, then the offer as the bridge. No before/after "
        "list formatting: plain sentences only."
    ),
    "direct_offer": (
        "FRAMEWORK: Direct offer. Lead with the free useful thing you can "
        "send them, one sentence of why it fits them, one yes/no ask."
    ),
    "question_lead": (
        "FRAMEWORK: Question lead. Open with one question about their "
        "situation a peer would actually answer, then the offer in one line."
    ),
}


def known_frameworks(config=None) -> tuple[str, ...]:
    """The bandit's arms, from writer.frameworks in config.

    Whatever the operator lists are the arms — those are what get tested
    against each other. Unknown names are dropped. With nothing (or nothing
    usable) configured there is no bandit: the Writer uses its built-in
    shape, and (BUILT_IN,) is the single arm so behaviour is unchanged.
    """
    raw = getattr(getattr(config, "writer", None), "frameworks", None) or []
    out = []
    for name in raw:
        key = str(name or "").strip().lower()
        if key in FRAMEWORK_INSTRUCTIONS and key not in out:
            out.append(key)
    return tuple(out) if out else DEFAULT_FRAMEWORKS


def instruction_for(framework: str) -> str:
    """The prompt line for a framework; empty for the built-in shape."""
    return FRAMEWORK_INSTRUCTIONS.get(str(framework or "").strip().lower(), "")


def choose_framework(stats: dict[str, tuple[int, int]],
                     frameworks: tuple[str, ...] | None = None,
                     *, rng=None) -> str:
    """Pick the next framework by Thompson sampling.

    ``stats`` maps framework -> (replies, finished_sequences). A framework
    with no history explores first: Beta(1, 1) gives it a fair draw. The
    draw is seeded per call by default (system randomness); tests pass an
    rng for determinism.
    """
    import random as _random
    rng = rng or _random
    frameworks = frameworks or DEFAULT_FRAMEWORKS
    best, best_draw = BUILT_IN, -1.0
    for name in frameworks:
        replied, finished = stats.get(name, (0, 0))
        # Beta(1 + replies, 1 + finished - replies): uniform prior, so an
        # untried framework draws from Beta(1, 1) and gets its chance.
        alpha = 1 + max(0, int(replied))
        beta = 1 + max(0, int(finished) - max(0, int(replied)))
        draw = rng.betavariate(alpha, beta)
        if draw > best_draw:
            best, best_draw = name, draw
    return best
