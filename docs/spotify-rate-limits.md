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
  of throttling after tens of requests (2026-02 threads). Pre-2026 figures
  (~250/30s client-credentials, ~1000/30s auth-code, "180/min") are stale.
- `/search` behaves like the scarcest resource with its own (unpublished,
  low) daily cap — search-heavy apps 429 while other endpoints still work.
- **Giant Retry-After penalties (6-48h) are normal**, documented since 2023
  (43,200s / 31,566s / ~49,000s reports). Ours: 46,649s after ~900 `isrc:`
  searches in ~1.5h. Penalties can be endpoint-scoped or app-wide.
- **Calling during a penalty extends it** (reports of 13h escalating toward
  48h). Some 429s carry no `Retry-After` at all — treat that as "day over."
- Reset behavior: a rolling countdown from budget exhaustion, not a clean
  midnight reset (best evidence; unconfirmed).
- `external_id`/ISRC surfaces were reduced in early-2026 changes and the ISRC
  path is one Spotify watches; our app still gets `external_ids.isrc` and
  `isrc:` search hits, but treat this as fragile.

## Operating rules for this app

1. Serialize all requests; ≤1 req/s sustained.
2. Treat each day as a budget. Search: a few hundred calls/day, spread out,
   never bursted; cache every result permanently (each ISRC searched at most
   once, ever — phantom_cache.json).
3. Prefer batch reads (100 tracks/page playlists, 50 liked); cache
   `snapshot_id` and skip unchanged playlists.
4. On ANY 429: stop the entire app, not just the endpoint. Never probe
   during a penalty. A 429 without `Retry-After` = quota exhausted, halt for
   the day. (`phantom_audit.py` implements this via QuotaExhausted.)
5. Instrument our own counters (no reliable rate-limit headers exist);
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
