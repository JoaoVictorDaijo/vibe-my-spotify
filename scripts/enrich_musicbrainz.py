"""Enrich playlist exports with artist genres/tags from MusicBrainz.

Spotify's dev-mode API returns empty artist genres; MusicBrainz serves them
free with no auth at ~1 req/s (their published etiquette limit). Never
touches Spotify — safe to run during a Spotify penalty window.

    python3 scripts/enrich_musicbrainz.py <export-dir>

Writes mb_artists.json (cache) and artists.txt (the `Artist: genres`
appendix the judgment agents read).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests

MB_ROOT = "https://musicbrainz.org/ws/2"
# MusicBrainz requires a meaningful User-Agent and asks for <=1 req/s.
HEADERS = {"User-Agent": "VibeMySpotify/0.1 (https://github.com/JoaoVictorDaijo/vibe-my-spotify)"}
PACE_SECONDS = 1.1
MIN_MATCH_SCORE = 90


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


def mb_get(path: str, params: dict) -> dict:
    for attempt in range(4):
        try:
            r = requests.get(f"{MB_ROOT}/{path}", params={**params, "fmt": "json"},
                             headers=HEADERS, timeout=30)
            if r.status_code == 503:
                time.sleep(3 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            time.sleep(2 * (attempt + 1))
    return {}


def top_genres(entity: dict, limit: int = 5) -> list[str]:
    pools = (entity.get("genres") or []) + (entity.get("tags") or [])
    ranked = sorted(pools, key=lambda g: -(g.get("count") or 0))
    seen, out = set(), []
    for g in ranked:
        name = (g.get("name") or "").lower()
        if name and name not in seen:
            seen.add(name)
            out.append(name)
        if len(out) >= limit:
            break
    return out


def resolve_artist(name: str) -> dict:
    search = mb_get("artist", {"query": f'artist:"{name}"', "limit": 3})
    best = next(iter(search.get("artists") or []), None)
    if not best or int(best.get("score") or 0) < MIN_MATCH_SCORE:
        return {"name": name, "genres": [], "matched": False}
    time.sleep(PACE_SECONDS)
    full = mb_get(f"artist/{best['id']}", {"inc": "genres tags"})
    return {
        "name": name,
        "mbid": best["id"],
        "mb_name": best.get("name"),
        "genres": top_genres(full),
        "matched": True,
    }


def main() -> None:
    out_dir = Path(sys.argv[1])
    playlists = load_exports(out_dir)
    cache_path = out_dir / "mb_artists.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    names = sorted({name for pl in playlists for t in pl["tracks"] for name in t["artists"]})
    todo = [n for n in names if n not in cache]
    print(f"{len(names)} unique artists, {len(todo)} to resolve")
    for i, name in enumerate(todo):
        cache[name] = resolve_artist(name)
        if (i + 1) % 25 == 0:
            print(f"musicbrainz {i + 1}/{len(todo)}", flush=True)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False))
        time.sleep(PACE_SECONDS)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))

    lines = []
    for name in names:
        info = cache.get(name) or {}
        genres = "; ".join(info.get("genres") or []) or "-"
        lines.append(f"{name}: {genres}")
    (out_dir / "artists.txt").write_text("\n".join(lines) + "\n")
    matched = sum(1 for n in names if (cache.get(n) or {}).get("matched"))
    print(f"artists.txt written: {matched}/{len(names)} matched with genres")


if __name__ == "__main__":
    main()
