"""Deterministic duplicate detection across exported playlists.

Reads export JSONs from a directory and reports exact URI duplicates (within
and across playlists) plus fuzzy title+artist matches that likely point at
alternate releases of the same recording:
    python scripts/dedup_report.py <export-dir>
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# Suffixes Spotify appends to alternate releases of the same recording.
VERSION_HINTS = (
    r"remaster(?:ed)?|deluxe|mono|stereo|live|demo|edit|version|mix|single|"
    r"reissue|remix|anniversary|bonus|session"
)


def norm_text(s: str) -> str:
    s = re.sub(r"[^\w\s]", "", s.lower())
    return re.sub(r"\s+", " ", s).strip()


def norm_title(title: str) -> str:
    t = title.lower()
    t = re.sub(rf"\s*[\(\[][^)\]]*(?:{VERSION_HINTS})[^)\]]*[\)\]]", "", t)
    t = re.sub(rf"\s+-\s+[^-]*(?:{VERSION_HINTS}).*$", "", t)
    return norm_text(t)


def load_exports(out_dir: Path) -> list[dict]:
    playlists = []
    for p in sorted(out_dir.glob("*.json")):
        try:
            data = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and "tracks" in data:
            playlists.append(data)
    return playlists


def main() -> None:
    out_dir = Path(sys.argv[1])
    playlists = load_exports(out_dir)

    within = []
    for pl in playlists:
        by_uri = defaultdict(list)
        for t in pl["tracks"]:
            if t["uri"] and not t["is_local"]:
                by_uri[t["uri"]].append(t)
        for uri, ts in sorted(by_uri.items()):
            if len(ts) > 1:
                within.append(
                    {
                        "playlist": pl["name"],
                        "uri": uri,
                        "track": f"{', '.join(ts[0]['artists'])} – {ts[0]['name']}",
                        "keep_pos": ts[0]["pos"],
                        "remove_pos": [t["pos"] for t in ts[1:]],
                    }
                )

    cross = defaultdict(lambda: defaultdict(list))
    meta = {}
    for pl in playlists:
        for t in pl["tracks"]:
            if t["uri"] and not t["is_local"]:
                cross[t["uri"]][pl["name"]].append(t["pos"])
                meta[t["uri"]] = f"{', '.join(t['artists'])} – {t['name']}"
    cross_dups = [
        {"uri": uri, "track": meta[uri], "playlists": {k: v for k, v in where.items()}}
        for uri, where in sorted(cross.items())
        if len(where) > 1
    ]

    fuzzy_groups = defaultdict(list)
    for pl in playlists:
        for t in pl["tracks"]:
            if not t["name"] or not t["artists"]:
                continue
            key = (norm_title(t["name"]), norm_text(t["artists"][0]))
            fuzzy_groups[key].append(
                {
                    "playlist": pl["name"],
                    "pos": t["pos"],
                    "uri": t["uri"],
                    "track": f"{', '.join(t['artists'])} – {t['name']}",
                    "album": t["album"],
                    "year": t["year"],
                }
            )
    fuzzy = [
        {"title_artist": " / ".join(k), "entries": v}
        for k, v in sorted(fuzzy_groups.items())
        if len({e["uri"] for e in v}) > 1
    ]

    report = {"within_playlist": within, "cross_playlist": cross_dups, "fuzzy": fuzzy}
    (out_dir / "dedup.json").write_text(json.dumps(report, ensure_ascii=False, indent=1))

    md = ["# Dedup report", ""]
    md.append(f"## Exact duplicates within a playlist ({len(within)})")
    for d in within:
        md.append(f"- **{d['playlist']}**: {d['track']} — keep pos {d['keep_pos']}, remove pos {d['remove_pos']}")
    md.append("")
    md.append(f"## Same track in multiple playlists ({len(cross_dups)})")
    for d in cross_dups:
        where = "; ".join(f"{k} @ {v}" for k, v in d["playlists"].items())
        md.append(f"- {d['track']} — {where}")
    md.append("")
    md.append(f"## Fuzzy matches, different URIs — likely alternate releases ({len(fuzzy)})")
    for g in fuzzy:
        md.append(f"- {g['title_artist']}:")
        for e in g["entries"]:
            md.append(f"    - {e['playlist']} pos {e['pos']}: {e['track']} [{e['album']}, {e['year']}]")
    (out_dir / "dedup.md").write_text("\n".join(md) + "\n")

    print(f"within-playlist exact dupes: {len(within)}")
    print(f"cross-playlist shared tracks: {len(cross_dups)}")
    print(f"fuzzy alternate-release groups: {len(fuzzy)}")
    print(f"reports: {out_dir / 'dedup.md'}, {out_dir / 'dedup.json'}")


if __name__ == "__main__":
    main()
