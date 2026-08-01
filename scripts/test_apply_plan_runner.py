"""Self-contained tests for apply_plan_runner against an in-memory fake Spotify.

    python3 scripts/test_apply_plan_runner.py [-v]

No pytest, no credentials, no network. The plan fixture is synthesised here rather
than read from a data directory so the suite runs anywhere the repo is checked out.

The fixture deliberately reproduces the structural shapes of the real plan that a
2-playlist toy would miss, because each one hides a class of defect:

  * a URI that is a removal target in TWO playlists (`aDUP`) — a URI-keyed decision
    applied without playlist scope silently spares or deletes the wrong copy;
  * unpaired overlay copies whose URI is a removal target elsewhere (`a000`, `a200`
    in Overlay) — a copy must survive its source's removal;
  * a playlist that is both an add target and a removal target (`PL_DST`) — the
    phase-2.5 expectation has to account for removals already journalled, which is
    what makes resuming mid-phase-3 possible.

What the assertions are load-bearing for: every case guards a path that deletes or
duplicates owner data. The resume cases pin the rule that a `done` record without
success evidence is UNKNOWN — a weaker "was it attempted?" check still passes the
happy path but re-adds or drops tracks on resume. The gate cases pin that a phase-2.5
pass authorises deletions only for the run that earned it; a check that merely looks
for any historical pass still passes the happy path.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import types
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory

RUNNER_PATH = Path(__file__).resolve().parent / "apply_plan_runner.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("apply_plan_runner", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load_runner()


# --------------------------------------------------------------------------- #
# fake Spotify
# --------------------------------------------------------------------------- #

class FakeSpotifyException(Exception):
    def __init__(self, http_status, headers=None):
        super().__init__(f"fake {http_status}")
        self.http_status = http_status
        self.headers = headers or {}


class FakeRetryError(Exception):
    pass


WRITE_ENDPOINTS = ("create_playlist", "add_tracks", "remove_tracks")


class FakeSpotify:
    """Minimal stand-in for the spotipy surface the runner touches."""

    def __init__(self, store: dict[str, list[str]], owner="me"):
        self.store = {pid: list(uris) for pid, uris in store.items()}
        self.names = {pid: pid for pid in store}
        self.owner = owner
        self.owners = {pid: owner for pid in store}
        self.snap = {pid: f"snap-{pid}-0" for pid in store}
        self._rev = {pid: 0 for pid in store}
        self.calls: list[tuple[str, str]] = []
        self.faults: dict[str, list] = {}
        self._created = 0
        # spotipy attributes the session rebuild touches
        self.retries = 3
        self.status_retries = 3
        self.status_forcelist = [429]

    def _build_session(self):
        pass

    def fail_once(self, endpoint: str, exc):
        self.faults.setdefault(endpoint, []).append(exc)

    def writes(self) -> list[tuple[str, str]]:
        return [c for c in self.calls if c[0] in WRITE_ENDPOINTS]

    def _maybe_fail(self, endpoint: str, pid=""):
        self.calls.append((endpoint, pid))
        queue = self.faults.get(endpoint)
        if queue:
            raise queue.pop(0)

    def touch(self, pid):
        """Move the snapshot without changing content, as any edit would."""
        self._rev[pid] += 1
        self.snap[pid] = f"snap-{pid}-{self._rev[pid]}"
        return self.snap[pid]

    # ---- reads ---- #
    def playlist(self, pid, fields=None):
        """The Feb-2026 playlist object: `tracks` is present but empty.

        No track count is reachable here under any `fields` expression, and the filter
        returns only the requested paths that actually exist — asking for
        `tracks.total` yields a response with no `tracks` key at all.
        """
        self._maybe_fail("playlist", pid)
        full = {"id": pid, "name": self.names[pid], "snapshot_id": self.snap[pid],
                "owner": {"id": self.owners[pid]}, "tracks": {}}
        if fields is None:
            return full
        out = {}
        for path in fields.split(","):
            head = path.strip().split(".")[0]
            if head in full and full[head] not in ({}, None):
                out[head] = full[head]
        return out

    def playlist_items(self, pid, limit=100, offset=0, market=None):
        self._maybe_fail("playlist_items", pid)
        page = self.store[pid][offset:offset + limit]
        return {"items": [{"item": {"uri": u}} for u in page],
                "next": "more" if offset + limit < len(self.store[pid]) else None,
                "total": len(self.store[pid])}

    def me(self):
        self._maybe_fail("me")
        return {"id": self.owner}

    def current_user_playlists(self, limit=50, offset=0):
        self._maybe_fail("me_playlists")
        rows = [{"id": pid, "name": self.names[pid], "tracks": {"total": len(uris)},
                 "owner": {"id": self.owners[pid]}}
                for pid, uris in self.store.items()]
        return {"items": rows[offset:offset + limit],
                "next": "more" if offset + limit < len(rows) else None}

    # ---- writes ---- #
    def current_user_playlist_create(self, name, public=True, collaborative=False, description=""):
        self._maybe_fail("create_playlist", name)
        self._created += 1
        pid = f"new{self._created}"
        self.store[pid] = []
        self.names[pid] = name
        self.owners[pid] = self.owner
        self._rev[pid] = 0
        self.snap[pid] = f"snap-{pid}-0"
        return {"id": pid, "name": name}

    def playlist_add_items(self, pid, items, position=None):
        self._maybe_fail("add_tracks", pid)
        self.store[pid].extend(items)
        return {"snapshot_id": self.touch(pid)}

    def playlist_remove_all_occurrences_of_items(self, pid, items, snapshot_id=None):
        self._maybe_fail("remove_tracks", pid)
        drop = set(items)
        self.store[pid] = [u for u in self.store[pid] if u not in drop]
        return {"snapshot_id": self.touch(pid)}


def install_fakes():
    """Point the runner's lazily-imported Spotify names at the fakes."""
    runner.SpotifyException = FakeSpotifyException
    runner.requests = types.SimpleNamespace(
        exceptions=types.SimpleNamespace(RetryError=FakeRetryError,
                                         RequestException=ConnectionError))
    runner.PACE_SECONDS = 0.0
    runner.PHASE4_COOLDOWN_SECONDS = 0
    runner.time.sleep = lambda _s: None


install_fakes()


# --------------------------------------------------------------------------- #
# plan fixture
# --------------------------------------------------------------------------- #

def A(n):
    return f"spotify:track:a{n:03d}"


DUP = "spotify:track:aDUP"           # a removal target in PL_SRC *and* PL_ALT

EXISTING = {
    "PL_SRC": [A(i) for i in range(10)] + [DUP],
    "PL_ALT": [A(i) for i in range(200, 205)] + [DUP],
    "PL_DST": [A(i) for i in range(100, 105)],
}

ADD_DETAIL = {
    "NewOne": [{"uri": A(9), "label": "claimed", "reason": "founding claim", "pair_id": "P04"}],
    "Overlay": [{"uri": A(0), "label": "copy", "reason": "overlay copy", "pair_id": None},
                {"uri": A(200), "label": "copy", "reason": "overlay copy", "pair_id": None}],
    "PL_DST": [{"uri": A(0), "label": "m0", "reason": "move-in", "pair_id": "P01"},
               {"uri": A(1), "label": "m1", "reason": "move-in", "pair_id": "P02"},
               {"uri": A(2), "label": "m2", "reason": "move-in", "pair_id": "P03"}],
}
REMOVE_DETAIL = {
    "PL_SRC": [{"uri": A(0), "label": "m0", "reason": "move-out", "pair_id": "P01"},
               {"uri": A(1), "label": "m1", "reason": "move-out", "pair_id": "P02"},
               {"uri": A(2), "label": "m2", "reason": "move-out", "pair_id": "P03"},
               {"uri": A(9), "label": "claimed", "reason": "claimed by NewOne", "pair_id": "P04"},
               {"uri": A(5), "label": "dedup", "reason": "dedup SAME verdict", "pair_id": None},
               {"uri": DUP, "label": "shared", "reason": "dedup SAME verdict", "pair_id": None}],
    "PL_ALT": [{"uri": DUP, "label": "shared", "reason": "dedup SAME verdict", "pair_id": None},
               {"uri": A(200), "label": "overlaid", "reason": "claimed by Overlay",
                "pair_id": None}],
    "PL_DST": [{"uri": A(100), "label": "dedup", "reason": "dedup SAME verdict", "pair_id": None}],
}
NEW_PLAYLISTS = ["NewOne", "Overlay"]


def make_plan() -> dict:
    ops = []
    for pid in ("PL_SRC", "PL_ALT", "PL_DST"):
        ops.append({"phase": 0, "action": "snapshot_check", "playlist_id_or_name": pid,
                    "playlist_name": pid, "calls": 1,
                    "cold_start_expected_snapshot_id": f"snap-{pid}-0",
                    "cold_start_expected_total": len(EXISTING[pid])})
    for i, name in enumerate(NEW_PLAYLISTS):
        ops.append({"phase": 1, "action": "create_playlist", "playlist_id_or_name": name,
                    "playlist_name": name, "calls": 1, "canary": i == 0,
                    "body": {"name": name, "public": False, "collaborative": False,
                             "description": ""},
                    "expected_track_count": len(ADD_DETAIL[name])})
    first = True
    for name in NEW_PLAYLISTS + ["PL_DST"]:
        entries = ADD_DETAIL[name]
        target = f"<created: {name}>" if name in NEW_PLAYLISTS else name
        ops.append({"phase": 2, "action": "add_tracks", "playlist_id_or_name": target,
                    "playlist_name": name, "uris": [e["uri"] for e in entries],
                    "count": len(entries), "chunk_index": 0, "chunks_total": 1, "calls": 1,
                    "pair_ids": [e["pair_id"] for e in entries], "canary": first})
        first = False
    for name in NEW_PLAYLISTS + ["PL_DST"]:
        target = f"<created: {name}>" if name in NEW_PLAYLISTS else name
        before = len(EXISTING.get(name, []))
        ops.append({"phase": 2.5, "action": "verify_total", "playlist_id_or_name": target,
                    "playlist_name": name, "calls": 1,
                    "expected_total": before + len(ADD_DETAIL[name])})
    for name in ("PL_SRC", "PL_ALT", "PL_DST"):
        entries = REMOVE_DETAIL[name]
        ops.append({"phase": 3, "action": "remove_tracks", "playlist_id_or_name": name,
                    "playlist_name": name, "uris": [e["uri"] for e in entries],
                    "count": len(entries), "chunk_index": 0, "chunks_total": 1, "calls": 1,
                    "pair_ids": [e["pair_id"] for e in entries]})
    end = {}
    for name in ("PL_SRC", "PL_ALT", "PL_DST"):
        dropped = {e["uri"] for e in REMOVE_DETAIL[name]}
        end[name] = ([u for u in EXISTING[name] if u not in dropped]
                     + [e["uri"] for e in ADD_DETAIL.get(name, [])])
    for name in NEW_PLAYLISTS:
        end[name] = [e["uri"] for e in ADD_DETAIL[name]]
    for name in ("PL_SRC", "PL_ALT", "PL_DST", *NEW_PLAYLISTS):
        target = f"<created: {name}>" if name in NEW_PLAYLISTS else name
        ops.append({"phase": 4, "action": "reexport_verify", "playlist_id_or_name": target,
                    "playlist_name": name, "calls": 1, "expected_total": len(end[name])})
    return {
        "config_id": "test-config", "chunk_size": 40, "pacing_seconds": 0.0,
        "hard_check_errors": [],
        "playlists": {n: {"id": n, "snapshot_id": f"snap-{n}-0", "total": len(v)}
                      for n, v in EXISTING.items()},
        "new_playlists": [{"name": n, "public": False, "description": "",
                           "expected_tracks": len(ADD_DETAIL[n])} for n in NEW_PLAYLISTS],
        "op_totals": {"add_ops": sum(len(v) for v in ADD_DETAIL.values()),
                      "remove_ops": sum(len(v) for v in REMOVE_DETAIL.values())},
        "budget": {"total": {"calls_expected": len(ops), "write_calls": 8}},
        "ops": ops, "expected_end_state": end,
        "add_detail": ADD_DETAIL, "remove_detail": REMOVE_DETAIL,
    }


class Args:
    def __init__(self, plan_path, **kw):
        self.plan = plan_path
        self.journal = kw.get("journal")
        self.resume = kw.get("resume", False)
        self.dry_run = kw.get("dry_run", False)
        self.phase = kw.get("phase")
        self.yes = kw.get("yes", True)


def run_plan(tmp: Path, sp: FakeSpotify, **kw) -> tuple[int, str]:
    plan = kw.pop("plan", None) or make_plan()
    plan_path = tmp / "apply_plan.json"
    plan_path.write_text(json.dumps(plan))
    buf = io.StringIO()
    with redirect_stdout(buf):
        code = runner.run(plan, Args(plan_path, **kw), sp=sp)
    return code, buf.getvalue()


def fresh():
    tmp = TemporaryDirectory()
    return tmp, Path(tmp.name), FakeSpotify(EXISTING)


def journal_lines(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in (path / "apply_journal.jsonl").read_text().splitlines() if ln]


def named_store(sp: FakeSpotify) -> dict[str, list[str]]:
    return {sp.names[pid]: uris for pid, uris in sp.store.items()}


# --------------------------------------------------------------------------- #
# tests
# --------------------------------------------------------------------------- #

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))


def test_happy_path():
    tmp, path, sp = fresh()
    with tmp:
        code, out = run_plan(path, sp)
        end, want = named_store(sp), make_plan()["expected_end_state"]
        check("happy path: exit 0", code == 0, out[-300:])
        check("happy path: account matches expected_end_state", end == want, str(end))
        check("happy path: a URI removed in two playlists leaves both",
              DUP not in end["PL_SRC"] and DUP not in end["PL_ALT"])
        check("happy path: overlay copies survive their sources' removal",
              end["Overlay"] == [A(0), A(200)], str(end["Overlay"]))


def test_resume_shapes():
    plan = make_plan()
    add_seq = next(i for i, o in enumerate(plan["ops"])
                   if o["phase"] == 2 and o["playlist_name"] == "PL_DST")
    op = plan["ops"][add_seq]

    tmp, path, sp = fresh()
    with tmp:
        j = runner.Journal(path / "apply_journal.jsonl", "test-config")
        j.attempt(add_seq, op, "PL_DST")
        j.done(add_seq, ok=False, http_status=500)
        check("resume: done+5xx reads UNKNOWN", j.status(add_seq) == "unknown")
        code, out = run_plan(path, sp, journal=path / "apply_journal.jsonl")
        check("resume: done+5xx re-adds and reconciles",
              code == 0 and sp.store["PL_DST"].count(A(0)) == 1, f"exit {code} {out[-300:]}")

    tmp, path, sp = fresh()
    with tmp:
        j = runner.Journal(path / "apply_journal.jsonl", "test-config")
        j.attempt(add_seq, op, "PL_DST")
        j.done(add_seq, ok=True, http_status=201, snapshot_id=None)
        check("resume: done without evidence reads UNKNOWN", j.status(add_seq) == "unknown")

    tmp, path, sp = fresh()
    with tmp:
        sp.store["PL_DST"].extend([A(0), A(1), A(2)])
        sp.touch("PL_DST")
        j = runner.Journal(path / "apply_journal.jsonl", "test-config")
        j.attempt(add_seq, op, "PL_DST")
        j.done(add_seq, ok=True, snapshot_id=sp.snap["PL_DST"])
        check("resume: a done record with a real snapshot reads done",
              runner.Journal(path / "apply_journal.jsonl", "test-config").status(add_seq) == "done")

    tmp, path, sp = fresh()
    with tmp:
        sp.store["PL_DST"].extend([A(0), A(1), A(2)])
        snap = sp.touch("PL_DST")
        jp = path / "apply_journal.jsonl"
        j = runner.Journal(jp, "test-config")
        j.attempt(add_seq, op, "PL_DST")
        j.done(0, ok=True, snapshot_id=snap, placeholder=True)  # phase-0 style read record
        check("resume: dangling attempt with no done reads UNKNOWN", j.status(add_seq) == "unknown")
        code, out = run_plan(path, sp, journal=jp, resume=True)
        check("resume: a landed call is verified by diff, not re-added",
              sp.store["PL_DST"].count(A(0)) == 1 and "verified present by diff" in out,
              out[-400:])

    tmp, path, sp = fresh()
    with tmp:
        sp.store["PL_DST"].append(A(0))
        sp.touch("PL_DST")
        jp = path / "apply_journal.jsonl"
        j = runner.Journal(jp, "test-config")
        j.attempt(add_seq, op, "PL_DST")
        code, out = run_plan(path, sp, journal=jp, resume=True)
        counts = [sp.store["PL_DST"].count(A(i)) for i in (0, 1, 2)]
        check("resume: a partial landing adds only the missing", counts == [1, 1, 1], str(counts))

    tmp, path, sp = fresh()
    with tmp:
        j = runner.Journal(path / "apply_journal.jsonl", "test-config")
        check("resume: an untouched op reads todo", j.status(add_seq) == "todo")


def test_placeholder_evidence_is_not_a_write_snapshot():
    tmp, path, sp = fresh()
    with tmp:
        j = runner.Journal(path / "apply_journal.jsonl", "test-config")
        # An add_tracks op, so the action filter cannot satisfy the assertion on its
        # own: only the placeholder flag stops a diff-verified add from being taken as
        # a real write snapshot and poisoning resume-mode drift comparison.
        add_op = {"phase": 2, "action": "add_tracks", "playlist_name": "PL_DST"}
        j.attempt(0, add_op, "PL_DST")
        j.done(0, ok=True, snapshot_id="verified-by-diff", placeholder=True)
        check("a diff-verified add does not pose as a write snapshot",
              j.last_snapshot("PL_DST") is None, str(j.last_snapshot("PL_DST")))
        j.attempt(1, add_op, "PL_DST")
        j.done(1, ok=True, snapshot_id="snap-real-1")
        check("a real write snapshot is still reported",
              j.last_snapshot("PL_DST") == "snap-real-1", str(j.last_snapshot("PL_DST")))


def test_dangling_create_adopts():
    tmp, path, sp = fresh()
    with tmp:
        sp.store["ghost"] = []
        sp.names["ghost"] = "NewOne"
        sp.owners["ghost"] = sp.owner
        sp.snap["ghost"] = "snap-ghost-0"
        sp._rev["ghost"] = 0
        plan = make_plan()
        create_seq = next(i for i, o in enumerate(plan["ops"]) if o["phase"] == 1)
        j = runner.Journal(path / "apply_journal.jsonl", "test-config")
        j.attempt(create_seq, plan["ops"][create_seq], "NewOne")
        run_plan(path, sp, journal=path / "apply_journal.jsonl")
        named = [pid for pid, nm in sp.names.items() if nm == "NewOne"]
        check("dangling create: adopts the empty playlist instead of duplicating",
              len(named) == 1 and sp.store["ghost"] == [A(9)], str(named))


def test_create_adoption_skips_foreign_playlist():
    tmp, path, sp = fresh()
    with tmp:
        sp.store["theirs"] = []
        sp.names["theirs"] = "NewOne"
        sp.owners["theirs"] = "someone-else"
        sp.snap["theirs"] = "snap-theirs-0"
        sp._rev["theirs"] = 0
        plan = make_plan()
        create_seq = next(i for i, o in enumerate(plan["ops"]) if o["phase"] == 1)
        j = runner.Journal(path / "apply_journal.jsonl", "test-config")
        j.attempt(create_seq, plan["ops"][create_seq], "NewOne")
        run_plan(path, sp, journal=path / "apply_journal.jsonl")
        check("create adoption: a playlist owned by someone else is not adopted",
              sp.store["theirs"] == [] and A(9) in sp.store.get("new1", []))


# ---- drift is a hard halt ---- #

def _drift_case(name: str, mutate, expect_in_output=None):
    tmp, path, sp = fresh()
    with tmp:
        mutate(sp)
        before = {pid: list(u) for pid, u in sp.store.items()}
        code, out = run_plan(path, sp)
        check(f"drift ({name}): exits 4", code == runner.EXIT_DRIFT, f"exit {code} {out[-300:]}")
        check(f"drift ({name}): halts before any write", sp.writes() == [], str(sp.writes()))
        check(f"drift ({name}): account untouched",
              {pid: u for pid, u in sp.store.items()} == before)
        if expect_in_output:
            check(f"drift ({name}): message names the cause", expect_in_output in out,
                  out[-400:])
        check(f"drift ({name}): tells the operator to regenerate",
              "re-export and regenerate the plan" in out, out[-300:])


def test_drift_snapshot_only():
    _drift_case("snapshot moved, same content", lambda sp: sp.touch("PL_SRC"),
                "snapshot mismatch")


def test_drift_count_change():
    def mutate(sp):
        sp.store["PL_SRC"].append("spotify:track:zzzNEW")
        sp.touch("PL_SRC")
    _drift_case("owner added a track", mutate, "count 12 vs expected 11")


def test_drift_vanished_removal_target():
    def mutate(sp):
        sp.store["PL_SRC"].remove(A(0))
        sp.touch("PL_SRC")
    _drift_case("removal target vanished", mutate, "vanished removal target")


def test_drift_duplicated_removal_target():
    def mutate(sp):
        sp.store["PL_SRC"].append(A(0))
        sp.touch("PL_SRC")
    _drift_case("removal target duplicated", mutate, "removal target duplicated")


def test_drift_on_an_add_target():
    def mutate(sp):
        sp.store["PL_DST"].append("spotify:track:zzzNEW")
        sp.touch("PL_DST")
    _drift_case("drifted playlist is also an add target", mutate, "PL_DST")


# ---- the phase-3 gate ---- #

def _count_neutral_owner_edit(sp: FakeSpotify) -> None:
    """Delete a removal target and duplicate another track: contents move, count does not."""
    sp.store["PL_DST"].remove(A(100))
    sp.store["PL_DST"].append(A(101))
    sp.touch("PL_DST")


def test_drift_count_neutral_with_pending_write():
    tmp, path, sp = fresh()
    with tmp:
        plan = make_plan()
        add_seq = next(i for i, o in enumerate(plan["ops"])
                       if o["phase"] == 2 and o["playlist_name"] == "PL_DST")
        jp = path / "apply_journal.jsonl"
        j = runner.Journal(jp, "test-config")
        j.attempt(add_seq, plan["ops"][add_seq], "PL_DST")   # in-flight, outcome UNKNOWN
        _count_neutral_owner_edit(sp)
        check("count-neutral edit leaves the total unchanged, so a range check is blind",
              len(sp.store["PL_DST"]) == 5, str(len(sp.store["PL_DST"])))
        code, out = run_plan(path, sp, journal=jp, resume=True)
        check("drift (count-neutral, pending write): exits 4 rather than passing as in-flight",
              code == runner.EXIT_DRIFT, f"exit {code} {out[-400:]}")
        check("drift (count-neutral, pending write): halts before any write",
              sp.writes() == [], str(sp.writes()))
        check("drift (count-neutral, pending write): names the vanished removal target",
              "vanished removal target" in out, out[-400:])


def test_drift_count_neutral_control_without_pending_write():
    tmp, path, sp = fresh()
    with tmp:
        _count_neutral_owner_edit(sp)
        code, out = run_plan(path, sp)
        check("drift (count-neutral, no pending write): exits 4",
              code == runner.EXIT_DRIFT, f"exit {code} {out[-300:]}")
        check("drift (count-neutral, no pending write): halts before any write",
              sp.writes() == [], str(sp.writes()))


def test_pending_write_that_landed_is_still_explained():
    tmp, path, sp = fresh()
    with tmp:
        plan = make_plan()
        add_seq = next(i for i, o in enumerate(plan["ops"])
                       if o["phase"] == 2 and o["playlist_name"] == "PL_DST")
        jp = path / "apply_journal.jsonl"
        j = runner.Journal(jp, "test-config")
        j.attempt(add_seq, plan["ops"][add_seq], "PL_DST")
        sp.store["PL_DST"].extend([A(0), A(1), A(2)])        # the in-flight add landed
        sp.touch("PL_DST")
        code, out = run_plan(path, sp, journal=jp, resume=True)
        check("a landed in-flight write is explained, not reported as drift",
              code == 0 and "contents explained" in out, f"exit {code} {out[:600]}")
        check("the explained resume still reaches the expected end state",
              named_store(sp) == make_plan()["expected_end_state"], str(named_store(sp)))


def test_playlist_object_carries_no_track_count():
    """The old `playlist(...)["tracks"]["total"]` read is unavailable, by any route."""
    tmp, path, sp = fresh()
    with tmp:
        full = sp.playlist("PL_SRC")
        check("api shape: the playlist object's tracks dict is empty",
              full["tracks"] == {}, str(full))
        check("api shape: reading a count from the playlist object yields nothing",
              (full.get("tracks") or {}).get("total") is None)
        filtered = sp.playlist("PL_SRC", fields="snapshot_id,tracks.total")
        check("api shape: a fields filter omits tracks entirely",
              "tracks" not in filtered and filtered["snapshot_id"] == sp.snap["PL_SRC"],
              str(filtered))
        check("api shape: the items endpoint is the only source of the count",
              sp.playlist_items("PL_SRC", limit=1)["total"] == len(sp.store["PL_SRC"]))
        check("runner reads the snapshot from the playlist object",
              runner.fetch_snapshot(runner.Api(path / "c.json", sp=sp), "PL_SRC")
              == sp.snap["PL_SRC"])
        check("runner reads the count from the items endpoint",
              runner.fetch_total(runner.Api(path / "c.json", sp=sp), "PL_SRC")
              == len(sp.store["PL_SRC"]))


def test_unreadable_field_is_an_error_not_drift():
    """A field the API stops serving must never be reported as an owner edit."""
    class NoSnapshot(FakeSpotify):
        def playlist(self, pid, fields=None):
            self._maybe_fail("playlist", pid)
            return {}

    class NoTotal(FakeSpotify):
        def playlist_items(self, pid, limit=100, offset=0, market=None):
            page = super().playlist_items(pid, limit, offset, market)
            page.pop("total")
            return page

    for label, cls in (("snapshot", NoSnapshot), ("count", NoTotal)):
        tmp = TemporaryDirectory()
        with tmp:
            path, sp = Path(tmp.name), cls(EXISTING)
            code, out = run_plan(path, sp)
            check(f"unreadable {label}: halts as an error, not drift",
                  code == runner.EXIT_HALT, f"exit {code} {out[-300:]}")
            check(f"unreadable {label}: says read failure, not drift",
                  "read failure, not drift" in out or "read failure" in out, out[-300:])
            check(f"unreadable {label}: does not tell the owner to re-export",
                  "re-export and regenerate the plan" not in out, out[-300:])
            check(f"unreadable {label}: issues no writes", sp.writes() == [], str(sp.writes()))


def test_drift_message_reports_only_observed_causes():
    """A cause the run did not observe must not appear — it sends the owner hunting."""
    tmp, path, sp = fresh()
    with tmp:
        sp.store["PL_SRC"].append("spotify:track:zzzNEW")   # count moves, snapshot does not
        code, out = run_plan(path, sp)
        check("drift message: a count-only divergence is reported as such",
              code == runner.EXIT_DRIFT and "count 12 vs expected 11" in out, out[-300:])
        check("drift message: does not claim a snapshot mismatch that did not happen",
              "snapshot mismatch" not in out, out[-400:])


def test_phase3_gate_refuses_without_phase25():
    tmp, path, sp = fresh()
    with tmp:
        before = {pid: list(u) for pid, u in sp.store.items()}
        code, out = run_plan(path, sp, phase=3)
        check("phase 3 gate: refuses on a fresh journal", code == runner.EXIT_HALT, f"exit {code}")
        check("phase 3 gate: nothing was deleted", sp.store == before)
        check("phase 3 gate: names the unmet phase-2 precondition",
              "phase-2 add op(s) are not done" in out, out[-300:])


def test_phase3_gate_reruns_25_in_a_new_invocation():
    tmp, path, sp = fresh()
    with tmp:
        jp = path / "apply_journal.jsonl"
        for phase in (0, 1, 2, 2.5):
            run_plan(path, sp, journal=jp, phase=phase)
        passes = [r for r in journal_lines(path) if r.get("kind") == "gate_pass"]
        check("gate: phase 2.5 records a pass bound to its run",
              len(passes) == 1 and passes[0]["run_id"] and passes[0]["config_id"] == "test-config",
              str(passes))
        code, out = run_plan(path, sp, journal=jp, phase=3)
        check("gate: a separate invocation re-runs 2.5 rather than trusting the old pass",
              "no phase-2.5 pass for this run" in out, out[:400])
        check("gate: after the re-run the removals proceed",
              code == 0 and A(0) not in sp.store["PL_SRC"], f"exit {code} {out[-300:]}")


def test_stale_gate_does_not_authorise_deletion():
    tmp, path, sp = fresh()
    with tmp:
        jp = path / "apply_journal.jsonl"
        for phase in (0, 1, 2, 2.5):
            run_plan(path, sp, journal=jp, phase=phase)
        # Between invocations the owner removes a just-added track; the stale pass must
        # not authorise deletions against this changed account.
        new_pid = next(pid for pid, nm in sp.names.items() if nm == "NewOne")
        sp.store[new_pid].remove(A(9))
        sp.touch(new_pid)
        src_before = list(sp.store["PL_SRC"])
        code, out = run_plan(path, sp, journal=jp, phase=3)
        check("stale gate: phase 3 halts instead of deleting", code == runner.EXIT_HALT,
              f"exit {code} {out[-300:]}")
        check("stale gate: the source playlist is untouched", sp.store["PL_SRC"] == src_before)
        check("stale gate: the re-run gate reports the mismatch",
              "phase 2.5 gate failed" in out, out[-300:])


def test_gate_is_scoped_to_run_and_config():
    tmp, path, sp = fresh()
    with tmp:
        jp = path / "apply_journal.jsonl"
        j = runner.Journal(jp, "test-config")
        j.record_gate_pass(runner.GATE_PHASE25, "run-A")
        check("gate: honoured for the run that earned it",
              j.gate_passed(runner.GATE_PHASE25, "run-A"))
        check("gate: not honoured for another run",
              not j.gate_passed(runner.GATE_PHASE25, "run-B"))
        op = {"phase": 2, "action": "add_tracks", "playlist_name": "PL_DST"}
        j.attempt(99, op, "PL_DST")
        check("gate: invalidated by non-phase-3 work journalled after it",
              not j.gate_passed(runner.GATE_PHASE25, "run-A"))

    # The header guard rejects a journal built for another config, so the gate's own
    # config_id check only bites on a hand-edited or spliced journal — which is exactly
    # the tampering it exists to catch.
    tmp, path, sp = fresh()
    with tmp:
        jp = path / "apply_journal.jsonl"
        runner.Journal(jp, "test-config")
        with jp.open("a") as fh:
            fh.write(json.dumps({"state": "note", "kind": "gate_pass",
                                 "gate": runner.GATE_PHASE25, "run_id": "run-A",
                                 "config_id": "some-other-config", "journal_len": 1}) + "\n")
        spliced = runner.Journal(jp, "test-config")
        check("gate: a pass carrying a foreign config_id is not honoured",
              not spliced.gate_passed(runner.GATE_PHASE25, "run-A"))


def test_resume_mid_phase3():
    tmp, path, sp = fresh()
    with tmp:
        jp = path / "apply_journal.jsonl"
        # PL_DST is both an add target and a removal target, so a resume after its
        # removals landed must not read the shortfall as a failed add.
        sp.fail_once("remove_tracks", FakeSpotifyException(400))
        code, _ = run_plan(path, sp, journal=jp)
        check("resume mid-phase-3: the first failure halts", code == runner.EXIT_HALT, f"exit {code}")
        done_removals = [r for r in journal_lines(path)
                         if r.get("state") == "attempt" and r.get("action") == "remove_tracks"]
        code, out = run_plan(path, sp, journal=jp, resume=True)
        end, want = named_store(sp), make_plan()["expected_end_state"]
        check("resume mid-phase-3: phase 0 does not report drift for our own writes",
              "DRIFT" not in out, out[:600])
        check("resume mid-phase-3: phase 2.5 accounts for removals already done",
              "already removed" in out or code == 0, out[-500:])
        check("resume mid-phase-3: the run completes to the expected end state",
              code == 0 and end == want, f"exit {code} {end}")
        check("resume mid-phase-3: at least one removal had been journalled first",
              len(done_removals) >= 1)


# ---- quota / transport ---- #

def test_429_on_every_path():
    cases = {
        "phase 0 snapshot read": ("playlist", {}),
        "phase 0 count read": ("playlist_items", {}),
        "phase 1 write": ("create_playlist", {"phase": 1}),
        "phase 2 write": ("add_tracks", {"phase": 2}),
        "phase 2.5 count read": ("playlist_items", {"phase": 2.5}),
        "phase 3 write": ("remove_tracks", {"phase": 3}),
        "phase 4 read": ("playlist_items", {"phase": 4}),
    }
    for label, (endpoint, kw) in cases.items():
        tmp, path, sp = fresh()
        with tmp:
            jp = path / "apply_journal.jsonl"
            phase = kw.get("phase")
            for prep in [p for p in (0, 1, 2, 2.5, 3) if phase is not None and p < phase]:
                run_plan(path, sp, journal=jp, phase=prep)
            sp.fail_once(endpoint, FakeSpotifyException(429, {"Retry-After": "46649"}))
            code, out = run_plan(path, sp, journal=jp, **kw)
            check(f"429 on {label} -> exit 2 with a checkpoint",
                  code == runner.EXIT_QUOTA and "QUOTA EXHAUSTED" in out,
                  f"exit {code} {out[-200:]}")


def test_429_short_retry_after_sleeps_and_continues():
    tmp, path, sp = fresh()
    with tmp:
        sp.fail_once("add_tracks", FakeSpotifyException(429, {"Retry-After": "5"}))
        code, out = run_plan(path, sp)
        check("429 with a short Retry-After sleeps then succeeds",
              code == 0 and "sleeping" in out, f"exit {code} {out[-200:]}")


def test_429_without_header_is_quota_exhausted():
    tmp, path, sp = fresh()
    with tmp:
        sp.fail_once("playlist", FakeSpotifyException(429, {}))
        code, out = run_plan(path, sp)
        check("429 with no Retry-After ends the day",
              code == runner.EXIT_QUOTA and "no usable Retry-After" in out,
              f"exit {code} {out[-200:]}")


def test_transient_5xx_gets_one_bounded_retry():
    tmp, path, sp = fresh()
    with tmp:
        sp.fail_once("add_tracks", FakeSpotifyException(503))
        code, out = run_plan(path, sp)
        check("a single 503 is retried and the run completes", code == 0, out[-200:])
    tmp, path, sp = fresh()
    with tmp:
        sp.fail_once("add_tracks", FakeSpotifyException(503))
        sp.fail_once("add_tracks", FakeSpotifyException(503))
        code, out = run_plan(path, sp)
        check("a second consecutive 503 halts cleanly rather than escaping",
              code == runner.EXIT_HALT and "HALTED" in out, f"exit {code} {out[-200:]}")


def test_transport_error_halts_cleanly():
    tmp, path, sp = fresh()
    with tmp:
        sp.fail_once("playlist", ConnectionError("connection reset"))
        code, out = run_plan(path, sp)
        check("a transport error halts with exit 3, not a traceback",
              code == runner.EXIT_HALT and "transport failure" in out,
              f"exit {code} {out[-200:]}")


# ---- plan / journal hygiene ---- #

def test_dry_run_makes_no_calls():
    tmp, path, sp = fresh()
    with tmp:
        buf = io.StringIO()
        with redirect_stdout(buf):
            runner.dry_run(make_plan())
        out = buf.getvalue()
        check("dry-run: makes zero API calls", sp.calls == [], str(sp.calls))
        check("dry-run: writes no journal and no counters",
              not (path / "apply_journal.jsonl").exists()
              and not (path / "apply_run_counters.json").exists())
        check("dry-run: lists every op and the call total",
              "total calls:" in out and "no API calls were made" in out)


def test_config_mismatch_and_torn_journal():
    tmp, path, sp = fresh()
    with tmp:
        jp = path / "apply_journal.jsonl"
        runner.Journal(jp, "test-config")
        try:
            runner.Journal(jp, "other-config")
            check("journal: refuses a different config_id", False, "no error raised")
        except runner.PlanHalt:
            check("journal: refuses a different config_id", True)

        with jp.open("a") as fh:
            fh.write('{"state": "note", "kind": "tor')
        try:
            j = runner.Journal(jp, "test-config")
            check("journal: tolerates a torn final record", len(j.records) >= 1)
        except Exception as exc:  # noqa: BLE001
            check("journal: tolerates a torn final record", False, repr(exc))

        with jp.open("a") as fh:
            fh.write('\n{"state": "note", "kind": "ok"}\n')
        try:
            runner.Journal(jp, "test-config")
            check("journal: refuses corruption that is not the final record", False)
        except runner.PlanHalt:
            check("journal: refuses corruption that is not the final record", True)


def test_hard_check_errors_block_execution():
    tmp, path, sp = fresh()
    with tmp:
        plan = make_plan()
        plan["hard_check_errors"] = ["ASSERT F12 SAME group has 0 surviving URIs"]
        plan_path = path / "apply_plan.json"
        plan_path.write_text(json.dumps(plan))
        buf, saved = io.StringIO(), sys.argv
        try:
            with redirect_stdout(buf):
                sys.argv = ["runner", str(plan_path), "--dry-run"]
                runner.main()
            code = 0
        except SystemExit as exc:
            code = exc.code
        finally:
            sys.argv = saved
        check("a plan with hard-check errors is refused before any call",
              code == runner.EXIT_HALT and sp.calls == [], f"exit {code}")


# --------------------------------------------------------------------------- #

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for test in tests:
        try:
            test()
        except Exception:  # noqa: BLE001
            import traceback
            RESULTS.append((f"{test.__name__} (raised)", False, traceback.format_exc()[-500:]))

    verbose = "-v" in sys.argv
    failed = [r for r in RESULTS if not r[1]]
    for name, ok, detail in RESULTS:
        if not ok or verbose:
            print(f"{'PASS' if ok else 'FAIL'}  {name}")
            if detail and not ok:
                print(f"      {detail}")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
