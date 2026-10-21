# Vibe My Spotify — operating guide

Claude-driven curation of the owner's real Spotify playlists via the vendored
MCP server in [spotify-mcp/](spotify-mcp/) and the zero-token scripts in
[scripts/](scripts/). The policies below were agreed with the owner — follow
them, and when a policy changes update this file rather than ad-hoc memory.

The vendored server tracks upstream jamiew/spotify-mcp **v0.7.0** plus one
local patch ([spotify-mcp/VENDORED.md](spotify-mcp/VENDORED.md)). The scripts
use only its authenticated client. Account writes never go through MCP tools,
only through the journaled apply runner. Any scope change invalidates the
token: re-run `./auth.sh` once.

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
- `enrich_musicbrainz.py DIR` — artist genre tags from MusicBrainz (no Spotify
  calls); writes `artists.txt` for judges + a `mb_artists.json` cache — keep
  both in `curation-review/reference/`
- `phantom_audit.py DIR` — read-only stale-URI detection (ISRC + relinking)
- `dedup_review.py DIR OUT.md` — checkbox review file from dedup + ISRC evidence
- `verdict_convergence.py A.txt B.txt OUT_PREFIX [--field verdict|fallback]` —
  joins two independent judges' verdict files (local, no API): agreements
  become defaults, crossings become owner toggles
- `gen_moves_plan.py SPEC.json PLAN.json` — turns an owner-approved move spec
  (creates, moves, adds, removes, title waivers, Liked saves/removes) into a
  runner plan; refuses to emit on dead URIs, known ghosts, missing sources or
  URI/ISRC/title twins
- `apply_plan_runner.py PLAN --journal J` — executes an owner-approved plan,
  playlists and Liked Songs alike (Liked writes are proven by a
  `/me/library/contains` read-back, since Liked has no snapshot_id)

Every Spotify call in these scripts goes through `apply_plan_runner.Api`:
one request per 2 s, 429s classified per the rule below, and a shared daily
per-endpoint ledger in `curation-review/apply_run_counters.json`. A full
Liked export therefore takes ~4.5 minutes.

Feb 2026 API notes: `artist_albums` rejects limit > 10 (400 "Invalid limit",
2026-10-06); playlist items nest the track under `"item"`; batch GET
endpoints are gone (403, re-probed 2026-10-06 on `/tracks?ids=`); **/search is 403-forbidden for this app entirely**
(both plain and `isrc:` queries — probe-verified 2026-07-29), so the MCP
search tool and any search-based flow are dead: adds need URIs from exports
or external sources. Playlist pages fetched with `market="from_token"` carry
`external_ids.isrc`, `is_playable`, and relink via `linked_from` — the
cheapest per-track data that exists (100/request). Liked-membership checks: spotipy's
`current_user_saved_tracks_contains` now hits `/me/library/contains?uris=` —
alive at 40 ids per call (probe-verified 2026-10-06; 50 ids 400s). It
answers by URI only, so resolve through `uri-remaps.json`/ISRC first —
a re-liked song on a republished edition reads as "not liked" under its
old URI (the Slanted & Enchanted lesson). Rate limits are
unpublished — full picture in
[docs/spotify-rate-limits.md](docs/spotify-rate-limits.md). Operating rules:
one request per 2 s (sustained ~1 req/s is this app's throttle edge); daily
quotas exist and, since Spotify's 2026-07-23 update, are counted per
developer account (every app on the account shares one pool) — treat each
day as a budget: per-track GETs ≈350-600/day measured (our 13h penalty came
from ~350 track GETs, not search — searches were 403ing all along); chunk
writes at ≤40 items; cache every probe result permanently. **Classify every
429 (owner-approved 2026-10-06):** `reason: QUOTA_EXCEEDED`, no
Retry-After, or a wait over 300 s = the quota is spent, stop the whole app
for the day; a short Retry-After = a rolling-window throttle, sleep it out
and continue. spotipy's log line "Retry will occur after: 0 s" cannot tell
the two apart. Extended quota mode is business-only (≥250k MAU; "AI/ML" is a
documented rejection reason) — design inside dev mode. Judgment stages run
off exports on disk and need no API — a penalty only blocks audits and
applies, not analysis. For recording-identity questions prefer MusicBrainz
(`/isrc/{isrc}`, free, no auth, ~1 req/s) over Spotify search; batch track
fetches already return `external_ids.isrc` for dedup evidence. Alternatives
landscape (Tidal = designated escape hatch):
[docs/streaming-api-alternatives.md](docs/streaming-api-alternatives.md).

## Model tiering (owner-approved; final revision 2026-07-30 post-operation)

- Scripts for all mechanics — never a model.
- **Sonnet is deprecated for this project's judgment work** (drain-scale
  failure modes documented in
  [docs/operation-learnings.md](docs/operation-learnings.md)).
- **Opus does all judgment**: classification, drains, clustering, proposals,
  plan design, and adversarial review rounds.
- **A single Fable agent is the standing final conciliator** — synthesizer /
  advisor / reviewer merging multi-agent findings, arbitrating conflicts,
  and checking the whole before anything reaches the owner.
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
  primary homes without removing them (Night Wave is the model; Third Eye
  was, until its 2026-08-01 promotion to a standalone primary home). Overlay
  playlists are exempt from cross-playlist dedup — their copies are
  intentional. Overlays may also carry externally-suggested songs the owner
  adds manually.

## Dedup policy (owner-approved)

- Same-recording confidence: identical ISRC = certain; duration within ±2s =
  probable; clearly different length = different recordings, keep both.
- Keep order when collapsing copies: Deluxe/Extended > original album >
  compilation/Best-of. The same ranking picks canonical URIs for phantom swaps.
- Acoustic versions in Unplugged duplicating an electric original
  elsewhere are intentional parallel curation, not dupes.
- Review files: every group carries a computed PROPOSED action; an unchecked
  checkbox executes it, a checked one overrides (flips) it — one uniform
  semantic across all sections.
- **The curated roster is the edition authority** (2026-08-01): when the
  same recording (ISRC-identical) exists as different editions in Liked vs
  a playlist, Liked aligns to the roster's URI — never fork the roster's
  choice. Delisted-edition "ghosts" are canonicalized against the artist
  page (probe-validated: `artist_albums` + `album_tracks` alive; match by
  title + duration ±2s, MusicBrainz ISRC for ties). Re-liking resets
  `added_at` — the pre-fix dated Liked snapshot
  (curation-review/liked-songs-dated-prefix-2026-08-01.json) is the
  permanent reference for album-spam analysis. Every add-plan must check
  destination ISRCs, not just URIs and titles, so edition dups can never
  re-enter through the pipeline. Ghost→canonical mappings live permanently
  in `curation-review/uri-remaps.json` (discovered vs applied, with
  fixed_in) — consult before any discovery, update after any fix, so
  Liked-side and playlist-side repairs share one memory.
- **Tombstone doctrine (owner, 2026-08-01): unplayable rows are NEVER
  removed or unliked for being dead** — they may be relisted someday. Swap
  an unplayable only when an ISRC-identical version exists on a republish
  of the same album; a truly-gone recording (e.g., a removed live album)
  stays in place as a tombstone. (Tombstones still can't be ADDED anywhere
  — dead URIs 400 the batch — so they keep their current homes.) The
  playable-ghost evidence ladder (title + duration ±2s) applies only to
  playable rows.

## The roster and the workspace

The live roster — every playlist with its count, id and kind (archetype,
open home, owner-nurtured standalone, overlay) — is the table in
`curation-review/STATUS.md`, refreshed after every apply; counts here would
only go stale. As of 2026-10-21: 23 playlists + Liked Songs (~6.4k), with
Night Wave the one OVERLAY (copies, dedup-exempt), Alternative Rock and Rock
the open homes, and Britpop, Slowcore and First Wave owner-nurtured under the
≥50 floor. About 115 older owned playlists sit outside the roster (issue #14).
The placement pipeline is the repo skill `.claude/skills/track-placement` —
invoke it for any where-does-this-song-belong question.

Owner doctrines worth knowing before judging (full rulings per playlist in
`curation-review/vibes/`):
- scene bands classify album-aware (Strokes precedent); for PW the test is
  sonic lineage (the Yuck gazey rule), not era;
- Unplugged admits only acoustic VERSIONS of electric songs and hushed covers,
  folk-genre songs live in Folk, and an originally-acoustic song stays with
  its genre archetype; Indie Rock tolerates hushed songs by resident bands;
- no "bridge" category — every row coalesces into a defined bin;
- Soft 70's is mellow 70s rock with groove (the owner widened Yacht Rock by
  hand), with yacht and sophisti-pop as its core, not its boundary.

API quirks met in applies: `playlist_change_details` silently no-ops — rename
with a raw `PUT playlists/{id}` and the full payload (it 400s with "You must
follow the playlist" when the owner has unfollowed it); don't trust immediate
GET read-backs of name/description. Playlist adds reject dead URIs
(unplayable, no relink) with a whole-batch 400 — filter phantoms out of every
add list.

Every account write needs an explicitly owner-approved plan. Re-export before
mutating — playlist state drifts. Live state lives in the gitignored
`curation-review/` (layout in its STATUS.md):
- `baseline/` holds the latest full export of every playlist (JSON + compact
  txt, plus liked-songs.json). Refresh it after every apply and diff future
  pulls against it (`scripts/diff_baseline.py NEW_DIR`). The baseline is the
  source of truth between sessions.
- `vibes/` is the learning loop: per-playlist profiles (theme, canon, borders,
  owner rulings, expulsions) that judgment jobs read BEFORE judging; owner
  hand-edits surfaced by the baseline diff are taste signals folded back in
  each cycle.
- `archive/<date>-<cycle>/` keeps each finished cycle's plan, journal, spec,
  judge verdicts and drafts, so every decision stays traceable.

Liked Songs caveat: old Spotify auto-liked every track of a liked album, so
the Liked pool contains album-spam — a liked track is NOT reliable
evidence of curated affection. Any Liked-driven pass (backfill, suggestions)
must weigh this. The rule (owner, 2026-09-05): an album counts as a
whole-album like when liked tracks / album tracks ≥ 75% (albums of ≥5
tracks); a like-burst below that is curation. Exports carry `added_at`,
`album_id` and `album_total_tracks` for this.

## Data hygiene — the repo is public

Never commit `.env`, token caches, playlist exports, or anything under
`curation-review/` (personal listening data). Scripts and policies: yes.
