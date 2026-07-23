# Streaming-service APIs for personal AI curation — landscape (July 2026)

Researched 2026-07-23 (Opus web agent; GitHub stats pulled live via `gh`).
Context: Spotify's Feb 2026 lockdown (Premium-only dev mode, 5 users, reduced
endpoints, business-only extended quota, "AI-aided usage" cited as rationale).

## Quick comparison

| Service | API open to individuals? | ISRC lookup | Full write (create/reorder/remove) | Rate limits | Best client | Anti-AI terms? |
|---|---|---|---|---|---|---|
| **Tidal** | Yes — free, self-serve OAuth2/PKCE | **Yes** (`get-tracks-by-isrc`) | Yes via `python-tidal`; official v2 write likely shipping | Undocumented, soft (~500ms/req) | python-tidal (551★, active) | No — tooling embraces LLM agents |
| **Deezer** | New app registration closed | Yes (undocumented `/track/isrc:`) | Yes (historically full) | ~50 req/5s | deezer-python (151★); mcp-deezer (free-account ARL route) | None found |
| **Apple Music** | Yes, $99/yr dev program | **Best-in-class** (`filter[isrc]`) | **No — append-only; no reorder, no remove** | ~20 req/s community estimate | apple-music-python (110★, slowing) | General anti-bulk terms |
| **YouTube Music** | No official YTM API | **No ISRC concept** | Via ytmusicapi (2,876★) — ToS-violating | Data API: 10k units/day; write = 50 units | ytmusicapi | Automation against YouTube ToS |
| **Qobuz** | Partner-only (email for keys) | Not documented | Reverse-engineered libs only | Unknown | None credible (all MCP 0★) | n/a |

## Verdict (ranked)

1. **Tidal** — the escape hatch. Free individual API, ISRC lookup, no anti-AI
   stance, proven migration tool (`spotify2tidal/spotify_to_tidal`, 1,076★,
   active), mature write client (`python-tidal`). MCP: `lucaperret/tidal-cli`
   (official v2 API, 32 tools, built for LLM agents, active 2026-07).
2. **Deezer** — backup; works today via the semi-gray free-account route.
3. **YouTube Music** — capable but no ISRC → fuzzy-matching 6.5k songs; ToS risk.
4. **Apple Music** — eliminated: cannot reorder or remove playlist tracks.
5. **Qobuz** — not viable.

## The hybrid insight (recommended posture)

Spotify's squeeze targets search/bulk-metadata/extended-quota — NOT an owner
doing personal CRUD on their own playlists (which is exactly what our MCP
does and what dev mode still permits). Therefore:

- **Keep Spotify for playback + personal playlist CRUD.**
- **Use MusicBrainz for metadata**: `/isrc/{isrc}` resolves ISRC→recordings
  free, no auth, ~1 req/s (<https://musicbrainz.org/doc/MusicBrainz_API>).
  Our library objects already carry `external_ids.isrc`, so recording-identity
  questions (dedup evidence!) never need Spotify's search endpoint.
- **ListenBrainz + Troi** (`metabrainz/troi-recommendation-playground`) can
  replace Spotify's deleted recommendation/audio-features endpoints.
- **Migrate to Tidal only if** Spotify tightens personal CRUD further; the
  path is a one-time paid Soundiiz/FreeYourMusic transfer (free tiers cap at
  200-600 tracks — the 6.5k liked songs need the ~$5-40 paid tier).

## Migration tooling status (2026)

Soundiiz (40+ services, 200 tracks/transfer free, Premium ~$4.50/mo),
TuneMyMusic, FreeYourMusic (playlists + liked songs + followed artists; free
= 600 tracks/1 playlist). All match by ISRC with fuzzy fallback and report
unmatched tracks.

## Community sentiment

Post-lockdown consolidation around: Tidal/Apple Music as API alternatives,
MusicBrainz/ListenBrainz for the metadata gap. Tidal singled out as the
AI-agent-friendly platform. (Zuplo, freqblog, Medium pieces, 2026.)
