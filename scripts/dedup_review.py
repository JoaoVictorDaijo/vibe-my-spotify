"""Generate an interactive dedup review file with a computed proposal per group.

Combines dedup.json with phantom-audit ISRC evidence and the owner's
keep-preference (Deluxe/Extended > original album > compilation/Best-of).
Checkbox semantics are uniform: unchecked = proposal executes, checked =
override (flip the proposal).

    python scripts/dedup_review.py <export-dir> <output-md>

Also writes dedup_proposals.json next to dedup.json so the apply step can join
checkbox states (parsed back from the md by line ID) with machine data.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

COMP_HINT = re.compile(
    r"best of|greatest|hits|collection|anthology|singles|sampler|late night tales|b.sides", re.I
)
DELUXE_HINT = re.compile(r"deluxe|expanded|special|anniversary|extended|legacy|super", re.I)
# User rule: identical length is strong evidence of the same recording.
DURATION_TOLERANCE_MS = 2000
# A version marker in one title but not the other means different recordings
# even at identical length (e.g. an acoustic take that happens to match).
VERSION_MARKER = re.compile(r"acoustic|live|demo|unplugged|session|remix", re.I)


def album_rank(name: str | None) -> int:
    n = name or ""
    if COMP_HINT.search(n):
        return 1
    if DELUXE_HINT.search(n):
        return 3
    return 2


RANK_LABEL = {1: "compilation", 2: "original", 3: "deluxe/extended"}


def fmt_dur(ms) -> str:
    if not ms:
        return "?:??"
    s = round(ms / 1000)
    return f"{s // 60}:{s % 60:02d}"


def load_exports(out_dir: Path) -> dict:
    uri_info = {}
    for p in sorted(out_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not (isinstance(data, dict) and "tracks" in data):
            continue
        for t in data["tracks"]:
            if t.get("uri"):
                uri_info[t["uri"]] = t
    return uri_info


def main() -> None:
    out_dir = Path(sys.argv[1])
    out_md = Path(sys.argv[2])
    dedup = json.loads((out_dir / "dedup.json").read_text())
    cache_path = out_dir / "phantom_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    uri_info = load_exports(out_dir)

    proposals = []
    md = [
        "# Dedup review",
        "",
        "Every group shows evidence and a **PROPOSED** action.",
        "**Unchecked box = the proposal executes. Checked box = override (flip it).**",
        "Add a note after any line where the flip needs detail.",
        "",
        f"## A. Same song (same URI) in multiple playlists ({len(dedup['cross_playlist'])})",
        "",
        "Proposal for all of these: the drain/rebalance stage picks ONE home and the",
        "other copy is removed. Check a box to declare the overlap intentional (song",
        "stays in both playlists).",
        "",
    ]

    for i, d in enumerate(dedup["cross_playlist"]):
        gid = f"X{i + 1:02d}"
        where = "; ".join(f"{k} @ {v}" for k, v in d["playlists"].items())
        info = uri_info.get(d["uri"]) or {}
        md.append(f"- [ ] ({gid}) {d['track']} [{fmt_dur(info.get('duration_ms'))}] — {where}")
        proposals.append({"id": gid, "kind": "cross", "uri": d["uri"], "track": d["track"],
                          "playlists": d["playlists"], "proposed": "single-home-via-drain",
                          "override_means": "keep-in-both"})

    md += ["", f"## B. Same-name variants, different URIs ({len(dedup['fuzzy'])})", ""]

    for i, g in enumerate(dedup["fuzzy"]):
        gid = f"F{i + 1:02d}"
        entries = []
        for e in g["entries"]:
            info = uri_info.get(e["uri"]) or {}
            tid = info.get("id")
            isrc = (cache.get(tid) or {}).get("isrc") if tid else None
            entries.append({**e, "duration_ms": info.get("duration_ms"), "isrc": isrc,
                            "rank": album_rank(e.get("album"))})

        uris = {e["uri"] for e in entries}
        isrcs = {e["isrc"] for e in entries if e["isrc"]}
        durs = [e["duration_ms"] for e in entries if e["duration_ms"]]
        dur_close = len(durs) >= 2 and max(durs) - min(durs) <= DURATION_TOLERANCE_MS
        marker_sets = {frozenset(m.lower() for m in VERSION_MARKER.findall(e["track"])) for e in entries}

        if len(isrcs) == 1 and all(e["isrc"] for e in entries):
            verdict, why = "SAME", "identical ISRC"
        elif len(marker_sets) > 1:
            verdict, why = "DIFFERENT", "version markers differ"
        elif len(isrcs) > 1 and not dur_close:
            verdict, why = "DIFFERENT", "different ISRC and length"
        elif dur_close:
            verdict, why = "SAME", "identical length" if len(isrcs) <= 1 else "same length, mixed ISRC"
        else:
            verdict, why = "DIFFERENT", "length differs"

        keep = max(entries, key=lambda e: (e["rank"], -(e["pos"])))
        if verdict == "SAME":
            removals = sorted(uris - {keep["uri"]})
            action = (f"keep {keep['playlist']} pos {keep['pos']} "
                      f"({RANK_LABEL[keep['rank']]}: {keep['album']}), remove the other cop"
                      f"{'y' if len(removals) == 1 else 'ies'}")
        else:
            action = "keep both (different recordings)"

        md.append(f"- [ ] ({gid}) **{g['title_artist']}** — {verdict} ({why}) — PROPOSED: {action}")
        for e in entries:
            tag = RANK_LABEL[e["rank"]]
            md.append(f"    - {e['playlist']} pos {e['pos']}: [{fmt_dur(e['duration_ms'])}] "
                      f"{e['track']} [{e['album']}, {e['year']}] ({tag})")
        proposals.append({"id": gid, "kind": "fuzzy", "title_artist": g["title_artist"],
                          "verdict": verdict, "why": why, "keep_uri": keep["uri"],
                          "entries": entries, "proposed": action,
                          "override_means": "flip-verdict"})

    (out_dir / "dedup_proposals.json").write_text(json.dumps(proposals, ensure_ascii=False, indent=1))
    out_md.write_text("\n".join(md) + "\n")
    same = sum(1 for p in proposals if p.get("verdict") == "SAME")
    diff = sum(1 for p in proposals if p.get("verdict") == "DIFFERENT")
    print(f"review written: {out_md}")
    print(f"cross-playlist: {len(dedup['cross_playlist'])}  fuzzy SAME: {same}  fuzzy DIFFERENT: {diff}")


if __name__ == "__main__":
    main()
