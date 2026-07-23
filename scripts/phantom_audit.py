"""Audit playlist exports for phantom track URIs — copies pointing at
re-delivered album versions Spotify has hidden from search and the artist
profile. Read-only: produces a report, changes nothing.

    cd spotify-mcp && uv run python ../scripts/phantom_audit.py <export-dir>

Signals, strongest first:
- relinked: a market-aware track fetch returns linked_from -> Spotify itself
  maps our URI to a canonical replacement (definite).
- isrc-invisible: searching the track's ISRC (the industry ID of the
  recording) returns the recording but not our URI -> our copy is hidden from
  discovery (probable). Remasters/live versions carry different ISRCs, so
  legitimate variants never trigger this.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from spotipy.exceptions import SpotifyException

from spotify_mcp import spotify_api

# Spotify's search endpoint hands out 429s with huge Retry-After values;
# letting spotipy's internal retry sleep on them stalls the audit for hours.
# We disable internal retries, pace requests ourselves, and cap 429 waits.
PACE_SECONDS = 0.35
MAX_429_WAIT = 60


def call(fn, *args, **kwargs):
    for _ in range(5):
        try:
            return fn(*args, **kwargs)
        except SpotifyException as e:
            if e.http_status == 429:
                retry_after = int((getattr(e, "headers", None) or {}).get("Retry-After", 5))
                time.sleep(min(retry_after, MAX_429_WAIT))
            elif e.http_status in (500, 502, 503):
                time.sleep(2)
            else:
                raise
    raise RuntimeError("rate-limited beyond patience")


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


def probe_track(sp, tid: str) -> dict:
    entry: dict = {}
    full = call(sp.track, tid, market="from_token")
    entry["fetched_id"] = full.get("id")
    entry["linked_from"] = (full.get("linked_from") or {}).get("id")
    entry["isrc"] = (full.get("external_ids") or {}).get("isrc")
    entry["playable"] = full.get("is_playable")
    entry["album"] = (full.get("album") or {}).get("name")
    if entry["isrc"]:
        time.sleep(PACE_SECONDS)
        res = call(sp.search, f"isrc:{entry['isrc']}", type="track", limit=10, market="from_token")
        items = ((res or {}).get("tracks") or {}).get("items") or []
        entry["isrc_hits"] = [
            {
                "id": i.get("id"),
                "album": (i.get("album") or {}).get("name"),
                "year": ((i.get("album") or {}).get("release_date") or "")[:4],
                "popularity": i.get("popularity"),
            }
            for i in items
        ]
    return entry


def main() -> None:
    out_dir = Path(sys.argv[1])
    playlists = load_exports(out_dir)
    sp = spotify_api.Client().sp
    # Internal retries off — the call() wrapper owns 429 handling.
    for attr in ("retries", "status_retries"):
        if hasattr(sp, attr):
            setattr(sp, attr, 0)

    cache_path = out_dir / "phantom_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    todo = [(pl, t) for pl in playlists for t in pl["tracks"] if t["id"] and not t["is_local"]]
    probe_errors = 0
    for n, (_pl, t) in enumerate(todo):
        tid = t["id"]
        if tid in cache:
            continue
        try:
            cache[tid] = probe_track(sp, tid)
        except Exception as e:  # noqa: BLE001 — leave uncached so a rerun retries it
            probe_errors += 1
            print(f"probe failed {tid}: {str(e)[:80]}")
        if (n + 1) % 25 == 0:
            print(f"audit {n + 1}/{len(todo)}", flush=True)
            cache_path.write_text(json.dumps(cache))
        time.sleep(PACE_SECONDS)
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    if probe_errors:
        print(f"probe errors this run (uncached, rerun to retry): {probe_errors}")

    findings, no_isrc, errors = [], 0, 0
    for pl, t in todo:
        e = cache.get(t["id"]) or {}
        if e.get("error"):
            errors += 1
            continue
        verdict = canonical = None
        # Relinked responses return the canonical track and put OUR id in linked_from.
        if e.get("linked_from") == t["id"] and e.get("fetched_id") not in (None, t["id"]):
            verdict = "relinked"
            hits = e.get("isrc_hits") or []
            canonical = next((h for h in hits if h["id"] == e["fetched_id"]),
                             {"id": e["fetched_id"], "album": e.get("album"), "year": "", "popularity": None})
        elif e.get("isrc") is None:
            no_isrc += 1
        else:
            hits = e.get("isrc_hits") or []
            if hits and t["id"] not in [h["id"] for h in hits]:
                verdict = "isrc-invisible"
                canonical = max(hits, key=lambda h: h.get("popularity") or 0)
        if verdict:
            findings.append(
                {
                    "playlist": pl["name"],
                    "pos": t["pos"],
                    "track": f"{', '.join(t['artists'])} – {t['name']}",
                    "current_album": t["album"],
                    "current_uri": t["uri"],
                    "verdict": verdict,
                    "canonical_id": canonical["id"],
                    "canonical_album": canonical.get("album"),
                    "canonical_year": canonical.get("year"),
                }
            )

    (out_dir / "phantom_report.json").write_text(json.dumps(findings, ensure_ascii=False, indent=1))
    md = ["# Phantom URI report", "",
          "Tracks whose URI points at a hidden/re-delivered release. Proposed fix:",
          "in-place swap to the canonical URI at the same playlist position.", ""]
    for pl in playlists:
        rows = [f for f in findings if f["playlist"] == pl["name"]]
        md.append(f"## {pl['name']} — {len(rows)} phantom(s)")
        for f in rows:
            md.append(
                f"- pos {f['pos']}: {f['track']} — [{f['current_album']}] -> "
                f"[{f['canonical_album']}, {f['canonical_year']}] ({f['verdict']})"
            )
        md.append("")
    (out_dir / "phantom_report.md").write_text("\n".join(md))

    total = len(todo)
    print(f"tracks audited: {total}  phantoms: {len(findings)} "
          f"(relinked {sum(1 for f in findings if f['verdict'] == 'relinked')}, "
          f"isrc-invisible {sum(1 for f in findings if f['verdict'] == 'isrc-invisible')})  "
          f"no-isrc: {no_isrc}  errors: {errors}")
    print(f"report: {out_dir / 'phantom_report.md'}")


if __name__ == "__main__":
    main()
