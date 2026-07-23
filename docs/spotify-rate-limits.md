# Spotify Web API rate limits — community-measured reality (July 2026)

Spotify publishes no numbers. This is the operative picture assembled from
staff forum posts, first-hand reports, and our own measurements. Claims are
dated; the Feb 2026 API overhaul obsoleted most older figures.

## What is confirmed

- **Mechanism (official):** rolling 30-second window + per-endpoint custom
  limits; 429 with `Retry-After` (seconds). No numbers, ever.
  <https://developer.spotify.com/documentation/web-api/concepts/rate-limits>
- **Daily quotas exist (staff-confirmed, 2026-02-27):** "we bumped the
  playlist related daily quota, made it 8 times bigger … some will still run
  into the daily quota limit." The day is a budget, not just a rate.
  <https://community.spotify.com/t5/Spotify-for-Developers/Low-number-of-requests-leading-to-429-response/td-p/7338415>
- **Feb 2026 dev-mode tightening (official blog, 2026-02-06):** Premium
  required, ONE dev-mode client-ID per developer, max 5 users, reduced
  endpoint set. Rationale cites automation and AI risks.
  <https://developer.spotify.com/blog/2026-02-06-update-on-developer-access-and-platform-security>
- **Extended quota is business-only (since 2025-05-15):** registered business,
  launched commercial service, ≥250k MAU. "AI/ML use of Spotify content" is a
  documented rejection reason. No indie path.
  <https://github.com/spotify/spotify-web-api-ts-sdk/issues/159>

## What the community has measured (anecdotal but consistent)

- Post-Feb-2026 dev-mode 429s arrive at sub-1-req/s sustained rates; reports
  of throttling after tens of requests (2026-02 threads). **The binding
  constraint is the daily bucket, not the per-second rate** — polite pacing
  still ends in a multi-hour ban once the bucket empties.
- Measured per-endpoint daily budgets (community, primary sources):
  `/v1/tracks` ≈ **600 requests/24h** before a ban (2026-05, ≥1s spacing,
  cold-start app); recommendations ≈ 200/24h (2024, endpoint now dead).
  `/search` numbers: never measured publicly — ours (~900 in 1.5h → 13h ban)
  is the only data point we have.
- **The adaptive-ceiling model (r/spotifyapi, 2026-06, unverified but
  use-case-identical):** the rolling-30s limit is dynamic per app and per
  endpoint and GROWS with sustained gradual usage — start ~100 req/min and
  ramp volume day by day over weeks; one dev claims ~1M req/day eventually on
  a personal playlist tool. Corollary: daily cliffs like the 600 figure are
  cold-start values, not fixed ceilings — cultivate quota, never burst.
  Counterpoint from the same thread: a flat 10s-between-calls pace still
  eventually earned a 12-15h lockout, so slow pacing alone is not a cure.
- Write item caps appear endpoint-specific: playlist adds historically 100
  URIs/call; `/me/tracks?ids=` reported cut 50 → 40. We chunk all writes at
  ≤40 to be safe.
- **Retry-After penalties observed: 6-24h band** (retry headers of 6-24h;
  lockouts of 12/20/23/24h; ours 46,649s ≈ 13h). Claims of 48h exist in one
  unmined thread but are unconfirmed. Penalties can be endpoint-scoped or
  app-wide.
- "Calling during a penalty extends it": **no primary-source evidence** in
  the mined threads — treat as unproven folklore, but still don't probe
  during a penalty (no upside). Some 429s carry no `Retry-After` at all —
  treat that as "day over."
- Reset timing (rolling vs fixed): **unknown** — nobody has established it.
- **"180 requests/min" is folklore**: it originates from a third-party Mendix
  Medium article's own testing (2023), pasted into a forum thread — never a
  Spotify statement. The staff-accepted answer (2022) explicitly gives no
  numbers.
- **Batch/multiple-item READ endpoints (GET several artists/tracks, ISRC
  surfaces) are reported deprecated/removed for dev-mode apps in Feb 2026**;
  the `?ids=` WRITE endpoints remain (they just 429), and one user reports
  per-call max items reduced **50 → 40**. Chunk writes at ≤40 items.
- Staff communication timeline ends 2026-02-27 (the 8× playlist-quota bump);
  the transparency improvements remain promises. Our app still gets
  `external_ids.isrc` and `isrc:` search hits — treat as fragile.

## Operating rules for this app

1. Serialize all requests; ≤1 req/s sustained.
2. Treat each day as a budget. Search: a few hundred calls/day, spread out,
   never bursted; cache every result permanently (each ISRC searched at most
   once, ever — phantom_cache.json).
3. Prefer paged playlist reads (100 tracks/page, 50 liked) — these are the
   cheapest per-track calls that exist; cache `snapshot_id` and skip
   unchanged playlists. Chunk all writes at ≤40 items per call.
4. If per-track fetches are needed, budget ~500/day max (measured cliff
   ≈600/day on `/v1/tracks`) — but first check whether the playlist/liked
   page payload already carries the field (e.g. `external_ids.isrc`,
   `linked_from` with a market context) before spending per-track calls.
5. On ANY 429: stop the entire app, not just the endpoint. Never probe
   during a penalty. A 429 without `Retry-After` = quota exhausted, halt for
   the day. (`phantom_audit.py` implements this via QuotaExhausted.)
6. Instrument our own counters (no reliable rate-limit headers exist);
   log daily per-endpoint totals to back into the real cliff empirically.

## Open questions (nobody knows)

- The actual requests-per-30s number for dev mode.
- The daily search cap.
- Rolling vs fixed-time daily reset.

## Sources still unread (Cloudflare/login-blocked — mine manually)

1. Deep replies (68 pages) of the definitive 2026 thread `td-p/7338415`.
2. Replies in quota-extension rejections thread `td-p/6966559`.
3. `td-p/7345683`, `td-p/7404600` (2026 extended-quota threads).
4. `td-p/6667128` (rate-penalty-avoiding architecture).
5. Comments on spotify-web-api-ts-sdk issue #159; GitHub web-api issue #644.
6. `td-p/5330410` accepted answer (possible origin of the 180/min folklore).
7. r/spotifydev (blocked to bots) for 2026 safe-rate anecdotes.
