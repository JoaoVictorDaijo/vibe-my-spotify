"""On-demand Last.fm crowd tags for doubtful placements — a second opinion,
not a bulk source. Run it on the handful of rows where MusicBrainz tags and
ListenBrainz neighbours leave a judge unsure, above all artists with no tags.

Needs a free API key (https://www.last.fm/api/account/create) in .env as
LASTFM_API_KEY; only the key, never the shared secret. Terms: personal
noncommercial use and at most 100 MB of Last.fm data kept. Last.fm sent no
Cache-Control headers when probed (2026-10-26), so entries expire on a fixed
CACHE_DAYS and the run refuses to grow the cache past MAX_CACHE_BYTES.
No Spotify calls.

    python3 scripts/lastfm_tags.py OUT_DIR "Artist" "Artist – Title @ Album" ...
    python3 scripts/lastfm_tags.py OUT_DIR --file doubtful.txt

"Artist" fetches artist tags; "– Title" adds that track's tags and "@ Album"
the album's. Album tags are the useful per-catalogue signal: track tags are
empty for most non-hits, while album tags split a band's eras (Young
Americans: 70s, soul, funk) and often carry the release year as a tag.

Artist, album and track answers are cached separately, so queries sharing an
artist or album cost one request each. OUT_DIR/lastfm.txt is rewritten from
the cache on every run: one line per query, `query => artist [...] | album [...]`.
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import date, timedelta
from pathlib import Path

from enrich_musicbrainz import norm_name
from open_data_client import OpenDataClient, ServiceStopped, service_lock

LASTFM_ROOT = "https://ws.audioscrobbler.com/2.0/"
CACHE_DAYS = 30
MAX_CACHE_BYTES = 50 * 1024 * 1024  # half the terms' 100 MB cap
MIN_TAG_COUNT = 10  # Last.fm scales counts to 0-100 per entity; below this is noise
MAX_TAGS = 8
# Personal-library tags that say nothing about where a song belongs.
JUNK_TAGS = {"seen live", "favorites", "favourite", "favorite", "albums i own", "love",
             "awesome", "beautiful", "favourites", "under 2000 listeners", "my favorite",
             "favourite songs", "1001 albums you must hear before you die", "all"}
LEVELS = (("artist", "artist.getTopTags"), ("album", "album.getTopTags"), ("track", "track.getTopTags"))


def clean_tags(body: dict | None, artist: str) -> list[str] | None:
    """Filtered tag names; [] for a real "not found" or no tags; None for a bad answer."""
    if body is None:
        return None
    if body.get("error") == 6:
        return []
    if not isinstance(body.get("toptags"), dict):
        return None
    out = []
    for t in body["toptags"].get("tag") or []:
        name = (t.get("name") or "").strip().lower()
        if (int(t.get("count") or 0) >= MIN_TAG_COUNT and name
                and name not in JUNK_TAGS and norm_name(name) != norm_name(artist)):
            out.append(name)
        if len(out) >= MAX_TAGS:
            break
    return out


def parse_query(query: str) -> tuple[str, str, str]:
    rest, _, album = query.partition(" @ ")
    artist, _, title = rest.partition(" – ")
    return artist.strip(), title.strip(), album.strip()


def entity_keys(query: str) -> list[tuple[str, str, dict]]:
    """(level, cache key, extra params) for each level the query asks for."""
    artist, title, album = parse_query(query)
    wanted = {"artist": {}, "album": {"album": album} if album else None,
              "track": {"track": title} if title else None}
    keys = []
    for level, _ in LEVELS:
        if wanted[level] is None:
            continue
        ident = "|".join([norm_name(artist)] + [norm_name(v) for v in wanted[level].values()])
        keys.append((level, f"{level}:{ident}", wanted[level]))
    return keys


def fetch_entity(client: OpenDataClient, key: str, artist: str, level: str, extra: dict) -> list[str] | None:
    method = dict(LEVELS)[level]
    params = {"api_key": key, "format": "json", "autocorrect": 1, "artist": artist, "method": method, **extra}
    return clean_tags(client.get_json(LASTFM_ROOT, params), artist)


def line(query: str, cache: dict) -> str | None:
    parts = []
    for level, ckey, _ in entity_keys(query):
        if ckey not in cache:
            return None
        parts.append(f"{level} [{'; '.join(cache[ckey]['tags']) or '-'}]")
    return f"{query} => " + " | ".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out_dir", type=Path)
    ap.add_argument("queries", nargs="*")
    ap.add_argument("--file", type=Path, help="one query per line")
    args = ap.parse_args()
    key = os.environ.get("LASTFM_API_KEY")
    if not key:
        raise SystemExit("LASTFM_API_KEY is not set: register at https://www.last.fm/api/account/create")
    queries = list(args.queries)
    if args.file:
        queries += [q.strip() for q in args.file.read_text().splitlines() if q.strip()]
    with service_lock(args.out_dir, "lastfm"):
        fetch_and_write(args.out_dir, key, queries)


def fetch_and_write(out_dir: Path, key: str, queries: list[str]) -> None:
    cache_path = out_dir / "lastfm_cache.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    cutoff = (date.today() - timedelta(days=CACHE_DAYS)).isoformat()
    cache = {k: e for k, e in cache.items() if isinstance(e, dict) and "tags" in e and e["fetched"] > cutoff}
    known_queries = [q for q in cache.pop("_queries", {}).get("list", []) if q not in queries]

    client = OpenDataClient("lastfm")
    try:
        for q in queries:
            artist = parse_query(q)[0]
            for level, ckey, extra in entity_keys(q):
                if ckey in cache:
                    continue
                tags = fetch_entity(client, key, artist, level, extra)
                if tags is None:
                    print(f"{q}: {level} failed, not cached")
                    continue
                cache[ckey] = {"tags": tags, "fetched": date.today().isoformat()}
    except ServiceStopped as e:
        print(f"stopped: {e}")

    # Queries whose entities expired drop out of the file and the query list.
    answered_queries = [q for q in dict.fromkeys(known_queries + queries) if line(q, cache)]
    lines = [line(q, cache) for q in answered_queries]
    cache["_queries"] = {"list": answered_queries, "tags": [], "fetched": date.today().isoformat()}
    payload = json.dumps(cache, ensure_ascii=False, indent=1)
    if len(payload.encode()) > MAX_CACHE_BYTES:
        raise SystemExit(f"cache would exceed {MAX_CACHE_BYTES} bytes — prune it before fetching more")
    cache_path.write_text(payload)
    (out_dir / "lastfm.txt").write_text("\n".join(lines) + ("\n" if lines else ""))
    answered = sum(1 for q in queries if line(q, cache))
    print(f"{answered}/{len(queries)} answered, {client.requests} requests; lastfm.txt has {len(lines)} lines")


if __name__ == "__main__":
    main()
