"""Scene evidence for judges: each artist's ListenBrainz similar artists,
annotated with the roster playlists those neighbours already live in.

Similar artists come from listening sessions (who gets played next to whom),
so they answer the band-graph question tags can't: which scene a band lives
in. No auth, no Spotify calls. Needs mb_artists.json from
enrich_musicbrainz.py first — artists are looked up by MusicBrainz id.

    python3 scripts/enrich_listenbrainz.py <export-dir> [--out DIR]

Writes lb_similar.json (cache) and similar.txt into --out (default: the
export dir), one line per artist:

    Tamaryn: Slowdive 186 [Dreamy, Ether]; Beach House 156 [Dreamy]; ... || roster pull: Ether 4, Dreamy 3

Neighbour scores are co-listening counts, not probabilities; compare them only
within one line. Liked Songs is never an anchor — it is not a home.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from enrich_musicbrainz import load_exports, norm_name
from open_data_client import OpenDataClient, ServiceStopped, service_lock

LB_SIMILAR = "https://labs.api.listenbrainz.org/similar-artists/json"
# A dataset name, not a versioned API: Labs can retire it. Probed 2026-10-26
# against the 7500-day variant; the 5-year window favours current scenes.
ALGORITHM = "session_based_days_1825_session_300_contribution_3_threshold_10_limit_100_filter_True_skip_30"
KEEP_NEIGHBOURS = 15
SHOW_NEIGHBOURS = 8
REFRESH_DAYS = 90
EMPTY_TTL_DAYS = 30


def needs_fetch(entry: dict | None, mbid: str, today: date) -> bool:
    if not entry or entry.get("algorithm") != ALGORITHM or entry.get("mbid") != mbid:
        return True
    ttl = REFRESH_DAYS if entry.get("neighbours") else EMPTY_TTL_DAYS
    return date.fromisoformat(entry["fetched"]) <= today - timedelta(days=ttl)


def fetch_similar(client: OpenDataClient, mbid: str, today: date) -> dict | None:
    body = client.get_json(LB_SIMILAR, {"artist_mbids": mbid, "algorithm": ALGORITHM})
    if not isinstance(body, list):
        return None
    rows = [r for r in body if isinstance(r, dict) and r.get("artist_mbid") != mbid]
    rows = sorted(rows, key=lambda r: -(r.get("score") or 0))[:KEEP_NEIGHBOURS]
    return {
        "mbid": mbid,
        "algorithm": ALGORITHM,
        "fetched": today.isoformat(),
        "neighbours": [{"name": r.get("name"), "mbid": r.get("artist_mbid"), "score": r.get("score")}
                       for r in rows],
    }


def roster_index(playlists: list[dict], mb_cache: dict) -> tuple[dict, dict]:
    """Roster artist -> playlists it appears in.

    by_mbid holds every artist MusicBrainz identified; by_name only those it
    did not, so a homonym with a different MBID can never borrow a home by
    spelling alone."""
    by_mbid, by_name = {}, {}
    for pl in playlists:
        if pl.get("name") == "Liked Songs":
            continue
        for t in pl["tracks"]:
            for artist in t["artists"]:
                mbid = (mb_cache.get(artist) or {}).get("mbid")
                key = norm_name(artist)
                if mbid:
                    by_mbid.setdefault(mbid, set()).add(pl["name"])
                elif key:
                    by_name.setdefault(key, set()).add(pl["name"])
    return by_mbid, by_name


def neighbour_homes(n: dict, by_mbid: dict, by_name: dict) -> set:
    if n.get("mbid") in by_mbid:
        return by_mbid[n["mbid"]]
    key = norm_name(n.get("name") or "")
    return by_name.get(key, set()) if key else set()


def similar_line(name: str, entry: dict | None, by_mbid: dict, by_name: dict) -> str:
    if not entry:
        return f"{name}: (no MusicBrainz id or not fetched)"
    own = norm_name(name)
    neighbours = [n for n in entry.get("neighbours") or []
                  if n.get("mbid") != entry.get("mbid") and norm_name(n.get("name") or "") != own]
    if not neighbours:
        return f"{name}: -"
    parts, pull = [], Counter()
    for n in neighbours[:SHOW_NEIGHBOURS]:
        homes = neighbour_homes(n, by_mbid, by_name)
        pull.update(homes)
        tag = f" [{', '.join(sorted(homes))}]" if homes else ""
        parts.append(f"{n.get('name')} {n.get('score')}{tag}")
    line = f"{name}: " + "; ".join(parts)
    if pull:
        ranked = sorted(pull.items(), key=lambda pc: (-pc[1], pc[0]))
        line += " || roster pull: " + ", ".join(f"{p} {c}" for p, c in ranked)
    return line


def run(playlists: list[dict], out_dir: Path) -> None:
    with service_lock(out_dir, "listenbrainz"):
        _run(playlists, out_dir)


def _run(playlists: list[dict], out_dir: Path) -> None:
    mb_cache = json.loads((out_dir / "mb_artists.json").read_text())
    cache_path = out_dir / "lb_similar.json"
    cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    today = date.today()

    names = sorted({name for pl in playlists for t in pl["tracks"] for name in t["artists"]})
    todo = [n for n in names
            if (mb_cache.get(n) or {}).get("mbid") and needs_fetch(cache.get(n), mb_cache[n]["mbid"], today)]
    print(f"{len(names)} unique artists, {len(todo)} to fetch from ListenBrainz")
    client = OpenDataClient("listenbrainz")
    errors = 0
    try:
        for i, name in enumerate(todo):
            entry = fetch_similar(client, mb_cache[name]["mbid"], today)
            if entry is None:
                errors += 1
            else:
                cache[name] = entry
            if (i + 1) % 25 == 0:
                print(f"listenbrainz {i + 1}/{len(todo)}", flush=True)
                cache_path.write_text(json.dumps(cache, ensure_ascii=False))
    except ServiceStopped as e:
        print(f"stopped: {e}")
    cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=1))

    by_mbid, by_name = roster_index(playlists, mb_cache)
    # A cached graph counts only while it belongs to the artist's current MBID.
    current = {n: e for n, e in cache.items() if e.get("mbid") == (mb_cache.get(n) or {}).get("mbid")}
    lines = [similar_line(n, current.get(n), by_mbid, by_name) for n in names]
    (out_dir / "similar.txt").write_text("\n".join(lines) + "\n")
    with_graph = sum(1 for n in names if (current.get(n) or {}).get("neighbours"))
    print(f"similar.txt written: {with_graph}/{len(names)} with neighbours, "
          f"{errors} transient errors (retried next run), {client.requests} requests")



def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("export_dir", type=Path)
    ap.add_argument("--out", type=Path, help="dir holding mb_artists.json; outputs go here too")
    args = ap.parse_args()
    run(load_exports(args.export_dir), args.out or args.export_dir)


if __name__ == "__main__":
    main()
