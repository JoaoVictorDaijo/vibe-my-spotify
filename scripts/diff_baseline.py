"""Diff a fresh export directory against the durable baseline.

The baseline (curation-review/baseline/) is the last known-good state; any
divergence in a fresh export is owner activity since then — the raw signal
future rebalance and vibe-learning jobs consume.

    python3 scripts/diff_baseline.py NEW_DIR [BASELINE_DIR] [--out FILE.md]

Reports per playlist: added tracks, removed tracks, and cross-playlist moves
(a URI removed from one playlist and added to another in the same window).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DEFAULT_BASELINE = Path(__file__).resolve().parent.parent / "curation-review" / "baseline"


def load_dir(d: Path) -> dict:
    playlists = {}
    for p in sorted(d.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and "tracks" in data:
            playlists[data.get("id") or p.stem] = data
    return playlists


def track_label(t: dict) -> str:
    return f"{', '.join(t.get('artists') or ['?'])} – {t.get('name')}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("new_dir", type=Path)
    ap.add_argument("baseline_dir", nargs="?", type=Path, default=DEFAULT_BASELINE)
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    new = load_dir(args.new_dir)
    base = load_dir(args.baseline_dir)

    added_by_uri, removed_by_uri = {}, {}
    lines = ["# Baseline diff", ""]
    changed = 0
    for pid in sorted(set(new) | set(base), key=lambda i: (base.get(i) or new.get(i))["name"]):
        b, n = base.get(pid), new.get(pid)
        if b is None:
            lines.append(f"## {n['name']} — NEW playlist ({n['total']} tracks)")
            continue
        if n is None:
            lines.append(f"## {b['name']} — missing from new export ({b['total']} tracks in baseline)")
            continue
        if b.get("snapshot_id") == n.get("snapshot_id"):
            continue
        b_uris = {t["uri"]: t for t in b["tracks"] if t.get("uri")}
        n_uris = {t["uri"]: t for t in n["tracks"] if t.get("uri")}
        added = [n_uris[u] for u in n_uris.keys() - b_uris.keys()]
        removed = [b_uris[u] for u in b_uris.keys() - n_uris.keys()]
        if not added and not removed:
            lines.append(f"## {b['name']} — snapshot moved, contents identical (reorder or metadata)")
            changed += 1
            continue
        changed += 1
        lines.append(f"## {b['name']} — {b['total']} → {n['total']}")
        for t in added:
            lines.append(f"- + {track_label(t)}")
            added_by_uri.setdefault(t["uri"], []).append(b["name"])
        for t in removed:
            lines.append(f"- − {track_label(t)}")
            removed_by_uri.setdefault(t["uri"], []).append((b["name"], track_label(t)))
        lines.append("")

    moves = [(src, label, dst)
             for uri, srcs in removed_by_uri.items()
             for (src, label) in [srcs[0]]
             for dst in added_by_uri.get(uri, [])]
    if moves:
        lines.append("## Cross-playlist moves detected")
        for src, label, dst in moves:
            lines.append(f"- {label}: {src} → {dst}")
        lines.append("")

    if changed == 0:
        lines.append("No drift — every playlist matches the baseline.")
    report = "\n".join(lines) + "\n"
    if args.out:
        args.out.write_text(report)
        print(f"written: {args.out}")
    print(f"playlists changed: {changed}  adds: {sum(len(v) for v in added_by_uri.values())}  "
          f"removals: {len(removed_by_uri)}  moves: {len(moves)}")
    if not args.out:
        print()
        print(report)


if __name__ == "__main__":
    main()
