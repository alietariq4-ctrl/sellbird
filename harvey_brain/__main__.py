"""Command line: ``python -m harvey_brain doctor`` / ``probe``.

``doctor`` prints the provider table (network only with ``--network``).
``probe`` dispatches to :mod:`harvey_brain.probe`, which is a dry run unless
``--live --yes`` is given.
"""

from __future__ import annotations

import argparse
import sys

from .config import load_dotenv
from .router_factory import doctor, find_config, load_settings


def _fmt(value: object, width: int) -> str:
    text = str(value if value is not None else "")
    if len(text) > width:
        text = text[: width - 1] + "…"
    return text.ljust(width)


COLUMNS = (
    ("provider", "name", 14),
    ("configured", "configured", 10),
    ("model", "model", 34),
    ("reachable", "reachable", 11),
    ("error", "error", 46),
)


def render(rows: list[dict]) -> str:
    lines = [
        "  ".join(_fmt(label, width) for label, _, width in COLUMNS).rstrip(),
        "  ".join("-" * width for _, _, width in COLUMNS),
    ]
    for row in rows:
        cells = []
        for label, key, width in COLUMNS:
            value = row.get(key, "")
            if label == "configured":
                value = "yes" if value else "no"
            cells.append(_fmt(value, width))
        lines.append("  ".join(cells).rstrip())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # `probe` owns its own flags (--live/--yes/--json), so dispatch it before
    # this parser validates anything.
    if argv and argv[0] == "probe":
        from .probe import main as probe_main
        return probe_main(argv[1:])

    parser = argparse.ArgumentParser(
        prog="python -m harvey_brain",
        description="Inspect the brain provider chain",
    )
    sub = parser.add_subparsers(dest="command")
    doc = sub.add_parser("doctor", help="show each provider's configuration")
    doc.add_argument("--config", default="", help="path to brain.yaml")
    doc.add_argument("--network", action="store_true",
                     help="also probe each configured provider (one GET to /models)")

    args = parser.parse_args(argv)
    if (args.command or "doctor") != "doctor":  # pragma: no cover
        parser.error(f"unknown command {args.command!r}")

    path = args.config or None
    found = None
    try:
        found = find_config(path)
    except FileNotFoundError as exc:
        print(f"  {exc}", file=sys.stderr)
        return 2

    if found is not None:
        load_dotenv(found.parent / ".env")
    elif path is None:
        load_dotenv()

    rows = doctor(path, network=args.network, load_env=False)
    print()
    if found is not None:
        print(f"  brain config: {found}")
        try:
            settings = load_settings(path)
            chain = " -> ".join(entry.provider for entry in settings.chain)
            print(f"  chain:        {chain}")
            cap = settings.max_usd_per_day
            print(f"  budget:       {f'${cap:.2f}/day' if cap else 'none'}")
        except Exception as exc:
            print(f"  chain:        invalid ({type(exc).__name__}: {exc})")
    else:
        print("  brain config: none found (showing every known provider)")
    print()
    print(render(rows))
    configured = sum(1 for row in rows if row["configured"])
    print()
    print(f"  {configured} of {len(rows)} provider(s) configured.")
    if not args.network:
        print("  Reachability not checked. Add --network to probe each one.")
    print()
    return 0 if configured else 1


if __name__ == "__main__":  # pragma: no cover - entry point
    raise SystemExit(main())
