"""Self-contained tests for gen_moves_plan's twin checks.

    python3 scripts/test_gen_moves_plan.py [-v]

No pytest, no credentials, no network. The title-twin check is load-bearing in both
directions: a false twin blocks a legitimate plan (every Japanese title once folded
to "" and matched every other), a missed twin lets an edition dup into a playlist.
"""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "gen_moves_plan", Path(__file__).with_name("gen_moves_plan.py"))
gmp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gmp)


def track(uri: str, name: str, isrc: str) -> dict:
    return {"uri": uri, "name": name, "isrc": isrc, "artists": ["A"], "is_playable": True}


class NormTitle(unittest.TestCase):
    def test_edition_noise_still_folds(self):
        self.assertEqual(gmp.norm_title("Moondance - 2013 Remaster"), gmp.norm_title("Moondance"))
        self.assertEqual(gmp.norm_title("Glory Box (Live)"), gmp.norm_title("Glory Box"))

    def test_non_latin_titles_keep_their_letters(self):
        self.assertEqual(gmp.norm_title("旅立ちの風"), "旅立ちの風")
        self.assertNotEqual(gmp.norm_title("旅立ちの風"), gmp.norm_title("今際の死神"))


class TitleTwinCheck(unittest.TestCase):
    def build(self, dest_tracks: list[dict], add: dict) -> list[str]:
        with tempfile.TemporaryDirectory() as d:
            Path(d, "dest.json").write_text(json.dumps(
                {"id": "PL", "name": "Dest", "snapshot_id": "s1",
                 "total": len(dest_tracks), "tracks": dest_tracks}))
            Path(d, "src.json").write_text(json.dumps(
                {"id": "SRC", "name": "Src", "tracks": [add]}))
            _, errors = gmp.build({"config_id": "t", "status": "test", "exports": d, "adds": [{"uri": add["uri"], "to": "Dest"}]})
        return errors

    def test_distinct_japanese_titles_are_not_twins(self):
        errors = self.build([track("u:1", "今際の死神", "I1")], track("u:2", "旅立ちの風", "I2"))
        self.assertEqual(errors, [])

    def test_same_japanese_title_is_still_a_twin(self):
        errors = self.build([track("u:1", "旅立ちの風", "I1")], track("u:2", "旅立ちの風", "I2"))
        self.assertTrue(any("title twin" in e for e in errors), errors)

    def test_titles_that_fold_to_nothing_are_not_twins(self):
        errors = self.build([track("u:1", "?", "I1")], track("u:2", "!", "I2"))
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
