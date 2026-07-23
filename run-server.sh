#!/usr/bin/env bash
# Launch the Spotify MCP server over stdio for Claude Code.
#
# Loads Spotify credentials from the project .env (which defines CLIENT_ID and
# CLIENT_SECRET), re-exports them under the SPOTIFY_* names the server reads,
# and runs from inside the clone so spotipy's ./.cache token file is found
# consistently (same cwd auth.sh authorized in). stdout is left untouched for
# the MCP JSON-RPC stream; the server logs to stderr.
set -euo pipefail

PROJECT_DIR="/home/mainframe/Vibe My Spotify"
CLONE_DIR="$PROJECT_DIR/spotify-mcp"

# Export everything sourced from .env, then map to the server's expected names.
set -a
# shellcheck source=/dev/null
. "$PROJECT_DIR/.env"
set +a

export SPOTIFY_CLIENT_ID="${SPOTIFY_CLIENT_ID:-${CLIENT_ID:-}}"
export SPOTIFY_CLIENT_SECRET="${SPOTIFY_CLIENT_SECRET:-${CLIENT_SECRET:-}}"
export SPOTIFY_REDIRECT_URI="http://127.0.0.1:8080/callback"

cd "$CLONE_DIR"
exec uv run spotify-mcp
