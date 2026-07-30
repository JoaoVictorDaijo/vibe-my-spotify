"""Execute apply_plan.json against Spotify — journalled, resumable, 429-safe.

    cd spotify-mcp && uv run python ../scripts/apply_plan_runner.py <plan.json> \
        [--journal PATH] [--resume] [--dry-run] [--phase N] [--yes]

Talks to raw spotipy (via the vendored server's auth client), never the MCP tools:
those re-chunk internally, gate removals behind an elicitation prompt, and keep no
journal — none of which is acceptable for 57 irreversible writes.

Safety model (see the plan's apply_plan.md):
  * adds (phase 2) strictly precede removals (phase 3), so an interruption can only
    leave duplicates, never a lost track;
  * any drift from the exported baseline halts the run — the plan resolves positions to
    URIs against one specific export, so a drifted playlist makes it stale, and
    regenerating it is free where patching it in flight is guesswork;
  * phase 2.5 re-reads every add target's total and hard-gates phase 3, and its pass is
    bound to the run that earned it;
  * every phase enforces its own entry gate, so running one phase in isolation is as
    safe as running the whole sequence;
  * every call gets an `attempt` journal record before it and a `done` record only
    after a verified-successful response, so a failed call can never be mistaken for a
    completed one on resume;
  * any 429 halts the whole run (docs/spotify-rate-limits.md rule 5).

Exit codes: 0 clean · 1 phase-4 mismatch · 2 quota exhausted · 3 halted precondition ·
4 drift detected (re-export and regenerate).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

# spotipy and the vendored auth client are imported lazily (see _load_spotify) so that
# --dry-run works anywhere, with no credentials and no venv.
requests = None  # type: ignore[assignment]
SpotifyException = None  # type: ignore[assignment]


def _load_spotify():
    """Import spotipy + the vendored auth client into module globals."""
    global requests, SpotifyException
    import requests as _requests
    from spotipy.exceptions import SpotifyException as _SpotifyException
    from spotify_mcp import spotify_api

    requests = _requests
    SpotifyException = _SpotifyException
    return spotify_api


# Post-Feb-2026 dev-mode apps 429 at sustained rates near 1 req/s, and this app has
# never issued a playlist WRITE — cold-start ceilings are the low ones
# (docs/spotify-rate-limits.md).
PACE_SECONDS = 2.0
QUOTA_STOP_SECONDS = 300
PHASE4_COOLDOWN_SECONDS = 60
TRANSIENT_STATUS = (500, 502, 503, 504)
PAGE = 100

GATE_PHASE25 = "phase25_pass"

EXIT_OK = 0
EXIT_MISMATCH = 1
EXIT_QUOTA = 2
EXIT_HALT = 3
EXIT_DRIFT = 4


class QuotaExhausted(RuntimeError):
    """The day's budget is gone — checkpoint and stop, never probe again today."""


class PlanHalt(RuntimeError):
    """A precondition or gate failed; stop before touching anything else."""


class PlanDrift(RuntimeError):
    """Live playlist state no longer matches the export the plan was generated from."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #

class Journal:
    """Append-only JSONL with two records per op (attempt, then done).

    An op counts as done ONLY IF it has a done record with ok=True and success
    evidence (a snapshot_id for writes, a playlist id for creates). Anything else —
    5xx, missing evidence, attempt without done — is UNKNOWN and must be resolved by
    re-reading the playlist, never by a blind replay.

    The journal is also the only durable home for cross-invocation state: adopted
    playlist ids and gate passes are read back at startup, so invoking a single phase
    later inherits what earlier phases established.
    """

    WRITE_ACTIONS = ("add_tracks", "remove_tracks")

    def __init__(self, path: Path, config_id: str):
        self.path = path
        self.config_id = config_id
        self.records: list[dict] = []
        if path.exists():
            self._load()
            header = next((r for r in self.records if r.get("state") == "header"), None)
            if header and header.get("config_id") != config_id:
                raise PlanHalt(
                    f"journal was written for config_id {header.get('config_id')!r} but the plan is "
                    f"{config_id!r}; refusing to mix configurations — start a fresh journal"
                )
        else:
            self._write({"state": "header", "config_id": config_id,
                         "started": _now(), "pace_seconds": PACE_SECONDS})
        self.ops_recorded = max([r.get("run_op_count", 0) for r in self.records] or [0])

    def _load(self) -> None:
        """Read the journal, tolerating a torn final record.

        A kill between write() and fsync() can leave a half-written last line; that
        line describes work whose outcome is unknown either way, and the UNKNOWN path
        already re-verifies by playlist diff. An unparseable line anywhere else is a
        real corruption and must not be silently accepted.
        """
        lines = [ln for ln in self.path.read_text().splitlines() if ln.strip()]
        for i, line in enumerate(lines):
            try:
                self.records.append(json.loads(line))
            except json.JSONDecodeError:
                if i == len(lines) - 1:
                    print(f"  journal: discarding a torn final record (line {i + 1})")
                    break
                raise PlanHalt(f"journal is corrupt at line {i + 1}; refusing to resume")

    def _write(self, rec: dict) -> None:
        rec.setdefault("ts", _now())
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.records.append(rec)

    def attempt(self, seq: int, op: dict, playlist_id: str, uris: list[str] | None = None) -> None:
        self.ops_recorded += 1
        self._write({
            "seq": seq, "state": "attempt", "phase": op["phase"], "action": op["action"],
            "playlist_name": op.get("playlist_name"), "playlist_id": playlist_id,
            "chunk_index": op.get("chunk_index"), "chunks_total": op.get("chunks_total"),
            "uris": op.get("uris") if uris is None else uris,
            "pair_ids": op.get("pair_ids"), "run_op_count": self.ops_recorded,
        })

    def done(self, seq: int, *, ok: bool, http_status: int | None = None,
             snapshot_id: str | None = None, playlist_id: str | None = None,
             placeholder: bool = False, detail: str | None = None) -> None:
        """Record an outcome.

        `placeholder` marks evidence that is not a real snapshot_id — a read, or an add
        verified by diff rather than by a write — so snapshot bookkeeping can ignore it
        without having to recognise the string.
        """
        self._write({"seq": seq, "state": "done", "ok": ok, "http_status": http_status,
                     "snapshot_id": snapshot_id, "playlist_id": playlist_id,
                     "placeholder": placeholder, "detail": detail,
                     "run_op_count": self.ops_recorded})

    def note(self, kind: str, **kw) -> None:
        self._write({"state": "note", "kind": kind, **kw})

    # ---- queries -------------------------------------------------------- #

    @staticmethod
    def _has_evidence(rec: dict) -> bool:
        return bool(rec.get("snapshot_id") or rec.get("playlist_id"))

    def _attempt_rec(self, seq: int) -> dict | None:
        return next((r for r in reversed(self.records)
                     if r.get("seq") == seq and r.get("state") == "attempt"), None)

    def _successes(self):
        """Yield (attempt_record, done_record) for every verified-successful op.

        A retried op writes a second attempt/done pair, so each done is matched to the
        attempt immediately preceding it — that attempt holds the URIs actually sent.
        """
        latest: dict[int, dict] = {}
        for rec in self.records:
            if rec.get("state") == "attempt":
                latest[rec.get("seq")] = rec
            elif rec.get("state") == "done" and rec.get("ok") and self._has_evidence(rec):
                att = latest.get(rec.get("seq"))
                if att:
                    yield att, rec

    def _notes(self, kind: str):
        return [r for r in self.records if r.get("state") == "note" and r.get("kind") == kind]

    def status(self, seq: int) -> str:
        """'done' | 'unknown' | 'todo'.

        'done' demands ok=True AND success evidence, so a 5xx or an evidence-less
        response is UNKNOWN rather than complete — the one rule standing between a
        failed add and a phase-3 delete of its source copy. The LAST done record wins:
        an op that failed and was later re-executed successfully is done.
        """
        for rec in reversed(self.records):
            if rec.get("seq") == seq and rec.get("state") == "done":
                return "done" if (rec.get("ok") and self._has_evidence(rec)) else "unknown"
        return "unknown" if self._attempt_rec(seq) else "todo"

    def created_playlists(self) -> dict[str, str]:
        """{playlist name: id} for playlists this run created or adopted.

        A create's attempt record carries the NAME in playlist_id (no id exists yet);
        its done record carries the real id.
        """
        out: dict[str, str] = {}
        for att, done in self._successes():
            if att.get("action") == "create_playlist" and done.get("playlist_id"):
                out[att["playlist_id"]] = done["playlist_id"]
        for rec in self._notes("adopted_playlist"):
            out[rec["name"]] = rec["playlist_id"]
        return out

    def last_snapshot(self, playlist_id: str) -> str | None:
        """The snapshot_id our own last successful WRITE to this playlist returned."""
        snap = None
        for att, done in self._successes():
            if (att.get("playlist_id") == playlist_id
                    and att.get("action") in self.WRITE_ACTIONS
                    and done.get("snapshot_id") and not done.get("placeholder")):
                snap = done["snapshot_id"]
        return snap

    def done_write_uris(self, playlist_id: str, action: str) -> set[str]:
        """URIs this run already successfully added to / removed from a playlist."""
        out: set[str] = set()
        for att, _done in self._successes():
            if att.get("playlist_id") == playlist_id and att.get("action") == action:
                out |= set(att.get("uris") or [])
        return out

    def pending_write_uris(self, playlist_id: str, action: str) -> set[str]:
        """URIs of in-flight writes to a playlist — attempted, outcome UNKNOWN.

        Such a write may or may not have landed, so it widens what counts as an
        expected live state until the per-op diff resolves it.
        """
        out: set[str] = set()
        for seq in {r.get("seq") for r in self.records if r.get("state") == "attempt"}:
            if self.status(seq) != "unknown":
                continue
            att = self._attempt_rec(seq)
            if att and att.get("playlist_id") == playlist_id and att.get("action") == action:
                out |= set(att.get("uris") or [])
        return out

    def record_gate_pass(self, gate: str, run_id: str) -> None:
        self.note("gate_pass", gate=gate, run_id=run_id, config_id=self.config_id,
                  journal_len=len(self.records))

    def gate_passed(self, gate: str, run_id: str) -> bool:
        """Whether `gate` holds for the CURRENT run and journal state.

        A gate authorises irreversible deletes, so it is bound to the run that earned it
        and to the journal state at that moment. A pass from an earlier invocation
        proves nothing about the account now — the owner may have changed a playlist in
        between — and anything journalled afterwards other than phase-3 work means the
        verified state has moved on.
        """
        last = None
        for i, rec in enumerate(self.records):
            if (rec.get("state") == "note" and rec.get("kind") == "gate_pass"
                    and rec.get("gate") == gate and rec.get("run_id") == run_id
                    and rec.get("config_id") == self.config_id):
                last = i
        if last is None:
            return False
        phase_of_seq = {r["seq"]: r.get("phase") for r in self.records
                        if r.get("state") == "attempt" and "seq" in r}
        for rec in self.records[last + 1:]:
            if rec.get("state") == "attempt" and rec.get("phase") != 3:
                return False
            if rec.get("state") == "done" and phase_of_seq.get(rec.get("seq")) != 3:
                return False
        return True


# --------------------------------------------------------------------------- #
# API plumbing
# --------------------------------------------------------------------------- #

def _retry_after_seconds(exc) -> int | None:
    headers = getattr(exc, "headers", None) or {}
    # HTTP/2 lowercases header names; don't miss it on case.
    header = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if header is None:
        return None
    try:
        return int(header)
    except (TypeError, ValueError):
        # RFC 9110 also permits an HTTP-date. Spotify sends seconds, so an unparseable
        # value is treated as "no usable hint" rather than guessed at.
        return None


class Api:
    """Paced, 429-aware spotipy wrapper with a per-endpoint daily call counter.

    Every API call in the run goes through `call()`, so quota semantics are inherited
    rather than reimplemented per phase: a 429 either sleeps (short Retry-After) or
    raises QuotaExhausted, a transient 5xx gets one bounded retry, and every other
    failure becomes a PlanHalt so the journal records a checkpoint instead of the run
    dying on an escaped traceback.
    """

    def __init__(self, counters_path: Path, sp=None):
        if sp is None:
            spotify_api = _load_spotify()
            sp = spotify_api.Client().sp
        # spotipy wires 429 into urllib3's Retry at __init__ and urllib3 honors
        # Retry-After by sleeping INSIDE the request; rebuild the session with retries
        # zeroed so 429s surface to call() with the header intact.
        sp.retries = 0
        sp.status_retries = 0
        sp.status_forcelist = []
        if hasattr(sp, "_build_session"):
            sp._build_session()
        self.sp = sp
        self.counters_path = counters_path
        self.counters = self._load_counters()
        self._last_call = 0.0

    def _load_counters(self) -> dict:
        if self.counters_path.exists():
            try:
                return json.loads(self.counters_path.read_text())
            except json.JSONDecodeError:
                print("  counters file unreadable — starting a fresh one")
        return {}

    def _bump(self, endpoint: str) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        today = self.counters.setdefault(day, {})
        today[endpoint] = today.get(endpoint, 0) + 1
        today["_total"] = today.get("_total", 0) + 1
        # temp+rename so a kill mid-write cannot leave the quota record truncated
        tmp = self.counters_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.counters, indent=1))
        os.replace(tmp, self.counters_path)

    def calls_today(self) -> int:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return self.counters.get(day, {}).get("_total", 0)

    def call(self, endpoint: str, fn, *args, **kwargs):
        """One paced API call. Raises QuotaExhausted on any hard 429."""
        transient_budget = 1
        for _ in range(6):
            wait = PACE_SECONDS - (time.monotonic() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._bump(endpoint)
            try:
                return fn(*args, **kwargs)
            except Exception as exc:
                if requests is not None and isinstance(exc, requests.exceptions.RetryError):
                    # Only reachable if the session rebuild was skipped — the 429
                    # response and its Retry-After are already consumed.
                    raise QuotaExhausted("internal retries exhausted on 429") from exc
                if requests is not None and isinstance(exc, requests.exceptions.RequestException):
                    raise PlanHalt(f"{endpoint} transport failure: {exc}") from exc
                if SpotifyException is None or not isinstance(exc, SpotifyException):
                    raise
                status = getattr(exc, "http_status", None)
                if status == 429:
                    retry_after = _retry_after_seconds(exc)
                    if retry_after is None:
                        # Community consensus: a 429 without a usable Retry-After means
                        # the daily quota is gone; probing further risks extending it.
                        raise QuotaExhausted("429 with no usable Retry-After header — day over")
                    if retry_after > QUOTA_STOP_SECONDS:
                        raise QuotaExhausted(f"Retry-After {retry_after}s — daily quota exhausted")
                    print(f"    429 with Retry-After {retry_after}s — sleeping", flush=True)
                    time.sleep(retry_after)
                elif status in TRANSIENT_STATUS and transient_budget > 0:
                    transient_budget -= 1
                    print(f"    {status} — one bounded retry", flush=True)
                    time.sleep(PACE_SECONDS * 2)
                else:
                    # A 403 on create is the expected shape if playlist writes turn out
                    # to be outside this dev-mode app's permitted endpoint set.
                    raise PlanHalt(f"{endpoint} failed with HTTP {status}: {exc}") from exc
            finally:
                self._last_call = time.monotonic()
        raise PlanHalt(f"{endpoint} exhausted its retry patience")


def fetch_uris(api: Api, playlist_id: str) -> list[str]:
    """Every track URI in a playlist, in order (100/page — the cheapest read).

    Uses the same stored-identity rule as scripts/export_playlists.py: with a market
    context Spotify relinks stale URIs and returns the playable replacement, while
    `linked_from` holds what the playlist actually stores. Removals and diffs must key
    on the stored identity, so `linked_from.uri` wins when present.
    """
    uris: list[str] = []
    offset = 0
    while True:
        page = api.call("playlist_items", api.sp.playlist_items, playlist_id,
                        limit=PAGE, offset=offset, market="from_token")
        items = (page or {}).get("items") or []
        for item in items:
            track = item.get("item") or item.get("track") or {}
            linked = track.get("linked_from") or {}
            uri = linked.get("uri") or track.get("uri")
            if uri:
                uris.append(uri)
        offset += len(items)
        if not items or not (page or {}).get("next"):
            return uris


def fetch_snapshot_total(api: Api, playlist_id: str) -> tuple[str | None, int | None]:
    info = api.call("playlist", api.sp.playlist, playlist_id, fields="snapshot_id,tracks.total")
    return (info or {}).get("snapshot_id"), ((info or {}).get("tracks") or {}).get("total")


def _write_snapshot(api: Api, endpoint: str, fn, *args, **kwargs) -> str:
    """One write, returning the snapshot_id that proves it landed."""
    res = api.call(endpoint, fn, *args, **kwargs)
    snap = (res or {}).get("snapshot_id")
    if not snap:
        raise PlanHalt(f"{endpoint} returned no snapshot_id — refusing to journal it as done")
    return snap


def resolve_pid(op: dict, created: dict[str, str]) -> str:
    pid = op["playlist_id_or_name"]
    if pid.startswith("<"):
        name = op.get("playlist_name") or ""
        if name not in created:
            raise PlanHalt(f"playlist {name!r} has not been created yet — run phase 1 first")
        return created[name]
    return pid


def _confirm(message: str, assume_yes: bool) -> None:
    if assume_yes:
        print(f"  ({message} — accepted via --yes)")
        return
    if not sys.stdin.isatty():
        raise PlanHalt(f"{message}: stdin is not a terminal — rerun with --yes to acknowledge")
    try:
        reply = input(f"  {message}? [yes/NO] ").strip().lower()
    except EOFError:
        raise PlanHalt(f"{message}: no input available — rerun with --yes to acknowledge")
    if reply != "yes":
        raise PlanHalt("halted by operator")


# --------------------------------------------------------------------------- #
# dry run
# --------------------------------------------------------------------------- #

def dry_run(plan: dict) -> None:
    print(f"DRY RUN — config_id {plan['config_id']}")
    print(f"  {len(plan['ops'])} ops, chunk size {plan['chunk_size']}, "
          f"pace {plan.get('pacing_seconds', PACE_SECONDS)}s")
    print(f"  add ops {plan['op_totals']['add_ops']}, remove ops {plan['op_totals']['remove_ops']}")
    print()
    calls = 0
    for phase in (0, 1, 2, 2.5, 3, 4):
        ops = [(seq, o) for seq, o in enumerate(plan["ops"]) if o["phase"] == phase]
        if not ops:
            continue
        pcalls = sum(o.get("calls", 0) for _, o in ops)
        calls += pcalls
        print(f"--- phase {phase}: {len(ops)} ops, {pcalls} call(s) ---")
        for seq, op in ops:
            target = op.get("playlist_name") or op["playlist_id_or_name"]
            bits = [f"[{seq:3d}]", f"{op['action']:<20}", f"{target:<24}"]
            if "count" in op:
                bits.append(f"{op['count']:3d} uri(s)")
            if op.get("chunks_total"):
                bits.append(f"chunk {op['chunk_index'] + 1}/{op['chunks_total']}")
            if op.get("expected_total") is not None:
                bits.append(f"expect total {op['expected_total']}")
            if op.get("canary"):
                bits.append("** CANARY **")
            print("  " + "  ".join(bits))
        print()
    print(f"total calls: {calls}  (~{calls * PACE_SECONDS / 60:.1f} min at {PACE_SECONDS}s/call)")
    print("no API calls were made")


# --------------------------------------------------------------------------- #
# phases
# --------------------------------------------------------------------------- #

def phase0(api: Api, plan: dict, journal: Journal, resume: bool) -> None:
    """Verify every playlist still matches the baseline the plan was generated from.

    Any divergence raises PlanDrift. Every mutation moves `snapshot_id`, so comparing
    it (plus the count) detects drift completely without reading a single track.
    """
    print("=== phase 0 — drift check ===")
    drifted: list[dict] = []
    for seq, op in enumerate(plan["ops"]):
        if op["phase"] != 0 or op["action"] != "snapshot_check":
            continue
        name, pid = op["playlist_name"], op["playlist_id_or_name"]
        journal.attempt(seq, op, pid)
        snap, total = fetch_snapshot_total(api, pid)
        journal.done(seq, ok=True, snapshot_id=snap, placeholder=True, detail=f"total={total}")

        # On a resume our own writes have already moved the snapshot and the count, so
        # the baseline is the export adjusted by what the journal says we did. An
        # in-flight write whose outcome is UNKNOWN may or may not have landed, so it
        # widens the acceptable total into a range and makes the snapshot uninformative
        # — phase 2's per-op diff is what resolves it.
        added = len(journal.done_write_uris(pid, "add_tracks")) if resume else 0
        removed = len(journal.done_write_uris(pid, "remove_tracks")) if resume else 0
        pending_add = len(journal.pending_write_uris(pid, "add_tracks")) if resume else 0
        pending_rm = len(journal.pending_write_uris(pid, "remove_tracks")) if resume else 0
        expected_snap = (journal.last_snapshot(pid) if resume else None) \
            or op["cold_start_expected_snapshot_id"]
        expected_total = op["cold_start_expected_total"] + added - removed

        live = None
        if pending_add or pending_rm:
            live = fetch_uris(api, pid)
            if not _unexplained_by_pending(plan, journal, name, pid, live):
                print(f"  {name:<20} in-flight write pending — contents explained")
                continue
        elif snap == expected_snap and total == expected_total:
            print(f"  {name:<20} unchanged")
            continue

        detail = _classify_drift(api, plan, journal, name, pid, total, expected_total, resume,
                                 live=live)
        detail.update({"playlist": name, "playlist_id": pid,
                       "expected_snapshot_id": expected_snap, "live_snapshot_id": snap})
        drifted.append(detail)
        print(f"  {name:<20} DRIFT — {'; '.join(detail['classes'])}")

    if drifted:
        journal.note("drift_detected", playlists=[d["playlist"] for d in drifted], detail=drifted)
        lines = []
        for d in drifted:
            lines.append(f"    · {d['playlist']}: {'; '.join(d['classes'])}")
            for uri in d["vanished"][:5]:
                lines.append(f"        vanished removal target {uri}")
            for uri in d["duplicated"][:5]:
                lines.append(f"        removal target duplicated {uri}")
        raise PlanDrift("playlist state drifted — re-export and regenerate the plan\n"
                        + "\n".join(lines))


def _export_baseline(plan: dict, name: str) -> Counter:
    """The playlist's contents at export time, as a multiset.

    Reconstructed from the plan rather than carried separately: the end state is the
    baseline minus the planned removals plus the planned adds, so inverting that gives
    the baseline back exactly.
    """
    baseline = Counter(plan["expected_end_state"][name])
    baseline -= Counter(e["uri"] for e in plan["add_detail"].get(name, []))
    baseline += Counter(e["uri"] for e in plan["remove_detail"].get(name, []))
    return baseline


def _unexplained_by_pending(plan: dict, journal: Journal, name: str, pid: str,
                            live: list[str]) -> dict[str, int]:
    """Differences between live contents and what the journal can account for.

    A write whose outcome is UNKNOWN makes the playlist's *count* ambiguous but not its
    *content*: the runner knows exactly which URIs that op would have written, so each
    URI may differ from the journal-adjusted baseline by at most what the pending op
    would explain. Anything else is an owner edit, including count-neutral ones that a
    range check on the total cannot see.
    """
    expected = _export_baseline(plan, name)
    expected += Counter(journal.done_write_uris(pid, "add_tracks"))
    expected -= Counter(journal.done_write_uris(pid, "remove_tracks"))
    slack_add = Counter(journal.pending_write_uris(pid, "add_tracks"))
    slack_rm = Counter(journal.pending_write_uris(pid, "remove_tracks"))
    live_counts = Counter(live)
    unexplained: dict[str, int] = {}
    for uri in set(expected) | set(live_counts) | set(slack_add) | set(slack_rm):
        delta = live_counts[uri] - expected[uri]
        if not -slack_rm[uri] <= delta <= slack_add[uri]:
            unexplained[uri] = delta
    return unexplained


def _classify_drift(api: Api, plan: dict, journal: Journal, name: str, pid: str,
                    total: int | None, expected_total: int, resume: bool,
                    live: list[str] | None = None) -> dict:
    """Describe how a playlist diverged, so the halt message is actionable.

    Costs one paged read of an already-doomed run; the alternative is telling the owner
    only that something changed.
    """
    classes = ["snapshot mismatch"]
    if total != expected_total:
        classes.append(f"count {total} vs expected {expected_total}")
    if live is None:
        live = fetch_uris(api, pid)
    planned = [entry["uri"] for entry in plan["remove_detail"].get(name, [])]
    ours = (journal.done_write_uris(pid, "remove_tracks")
            | journal.pending_write_uris(pid, "remove_tracks")) if resume else set()
    vanished = [u for u in planned if u not in live and u not in ours]
    duplicated = [u for u in set(planned) if live.count(u) > 1]
    if vanished:
        classes.append(f"{len(vanished)} removal target(s) vanished")
    if duplicated:
        classes.append(f"{len(duplicated)} removal target(s) duplicated")
    return {"classes": classes, "vanished": vanished, "duplicated": duplicated,
            "live_total": total}


def phase1(api: Api, plan: dict, journal: Journal, assume_yes: bool) -> dict[str, str]:
    print("=== phase 1 — create playlists ===")
    created = journal.created_playlists()
    for seq, op in enumerate(plan["ops"]):
        if op["phase"] != 1 or op["action"] != "create_playlist":
            continue
        name = op["playlist_id_or_name"]
        state = journal.status(seq)
        if state == "done" and name in created:
            print(f"  {name:<24} already created ({created[name]})")
            continue
        if state == "unknown":
            # The id was never journalled, so a blind replay would make a second
            # playlist with the same name. Adopt an empty one instead.
            print(f"  {name:<24} dangling create — searching for an existing empty one")
            found = _find_empty_playlist(api, name)
            if found:
                created[name] = found
                journal.note("adopted_playlist", name=name, playlist_id=found)
                print(f"    adopted {found}")
                continue
        body = op["body"]
        journal.attempt(seq, op, name)
        res = api.call("create_playlist", api.sp.current_user_playlist_create,
                       body["name"], public=body["public"], description=body["description"])
        pid = (res or {}).get("id")
        if not pid:
            journal.done(seq, ok=False, detail="no playlist id in response")
            raise PlanHalt(f"create {name!r} returned no playlist id")
        journal.done(seq, ok=True, http_status=201, playlist_id=pid)
        created[name] = pid
        print(f"  {name:<24} created {pid}")
        if op.get("canary"):
            _confirm(f"first playlist created ({pid}) — confirm it looks right in Spotify",
                     assume_yes)
    return created


def _find_empty_playlist(api: Api, name: str) -> str | None:
    """An owner-owned, empty playlist of this name, or None.

    Ownership matters: current_user_playlists also returns followed playlists, and
    adopting someone else's empty playlist would 403 every subsequent add.
    """
    me_id = (api.call("me", api.sp.me) or {}).get("id")
    offset = 0
    while True:
        page = api.call("me_playlists", api.sp.current_user_playlists, limit=50, offset=offset)
        items = (page or {}).get("items") or []
        for item in items:
            if (item.get("name") == name
                    and ((item.get("tracks") or {}).get("total") == 0)
                    and ((item.get("owner") or {}).get("id") == me_id)):
                return item.get("id")
        offset += len(items)
        if not items or not (page or {}).get("next"):
            return None


def phase2(api: Api, plan: dict, journal: Journal, created: dict[str, str],
           assume_yes: bool) -> None:
    print("=== phase 2 — adds ===")
    missing = sorted({p["name"] for p in plan["new_playlists"]} - set(created))
    if missing:
        raise PlanHalt(f"phase 2 gate: these playlists do not exist yet: {missing} — run phase 1")

    for seq, op in enumerate(plan["ops"]):
        if op["phase"] != 2 or op["action"] != "add_tracks":
            continue
        pid = resolve_pid(op, created)
        label = f"{op['playlist_name']} {op['chunk_index'] + 1}/{op['chunks_total']}"
        if journal.status(seq) == "done":
            print(f"  {label:<30} already done")
            continue

        uris = list(op["uris"])
        if journal.status(seq) == "unknown":
            # Never blind-replay an add: a replay appends a second copy.
            live = fetch_uris(api, pid)
            if all(live.count(u) == 1 for u in uris):
                journal.done(seq, ok=True, snapshot_id="verified-by-diff", placeholder=True,
                             detail="all uris already present exactly once")
                print(f"  {label:<30} verified present by diff")
                continue
            uris = [u for u in uris if live.count(u) == 0]
            print(f"  {label:<30} partial — adding {len(uris)} missing uri(s)")
            if not uris:
                raise PlanHalt(f"{label}: some URIs are present more than once — a prior replay "
                               "duplicated them; resolve by hand before continuing")

        journal.attempt(seq, op, pid, uris=uris)
        snap = _write_snapshot(api, "add_tracks", api.sp.playlist_add_items, pid, uris)
        journal.done(seq, ok=True, http_status=201, snapshot_id=snap)
        print(f"  {label:<30} +{len(uris)}")
        if op.get("canary"):
            _confirm(f"first track write landed in {op['playlist_name']} (snapshot {snap})",
                     assume_yes)


def phase25(api: Api, plan: dict, journal: Journal, created: dict[str, str],
            run_id: str) -> None:
    """The hard gate for phase 3: every add target's total must match.

    Phase 0 has already proved the export baseline is the live baseline, so the only
    other movement to account for is removals this run already completed — which is
    what makes resuming mid-phase-3 possible.
    """
    print("=== phase 2.5 — verify every add landed (HARD GATE for phase 3) ===")
    # The journal check is free, so it runs first: a gate invoked before phase 2 has
    # finished fails without spending 17 calls proving what the journal already knows.
    unfinished = [op["playlist_name"] for seq, op in enumerate(plan["ops"])
                  if op["phase"] == 2 and op["action"] == "add_tracks"
                  and journal.status(seq) != "done"]
    if unfinished:
        raise PlanHalt(f"phase 2.5 gate failed: {len(unfinished)} phase-2 add op(s) are not done "
                       f"({sorted(set(unfinished))}) — finish phase 2 before phase 3")

    failures = []
    for seq, op in enumerate(plan["ops"]):
        if op["phase"] != 2.5 or op["action"] != "verify_total":
            continue
        pid = resolve_pid(op, created)
        name = op["playlist_name"]
        removed = len(journal.done_write_uris(pid, "remove_tracks"))
        expected = op["expected_total"] - removed
        journal.attempt(seq, op, pid)
        _, total = fetch_snapshot_total(api, pid)
        ok = total == expected
        journal.done(seq, ok=True, snapshot_id="n/a-read", placeholder=True,
                     detail=f"total={total} expected={expected}")
        adjust = f" (less {removed} already removed)" if removed else ""
        print(f"  {name:<24} {total:>4} / {expected:<4}{adjust} {'ok' if ok else 'MISMATCH'}")
        if not ok:
            failures.append((name, total, expected))
    if failures:
        raise PlanHalt(f"phase 2.5 gate failed: {failures} — finish the adds before phase 3")
    journal.record_gate_pass(GATE_PHASE25, run_id)
    print("  gate passed")


def phase3(api: Api, plan: dict, journal: Journal, created: dict[str, str],
           run_id: str) -> None:
    print("=== phase 3 — removals ===")
    # Phase 3 is the only irreversible phase, so it proves its own precondition rather
    # than trusting the caller's phase sequencing.
    if not journal.gate_passed(GATE_PHASE25, run_id):
        print("  no phase-2.5 pass for this run — running the gate now")
        phase25(api, plan, journal, created, run_id)

    for seq, op in enumerate(plan["ops"]):
        if op["phase"] != 3 or op["action"] != "remove_tracks":
            continue
        name, pid = op["playlist_name"], resolve_pid(op, created)
        label = f"{name} {op['chunk_index'] + 1}/{op['chunks_total']}"
        if journal.status(seq) == "done":
            print(f"  {label:<30} already done")
            continue
        uris = list(op["uris"])
        journal.attempt(seq, op, pid, uris=uris)
        snap = _write_snapshot(api, "remove_tracks",
                               api.sp.playlist_remove_all_occurrences_of_items, pid, uris)
        journal.done(seq, ok=True, http_status=200, snapshot_id=snap)
        print(f"  {label:<30} -{len(uris)}")


def phase4(api: Api, plan: dict, journal: Journal, created: dict[str, str]) -> int:
    print("=== phase 4 — verification ===")
    expected = plan["expected_end_state"]
    new_names = {p["name"] for p in plan["new_playlists"]}
    problems = 0
    for seq, op in enumerate(plan["ops"]):
        if op["phase"] != 4 or op["action"] != "reexport_verify":
            continue
        name = op["playlist_name"]
        pid = resolve_pid(op, created)
        journal.attempt(seq, op, pid)
        live = fetch_uris(api, pid)
        want = expected[name]
        ok = live == want if name in new_names else sorted(live) == sorted(want)
        mode = "ordered" if name in new_names else "multiset"
        journal.done(seq, ok=True, snapshot_id="n/a-read", placeholder=True,
                     detail=f"{mode} match={ok} live={len(live)} expected={len(want)}")
        print(f"  {name:<24} {len(live):>4} / {len(want):<4} {mode:<9} "
              f"{'ok' if ok else 'MISMATCH'}")
        if not ok:
            problems += 1
            missing = [u for u in want if u not in live]
            extra = [u for u in live if u not in want]
            journal.note("verify_mismatch", playlist=name, missing=missing[:20],
                         extra=extra[:20], missing_count=len(missing), extra_count=len(extra))
    return problems


# --------------------------------------------------------------------------- #

def run(plan: dict, args, sp=None) -> int:
    journal_path = args.journal or args.plan.parent / "apply_journal.jsonl"
    counters_path = args.plan.parent / "apply_run_counters.json"
    run_id = uuid.uuid4().hex
    journal = None
    api = None
    try:
        journal = Journal(journal_path, plan["config_id"])
        api = Api(counters_path, sp=sp)
        journal.note("run_start", run_id=run_id, phase=args.phase, resume=args.resume)
        print(f"plan {args.plan.name} · config {plan['config_id']} · journal {journal_path.name}")
        print(f"run {run_id[:8]} · pace {PACE_SECONDS}s/call · budget "
              f"{plan['budget']['total']['calls_expected']} calls "
              f"({plan['budget']['total']['write_calls']} writes)\n")

        wanted = args.phase
        created = journal.created_playlists()
        if wanted in (None, 0):
            phase0(api, plan, journal, args.resume)
        if wanted in (None, 1):
            created = phase1(api, plan, journal, args.yes)
        if wanted in (None, 2):
            phase2(api, plan, journal, created, args.yes)
        if wanted in (None, 2.5):
            phase25(api, plan, journal, created, run_id)
        if wanted in (None, 3):
            phase3(api, plan, journal, created, run_id)
        if wanted in (None, 4):
            if wanted is None:
                print(f"\ncooling down {PHASE4_COOLDOWN_SECONDS}s before verification\n")
                time.sleep(PHASE4_COOLDOWN_SECONDS)
            problems = phase4(api, plan, journal, created)
            journal.note("run_complete", mismatches=problems, calls_today=api.calls_today())
            print(f"\n{'FAILED' if problems else 'OK'} — {problems} playlist mismatch(es); "
                  f"{api.calls_today()} calls today (see apply_run_counters.json)")
            return EXIT_MISMATCH if problems else EXIT_OK
        return EXIT_OK
    except PlanDrift as exc:
        print(f"\nDRIFT — {exc}\n"
              "  Re-export the affected playlists and regenerate apply_plan.json: the plan's "
              "positions and counts are only valid against the export it was built from.")
        return EXIT_DRIFT
    except QuotaExhausted as exc:
        calls = api.calls_today() if api else 0
        if journal:
            journal.note("quota_exhausted", reason=str(exc), calls_today=calls)
        print(f"\nQUOTA EXHAUSTED ({exc}) — checkpointed after {calls} calls today "
              f"(see apply_run_counters.json). Do not call again today; "
              f"rerun with --resume tomorrow.")
        return EXIT_QUOTA
    except PlanHalt as exc:
        calls = api.calls_today() if api else 0
        if journal:
            journal.note("halted", reason=str(exc), calls_today=calls)
        print(f"\nHALTED — {exc}")
        return EXIT_HALT


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", type=Path, help="apply_plan.json")
    ap.add_argument("--journal", type=Path, default=None,
                    help="journal path (default: <plan dir>/apply_journal.jsonl)")
    ap.add_argument("--resume", action="store_true",
                    help="resume from the journal; phase 0 compares against journalled state")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the full op sequence and exit; makes zero API calls")
    ap.add_argument("--phase", type=float, default=None, help="run a single phase and stop")
    ap.add_argument("--yes", action="store_true", help="skip the interactive canary gates")
    args = ap.parse_args()

    plan = json.loads(args.plan.read_text())
    if plan.get("hard_check_errors"):
        # The generator's own invariants failed — most likely a half-applied toggle,
        # which is the one edit that can delete both copies of a recording.
        print(f"HALTED — plan carries {len(plan['hard_check_errors'])} hard-check error(s); "
              "regenerate it before executing:")
        for err in plan["hard_check_errors"]:
            print(f"  · {err}")
        sys.exit(EXIT_HALT)

    if args.dry_run:
        dry_run(plan)
        sys.exit(EXIT_OK)

    sys.exit(run(plan, args))


if __name__ == "__main__":
    main()
