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
endpoints are gone; search caps at 10 results. Dev-mode apps have a SMALL
app-wide daily quota — ~900 searches in one run earned a ~13h penalty box
(Retry-After ≈ 46000s on every endpoint). Never run per-track search sweeps:
audit with the relink signal (1 track fetch each) and reserve ISRC searches
for targeted cases (fuzzy-group members, flagged tracks). Judgment stages run
off exports on disk and need no API — a penalty only blocks audits and
applies, not analysis.

## Model tiering (owner-approved)

- Scripts for all mechanics — never a model.
- Sonnet is the workhorse for routine judgment (reservoir drain, thematic rebalance).
- Opus re-judges every Sonnet verdict below high confidence (medium AND low).
- Clustering and new-playlist proposals go straight to Opus.

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

11 playlists + Liked Songs (~6.5k tracks). Alternative Rock folder — sisters:
Ether (shoegaze, a bit of psych), Permanent Wave (Sonic Youth/Fugazi/Pixies
canon), Dreamy (dreampop/slowcore), Dreamo (heavy gaze/slowcore hybrids);
plus Alternative Rock (true reservoir), Garage Rock (pseudo-reservoir:
Strokes/Arctic Monkeys/Interpol, inflated), Indie Rock (semi-thematic classic
indie), Post-Punk (thematic). Outside the folder: Acoustic/Folk (acoustic
reservoir), Rock (true reservoir), 70's Sunday Rock (half-formed 70s theme,
~half still duplicated in Rock).

Stages, in order: 0) phantom-URI normalization incl. Liked Songs →
1) thematic rebalance → 2) reservoir drain (one-way into thematic homes) →
3) residue clustering → new-playlist proposals.

Every account write needs an explicitly owner-approved plan. Re-export before
mutating — playlist state drifts. Live session state (exports, review files,
applied vs parked) lives in the gitignored `curation-review/`.

## Data hygiene — the repo is public

Never commit `.env`, token caches, playlist exports, or anything under
`curation-review/` (personal listening data). Scripts and policies: yes.
