"""Summarize trader.log volume and detect high-frequency no-op audit events."""
from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path


LINE_RE = re.compile(r"^.*? \| (\w+)\s*\| ([^ ]+) - (.*)$")
PREFIX_RE = re.compile(r"^(\[[^]]+\])")
NOISY_PREFIXES = {"[TRADE_IMPORT_UPDATE]", "[TRADE_STATUS]"}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?", default="logs/trader.log")
    parser.add_argument("--tail", type=int, default=5000)
    parser.add_argument("--top", type=int, default=15)
    parser.add_argument("--max-noisy-ratio", type=float, default=0.25)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"log not found: {path}")
        return 2

    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    sampled = lines[-max(1, args.tail) :]
    levels: Counter[str] = Counter()
    prefixes: Counter[str] = Counter()
    modules: Counter[str] = Counter()
    parsed = 0
    for line in sampled:
        match = LINE_RE.match(line)
        if not match:
            continue
        level, source, message = match.groups()
        parsed += 1
        levels[level] += 1
        modules[source.rsplit(":", 2)[0]] += 1
        prefix = PREFIX_RE.match(message)
        prefixes[prefix.group(1) if prefix else "(unprefixed)"] += 1

    noisy = sum(prefixes[prefix] for prefix in NOISY_PREFIXES)
    noisy_ratio = noisy / parsed if parsed else 0.0
    print(
        f"path={path} size_bytes={path.stat().st_size} "
        f"sampled={len(sampled)} parsed={parsed}"
    )
    print(f"levels={dict(levels)}")
    print(f"noisy_events={noisy} noisy_ratio={noisy_ratio:.3f}")
    print("top_prefixes:")
    for prefix, count in prefixes.most_common(args.top):
        print(f"{count:7} {prefix}")
    print("top_modules:")
    for module, count in modules.most_common(args.top):
        print(f"{count:7} {module}")

    if args.check and noisy_ratio > args.max_noisy_ratio:
        print(
            "FAIL: no-op trade synchronization logs exceed "
            f"{args.max_noisy_ratio:.3f} of parsed lines"
        )
        return 1
    if args.check:
        print("OK: noisy event ratio is within the configured limit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
