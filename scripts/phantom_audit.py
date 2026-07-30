"""Audit playlist exports for phantom track URIs — copies pointing at
re-delivered album versions Spotify has hidden from search and the artist
profile. Read-only: produces a report, changes nothing.

    cd spotify-mcp && uv run python ../scripts/phantom_audit.py <export-dir> [--no-search]

Market-aware exports carry everything the relink signal needs (probe-verified
2026-07-29: playlist pages include external_ids.isrc, and relinked entries
expose linked_from → export_playlists.py stores it as canonical_id). So the
relink tier costs zero API calls — it reads exports. The only API tier left
is the targeted `isrc:` search (catalog-visibility check) for tracks
implicated by dedup fuzzy groups; --no-search skips it entirely.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import requests
from spotipy.exceptions import SpotifyException

from spotify_mcp import spotify_api

# Post-Feb-2026 dev-mode apps 429 at sustained rates near 1 req/s
# (docs/spotify-rate-limits.md) — stay under it.
PACE_SECONDS = 1.0
QUOTA_STOP_SECONDS = 300


class QuotaExhausted(RuntimeError):
    pass


def call(fn, *args, **kwargs):
    for _ in range(5):
        try:
            return fn(*args, **kwargs)
        except requests.exceptions.RetryError as e:
            # Surfaces only if spotipy's internal retry layer was left active
            # (session not rebuilt) — the 429 response is gone at this point.
            raise QuotaExhausted("internal retries exhausted on 429") from e
        except SpotifyException as e:
            if e.http_status == 429:
                headers = getattr(e, "headers", None) or {}
                # HTTP/2 lowercases header names; don't miss it on case.
                header = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
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
    do_search = "--no-search" not in sys.argv[2:]
    playlists = load_exports(out_dir)

    cache_path = out_dir / "phantom_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}

    tracks_by_id = {}
    for pl in playlists:
        for t in pl["tracks"]:
            if t.get("id") and not t.get("is_local"):
                tracks_by_id.setdefault(t["id"], t)

    searched = 0
    if do_search:
        sp = spotify_api.Client().sp
        # spotipy wires 429 into urllib3's Retry at __init__ and urllib3
        # honors Retry-After by sleeping INSIDE the request; rebuild the
        # session with retries zeroed so 429s surface to call() immediately.
        sp.retries = 0
        sp.status_retries = 0
        sp.status_forcelist = []
        if hasattr(sp, "_build_session"):
            sp._build_session()

        suspects = [tid for tid in sorted(fuzzy_suspects(out_dir, playlists))
                    if (tracks_by_id.get(tid) or {}).get("isrc")
                    and (cache.get(tid) or {}).get("isrc_hits") is None]
        try:
            for n, tid in enumerate(suspects):
                entry = cache.setdefault(tid, {})
                entry["isrc"] = tracks_by_id[tid]["isrc"]
                entry["isrc_hits"] = isrc_search(sp, entry["isrc"])
                searched += 1
                if (n + 1) % 25 == 0:
                    print(f"search tier {n + 1}/{len(suspects)}", flush=True)
                    cache_path.write_text(json.dumps(cache, ensure_ascii=False))
                time.sleep(PACE_SECONDS)
        except QuotaExhausted as e:
            print(f"quota exhausted ({e}) — checkpointed after {searched} searches; rerun to resume")
        cache_path.write_text(json.dumps(cache, ensure_ascii=False))

    findings = []
    for pl in playlists:
        for t in pl["tracks"]:
            if not t.get("id") or t.get("is_local"):
                continue
            verdict = canonical = None
            if t.get("canonical_id"):
                verdict = "relinked"
                canonical = {"id": t["canonical_id"], "album": None, "year": ""}
            else:
                hits = (cache.get(t["id"]) or {}).get("isrc_hits")
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
            target = f["canonical_album"] or f["canonical_id"]
            md.append(f"- pos {f['pos']}: {f['track']} — [{f['current_album']}] -> "
                      f"[{target}{', ' + f['canonical_year'] if f['canonical_year'] else ''}] ({f['verdict']})")
        md.append("")
    (out_dir / "phantom_report.md").write_text("\n".join(md))

    unplayable = sum(1 for pl in playlists for t in pl["tracks"] if t.get("is_playable") is False)
    print(f"tracks scanned: {len(tracks_by_id)}  phantoms: {len(findings)} "
          f"(relinked {sum(1 for f in findings if f['verdict'] == 'relinked')}, "
          f"isrc-invisible {sum(1 for f in findings if f['verdict'] == 'isrc-invisible')})  "
          f"searches spent: {searched}  unplayable tracks: {unplayable}")
    print(f"report: {out_dir / 'phantom_report.md'}")


if __name__ == "__main__":
    main()
