# Learnings from the first full curation operation (July 2026)

Process knowledge extracted from the real 2026-07 run: 8,300-track library,
~30 agent invocations, 5 adversarial review rounds, 57 irreversible writes,
0 mismatches. Reference this before designing the next multi-agent operation.

## Judgment-agent patterns

- **Scale breaks Sonnet before knowledge does.** On a 692-track single-pass
  classification, Sonnet produced a 33% overturn rate on its uncertain calls
  AND mechanical defects: reasons/labels shifted off-by-one across runs of a
  dozen verdicts, plus circular reasoning ("artist already there" on a
  playlist documented as inflated). Its high-confidence set held (13%
  overturn) — the calibration was honest, the endurance wasn't. Big
  classification passes go to Opus directly; Sonnet remains fine for small,
  well-bounded judgment.
- **Output ceilings kill long single responses.** An agent judging ~1,000
  items died at the 64k output-token limit. Any agent producing large
  verdict files must build them incrementally (Write a skeleton, Edit per
  section) and keep its final text to counts.
- **Rows are not recordings.** Four independent rosters inflated their counts
  by counting rows while duplicate editions of the same recording sat among
  them. Any track-list count an agent reports must be verified as unique
  recordings, mechanically.
- **Agents fabricate supporting citations.** Verdicts that were themselves
  correct arrived with confident false evidence ("X is already in that
  playlist" when it wasn't). Treat every factual claim in a reason string as
  unverified until checked against the data; never let one verdict's citation
  become another verdict's premise.
- **Parallel agents sharing entities need an arbiter.** Two rebalance agents
  consolidated the same band in opposite directions simultaneously; three
  verdict layers each cited a premise another layer was dissolving. A
  synthesis tier with the global view is mandatory whenever work is split.

## Verification-stack patterns

- **Every layer caught defects the layer below missed.** Opus re-judge caught
  Sonnet's; the Fable synthesis caught cross-agent conflicts and row bugs;
  the apply planner caught plan-data defects (opposite keepers that would
  have deleted both copies of a recording; 23 removals present in a plan's
  markdown but missing from its JSON); the adversarial reviewer caught runner
  bugs; the live API caught the test fake's fidelity gap. Nothing was wasted.
- **Reviewers must execute, not read.** Every blocking runner defect was
  found by running the code against a fake API and injecting faults — none
  by code reading alone. Give adversarial reviewers a harness and reproduction
  mandates ("drive the count-neutral drift shape through a resumed run").
- **Mutation-test the tests.** Three of nine injected regressions survived a
  "passing" suite — each survivor was a vacuous test. A suite's pass count
  means nothing until mutations prove the assertions bite.
- **Delete defect-prone machinery instead of fixing it.** The drift-absorbing
  fallback path collected defects across two review rounds; replacing it with
  a hard halt ("re-export and regenerate") retired four findings in fewer
  lines. Complexity that keeps failing review has earned removal, not repair.
- **Freeze data early, iterate machinery.** The op set froze in round 2 and
  every later round re-verified it cheaply by hash while the executable
  machinery iterated. Separating plan-data from execution-code let five
  rounds converge instead of thrash.

## Applying irreversible writes

- **Adds strictly before removals** — an interruption can then only leave
  duplicates, never lose a track. Verify all adds landed (a hard gate reading
  live totals) before the first removal.
- **Two-record journal:** an "attempt" record before each call, a "done"
  record only after a verified-successful response with evidence
  (snapshot_id). A done without evidence is unknown, not done. Resume decides
  replay-vs-skip by diffing live state, never by journal trust alone.
- **Gates carry run identity.** A phase-3 authorization must be bound to the
  run and journal state that earned it; a stale gate from any prior state
  must trigger re-verification, never deletions.
- **Group ops by (playlist, operation); reasons share calls.** All removal
  reasons for one playlist merge into one chunked URI list. 750 logical
  actions became 57 writes.
- **Ramp the first live run phase-by-phase.** Running new write-machinery one
  phase at a time with inspection between phases caught a real integration
  bug at the read-only phase — zero writes at risk. The convenience of one
  continuous run is worth nothing on a first execution.

## API reality

- **A green test suite is a hypothesis about the API, not evidence.** 85/102
  tests passed against a fake serving the previous payload shape while the
  very first live call failed (`tracks.total` no longer exists). Probe the
  real API for every payload field the code depends on, then make the fake
  serve the probed shape.
- **"Could not read" must never equal "changed."** A None from a trimmed API
  field masqueraded as drift on all 11 playlists. Distinguish read-failure
  (error, halt) from observed-difference (drift, halt with regenerate
  advice) explicitly.
- **Live probes beat documentation and community lore.** Three claims died on
  one-call probes: batch GETs (403), /search (403 entirely), playlist-page
  ISRC (present — collapsed an 8,300-call audit into the exports we already
  made). One probe call is always cheaper than designing around a wrong
  assumption.

## Orchestration mechanics that worked

- Background agents + completion notifications for the whole cascade; resume
  agents from transcript after transient 529s instead of relaunching.
- Mid-flight SendMessage to running agents for owner refinements (scope
  corrections landed before the agent finished the affected section).
- Owner feedback rounds as first-class pipeline stages: the v1→v2 revision
  (archetypes-first) and the toggle mechanism (encoded defaults, one-call
  reversals) kept judgment calls cheap to change late.
