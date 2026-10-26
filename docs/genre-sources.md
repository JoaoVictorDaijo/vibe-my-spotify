# Genre-space evidence sources and their limits

What the placement judges read besides the vibe profiles, where each line
comes from, and the request rules that keep each service from blocking us.
Spotify gives us nothing here: dev-mode artist genres are empty, and search,
recommendations, related artists and audio features are gone
([spotify-rate-limits.md](spotify-rate-limits.md)).

Researched 2026-10-26: live probes of every candidate (114 requests), then
blinded placement tests on Opus and Sol (results below).

## What runs, and what judges read

| Script | Source | Output (in `curation-review/reference/`) | Signal |
|---|---|---|---|
| `enrich_musicbrainz.py` | MusicBrainz artist tags + genres, identity proved by ISRC | `artists.txt` (`Artist: tag; tag`) + `mb_artists.json` | What the band is called, genre-wise |
| `enrich_listenbrainz.py` | ListenBrainz Labs similar artists (by MBID) | `similar.txt` + `lb_similar.json` | Which scene the band lives in: its neighbours, and which roster playlists they already sit in |
| `lastfm_tags.py` (on demand) | Last.fm artist + album + track top tags | `lastfm.txt` (`query => artist [...] \| album [...] \| track [...]`) + `lastfm_cache.json` | Second opinion on a doubtful row; album tags split a band's eras |

`python3 scripts/enrich_genres.py` runs MusicBrainz then ListenBrainz (which
looks artists up by the MBIDs MusicBrainz caches) for every roster artist,
reading `curation-review/baseline/` without Liked Songs; `--include-liked`
adds Liked artists. Run it after every baseline refresh. It refetches only
what is new or stale, so later runs are cheap.

**Artist identity is the foundation.** MusicBrainz identifies each artist
through the ISRC of one of their tracks: the recording's credits name the
exact artist. Name search is only the fallback, and it accepts a hit only on
an exact (case-, accent- and punctuation-folded) name or alias. Before this,
a search score of 90+ was trusted, and 25 of 834 artists were someone else:
Beck was Rufus Beck, Whitney was Whitney Houston, Crumb was George Crumb.
Their tags and graphs were wrong too. Rejected search hits stay in the cache
as `candidate` for review. A ListenBrainz graph counts only while its MBID
matches the artist's current MusicBrainz identity.

A `similar.txt` line reads:

    Tamaryn: Slowdive 186 [Dreamy, Ether]; Beach House 156 [Dreamy]; DIIV 144 [Ether] || roster pull: Ether 4, Dreamy 3

Scores are co-listening counts: compare them within a line, never across
lines. The roster pull counts how many of the top 8 neighbours live in each
playlist (ties listed alphabetically). The artist itself is never its own
neighbour, and a neighbour matches a roster artist by MBID; a match by name
alone is allowed only for roster artists MusicBrainz could not identify, so
a homonym can't borrow a home. It is evidence for the band-graph test, not a verdict: a popular
band's graph is broad (Bowie's neighbours are The Beatles and Led Zeppelin
for every era), and album/era rulings and owner doctrines still come first.

## Measured value (2026-10-26, test v2)

Test: 52 tracks the owner already placed, from 19 playlists, all by artists
named nowhere in the vibe profiles. Britpop, Dreamo and First Wave have no
such artists: their canons are fully written down. Judges read every vibe
profile in full plus the roster with each playlist's kind, the same input
production judges get. Every track carried the same credits, album and year
in all four combinations; only the evidence block changed. Artist identities
were proved by ISRC first. A track counts as correct in any playlist that
holds it.

| Evidence | Opus 5.5 (3 runs) | Sol / Codex (2 runs) |
|---|---|---|
| MusicBrainz tags | **41.7** | **40.5** |
| + ListenBrainz neighbours | 40.3 (fixed 1, broke 3) | 40.0 (1 / 3) |
| + Last.fm tags | 40.0 (1 / 3) | 40.0 (1 / 4) |
| + both | 39.3 (1 / 4) | 40.0 (2 / 3) |

- **With full profiles and correct identities, MusicBrainz tags are
  enough.** About 80% of tracks land in the owner's home, and adding
  ListenBrainz or Last.fm doesn't raise that on average. ListenBrainz fixed
  The Sleepy Jackson (to Psychedelia) but pulled Mark Kozelek & Jimmy LaValle
  out of Slowcore toward Heartland. So the extra sources are for doubtful
  rows, not every row.
- **Sol is a fair substitute for Opus here.** It scored within about a
  point, and the two models' majority answers agree on 45 of 52 tracks. On 7
  of the 8 tracks both got wrong, they gave the same wrong answer, so those
  misses come from the profiles, not the model.
- **What the remaining misses are:** Rock vs Alternative Rock for big
  classics (T. Rex, U2), Unplugged covers judged as genre rather than version
  (Nick Lowe, Priscilla Ahn), Chamber Pop outsiders (Polyphonic Spree, Andrea
  Laszlo De Simone) and one Post-Punk vs New Wave call (OMD). Clearer profile
  wording fixes these; more data doesn't.
- **History:** an earlier run (test v1) seemed to show ListenBrainz helping
  Opus by about 2.5 tracks. A review found that test flawed: stale extracted
  rules, leaked artist names, multi-artist credits that lost their evidence,
  and 25 artists matched to the wrong person. v2 replaces it. Haiku and
  Sonnet were only tested in v1, so no claim about them stands.
- Caveat: this measures agreement with the owner's homes, which mix genre,
  era, mood and rulings, on one sample. It does not measure genre accuracy as
  such. The harness and the per-track tables live outside the repo in the
  owner's working folder.

## Rate limits and how the client honours them

Every open-data call goes through `scripts/open_data_client.py`: one request
per 1.1 s per service, a contact User-Agent, and one failure contract. Only a
real answer is cached: a 2xx body, Last.fm's "not found" (error 6), or a 404
where the caller declares it meaningful (an unknown ISRC). Error bodies,
404s elsewhere, 5xx and network errors return nothing and are retried next
run. A stop signal ends that service's run and keeps what was fetched. The
pace is per process, so never run two scripts against one service at once.

| Service | Published rule | Over-limit signal | Client behaviour |
|---|---|---|---|
| MusicBrainz | ≤ 1 req/s per IP on average; a meaningful User-Agent with contact is required ([rate limiting](https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting)) | **503** for every request from the IP until the rate drops | 1.1 s pace; 503 backs off 5/10/20/40 s, then counts as transient |
| ListenBrainz | "never more than ONE call per second"; valid User-Agent required or requests "may be blocked without further notice" ([API docs](https://listenbrainz.readthedocs.io/en/latest/users/api/index.html)) | `X-RateLimit-Remaining/Reset-In` headers, then **429**. Labs (`labs.api.listenbrainz.org`) sends no rate headers | 1.1 s pace; an exhausted window delays the next call by `Reset-In` (≤ 300 s), longer stops the run |
| Last.fm | No number published; the terms forbid getting around the limits they enforce ([API terms](https://www.last.fm/api/tos)) | Error **29** inside the JSON body (HTTP status may be 200); **26** = key suspended | 1.1 s pace; errors 29/26/10/9/4 stop the run; 8/11/16 (temporary) back off and stay uncached |
| All | — | **429** | `Retry-After` (seconds or HTTP date) ≤ 300 s: sleep it out and retry. Missing, unreadable or longer: stop that service for the day (the same split as the Spotify doctrine) |

Five transient failures in a row also stop a service: something is wrong,
and hammering it is how an IP gets blocked.

Budget at 1.1 s per request: about 800 artists take ~15 min (ListenBrainz,
one call each) to ~30 min (MusicBrainz, ISRC + lookup per artist). MusicBrainz
answers slowly at times (~5 s per request on 2026-10-26), which stretches a
full re-identification to about an hour.
ListenBrainz Labs rejects several MBIDs in one call (400, probed
2026-10-26), so there is no batching.

## Terms that shape the caches

- **MusicBrainz:** identifiers and dates are CC0; tags are CC BY-NC-SA 3.0.
  Fine for a local noncommercial cache. Empty or unmatched artists are retried
  after 30 days, since small artists gain tags over time.
- **ListenBrainz:** listening data is CC0. The similar-artists algorithm is a
  Labs dataset name, not a versioned API. It can be retired, and a changed
  name forces a refetch. Neighbours refresh every 90 days, empties every 30.
- **Last.fm:** a free API key from <https://www.last.fm/api/account/create>
  goes in `.env` as `LASTFM_API_KEY` (the key only, not the shared secret).
  Personal noncommercial use only; at most **100 MB** of Last.fm data kept;
  caching must follow the HTTP headers. Last.fm sent no Cache-Control headers
  when probed, so the cache keeps only filtered tag lists per artist, album
  and track, drops them after a fixed 30 days, and the script refuses to grow
  the cache past 50 MB. `lastfm.txt` is rebuilt from the cache each run.
  Use it per doubtful row, never as a library-wide crawl.

## Evaluated and not used

- **Wikidata:** song release dates are good (it fixes the First Wave era
  cases) but genre labels are thin. A candidate for an original-release-year
  enrichment.
- **Discogs:** release `genres` + `styles` are excellent, but the use is
  blocked on a terms question (re-checked 2026-10-26, Terms of Service updated
  2026-02-23). The Terms of Service ban "the development of any software
  program, including, but not limited to," ML/AI training, and providing
  "archived or cached data sets containing any Service content" to others.
  So this is not a training-only ban, and a hosted LLM reading cached lines
  arguably counts as passing a dataset to a third party. The API terms also
  cap display at "six (6) hours older" and retention at what is "necessary".
  The monthly CC0 dumps (masters ~600 MB gz) clear copyright but carry no
  express exemption from that clause. Anonymous API allowance: 25/min
  (`x-discogs-ratelimit`); the authenticated 60/min is unverified (the
  developer docs return 403). Revisit only with Discogs' written answer.
- **Deezer:** working keyless endpoints, but its terms oppose text/data
  mining and AI use, and its genres are album-level and broad.
- **iTunes Search:** one broad genre per track.
- **AcousticBrainz:** frozen since 2022, with coarse classifier output.
- **Every Noise at Once:** no licensed API.
- **RYM, Bandcamp:** scraping only, so excluded.
- **Audio models (Essentia, CLAP):** need lawfully obtained audio. Parked.
- **Generic "genre analysis" prompt skills** (e.g. a5c-ai's
  `genre-analysis`): a generic genre cheat-sheet. In an early Haiku test it
  added nothing over the vibe profiles and did worse without them.
