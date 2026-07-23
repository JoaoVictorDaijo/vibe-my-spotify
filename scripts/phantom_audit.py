"""Audit playlist exports for phantom track URIs — copies pointing at
re-delivered album versions Spotify has hidden from search and the artist
profile. Read-only: produces a report, changes nothing.

    cd spotify-mcp && uv run python ../scripts/phantom_audit.py <export-dir>

Two probe tiers, sized for the tiny dev-mode daily quota
(docs/spotify-rate-limits.md):
- Batch relink tier: /tracks?ids= (50 per request) with a market context —
  Spotify relinks stale URIs and confesses via linked_from (definite signal).
  Falls back to single-track fetches if batching is unavailable to this app.
- Targeted ISRC search tier: search is the scarcest resource, so only tracks
  implicated by dedup fuzzy groups get an isrc: search (invisibility signal).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from spotipy.exceptions import SpotifyException

from spotify_mcp import spotify_api

# Post-Feb-2026 dev-mode apps 429 at sustained rates near 1 req/s
# (docs/spotify-rate-limits.md) — stay under it.
PACE_SECONDS = 1.0
QUOTA_STOP_SECONDS = 300
BATCH_SIZE = 50


class QuotaExhausted(RuntimeError):
    pass


def call(fn, *args, **kwargs):
    for _ in range(5):
        try:
            return fn(*args, **kwargs)
        except SpotifyException as e:
            if e.http_status == 429:
                header = (getattr(e, "headers", None) or {}).get("Retry-After")
                if header is None:
                    # Community consensus: a 429 without Retry-After means the
                    # daily quota is gone; probing further extends penalties.
                    raise QuotaExhausted("429 with no Retry-After header")
                retry_after = int(header)
                if retry_after > QUOTA_STOP_SECONDS:
                    raise QuotaExhausted(f"Retry-After {retry_after}s — daily quota exhausted")
                time.sleep(retry_after)
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


def parse_full_track(full: dict) -> dict:
    return {
        "fetched_id": full.get("id"),
        "linked_from": (full.get("linked_from") or {}).get("id"),
        "isrc": (full.get("external_ids") or {}).get("isrc"),
        "playable": full.get("is_playable"),
        "album": (full.get("album") or {}).get("name"),
    }


def isrc_search(sp, isrc: str) -> list[dict]:
    res = call(sp.search, f"isrc:{isrc}", type="track", limit=10, market="from_token")
    items = ((res or {}).get("tracks") or {}).get("items") or []
    return [
        {
            "id": i.get("id"),
            "album": (i.get("album") or {}).get("name"),
            "year": ((i.get("album") or {}).get("release_date") or "")[:4],
            "popularity": i.get("popularity"),
        }
        for i in items
    ]


def fuzzy_suspects(out_dir: Path, playlists: list[dict]) -> set[str]:
    dedup_path = out_dir / "dedup.json"
    if not dedup_path.exists():
        return set()
    uri_to_id = {t["uri"]: t["id"] for pl in playlists for t in pl["tracks"]
                 if t.get("uri") and t.get("id")}
    dedup = json.loads(dedup_path.read_text())
    ids = set()
    for g in dedup.get("fuzzy") or []:
        for e in g["entries"]:
            tid = uri_to_id.get(e.get("uri"))
            if tid:
                ids.add(tid)
    return ids


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

    def checkpoint() -> None:
        cache_path.write_text(json.dumps(cache, ensure_ascii=False))

    todo = [(pl, t) for pl in playlists for t in pl["tracks"] if t["id"] and not t["is_local"]]
    todo_ids = list(dict.fromkeys(t["id"] for _pl, t in todo if t["id"] not in cache))

    try:
        batch_ok = True
        for i in range(0, len(todo_ids), BATCH_SIZE):
            chunk = todo_ids[i : i + BATCH_SIZE]
            if batch_ok:
                try:
                    res = call(sp.tracks, chunk, market="from_token")
                    for req_id, full in zip(chunk, (res or {}).get("tracks") or []):
                        cache[req_id] = parse_full_track(full or {})
                except QuotaExhausted:
                    raise
                except SpotifyException as e:
                    print(f"batch fetch unavailable ({e.http_status}) — falling back to single fetches")
                    batch_ok = False
            if not batch_ok:
                for tid in chunk:
                    if tid in cache:
                        continue
                    try:
                        cache[tid] = parse_full_track(call(sp.track, tid, market="from_token") or {})
                    except QuotaExhausted:
                        raise
                    except Exception as err:  # noqa: BLE001 — rerun retries uncached tracks
                        print(f"probe failed {tid}: {str(err)[:80]}")
                    time.sleep(PACE_SECONDS)
            if i % 500 < BATCH_SIZE:
                print(f"relink tier {min(i + BATCH_SIZE, len(todo_ids))}/{len(todo_ids)}", flush=True)
                checkpoint()
            time.sleep(PACE_SECONDS)

        suspects = [tid for tid in sorted(fuzzy_suspects(out_dir, playlists))
                    if (cache.get(tid) or {}).get("isrc") and cache[tid].get("isrc_hits") is None]
        for n, tid in enumerate(suspects):
            cache[tid]["isrc_hits"] = isrc_search(sp, cache[tid]["isrc"])
            if (n + 1) % 25 == 0:
                print(f"search tier {n + 1}/{len(suspects)}", flush=True)
                checkpoint()
            time.sleep(PACE_SECONDS)
    except QuotaExhausted as e:
        print(f"quota exhausted ({e}) — cache checkpointed, rerun after the window resets to resume")
    checkpoint()

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
                             {"id": e["fetched_id"], "album": e.get("album"), "year": ""})
        elif e and e.get("isrc") is None:
            no_isrc += 1
        else:
            hits = e.get("isrc_hits")
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

    probed = sum(1 for _pl, t in todo if t["id"] in cache)
    print(f"tracks probed: {probed}/{len(todo)}  phantoms: {len(findings)} "
          f"(relinked {sum(1 for f in findings if f['verdict'] == 'relinked')}, "
          f"isrc-invisible {sum(1 for f in findings if f['verdict'] == 'isrc-invisible')})  "
          f"no-isrc: {no_isrc}  cached-errors: {errors}")
    print(f"report: {out_dir / 'phantom_report.md'}")


if __name__ == "__main__":
    main()
