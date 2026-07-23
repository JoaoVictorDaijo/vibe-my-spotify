#!/usr/bin/env bash
# One-time Spotify OAuth for the MCP server.
#
# Uses the SAME credential mapping, redirect URI, scope list, and token-cache
# location as run-server.sh, so the token this writes is exactly what the server
# later reads. Runs from inside the clone so ./.cache lands where the server
# looks for it. Safe to re-run: if a valid token is already cached it just
# reports who you are authorized as.
set -euo pipefail

PROJECT_DIR="/home/mainframe/Vibe My Spotify"
CLONE_DIR="$PROJECT_DIR/spotify-mcp"

set -a
# shellcheck source=/dev/null
. "$PROJECT_DIR/.env"
set +a

export SPOTIFY_CLIENT_ID="${SPOTIFY_CLIENT_ID:-${CLIENT_ID:-}}"
export SPOTIFY_CLIENT_SECRET="${SPOTIFY_CLIENT_SECRET:-${CLIENT_SECRET:-}}"
export SPOTIFY_REDIRECT_URI="http://127.0.0.1:8080/callback"

echo "One-time Spotify authorization"
echo "Add http://127.0.0.1:8080/callback to your Spotify app's Redirect URIs"
echo "(Dashboard -> your app -> Settings) before continuing."
echo

cd "$CLONE_DIR"
exec uv run python "$PROJECT_DIR/auth_helper.py"
