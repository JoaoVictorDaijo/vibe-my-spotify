"""Execute apply_plan.json against Spotify — journalled, resumable, 429-safe.

    cd spotify-mcp && uv run python ../scripts/apply_plan_runner.py <plan.json> \
        [--journal PATH] [--resume] [--dry-run] [--phase N] [--yes]

Talks to raw spotipy (via the vendored server's auth client), never the MCP tools:
the MCP write tools re-chunk internally, gate removals behind an elicitation
prompt, and keep no journal — none of which is acceptable for 57 irreversible
writes.

Safety model (see the plan's own apply_plan.md):
  * adds (phase 2) strictly precede removals (phase 3), so an interruption can
    only ever leave duplicates, never a lost track;
  * phase 2.5 re-reads every add target's total and HARD-GATES phase 3;
  * every call gets an `attempt` journal record before it and a `done` record
    only after a verified-successful response, so a failed call can never be
    mistaken for a completed one on resume;
  * any 429 halts the whole run (docs/spotify-rate-limits.md rule 5).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# spotipy and the vendored auth client are imported lazily (see _load_spotify) so
# that --dry-run works anywhere, with no credentials and no venv.
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


# Post-Feb-2026 dev-mode apps 429 at sustained rates near 1 req/s, and this app
# has never issued a playlist WRITE — cold-start ceilings are the low ones
# (docs/spotify-rate-limits.md). 2.0s is the plan's chosen pace.
PACE_SECONDS = 2.0
QUOTA_STOP_SECONDS = 300
PHASE4_COOLDOWN_SECONDS = 60
OK_TRANSIENT = (500, 502, 503, 504)
PAGE = 100


class QuotaExhausted(RuntimeError):
    """The day's budget is gone — checkpoint and stop, never probe again today."""


class PlanHalt(RuntimeError):
    """A precondition or gate failed; stop before touching anything else."""


# --------------------------------------------------------------------------- #
# journal
# --------------------------------------------------------------------------- #

class Journal:
    """Append-only JSONL with two records per op (attempt, then done).

    An op counts as done ONLY IF it has a done record with ok=True and success
    evidence (a snapshot_id for writes, a playlist id for creates). Anything
    else — 5xx, missing evidence, attempt without done — is UNKNOWN and must be
    resolved by re-reading the playlist, never by a blind replay.
    """

    def __init__(self, path: Path, config_id: str):
        self.path = path
        self.config_id = config_id
        self.records: list[dict] = []
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line:
                    self.records.append(json.loads(line))
            header = next((r for r in self.records if r.get("state") == "header"), None)
            if header and header.get("config_id") != config_id:
                raise PlanHalt(
                    f"journal was written for config_id {header.get('config_id')!r} but the plan is "
                    f"{config_id!r}; refusing to mix configurations — start a fresh journal"
                )
        else:
            self._write({"state": "header", "config_id": config_id,
                         "started": _now(), "pace_seconds": PACE_SECONDS})
        self.calls = max([r.get("run_call_count", 0) for r in self.records] or [0])

    def _write(self, rec: dict) -> None:
        rec.setdefault("ts", _now())
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        self.records.append(rec)

    def attempt(self, seq: int, op: dict) -> None:
        self.calls += 1
        self._write({
            "seq": seq, "state": "attempt", "phase": op["phase"], "action": op["action"],
            "playlist_name": op.get("playlist_name"), "playlist_id": op["playlist_id_or_name"],
            "chunk_index": op.get("chunk_index"), "chunks_total": op.get("chunks_total"),
            "uris": op.get("uris"), "pair_ids": op.get("pair_ids"),
            "run_call_count": self.calls,
        })

    def done(self, seq: int, *, ok: bool, http_status: int | None = None,
             snapshot_id: str | None = None, playlist_id: str | None = None,
             detail: str | None = None) -> None:
        self._write({"seq": seq, "state": "done", "ok": ok, "http_status": http_status,
                     "snapshot_id": snapshot_id, "playlist_id": playlist_id,
                     "detail": detail, "run_call_count": self.calls})

    def note(self, kind: str, **kw) -> None:
        self._write({"state": "note", "kind": kind, **kw})

    # ---- queries -------------------------------------------------------- #

    @staticmethod
    def _has_evidence(rec: dict) -> bool:
        return bool(rec.get("snapshot_id") or rec.get("playlist_id"))

    def status(self, seq: int) -> str:
        """'done' | 'unknown' | 'todo'.

        'done' demands ok=True AND success evidence, so a 5xx or an evidence-less
        response is UNKNOWN rather than complete — the one rule standing between a
        failed add and a phase-3 delete of its source copy.
        """
        for rec in self.records:
            if rec.get("seq") == seq and rec.get("state") == "done":
                return "done" if (rec.get("ok") and self._has_evidence(rec)) else "unknown"
        return "unknown" if self._attempt(seq) else "todo"

    def _attempt(self, seq: int) -> dict | None:
        return next((r for r in self.records
                     if r.get("seq") == seq and r.get("state") == "attempt"), None)

    def _successes(self):
        """Yield (attempt_record, done_record) for every verified-successful op."""
        for rec in self.records:
            if rec.get("state") != "done" or not rec.get("ok") or not self._has_evidence(rec):
                continue
            att = self._attempt(rec.get("seq"))
            if att:
                yield att, rec

    def created_playlists(self) -> dict[str, str]:
        """{playlist name: id} for playlists this run created or adopted.

        A create's attempt record carries the NAME in playlist_id (the plan has no
        id yet); its done record carries the real id.
        """
        out: dict[str, str] = {}
        for att, done in self._successes():
            if att.get("action") == "create_playlist" and done.get("playlist_id"):
                out[att["playlist_id"]] = done["playlist_id"]
        for rec in self.records:
            if rec.get("state") == "note" and rec.get("kind") == "adopted_playlist":
                out[rec["name"]] = rec["playlist_id"]
        return out

    WRITE_ACTIONS = ("add_tracks", "remove_tracks", "remove_specific")

    def last_snapshot(self, playlist_id: str) -> str | None:
        """The snapshot_id our own last successful WRITE to this playlist returned.

        Read ops journal placeholder evidence, so only write actions count here —
        otherwise a verification read would be mistaken for a mutation.
        """
        snap = None
        for att, done in self._successes():
            if (att.get("playlist_id") == playlist_id
                    and att.get("action") in self.WRITE_ACTIONS
                    and done.get("snapshot_id")
                    and not done["snapshot_id"].startswith(("n/a", "verified"))):
                snap = done["snapshot_id"]
        return snap

    def done_write_uris(self, playlist_id: str, action: str) -> set[str]:
        """URIs this run already successfully added to / removed from a playlist."""
        out: set[str] = set()
        for att, _done in self._successes():
            if att.get("playlist_id") == playlist_id and att.get("action") == action:
                out |= set(att.get("uris") or [])
        return out


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# API plumbing
# --------------------------------------------------------------------------- #

class Api:
    """Paced, 429-aware spotipy wrapper with a per-endpoint call counter."""

    def __init__(self, journal: Journal, counters_path: Path):
        spotify_api = _load_spotify()
        sp = spotify_api.Client().sp
        # spotipy wires 429 into urllib3's Retry at __init__ and urllib3 honors
        # Retry-After by sleeping INSIDE the request; rebuild the session with
        # retries zeroed so 429s surface to call() with the header intact.
        sp.retries = 0
        sp.status_retries = 0
        sp.status_forcelist = []
        if hasattr(sp, "_build_session"):
            sp._build_session()
        self.sp = sp
        self.journal = journal
        self.counters_path = counters_path
        self.counters = self._load_counters()
        self._last_call = 0.0

    def _load_counters(self) -> dict:
        if self.counters_path.exists():
            return json.loads(self.counters_path.read_text())
        return {}

    def _bump(self, endpoint: str) -> None:
        day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.counters.setdefault(day, {})
        self.counters[day][endpoint] = self.counters[day].get(endpoint, 0) + 1
        self.counters[day]["_total"] = self.counters[day].get("_total", 0) + 1
        self.counters_path.write_text(json.dumps(self.counters, indent=1))

    def call(self, endpoint: str, fn, *args, **kwargs):
        """One paced API call. Raises QuotaExhausted on any hard 429."""
        wait = PACE_SECONDS - (time.monotonic() - self._last_call)
        if wait > 0:
            time.sleep(wait)
        self._bump(endpoint)
        try:
            result = fn(*args, **kwargs)
        except requests.exceptions.RetryError as exc:
            # Only reachable if the session rebuild above was skipped — the 429
            # response (and its Retry-After) is already consumed at this point.
            raise QuotaExhausted("internal retries exhausted on 429") from exc
        finally:
            self._last_call = time.monotonic()
        return result


def handle_spotify_error(exc: SpotifyException) -> None:
    """Translate a SpotifyException into halt/quota semantics, or re-raise."""
    if exc.http_status != 429:
        raise exc
    headers = getattr(exc, "headers", None) or {}
    # HTTP/2 lowercases header names; don't miss it on case.
    header = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if header is None:
        # Community consensus: a 429 without Retry-After means the daily quota
        # is gone; probing further only risks extending the penalty.
        raise QuotaExhausted("429 with no Retry-After header — day over")
    retry_after = int(header)
    if retry_after > QUOTA_STOP_SECONDS:
        raise QuotaExhausted(f"Retry-After {retry_after}s — daily quota exhausted")
    print(f"  429 with Retry-After {retry_after}s — sleeping", flush=True)
    time.sleep(retry_after)


def fetch_uris(api: Api, playlist_id: str) -> list[str]:
    """Every track URI in a playlist, in order (100/page — the cheapest read).

    Uses the same stored-identity rule as scripts/export_playlists.py: with a
    market context Spotify relinks stale URIs and returns the playable
    replacement, while `linked_from` holds what the playlist actually stores.
    Removals and diffs must key on the stored identity, so `linked_from.uri`
    wins when present — otherwise a relinked row would look like a mismatch.
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
    info = api.call("playlist", api.sp.playlist, playlist_id,
                    fields="snapshot_id,tracks.total")
    return (info or {}).get("snapshot_id"), ((info or {}).get("tracks") or {}).get("total")


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
        ops = [o for o in plan["ops"] if o["phase"] == phase]
        if not ops:
            continue
        pcalls = sum(o.get("calls", 0) for o in ops)
        calls += pcalls
        print(f"--- phase {phase}: {len(ops)} ops, {pcalls} call(s) ---")
        for seq, op in enumerate(plan["ops"]):
            if op["phase"] != phase:
                continue
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

def resolve_pid(op: dict, created: dict[str, str]) -> str:
    pid = op["playlist_id_or_name"]
    if pid.startswith("<"):
        name = op.get("playlist_name") or ""
        if name not in created:
            raise PlanHalt(f"playlist {name!r} has not been created yet — run phase 1 first")
        return created[name]
    return pid


def phase0(api: Api, plan: dict, journal: Journal, resume: bool) -> dict[str, str]:
    """Drift check. Returns {playlist_name: 'positional'} for playlists that must
    not use bare-URI removal."""
    print("=== phase 0 — drift check ===")
    positional: dict[str, str] = {}
    removes = plan["remove_detail"]
    for seq, op in enumerate(plan["ops"]):
        if op["phase"] != 0 or op["action"] != "snapshot_check":
            continue
        name, pid = op["playlist_name"], op["playlist_id_or_name"]
        journal.attempt(seq, op)
        try:
            snap, total = fetch_snapshot_total(api, pid)
        except SpotifyException as exc:
            handle_spotify_error(exc)
            snap, total = fetch_snapshot_total(api, pid)
        journal.done(seq, ok=True, snapshot_id=snap, detail=f"total={total}")

        # M4: on a resume, our own writes have already moved the snapshot, so the
        # cold-start export values are stale by construction. Compare against the
        # journal instead.
        journalled = journal.last_snapshot(pid) if resume else None
        expected_snap = journalled or op["cold_start_expected_snapshot_id"]
        if snap == expected_snap:
            print(f"  {name:<20} unchanged")
            continue

        print(f"  {name:<20} DRIFT (snapshot {expected_snap} -> {snap}, total {total})")
        live = fetch_uris(api, pid)
        # M3: bare-URI DELETE removes every occurrence, so a newly duplicated URI
        # would take the owner's fresh copy with it. Re-check and fall back.
        targets = [r["uri"] for r in removes.get(name, [])]
        dupes = [u for u in set(targets) if live.count(u) > 1]
        if dupes:
            positional[name] = "duplicate URI present after drift"
            print(f"    {len(dupes)} removal target(s) now appear twice — switching {name} to "
                  f"snapshot-guarded POSITIONAL removal")
            journal.note("positional_fallback", playlist=name, uris=dupes)
        already_removed = journal.done_write_uris(pid, "remove_tracks") if resume else set()
        missing = [u for u in targets if u not in live and u not in already_removed]
        if missing:
            journal.note("drift_missing_uris", playlist=name, uris=missing)
            print(f"    {len(missing)} removal target(s) vanished with no done-op — their ops and "
                  f"paired partners must be dropped (see journal)")
    return positional


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
            # S2: the id was never journalled, so a blind replay would make a
            # second playlist with the same name. Adopt an empty one instead.
            print(f"  {name:<24} dangling create — searching for an existing empty one")
            found = _find_empty_playlist(api, name)
            if found:
                created[name] = found
                journal.note("adopted_playlist", name=name, playlist_id=found)
                print(f"    adopted {found}")
                continue
        body = op["body"]
        journal.attempt(seq, op)
        try:
            res = api.call("create_playlist", api.sp.current_user_playlist_create,
                           body["name"], public=body["public"], description=body["description"])
        except SpotifyException as exc:
            handle_spotify_error(exc)
            res = api.call("create_playlist", api.sp.current_user_playlist_create,
                           body["name"], public=body["public"], description=body["description"])
        pid = (res or {}).get("id")
        if not pid:
            journal.done(seq, ok=False, detail="no playlist id in response")
            raise PlanHalt(f"create {name!r} returned no playlist id")
        journal.done(seq, ok=True, http_status=201, playlist_id=pid)
        created[name] = pid
        print(f"  {name:<24} created {pid}")
        if op.get("canary") and not assume_yes:
            _canary_gate(f"first playlist created ({pid}). Confirm it looks right in Spotify")
    return created


def _find_empty_playlist(api: Api, name: str) -> str | None:
    offset = 0
    while True:
        page = api.call("me_playlists", api.sp.current_user_playlists, limit=50, offset=offset)
        items = (page or {}).get("items") or []
        for it in items:
            if it.get("name") == name and ((it.get("tracks") or {}).get("total") == 0):
                return it.get("id")
        offset += 50
        if offset >= ((page or {}).get("total") or 0):
            return None


def _canary_gate(message: str) -> None:
    print(f"\n  CANARY STOP — {message}.")
    reply = input("  continue? [yes/NO] ").strip().lower()
    if reply != "yes":
        raise PlanHalt("halted at canary gate by operator")


def phase2(api: Api, plan: dict, journal: Journal, created: dict[str, str],
           assume_yes: bool) -> None:
    print("=== phase 2 — adds ===")
    for seq, op in enumerate(plan["ops"]):
        if op["phase"] != 2 or op["action"] != "add_tracks":
            continue
        pid = resolve_pid(op, created)
        label = f"{op['playlist_name']} {op['chunk_index'] + 1}/{op['chunks_total']}"
        state = journal.status(seq)
        if state == "done":
            print(f"  {label:<30} already done")
            continue
        uris = list(op["uris"])
        if state == "unknown":
            # M1: never blind-replay an add. Diff first.
            live = fetch_uris(api, pid)
            present = [u for u in uris if u in live]
            if len(present) == len(uris):
                journal.done(seq, ok=True, snapshot_id="verified-by-diff",
                             detail="all uris already present")
                print(f"  {label:<30} verified present by diff")
                continue
            uris = [u for u in uris if u not in live]
            print(f"  {label:<30} partial — adding {len(uris)} missing uri(s)")
        journal.attempt(seq, dict(op, uris=uris))
        snap = _write_with_retry(api, "add_tracks", api.sp.playlist_add_items, pid, uris)
        journal.done(seq, ok=True, http_status=201, snapshot_id=snap)
        print(f"  {label:<30} +{len(uris)}")
        if op.get("canary") and not assume_yes:
            _canary_gate(f"first track write landed in {op['playlist_name']} (snapshot {snap})")


def phase25(api: Api, plan: dict, journal: Journal, created: dict[str, str]) -> None:
    """M2: the hard gate for phase 3. Every add target's total must match."""
    print("=== phase 2.5 — verify every add landed (HARD GATE for phase 3) ===")
    failures = []
    for seq, op in enumerate(plan["ops"]):
        if op["phase"] != 2.5 or op["action"] != "verify_total":
            continue
        pid = resolve_pid(op, created)
        journal.attempt(seq, op)
        try:
            _, total = fetch_snapshot_total(api, pid)
        except SpotifyException as exc:
            handle_spotify_error(exc)
            _, total = fetch_snapshot_total(api, pid)
        ok = total == op["expected_total"]
        journal.done(seq, ok=True, snapshot_id="n/a-read", detail=f"total={total} expected={op['expected_total']}")
        flag = "ok" if ok else "MISMATCH"
        print(f"  {op['playlist_name']:<24} {total:>4} / {op['expected_total']:<4} {flag}")
        if not ok:
            failures.append((op["playlist_name"], total, op["expected_total"]))
    # all phase-2 ops must also be done with success evidence
    for seq, op in enumerate(plan["ops"]):
        if op["phase"] == 2 and op["action"] == "add_tracks" and journal.status(seq) != "done":
            failures.append((op["playlist_name"], "phase-2 op not done", seq))
    if failures:
        raise PlanHalt(f"phase 2.5 gate failed: {failures} — finish the adds before phase 3")
    print("  gate passed")


def phase3(api: Api, plan: dict, journal: Journal, positional: dict[str, str]) -> None:
    print("=== phase 3 — removals ===")
    for seq, op in enumerate(plan["ops"]):
        if op["phase"] != 3 or op["action"] != "remove_tracks":
            continue
        name, pid = op["playlist_name"], op["playlist_id_or_name"]
        label = f"{name} {op['chunk_index'] + 1}/{op['chunks_total']}"
        if journal.status(seq) == "done":
            print(f"  {label:<30} already done")
            continue
        uris = list(op["uris"])
        journal.attempt(seq, op)
        if name in positional:
            snap, _ = fetch_snapshot_total(api, pid)
            live = fetch_uris(api, pid)
            items = [{"uri": u, "positions": [i for i, x in enumerate(live) if x == u]}
                     for u in uris if u in live]
            new_snap = _write_with_retry(api, "remove_specific",
                                         api.sp.playlist_remove_specific_occurrences_of_items,
                                         pid, items, snapshot_id=snap)
            journal.done(seq, ok=True, http_status=200, snapshot_id=new_snap,
                         detail="positional removal (drifted playlist)")
            print(f"  {label:<30} -{len(items)} (positional)")
            continue
        new_snap = _write_with_retry(api, "remove_tracks",
                                     api.sp.playlist_remove_all_occurrences_of_items, pid, uris)
        journal.done(seq, ok=True, http_status=200, snapshot_id=new_snap)
        print(f"  {label:<30} -{len(uris)}")


def _write_with_retry(api: Api, endpoint: str, fn, *args, **kwargs) -> str:
    """One write, returning its snapshot_id.

    A 429 gets the repo's halt/sleep semantics (and a short sleep does not consume
    the 5xx budget). A transient 5xx gets exactly one bounded retry — safe here
    only because the caller has already diff-verified anything ambiguous.
    """
    transient_retries = 1
    for _ in range(6):
        try:
            res = api.call(endpoint, fn, *args, **kwargs)
        except SpotifyException as exc:
            if exc.http_status in OK_TRANSIENT:
                if transient_retries <= 0:
                    raise PlanHalt(f"{endpoint} still failing with {exc.http_status} after a "
                                   "bounded retry")
                transient_retries -= 1
                print(f"    {exc.http_status} — one bounded retry")
                time.sleep(PACE_SECONDS * 2)
                continue
            handle_spotify_error(exc)   # sleeps on a short Retry-After, else raises
            continue
        snap = (res or {}).get("snapshot_id")
        if not snap:
            raise PlanHalt(f"{endpoint} returned no snapshot_id — refusing to journal it as done")
        return snap
    raise PlanHalt(f"{endpoint} exhausted its retry patience")


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
        journal.attempt(seq, op)
        live = fetch_uris(api, pid)
        want = expected[name]
        if name in new_names:
            ok = live == want                      # S3: order matters for the new playlists
            mode = "ordered"
        else:
            ok = sorted(live) == sorted(want)
            mode = "multiset"
        journal.done(seq, ok=True, snapshot_id="n/a-read",
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

def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("plan", type=Path, help="apply_plan.json")
    ap.add_argument("--journal", type=Path, default=None,
                    help="journal path (default: <plan dir>/apply_journal.jsonl)")
    ap.add_argument("--resume", action="store_true",
                    help="resume from the journal; phase 0 compares against journalled snapshots")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the full op sequence and exit; makes zero API calls")
    ap.add_argument("--phase", type=float, default=None, help="run a single phase and stop")
    ap.add_argument("--yes", action="store_true", help="skip the interactive canary gates")
    args = ap.parse_args()

    plan = json.loads(args.plan.read_text())
    if plan.get("hard_check_errors"):
        # The generator's own invariants failed — most likely a half-applied
        # toggle, which is the one edit that can delete both copies of a
        # recording. Refuse even to dry-run it.
        print(f"HALTED — plan carries {len(plan['hard_check_errors'])} hard-check error(s); "
              "regenerate it before executing:")
        for err in plan["hard_check_errors"]:
            print(f"  · {err}")
        sys.exit(3)

    if args.dry_run:
        dry_run(plan)
        return

    journal_path = args.journal or args.plan.parent / "apply_journal.jsonl"
    journal = Journal(journal_path, plan["config_id"])
    api = Api(journal, args.plan.parent / "apply_run_counters.json")
    print(f"plan {args.plan.name} · config {plan['config_id']} · journal {journal_path.name}")
    print(f"pace {PACE_SECONDS}s/call · budget {plan['budget']['total']['calls_expected']} calls "
          f"({plan['budget']['total']['write_calls']} writes)\n")

    wanted = args.phase
    try:
        positional: dict[str, str] = {}
        created: dict[str, str] = journal.created_playlists()
        if wanted in (None, 0):
            positional = phase0(api, plan, journal, args.resume)
        if wanted in (None, 1):
            created = phase1(api, plan, journal, args.yes)
        if wanted in (None, 2):
            phase2(api, plan, journal, created, args.yes)
        if wanted in (None, 2.5):
            phase25(api, plan, journal, created)
        if wanted in (None, 3):
            phase3(api, plan, journal, positional)
        if wanted in (None, 4):
            if wanted is None:
                print(f"\ncooling down {PHASE4_COOLDOWN_SECONDS}s before verification\n")
                time.sleep(PHASE4_COOLDOWN_SECONDS)
            problems = phase4(api, plan, journal, created)
            journal.note("run_complete", mismatches=problems, calls=journal.calls)
            print(f"\n{'FAILED' if problems else 'OK'} — {problems} playlist mismatch(es), "
                  f"{journal.calls} calls this run")
            sys.exit(1 if problems else 0)
    except QuotaExhausted as exc:
        journal.note("quota_exhausted", reason=str(exc), calls=journal.calls)
        print(f"\nQUOTA EXHAUSTED ({exc}) — checkpointed after {journal.calls} calls. "
              f"Do not call again today; rerun with --resume tomorrow.")
        sys.exit(2)
    except PlanHalt as exc:
        journal.note("halted", reason=str(exc), calls=journal.calls)
        print(f"\nHALTED — {exc}")
        sys.exit(3)


if __name__ == "__main__":
    main()
