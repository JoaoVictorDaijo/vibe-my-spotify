"""Export Spotify playlists (or Liked Songs) to JSON + compact text for
token-cheap analysis.

Runs inside spotify-mcp/ so it reuses the server's spotipy client and token
cache:
    cd spotify-mcp && uv run python ../scripts/export_playlists.py <id>... --out DIR
    cd spotify-mcp && uv run python ../scripts/export_playlists.py --liked --out DIR
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from spotify_mcp import spotify_api


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "playlist"


def _parse_items(items: list, offset: int) -> list[dict]:
    tracks = []
    for i, item in enumerate(items):
        # Feb 2026 /items endpoint nests the track under "item"; older
        # responses used "track".
        t = item.get("item") or item.get("track") or {}
        album = t.get("album") or {}
        tracks.append(
            {
                "pos": offset + i,
                "uri": t.get("uri"),
                "id": t.get("id"),
                "name": t.get("name"),
                "artists": [a.get("name") for a in (t.get("artists") or []) if a.get("name")],
                "artist_ids": [a.get("id") for a in (t.get("artists") or []) if a.get("id")],
                "album": album.get("name"),
                "year": (album.get("release_date") or "")[:4],
                "duration_ms": t.get("duration_ms"),
                "is_local": bool(item.get("is_local") or t.get("is_local")),
            }
        )
    return tracks


def export_playlist(sp, pid: str) -> dict:
    info = sp.playlist(pid)
    tracks = []
    offset = 0
    while True:
        page = sp.playlist_items(pid, limit=100, offset=offset)
        items = page.get("items") or []
        tracks.extend(_parse_items(items, offset))
        offset += len(items)
        if not items or not page.get("next"):
            break
    return {
        "id": info.get("id", pid),
        "name": info.get("name", pid),
        "description": info.get("description") or "",
        "snapshot_id": info.get("snapshot_id"),
        "total": len(tracks),
        "tracks": tracks,
    }


def export_liked(sp) -> dict:
    tracks = []
    offset = 0
    while True:
        page = sp.current_user_saved_tracks(limit=50, offset=offset)
        items = page.get("items") or []
        tracks.extend(_parse_items(items, offset))
        offset += len(items)
        if not items or not page.get("next"):
            break
    return {
        "id": "liked-songs",
        "name": "Liked Songs",
        "description": "",
        "snapshot_id": None,
        "total": len(tracks),
        "tracks": tracks,
    }


def compact_lines(data: dict) -> str:
    lines = [f"# {data['name']} — {data['total']} tracks (playlist {data['id']})"]
    for t in data["tracks"]:
        artists = ", ".join(t["artists"]) or "?"
        year = f" ({t['year']})" if t["year"] else ""
        local = " [local]" if t["is_local"] else ""
        lines.append(f"{t['pos']:>4} | {artists} – {t['name']}{year}{local}")
    return "\n".join(lines) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("playlist_ids", nargs="*")
    ap.add_argument("--liked", action="store_true", help="export Liked Songs")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    sp = spotify_api.Client().sp
    args.out.mkdir(parents=True, exist_ok=True)
    datasets = [export_playlist(sp, pid) for pid in args.playlist_ids]
    if args.liked:
        datasets.append(export_liked(sp))
    for data in datasets:
        slug = slugify(data["name"])
        (args.out / f"{slug}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1))
        (args.out / f"{slug}.txt").write_text(compact_lines(data))
        print(f"{data['name']}: {data['total']} tracks -> {slug}.json/.txt")


if __name__ == "__main__":
    main()
