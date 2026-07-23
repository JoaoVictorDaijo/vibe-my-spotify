# Vendored: spotify-mcp

- **Source:** [jamiew/spotify-mcp](https://github.com/jamiew/spotify-mcp), a maintained fork of [varunneal/spotify-mcp](https://github.com/varunneal/spotify-mcp)
- **Vendored at upstream commit:** `2925554` (May 2026)
- **License:** MIT — see [LICENSE](LICENSE) (Copyright (c) 2025 Varun Neal Srivastava)

## Local patches

Applied on top of the base commit, in order. Full diffs live in [../patches/](../patches/):

1. `fix: robust playlist track parsing + lighter total lookup` (upstream PR #18 — playlists with local files no longer crash reads)
2. `fix: create_playlist uses current_user_playlist_create` (Feb 2026 API: `POST /me/playlists`)
3. `feat: positional keep-one track removal for dedup` (`remove_specific_track_occurrences` tool, snapshot-guarded)
4. `feat: chunk playlist writes at Spotify's 100-item cap`
5. `feat: expose track uri and playlist position for dedup`

## Syncing with upstream

Clone upstream at `2925554`, apply `../patches/*.patch` with `git am`, then diff against this directory.
