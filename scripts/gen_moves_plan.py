"""Turn an owner-approved move spec into an apply_plan_runner plan (zero LLM tokens).

The spec says WHAT moves; this script proves it is safe and emits the runner's
plan format. It refuses to emit when any check fails, so a bad spec can never
reach the account:

  * every source row exists in its source playlist's fresh export;
  * every added track is playable (dead URIs 400 the whole add batch) and is not
    a known ghost in the URI remap registry;
  * no destination ends up with a URI, ISRC or fuzzy-title twin — titles may be
    waived per destination for deliberate different-recording pairs, ISRCs never;
  * no URI is both added to and removed from the same playlist.

Spec (JSON):
    {"config_id": "...", "status": "owner-approved: ...",
     "exports": "DIR",              # fresh playlist exports + liked-songs.json
     "registry": "uri-remaps.json", # optional
     "create":  [{"name": "Folk", "description": "...", "public": true}],
     "moves":   [{"uri": "...", "from": "Acoustic/Folk", "to": "Folk"}],
     "adds":    [{"uri": "...", "to": "Folk"}],          # e.g. from Liked
     "removes": [{"uri": "...", "from": "Acoustic/Folk"}],
     "title_waivers": [{"to": "Laurel Canyon", "title": "Harvest Moon"}]}

Usage: gen_moves_plan.py SPEC.json OUT_PLAN.json
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

CHUNK = 40
PAGE = 100


def norm_title(title: str) -> str:
    """Fold edition noise so 'Song - 2011 Remaster' and 'Song (Live)' compare equal."""
    t = re.sub(r"\s*[-(\[].*$", "", (title or "").lower())
    return re.sub(r"[^a-z0-9 ]", "", t).strip()


def chunks(items: list, size: int = CHUNK) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def label(t: dict) -> str:
    return f"{', '.join(t.get('artists') or ['?'])} – {t.get('name')}"


def load_exports(d: Path) -> tuple[dict[str, dict], dict[str, dict]]:
    """Playlists by name, plus a uri → track row index over every export incl. Liked."""
    playlists, rows = {}, {}
    for p in sorted(d.glob("*.json")):
        data = json.loads(p.read_text())
        if not isinstance(data, dict) or "tracks" not in data:
            continue
        if data.get("id") != "liked-songs":
            playlists[data["name"]] = data
        for t in data["tracks"]:
            rows.setdefault(t["uri"], t)
    return playlists, rows


def build(spec: dict) -> tuple[dict | None, list[str]]:
    errors: list[str] = []
    playlists, rows = load_exports(Path(spec["exports"]))
    created = {c["name"]: c for c in spec.get("create", [])}
    ghosts = set()
    if spec.get("registry") and Path(spec["registry"]).exists():
        reg = json.loads(Path(spec["registry"]).read_text())
        ghosts = {m["ghost_uri"] for m in reg.get("mappings", reg if isinstance(reg, list) else [])}
    waived = {(w["to"], norm_title(w["title"])) for w in spec.get("title_waivers", [])}

    adds: dict[str, list[str]] = defaultdict(list)
    removes: dict[str, list[str]] = defaultdict(list)
    for m in spec.get("moves", []):
        adds[m["to"]].append(m["uri"])
        removes[m["from"]].append(m["uri"])
    for a in spec.get("adds", []):
        adds[a["to"]].append(a["uri"])
    for r in spec.get("removes", []):
        removes[r["from"]].append(r["uri"])

    for name in set(adds) | set(removes):
        if name not in playlists and name not in created:
            errors.append(f"unknown playlist {name!r} (not in exports, not created)")
    for name in created:
        if name in playlists:
            errors.append(f"create {name!r}: a playlist with that name already exists")
    if errors:
        return None, errors

    for src, uris in removes.items():
        if src in created:
            errors.append(f"cannot remove from {src!r}: it is created by this plan")
            continue
        live = Counter(t["uri"] for t in playlists[src]["tracks"])
        for u in uris:
            if live[u] == 0:
                errors.append(f"{src}: removal source row not found {u}")
        for u, n in Counter(uris).items():
            if n > 1:
                errors.append(f"{src}: removal listed twice {u}")

    for dest, uris in adds.items():
        after_removal = [] if dest in created else [
            t for t in playlists[dest]["tracks"] if t["uri"] not in set(removes.get(dest, []))]
        have_uri = {t["uri"] for t in after_removal}
        have_isrc = {t["isrc"]: t for t in after_removal if t.get("isrc")}
        have_title = {norm_title(t["name"]): t for t in after_removal}
        for u in uris:
            t = rows.get(u)
            if t is None:
                errors.append(f"{dest}: no export row for {u} — re-export its source")
                continue
            if t.get("is_playable") is False and not t.get("canonical_id"):
                errors.append(f"{dest}: dead URI (unplayable, no relink) {label(t)}")
            if u in ghosts:
                errors.append(f"{dest}: known ghost URI, use its canonical from the registry: {label(t)}")
            if u in set(removes.get(dest, [])):
                errors.append(f"{dest}: {label(t)} is both added and removed")
            if u in have_uri:
                errors.append(f"{dest}: URI twin {label(t)}")
            isrc = t.get("isrc")
            if isrc and isrc in have_isrc:
                errors.append(f"{dest}: ISRC twin {label(t)} = {label(have_isrc[isrc])}")
            key = norm_title(t["name"])
            if key in have_title and (dest, key) not in waived:
                errors.append(f"{dest}: title twin {label(t)} ~ {label(have_title[key])}")
            have_uri.add(u)
            if isrc:
                have_isrc[isrc] = t
            have_title.setdefault(key, t)

    if errors:
        return None, errors

    ops, budget_writes = [], 0
    touched = sorted((set(adds) | set(removes)) - set(created))
    for name in touched:
        pl = playlists[name]
        ops.append({"phase": 0, "action": "snapshot_check", "playlist_id_or_name": pl["id"],
                    "playlist_name": name, "calls": 2,
                    "cold_start_expected_snapshot_id": pl["snapshot_id"],
                    "cold_start_expected_total": pl["total"]})
    for i, (name, c) in enumerate(created.items()):
        ops.append({"phase": 1, "action": "create_playlist", "playlist_id_or_name": name,
                    "playlist_name": name, "calls": 1, "canary": i == 0,
                    "body": {"name": name, "public": c.get("public", True),
                             "description": c.get("description", "")}})
        budget_writes += 1

    def target(name: str) -> str:
        return f"<created:{name}>" if name in created else playlists[name]["id"]

    first_write = True
    for dest in sorted(adds, key=lambda n: (n not in created, n)):
        parts = chunks(adds[dest])
        for ci, part in enumerate(parts):
            ops.append({"phase": 2, "action": "add_tracks", "playlist_id_or_name": target(dest),
                        "playlist_name": dest, "calls": 1, "canary": first_write,
                        "uris": part, "count": len(part), "chunk_index": ci,
                        "chunks_total": len(parts)})
            first_write = False
            budget_writes += 1
        base = 0 if dest in created else playlists[dest]["total"]
        ops.append({"phase": 2.5, "action": "verify_total", "playlist_id_or_name": target(dest),
                    "playlist_name": dest, "calls": 1, "expected_total": base + len(adds[dest])})
    for src in sorted(removes):
        parts = chunks(removes[src])
        for ci, part in enumerate(parts):
            ops.append({"phase": 3, "action": "remove_tracks", "playlist_id_or_name": target(src),
                        "playlist_name": src, "calls": 1, "uris": part, "count": len(part),
                        "chunk_index": ci, "chunks_total": len(parts)})
            budget_writes += 1

    end_state: dict[str, list[str]] = {}
    for name in touched:
        gone = set(removes.get(name, []))
        end_state[name] = [t["uri"] for t in playlists[name]["tracks"] if t["uri"] not in gone]
        end_state[name] += adds.get(name, [])
    for name in created:
        end_state[name] = list(adds.get(name, []))
    for name, uris in end_state.items():
        ops.append({"phase": 4, "action": "reexport_verify", "playlist_id_or_name": target(name),
                    "playlist_name": name, "calls": max(1, -(-len(uris) // PAGE))})

    detail = lambda uris: [{"uri": u, "track": label(rows[u])} for u in uris]  # noqa: E731
    plan = {
        "generated": date.today().isoformat(),
        "config_id": spec["config_id"],
        "status": spec["status"],
        "source_of_truth": str(spec["exports"]),
        "chunk_size": CHUNK,
        "pacing_seconds": 2.0,
        "hard_check_errors": [],
        "new_playlists": [{"name": n} for n in created],
        "playlists": {n: {"id": playlists[n]["id"], "snapshot_id": playlists[n]["snapshot_id"],
                          "total": playlists[n]["total"]} for n in touched},
        "op_totals": {"add_ops": sum(1 for o in ops if o["action"] == "add_tracks"),
                      "remove_ops": sum(1 for o in ops if o["action"] == "remove_tracks")},
        "budget": {"total": {"calls_expected": sum(o["calls"] for o in ops),
                             "write_calls": budget_writes}},
        "ops": ops,
        "add_detail": {n: detail(u) for n, u in adds.items()},
        "remove_detail": {n: detail(u) for n, u in removes.items()},
        "expected_end_state": end_state,
    }
    return plan, []


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    spec = json.loads(Path(sys.argv[1]).read_text())
    plan, errors = build(spec)
    if errors:
        print(f"REFUSING TO EMIT — {len(errors)} failed check(s):")
        for e in errors:
            print(f"  - {e}")
        return 1
    Path(sys.argv[2]).write_text(json.dumps(plan, ensure_ascii=False, indent=1))
    print(f"plan {plan['config_id']}: {plan['budget']['total']['calls_expected']} calls "
          f"({plan['budget']['total']['write_calls']} writes)")
    for name, uris in plan["expected_end_state"].items():
        a = len(plan["add_detail"].get(name, []))
        r = len(plan["remove_detail"].get(name, []))
        new = " (created)" if name in {p["name"] for p in plan["new_playlists"]} else ""
        print(f"  {name:<24} +{a:<3} -{r:<3} → {len(uris)}{new}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
