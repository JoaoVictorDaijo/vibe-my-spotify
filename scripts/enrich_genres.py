"""One command for the genre evidence judges read: MusicBrainz tags, then
ListenBrainz scene graphs, for every artist on the roster.

    python3 scripts/enrich_genres.py                 # roster from the baseline
    python3 scripts/enrich_genres.py --include-liked # Liked Songs artists too (~1 h more)
    python3 scripts/enrich_genres.py --exports DIR --out DIR

No API keys, no Spotify calls, no LLM tokens. Writes artists.txt and
similar.txt (plus their caches) into curation-review/reference/ by default.
A first run proves every artist's identity through an ISRC and takes about an
hour (MusicBrainz is ~1 request/s and sometimes slow); later runs fetch only
new or stale artists and take minutes. Each service takes a lock in the
output dir, so a second run refuses to start instead of doubling the rate.
Last.fm stays on demand: scripts/lastfm_tags.py.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import enrich_listenbrainz
import enrich_musicbrainz
from enrich_musicbrainz import load_exports

REPO = Path(__file__).resolve().parent.parent


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exports", type=Path, default=REPO / "curation-review/baseline")
    ap.add_argument("--out", type=Path, default=REPO / "curation-review/reference")
    ap.add_argument("--include-liked", action="store_true",
                    help="also enrich Liked Songs artists (it is never a roster home)")
    args = ap.parse_args()
    playlists = load_exports(args.exports)
    if not args.include_liked:
        playlists = [pl for pl in playlists if pl.get("name") != "Liked Songs"]
    if not playlists:
        raise SystemExit(f"no playlist exports in {args.exports} — run export_playlists.py first")
    args.out.mkdir(parents=True, exist_ok=True)
    print(f"{len(playlists)} playlists from {args.exports} -> {args.out}")
    enrich_musicbrainz.run(playlists, args.out)
    enrich_listenbrainz.run(playlists, args.out)


if __name__ == "__main__":
    main()
