"""Enrich playlist exports with Spotify artist genres and ReccoBeats audio
features (energy/valence/tempo — Spotify killed its own audio-features API).

Runs inside spotify-mcp/ like export_playlists.py:
    cd spotify-mcp && uv run python ../scripts/enrich_exports.py <export-dir>

Writes artists.txt (artist -> genres appendix), audio_features.json, and a
*_enriched.txt compact file per playlist with per-track energy/valence/tempo.
"""

from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path

import requests

from spotify_mcp import spotify_api


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "playlist"


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


def fetch_genres(sp, playlists: list[dict], out_dir: Path) -> dict:
    cache_path = out_dir / "artists_genres.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    names = {}
    for pl in playlists:
        for t in pl["tracks"]:
            for aid, name in zip(t["artist_ids"], t["artists"]):
                names.setdefault(aid, name)
    todo = [a for a in names if a not in cache]
    for i, aid in enumerate(todo):
        try:
            art = sp.artist(aid)
            cache[aid] = {"name": art.get("name") or names[aid], "genres": art.get("genres") or []}
        except Exception as e:  # noqa: BLE001 — enrichment is best-effort
            cache[aid] = {"name": names[aid], "genres": [], "error": str(e)[:80]}
        if (i + 1) % 25 == 0:
            print(f"genres {i + 1}/{len(todo)}")
            cache_path.write_text(json.dumps(cache, ensure_ascii=False))
        time.sleep(0.05)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))

    lines = []
    for info in sorted(cache.values(), key=lambda v: v["name"].lower()):
        genres = "; ".join(info["genres"]) if info["genres"] else "-"
        lines.append(f"{info['name']}: {genres}")
    (out_dir / "artists.txt").write_text("\n".join(lines) + "\n")
    return cache


FEATURE_KEYS = ("energy", "valence", "tempo", "acousticness", "danceability", "instrumentalness")


def fetch_audio_features(playlists: list[dict], out_dir: Path) -> dict:
    track_ids = list(dict.fromkeys(t["id"] for pl in playlists for t in pl["tracks"] if t["id"]))
    feats = {}
    for i in range(0, len(track_ids), 40):
        chunk = track_ids[i : i + 40]
        try:
            r = requests.get(
                "https://api.reccobeats.com/v1/audio-features",
                params={"ids": ",".join(chunk)},
                timeout=30,
            )
            r.raise_for_status()
            body = r.json()
            items = body.get("content") if isinstance(body, dict) else body
            for item in items or []:
                m = re.search(r"track/([A-Za-z0-9]+)", item.get("href") or "")
                if m:
                    feats[m.group(1)] = {k: item.get(k) for k in FEATURE_KEYS}
        except Exception as e:  # noqa: BLE001 — enrichment is best-effort
            print(f"reccobeats chunk {i // 40}: {e}")
        time.sleep(0.2)
    (out_dir / "audio_features.json").write_text(json.dumps(feats))
    print(f"audio features: {len(feats)}/{len(track_ids)} tracks matched")
    return feats


def write_enriched(playlists: list[dict], feats: dict, out_dir: Path) -> None:
    for pl in playlists:
        lines = [f"# {pl['name']} — {pl['total']} tracks"]
        for t in pl["tracks"]:
            artists = ", ".join(t["artists"]) or "?"
            year = f" ({t['year']})" if t["year"] else ""
            f = feats.get(t["id"] or "")
            suffix = ""
            if f and f.get("energy") is not None:
                suffix = f" | e{f['energy']:.2f}"
                if f.get("valence") is not None:
                    suffix += f" v{f['valence']:.2f}"
                if f.get("tempo"):
                    suffix += f" {round(f['tempo'])}bpm"
            lines.append(f"{t['pos']:>4} | {artists} – {t['name']}{year}{suffix}")
        (out_dir / f"{slugify(pl['name'])}_enriched.txt").write_text("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("--skip-genres", action="store_true",
                    help="skip Spotify artist-genre fetch (dev-mode apps get empty genres)")
    args = ap.parse_args()
    playlists = load_exports(args.out_dir)
    if not args.skip_genres:
        sp = spotify_api.Client().sp
        fetch_genres(sp, playlists, args.out_dir)
    feats = fetch_audio_features(playlists, args.out_dir)
    write_enriched(playlists, feats, args.out_dir)


if __name__ == "__main__":
    main()
