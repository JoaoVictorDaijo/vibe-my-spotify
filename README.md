# Vibe My Spotify

Claude-driven management for your own Spotify playlists: deduplication, AI theme curation ("remove what doesn't fit this playlist"), and cross-playlist rebalancing — with edits applied directly to your account through a patched [spotify-mcp](spotify-mcp/) MCP server.

## Requirements

- Spotify **Premium** (required for Spotify dev-mode apps since Feb 2026)
- A Spotify app from the [developer dashboard](https://developer.spotify.com/dashboard) — Web API only, redirect URI `http://127.0.0.1:8080/callback`
- [uv](https://docs.astral.sh/uv/), Claude Code

## Setup

1. Create `.env` in the project root:

   ```
   CLIENT_ID=your_spotify_client_id
   CLIENT_SECRET=your_spotify_client_secret
   ```

2. One-time OAuth (opens your browser, or prints the URL to visit):

   ```
   ./auth.sh
   ```

3. Open the project in Claude Code and approve the `spotify` MCP server when prompted ([.mcp.json](.mcp.json) registers it; [run-server.sh](run-server.sh) launches it).

## Credits

The MCP server under [spotify-mcp/](spotify-mcp/) is vendored under the MIT license:

- Original author: **Varun Neal Srivastava** — [varunneal/spotify-mcp](https://github.com/varunneal/spotify-mcp)
- Based on the maintained fork: [jamiew/spotify-mcp](https://github.com/jamiew/spotify-mcp)
- Local modifications are listed in [spotify-mcp/VENDORED.md](spotify-mcp/VENDORED.md) with full diffs in [patches/](patches/)

Full license text: [spotify-mcp/LICENSE](spotify-mcp/LICENSE)
