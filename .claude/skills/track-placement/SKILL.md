---
name: track-placement
description: Use when deciding which playlist a song or band belongs in — the owner-approved placement pipeline (mechanical grounding, vibe-first judgment, band-graph test, escalation ladder, journaled apply).
---

# Track placement — the decision pipeline

Distilled from the July 2026 operations (NW/S onboarding, PW trim, Chamber
growth). Follow the stages in order; each exists because skipping it
produced a real defect.

## 1. Ground mechanically first (zero judgment tokens)

Before any opinion, gather the facts from local exports
(`curation-review/baseline/`):

- Locate the track EVERYWHERE: every playlist, Liked Songs, band playlists.
  A "where does X belong" question is often already answered by where its
  siblings sit.
- Pull evidence per copy: URI, ISRC, duration_ms, album, year. Identical
  ISRC = same recording (certain); duration within ±2s = probable; clearly
  different length = different recordings, keep both.
- Check for edition twins (same recording, different URIs — the Whitney
  "Light Upon the Lake" trap) and demo/live/acoustic variants.
- Detect owner drift: diff fresh exports against the baseline; owner
  hand-edits are taste signals, never noise.

## 2. Read the vibes before judging

`curation-review/vibes/<playlist>.md` files are the accumulated rulings.
Judgment without them re-derives borders that are already settled — or
worse, silently overturns an owner ruling. The candidate destinations'
themes, borders, "does NOT belong" lists and owner doctrines are the
constraint set, not suggestions.

## 3. The membership test, in order

1. **Band graph first**: does the BAND live in the playlist's
   scene/ecosystem graph? Playlists are scene-bubbles (the owner thinks in
   band clusters), not surface-feature buckets. Judging by instrumentation
   or production surface produces false positives (the Mercury Rev
   lesson: "symphonic" ≠ in-the-chamber-graph).
2. **Album/era-aware within the band**: a scene band's catalog splits by
   album (Strokes: Is This It era vs Angles; Beck: Sea Change vs Loser;
   Franz: debut vs later; Modest Mouse: pre/post 2004).
3. **Sound-lineage override for sound-defined playlists**: where the
   playlist is a sound (PW's gazey rule for Yuck), the per-track sonic test
   beats the album split — the album is only a proxy.
4. **Owner doctrines trump everything**: Acoustic/Folk's two admission
   lanes (special acoustic VERSIONS or folk-genre — an originally-acoustic
   song stays with its genre archetype); Indie Rock tolerates hushed songs
   by resident bands; overlays hold copies and are dedup-exempt; a demo is
   admissible as the only copy of its song, not beside its studio master
   (owner may override per band).

## 4. Escalation ladder — match process weight to stakes

- **Delegated single-track call**: the session (conciliator tier) decides
  from model knowledge, states the reasoning, acts, and reports — moves are
  one-call reversible. Use for owner phrases like "or wherever you think
  fits."
- **Contested or bulk decisions**: two INDEPENDENT Opus judges with an
  identical written mandate (locked owner rulings as constraints, open
  questions enumerated, output shape fixed, incremental file writes,
  counts-only final text) → conciliator synthesis: convergent verdicts
  become defaults, crossed verdicts go to the owner as toggles, each
  judge's un-seconded extras die unless doctrine-backed.
- **Every verdict claim is unverified until checked against the data** —
  judges cite positions; re-verify them mechanically before planning.
- **The owner has final say, always.** Present toggles, not faits accomplis.

## 5. Apply safely

- Every account write needs an explicitly owner-approved plan; re-export
  affected playlists immediately before generating it.
- Generate the plan with an evidence-checking script that REFUSES to emit
  on any failed check: source match unique, keeper-side verified live,
  destination free of URI and fuzzy-title twins, forbidden-URI guards.
- Execute via `scripts/apply_plan_runner.py` (journaled, adds strictly
  before removals, phase-2.5 hard gate, drift = halt, any 429 = stop).
- After verify: refresh the baseline for touched playlists, record the
  rulings in the vibe files (arrivals, departures, new doctrines), archive
  plan + journal + verdicts in `curation-review/`.

## Known API traps

Playlist rename: spotipy's `playlist_change_details` silently no-ops — use
a raw `PUT playlists/{id}` with the full payload, and don't trust the
immediate GET read-back (stale cache; verify in the app or via a later
export). /search is dead (403) for this app: URIs come from exports, Liked,
or the owner's other playlists. Adds reject dead URIs (unplayable AND no
`canonical_id` relink) with a whole-batch 400 — filter phantoms from every
add list. Plans that add to a playlist created in the same run must set
`playlist_id_or_name` to the runner's `<created:Name>` placeholder — a
literal name reaches the request URL and 400s as "Unsupported URL"; and a
halted-after-create rerun on a fresh journal will create a duplicate
(adoption only fires on an attempted-unknown create), so prefer resuming
the same journal or targeting the created id directly.
