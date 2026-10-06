# Vendored: spotify-mcp

- **Source:** [jamiew/spotify-mcp](https://github.com/jamiew/spotify-mcp), a maintained fork of [varunneal/spotify-mcp](https://github.com/varunneal/spotify-mcp)
- **Vendored at upstream release:** `v0.7.0` (commit `301399d`, 2026-09-20)
- **License:** MIT — see [LICENSE](LICENSE) (Copyright (c) 2025 Varun Neal Srivastava)

## Local patches

Applied on top of the base commit. Full diffs live in [../patches/](../patches/):

1. `feat: request ugc-image-upload scope for custom playlist cover images`

## How this repo uses the server

The repo's scripts (`../scripts/`) import only `spotify_mcp.spotify_api.Client`
for its authenticated spotipy client and token cache, and route every request
through `apply_plan_runner.Api` (pacing, 429 policy, daily call ledger). Every
account write goes through the journaled apply runner, never through MCP tools.
Upstream's MCP tool surface is used only for interactive reads.

Changing the scope list invalidates the cached token: run `../auth.sh` once
after any scope change.

## Syncing with upstream

1. Clone upstream and check out the new release tag.
2. `git am ../patches/*.patch` (rebase or drop any patch upstream has absorbed).
3. `rsync -a --delete` the result over this directory, excluding `.git`,
   `.cache` (the token), `.venv`, the tool caches and this file.
4. `uv sync`, `uv run pytest`, then `python3 ../scripts/test_apply_plan_runner.py`.
5. Regenerate `../patches/` with `git format-patch <tag>..HEAD`, update this file.
