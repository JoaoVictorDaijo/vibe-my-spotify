"""Join two independent judges' verdict files row by row (zero LLM tokens).

Verdict line shape shared by the judge mandates:
    <pos> | <artist> – <title> -> <VERDICT> [| fallback: <home>] | <H|M|L> | <reason>
Agreements become proposal defaults, crossings go to the conciliator/owner.

Usage: verdict_convergence.py A.txt B.txt OUT_PREFIX [--field primary|fallback]
Writes OUT_PREFIX-agreed.md and OUT_PREFIX-crossed.md; prints the tally.
"""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path

LINE = re.compile(
    r"^\s*(?P<pos>C?\d+)\s*\|\s*(?P<track>.+?)\s*->\s*(?P<verdict>[^|]+?)\s*"
    r"(?:\|\s*fallback:\s*(?P<fallback>[^|]+?)\s*)?"
    r"\|\s*(?P<conf>[HML])\s*\|\s*(?P<reason>.*)$"
)


def load(path: Path) -> dict[str, dict]:
    rows = {}
    for ln in path.read_text().splitlines():
        m = LINE.match(ln)
        if m:
            rows[m["pos"]] = {k: (v or "").strip() for k, v in m.groupdict().items()}
    return rows


def sort_key(pos: str) -> tuple[bool, int]:
    return pos.startswith("C"), int(pos.lstrip("C"))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("a", type=Path)
    ap.add_argument("b", type=Path)
    ap.add_argument("out_prefix")
    ap.add_argument("--field", choices=("verdict", "fallback"), default="verdict")
    args = ap.parse_args()

    a, b = load(args.a), load(args.b)
    f = args.field
    agree, cross, one_sided = defaultdict(list), [], sorted(set(a) ^ set(b), key=sort_key)
    for pos in sorted(set(a) & set(b), key=sort_key):
        if a[pos][f].upper() == b[pos][f].upper():
            agree[a[pos][f]].append(pos)
        else:
            cross.append(pos)

    with open(f"{args.out_prefix}-agreed.md", "w") as out:
        out.write(f"# Agreed {f}s (both judges)\n")
        for dest, rows in sorted(agree.items(), key=lambda kv: -len(kv[1])):
            out.write(f"\n## {dest} — {len(rows)}\n")
            for pos in rows:
                out.write(f"- [{a[pos]['conf']}{b[pos]['conf']}] {pos} | {a[pos]['track']} — {a[pos]['reason']}\n")
    with open(f"{args.out_prefix}-crossed.md", "w") as out:
        out.write(f"# Crossed {f}s\n\n")
        for pos in cross:
            out.write(f"- {pos} | {a[pos]['track']}\n")
            for name, side in (("A", a[pos]), ("B", b[pos])):
                fb = f" (fallback {side['fallback']})" if side["fallback"] and f == "verdict" else ""
                out.write(f"    {name}[{side['conf']}]: {side[f]}{fb} — {side['reason']}\n")

    n = len(set(a) & set(b))
    agreed = sum(len(v) for v in agree.values())
    print(f"{f}: joined {n} | AGREE {agreed} ({agreed / n:.0%}) | CROSS {len(cross)} | one-sided {one_sided or 'none'}")
    for dest, rows in sorted(agree.items(), key=lambda kv: -len(kv[1])):
        print(f"  {dest}: {len(rows)}")
    pairs = Counter(tuple(sorted((a[p][f], b[p][f]))) for p in cross)
    print("top crossings:")
    for (x, y), k in pairs.most_common(12):
        print(f"  {x} <-> {y}: {k}")


if __name__ == "__main__":
    main()
