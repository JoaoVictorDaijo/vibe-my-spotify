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
    # Paste-URL flow instead of the localhost listener: WSL can't auto-open a
    # browser, and the listener consumes its single request on any stray hit
    # (favicon/probe), aborting the flow before the real callback arrives.
    auth.open_browser = False

    token = auth.validate_token(auth.cache_handler.get_cached_token())
    if token is None:
        print("\n" + "=" * 72)
        print("1. Open the URL printed below in any browser and authorize the app.")
        print("2. You'll land on a 127.0.0.1:8080 page that FAILS to load — that is")
        print("   expected. Copy the FULL address-bar URL (it starts with")
        print("   http://127.0.0.1:8080/callback?code=...) and paste it at the prompt.")
        print("=" * 72 + "\n")

    # First API call drives the OAuth flow when no valid token is cached, then
    # spotipy writes the token into ./.cache (the cwd auth.sh runs from).
    profile = client.sp.current_user()
    who = profile.get("display_name") or profile.get("id")
    print(f"\nAuthorized as: {who}")
    print("Token cached. Start the server with run-server.sh.")


if __name__ == "__main__":
    main()
