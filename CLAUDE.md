# Vibe My Spotify — operating guide

Claude-driven curation of the owner's real Spotify playlists via the vendored
MCP server in [spotify-mcp/](spotify-mcp/) and the zero-token scripts in
[scripts/](scripts/). The policies below were agreed with the owner — follow
them, and when a policy changes update this file rather than ad-hoc memory.

## Core principle: LLM tokens only where judgment lives

Mechanical work (export, dedup detection, enrichment, phantom-URI audit,
applying an approved plan) runs as plain scripts — zero LLM tokens, never a
model, not even a cheap one. Models only judge: theme fit, clustering,
canonical homes. Exports are compact `pos | artist – title` lines fed to
subagents; raw API JSON never enters the orchestrator context.

## Running the scripts

Spotify-touching scripts run from inside `spotify-mcp/` so they reuse the
server's spotipy client and token cache:

    cd spotify-mcp && set -a && . ../.env && set +a && \
      export SPOTIFY_CLIENT_ID="$CLIENT_ID" SPOTIFY_CLIENT_SECRET="$CLIENT_SECRET" \
             SPOTIFY_REDIRECT_URI="http://127.0.0.1:8080/callback" && \
      uv run python ../scripts/<script>.py ...

- `export_playlists.py <ids...> | --liked --out DIR` — JSON + compact text per playlist
- `dedup_report.py DIR` — exact/cross/fuzzy duplicate report (local, no API)
- `enrich_exports.py DIR --skip-genres` — ReccoBeats energy/valence/tempo
  (always pass `--skip-genres`: dev-mode apps get empty artist genres)
- `phantom_audit.py DIR` — read-only stale-URI detection (ISRC + relinking)
- `dedup_review.py DIR OUT.md` — checkbox review file from dedup + ISRC evidence

Feb 2026 API notes: playlist items nest the track under `"item"`; batch GET
endpoints are gone (403); **/search is 403-forbidden for this app entirely**
(both plain and `isrc:` queries — probe-verified 2026-07-29), so the MCP
search tool and any search-based flow are dead: adds need URIs from exports
or external sources. Playlist pages fetched with `market="from_token"` carry
`external_ids.isrc`, `is_playable`, and relink via `linked_from` — the
cheapest per-track data that exists (100/request). Rate limits are
unpublished — full picture in
[docs/spotify-rate-limits.md](docs/spotify-rate-limits.md). Operating rules:
serialize requests at ≤1 req/s; daily quotas exist (staff-confirmed) — treat
each day as a budget: per-track GETs ≈350-600/day measured (our 13h penalty
came from ~350 track GETs, not search — searches were 403ing all along);
chunk writes at ≤40 items; cache every probe result permanently; on ANY 429
stop the whole app, and a 429 without Retry-After means the day's budget is
gone. Extended quota mode is business-only (≥250k MAU; "AI/ML" is a
documented rejection reason) — design inside dev mode. Judgment stages run
off exports on disk and need no API — a penalty only blocks audits and
applies, not analysis. For recording-identity questions prefer MusicBrainz
(`/isrc/{isrc}`, free, no auth, ~1 req/s) over Spotify search; batch track
fetches already return `external_ids.isrc` for dedup evidence. Alternatives
landscape (Tidal = designated escape hatch):
[docs/streaming-api-alternatives.md](docs/streaming-api-alternatives.md).

## Model tiering (owner-approved; revised by the July 2026 operation)

- Scripts for all mechanics — never a model.
- Sonnet for small, well-bounded judgment only. Large single-pass
  classification (hundreds of tracks) goes to Opus directly — the real run
  showed Sonnet degrading at drain scale (33% overturn, off-by-one reason
  shifts; see [docs/operation-learnings.md](docs/operation-learnings.md)).
- When Sonnet is used: Opus re-judges every verdict below high confidence.
- Clustering, new-playlist proposals, and synthesis go to Opus; a Fable agent
  checks/merges multi-agent findings on owner approval.
- Agents producing large verdict files must write incrementally (64k output
  ceiling) and return only counts as text.

## Playlist architecture policy (owner-approved)

- New playlists target ≥50 tracks; build big archetypes first. A narrow theme
  must not strip songs from a viable bigger playlist.
- Avoid single-band domination — a playlist that is mostly one or two bands
  reads as an anthology, not a theme.
- Reservoirs double as open playlists: legitimate homes for tracks fitting no
  archetype, not just staging areas.
- Niche/obtuse theme ideas get parked in the GitHub backlog issue and
  revisited only after the archetypes stabilize.
- Two playlist types: archetypes are PRIMARY HOMES (tracks move there);
  blessed niche standalones may be OVERLAYS holding copies fetched from
  primary homes without removing them (Third Eye is the model). Overlay
  playlists are exempt from cross-playlist dedup — their copies are
  intentional. Overlays may also carry externally-suggested songs the owner
  adds manually.

## Dedup policy (owner-approved)

- Same-recording confidence: identical ISRC = certain; duration within ±2s =
  probable; clearly different length = different recordings, keep both.
- Keep order when collapsing copies: Deluxe/Extended > original album >
  compilation/Best-of. The same ranking picks canonical URIs for phantom swaps.
- Acoustic versions in Acoustic/Folk duplicating an electric original elsewhere
  are intentional parallel curation, not dupes.
- Review files: every group carries a computed PROPOSED action; an unchecked
  checkbox executes it, a checked one overrides (flips) it — one uniform
  semantic across all sections.

## The curation operation (roster + stages)

APPLIED 2026-07-30 (plan v2, verified 18/18): 17 active playlists + Liked
Songs (~6.5k). Thematic: Ether 68 (pure shoegaze), Permanent Wave 258
(SY/Fugazi/Pixies canon; owner keeps it big — TFC and Yuck stay), Dreamy 201
(dreampop/slowcore), Dreamo 27 (heavy-gaze), Post-Punk 202, Indie Rock 103
(synth block kept under scene reading), Garage Rock 145 (2000s revival).
Archetypes created: Psychedelia 130, Heartland & Americana 89, Chamber Indie
60, 90s Alternative 53, Laurel Canyon 49, Britpop 40 (owner-nurtured, under
floor by choice). Overlay: Third Eye 55 (copies, dedup-exempt). Open homes:
Alternative Rock 101, Rock 109, Acoustic/Folk 76. Empty shell kept: 70's
Sunday Rock.

Completed cycles: dedup pass (2026-07-23), full rebalance + drain + clustering
+ apply (2026-07-30). Next cycle lives in backlog issue #2 (Liked backfill +
album-spam cleanup first; then Early Alternative, PW breakup, pockets) and
issue #1 (Troi growth). Phantom/unplayable cleanup (203 tracks) parked.

Every account write needs an explicitly owner-approved plan. Re-export before
mutating — playlist state drifts. Live session state (exports, review files,
applied vs parked) lives in the gitignored `curation-review/`.

Liked Songs caveat: old Spotify auto-liked every track of a liked album, so
the ~6.5k Liked pool contains album-spam — a liked track is NOT reliable
evidence of curated affection. Any Liked-driven pass (backfill, suggestions)
must weigh this; cleanup idea parked in backlog issue #2 (needs added_at in
the export to detect same-album timestamp runs).

## Data hygiene — the repo is public

Never commit `.env`, token caches, playlist exports, or anything under
`curation-review/` (personal listening data). Scripts and policies: yes.
