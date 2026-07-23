"""One-time Spotify OAuth helper for the MCP server.

Reuses the server's own auth manager (spotify_api.Client) so the scope list,
credential env mapping, redirect URI, and token-cache location are byte-for-byte
what the running server expects. Invoked by auth.sh from inside the clone.
"""

from __future__ import annotations

from spotify_mcp import spotify_api


def main() -> None:
    client = spotify_api.Client()
    auth = client.auth_manager

    token = auth.validate_token(auth.cache_handler.get_cached_token())
    if token is None:
        # Browser auto-open is unreliable (e.g. under WSL), so always print the
        # URL. Opening it on any machine works: the redirect hits the local
        # callback server this flow starts on 127.0.0.1:8080.
        print("\n" + "=" * 72)
        print("Open this URL in a browser and authorize the app:")
        print(auth.get_authorize_url())
        print("=" * 72)
        print("\nWaiting for the redirect to http://127.0.0.1:8080/callback ...\n")

    # First API call drives the OAuth flow when no valid token is cached, then
    # spotipy writes the token into ./.cache (the cwd auth.sh runs from).
    profile = client.sp.current_user()
    who = profile.get("display_name") or profile.get("id")
    print(f"\nAuthorized as: {who}")
    print("Token cached. Start the server with run-server.sh.")


if __name__ == "__main__":
    main()
