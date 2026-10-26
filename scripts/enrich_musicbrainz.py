"""Enrich playlist exports with artist genres/tags from MusicBrainz.

Spotify's dev-mode API returns empty artist genres; MusicBrainz serves them
free with no auth at ~1 req/s (their published etiquette limit). Never
touches Spotify — safe to run during a Spotify penalty window.

    python3 scripts/enrich_musicbrainz.py <export-dir> [--out DIR]

Writes mb_artists.json (cache) and artists.txt (the `Artist: genres`
appendix the judgment agents read) into --out (default: the export dir).
Tagged artists stay cached; empty or unmatched ones are retried once
EMPTY_TTL_DAYS have passed, since small artists gain tags over time.
A transient failure is never cached, so the next run retries it.

Identity comes from a recording ISRC when the exports have one: MusicBrainz
maps it to the exact credited artist. Name search is the fallback, and a hit
counts only when its name or an alias equals the export's artist name after
normalization — a high search score alone once mapped "Beck" to Rufus Beck
and "Whitney" to Whitney Houston. Rejected hits are kept as `candidate`.
"""

from __future__ import annotations

import argparse
import json
import re
import unicodedata
from datetime import date, timedelta
from pathlib import Path

from open_data_client import OpenDataClient, ServiceStopped, service_lock

MB_ROOT = "https://musicbrainz.org/ws/2"
MIN_MATCH_SCORE = 90
EMPTY_TTL_DAYS = 30


def norm_name(name: str) -> str:
    """Identity key for artist names: accent-, case- and punctuation-blind, any script."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"^the\s+", "", text.replace("&", " and "))
    return re.sub(r"[\W_]+", "", text)


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


def needs_fetch(entry: dict | None, isrc: str | None, today: date) -> bool:
    if not entry:
        return True
    stale = (not entry.get("fetched")
             or date.fromisoformat(entry["fetched"]) <= today - timedelta(days=EMPTY_TTL_DAYS))
    # Identity first: a name-search match is re-proved through a recording ISRC
    # whenever one is available, at most once per TTL.
    if isrc and entry.get("identity") != "isrc" and stale:
        return True
    if entry.get("matched") and entry.get("identity") is None:
        # Matched before identities were checked at all.
        return True
    if entry.get("matched") and entry.get("genres"):
        return False
    return stale


def pick_match(name: str, candidates: list[dict]) -> dict | None:
    key = norm_name(name)
    for c in candidates:
        if int(c.get("score") or 0) < MIN_MATCH_SCORE:
            continue
        names = [c.get("name") or ""] + [a.get("name") or "" for a in c.get("aliases") or []]
        if key and any(norm_name(n) == key for n in names):
            return c
    return None


def mbid_from_isrc(client: OpenDataClient, name: str, isrc: str, position: int):
    """The credited artist's MBID on a recording with this ISRC.

    Returns (mbid, mb_name), (None, None) when MusicBrainz has no usable answer,
    or None on a transient failure."""
    body = client.get_json(f"{MB_ROOT}/isrc/{isrc}", {"inc": "artist-credits", "fmt": "json"}, not_found={})
    if body is None:
        return None
    key = norm_name(name)
    by_position = None
    for rec in body.get("recordings") or []:
        credits = [c for c in rec.get("artist-credit") or [] if isinstance(c, dict) and c.get("artist")]
        for c in credits:
            if key and key in (norm_name(c.get("name") or ""), norm_name(c["artist"].get("name") or "")):
                return c["artist"]["id"], c["artist"].get("name")
        if by_position is None and position < len(credits):
            by_position = credits[position]["artist"]
    # A credit spelled differently (e.g. "Yusuf / Cat Stevens") still sits at
    # the same position as in Spotify's artist list.
    if by_position:
        return by_position["id"], by_position.get("name")
    return None, None


def resolve_artist(client: OpenDataClient, name: str, isrc_ref: tuple[str, int] | None,
                   today: date) -> dict | None:
    mbid = mb_name = None
    identity = None
    if isrc_ref:
        found = mbid_from_isrc(client, name, *isrc_ref)
        if found is None:
            return None
        mbid, mb_name = found
        identity = "isrc" if mbid else None
    if not mbid:
        search = client.get_json(f"{MB_ROOT}/artist",
                                 {"query": f'artist:"{name}"', "limit": 5, "fmt": "json"})
        if not isinstance(search, dict) or not isinstance(search.get("artists"), list):
            return None
        best = pick_match(name, search["artists"])
        if not best:
            top = next(iter(search["artists"]), None)
            entry = {"name": name, "genres": [], "matched": False, "identity": None,
                     "fetched": today.isoformat()}
            if top:
                entry["candidate"] = {"mbid": top.get("id"), "name": top.get("name"), "score": top.get("score")}
            return entry
        mbid, mb_name, identity = best["id"], best.get("name"), "name"
    full = client.get_json(f"{MB_ROOT}/artist/{mbid}", {"inc": "genres tags", "fmt": "json"})
    if not isinstance(full, dict) or full.get("id") != mbid:
        return None
    return {
        "name": name,
        "mbid": mbid,
        "mb_name": mb_name,
        "genres": top_genres(full),
        "matched": True,
        "identity": identity,
        "fetched": today.isoformat(),
    }


ISRC_SHAPE = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}")


def isrc_refs(playlists: list[dict]) -> dict[str, tuple[str, int]]:
    """Artist name -> (an ISRC of one of their tracks, their position in its credits).

    Spotify carries some malformed ISRCs that MusicBrainz rejects with a 400;
    those are skipped, and the artist falls back to another track or to name search."""
    refs = {}
    for pl in playlists:
        for t in pl["tracks"]:
            isrc = (t.get("isrc") or "").replace("-", "").upper()
            if not ISRC_SHAPE.fullmatch(isrc):
                continue
            for i, name in enumerate(t["artists"]):
                refs.setdefault(name, (isrc, i))
    return refs


def run(playlists: list[dict], out_dir: Path) -> None:
    with service_lock(out_dir, "musicbrainz"):
        _run(playlists, out_dir)


def _run(playlists: list[dict], out_dir: Path) -> None:
    cache_path = out_dir / "mb_artists.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    today = date.today()

    names = sorted({name for pl in playlists for t in pl["tracks"] for name in t["artists"]})
    refs = isrc_refs(playlists)
    todo = [n for n in names if needs_fetch(cache.get(n), (refs.get(n) or (None,))[0], today)]
    print(f"{len(names)} unique artists, {len(todo)} to resolve")
    client = OpenDataClient("musicbrainz")
    errors = 0
    try:
        for i, name in enumerate(todo):
            entry = resolve_artist(client, name, refs.get(name), today)
            if entry is None:
                errors += 1
            else:
                cache[name] = entry
            if (i + 1) % 25 == 0:
                print(f"musicbrainz {i + 1}/{len(todo)}", flush=True)
                cache_path.write_text(json.dumps(cache, ensure_ascii=False))
    except ServiceStopped as e:
        print(f"stopped: {e}")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))

    lines = []
    for name in names:
        info = cache.get(name) or {}
        genres = "; ".join(info.get("genres") or []) or "-"
        lines.append(f"{name}: {genres}")
    (out_dir / "artists.txt").write_text("\n".join(lines) + "\n")
    entries = [cache.get(n) or {} for n in names]
    matched = sum(1 for e in entries if e.get("matched"))
    by_isrc = sum(1 for e in entries if e.get("identity") == "isrc")
    tagged = sum(1 for e in entries if e.get("genres"))
    print(f"artists.txt written: {tagged}/{len(names)} with tags, {matched} matched "
          f"({by_isrc} proved by ISRC), "
          f"{errors} transient errors (retried next run), {client.requests} requests")



def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("export_dir", type=Path)
    ap.add_argument("--out", type=Path, help="cache + artists.txt dir (default: export_dir)")
    args = ap.parse_args()
    run(load_exports(args.export_dir), args.out or args.export_dir)


if __name__ == "__main__":
    main()
