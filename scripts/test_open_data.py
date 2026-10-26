"""Self-contained tests for the open-metadata enrichment scripts.

    python3 scripts/test_open_data.py [-v]

No pytest, no network: a scripted fake session stands in for requests.

What the assertions are load-bearing for:
  * the 429 split — a short wait is slept out, a long, unreadable or
    header-less one stops the service, and so does an announced window longer
    than we will sleep. A client that keeps going hammers a service that asked
    us to leave, and that is how an IP gets blocked;
  * nothing but a real answer is cached — the old MusicBrainz script cached
    failures as "no data", and an error body read as success would do the same
    for 30 days;
  * identity: an artist is proved through a recording ISRC, and a name-search
    hit counts only on an exact (normalized) name — search score alone mapped
    "Beck" to Rufus Beck and "Whitney" to Whitney Houston;
  * roster matching never invents a home: non-Latin names must stay distinct
    (stripping them once gave every Japanese artist the same key), a homonym
    with another MBID borrows nothing, and an artist is never its own neighbour;
  * Last.fm's judge-facing file is rebuilt from the cache, so expired or
    refreshed tags never linger beside new ones.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))

import enrich_listenbrainz as lb  # noqa: E402
import enrich_musicbrainz as mb  # noqa: E402
import lastfm_tags as lfm  # noqa: E402
from open_data_client import OpenDataClient, ServiceStopped, parse_wait, service_lock  # noqa: E402

TODAY = date(2026, 10, 26)


class FakeResponse:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}

    def json(self):
        if self._body is None:
            raise ValueError("no JSON")
        return self._body


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.responses.pop(0)


def client_for(responses):
    sleeps = []
    clock = [0.0]

    def sleep(s):
        sleeps.append(s)
        clock[0] += s

    c = OpenDataClient("test", session=FakeSession(responses), sleep=sleep, clock=lambda: clock[0])
    return c, sleeps


def tags(*pairs):
    return {"toptags": {"tag": [{"name": n, "count": c} for n, c in pairs]}}


class ClientRateLimits(unittest.TestCase):
    def test_short_429_is_slept_out_and_retried(self):
        c, sleeps = client_for([FakeResponse(429, headers={"Retry-After": "7"}),
                                FakeResponse(200, {"ok": 1})])
        self.assertEqual(c.get_json("u"), {"ok": 1})
        self.assertIn(7.0, sleeps)

    def test_long_429_stops_the_service(self):
        c, _ = client_for([FakeResponse(429, headers={"Retry-After": "3600"})])
        with self.assertRaises(ServiceStopped):
            c.get_json("u")

    def test_429_without_wait_header_stops_the_service(self):
        c, _ = client_for([FakeResponse(429)])
        with self.assertRaises(ServiceStopped):
            c.get_json("u")

    def test_unreadable_or_negative_wait_stops_instead_of_crashing(self):
        for value in ("soon", "-5", "nan", "inf"):
            c, _ = client_for([FakeResponse(429, headers={"Retry-After": value})])
            with self.assertRaises(ServiceStopped, msg=value):
                c.get_json("u")

    def test_http_date_wait_is_understood(self):
        self.assertEqual(parse_wait("Wed, 21 Oct 2015 07:28:00 GMT"), 0.0)  # in the past: no wait
        self.assertEqual(parse_wait("12"), 12.0)
        self.assertIsNone(parse_wait("nan"))

    def test_exhausted_window_within_limit_delays_next_request(self):
        c, sleeps = client_for([
            FakeResponse(200, [], {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "300"}),
            FakeResponse(200, []),
        ])
        c.get_json("u")
        c.get_json("u")
        self.assertGreaterEqual(sum(sleeps), 300)

    def test_exhausted_window_beyond_limit_stops(self):
        c, _ = client_for([FakeResponse(200, [], {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-In": "301"})])
        with self.assertRaises(ServiceStopped):
            c.get_json("u")

    def test_requests_are_paced(self):
        c, sleeps = client_for([FakeResponse(200, {}), FakeResponse(200, {})])
        c.get_json("u")
        c.get_json("u")
        self.assertAlmostEqual(sum(sleeps), 1.1, places=3)

    def test_persistent_503_is_transient_none(self):
        c, _ = client_for([FakeResponse(503)] * 4)
        self.assertIsNone(c.get_json("u"))

    def test_failure_streak_stops_the_service(self):
        c, _ = client_for([FakeResponse(503)] * 4 * 5)
        with self.assertRaises(ServiceStopped):
            for _ in range(5):
                c.get_json("u")

    def test_error_bodies_are_failures_not_answers(self):
        for resp in (FakeResponse(400, {"error": "invalid query"}),
                     FakeResponse(200, {"error": 7, "message": "Invalid resource"}),
                     FakeResponse(404, {"error": "Not Found"})):
            c, _ = client_for([resp])
            self.assertIsNone(c.get_json("u"))

    def test_404_is_an_answer_only_where_the_caller_says_so(self):
        c, _ = client_for([FakeResponse(404, {"error": "Not Found"})])
        self.assertEqual(c.get_json("u", not_found={}), {})

    def test_lastfm_temporary_errors_retry_then_fail_uncached(self):
        c, _ = client_for([FakeResponse(200, {"error": e}) for e in (8, 11, 16, 16)])
        self.assertIsNone(c.get_json("u"))

    def test_lastfm_rate_limit_in_body_stops_even_on_200(self):
        c, _ = client_for([FakeResponse(200, {"error": 29, "message": "Rate limit exceeded"})])
        with self.assertRaises(ServiceStopped):
            c.get_json("u")

    def test_lastfm_suspended_key_stops(self):
        c, _ = client_for([FakeResponse(403, {"error": 26, "message": "Suspended API key"})])
        with self.assertRaises(ServiceStopped):
            c.get_json("u")

    def test_lastfm_not_found_is_an_answer(self):
        c, _ = client_for([FakeResponse(200, {"error": 6, "message": "Track not found"})])
        self.assertEqual(c.get_json("u")["error"], 6)


class Locks(unittest.TestCase):
    def test_second_run_on_same_service_refuses(self):
        with TemporaryDirectory() as d:
            with service_lock(Path(d), "musicbrainz"):
                with self.assertRaises(SystemExit):
                    with service_lock(Path(d), "musicbrainz"):
                        pass
                with service_lock(Path(d), "listenbrainz"):
                    pass  # another service may run alongside


class Names(unittest.TestCase):
    def test_non_latin_names_stay_distinct_and_accents_fold(self):
        self.assertNotEqual(mb.norm_name("佐々木恵梨"), mb.norm_name("亜咲花"))
        self.assertTrue(mb.norm_name("佐々木恵梨"))
        self.assertEqual(mb.norm_name("Björk"), mb.norm_name("Bjork"))
        self.assertEqual(mb.norm_name("The Arcs"), mb.norm_name("Arcs"))
        self.assertEqual(mb.norm_name("Simon & Garfunkel"), mb.norm_name("Simon and Garfunkel"))


class MusicBrainzIdentity(unittest.TestCase):
    def test_isrc_credit_proves_identity(self):
        c, _ = client_for([
            FakeResponse(200, {"recordings": [{"artist-credit": [
                {"name": "Beck", "artist": {"id": "beck-hansen", "name": "Beck"}}]}]}),
            FakeResponse(200, {"id": "beck-hansen", "tags": [{"name": "alternative rock", "count": 5}]}),
        ])
        entry = mb.resolve_artist(c, "Beck", ("USGF19463901", 0), TODAY)
        self.assertEqual((entry["mbid"], entry["identity"]), ("beck-hansen", "isrc"))

    def test_isrc_credit_spelled_differently_is_taken_by_position(self):
        c, _ = client_for([
            FakeResponse(200, {"recordings": [{"artist-credit": [
                {"name": "Cat Stevens", "artist": {"id": "cs", "name": "Cat Stevens"}}]}]}),
            FakeResponse(200, {"id": "cs", "tags": []}),
        ])
        self.assertEqual(mb.resolve_artist(c, "Yusuf / Cat Stevens", ("X", 0), TODAY)["mbid"], "cs")

    def test_unknown_isrc_falls_back_to_exact_name_search(self):
        c, _ = client_for([
            FakeResponse(404, {"error": "Not Found"}),
            FakeResponse(200, {"artists": [{"id": "whitney-h", "name": "Whitney Houston", "score": 100},
                                           {"id": "whitney-band", "name": "Whitney", "score": 95}]}),
            FakeResponse(200, {"id": "whitney-band", "tags": []}),
        ])
        entry = mb.resolve_artist(c, "Whitney", ("X", 0), TODAY)
        self.assertEqual((entry["mbid"], entry["identity"]), ("whitney-band", "name"))

    def test_search_without_exact_name_is_unmatched_with_candidate(self):
        c, _ = client_for([FakeResponse(200, {"artists": [
            {"id": "hfb", "name": "Noel Gallagher’s High Flying Birds", "score": 100}]})])
        entry = mb.resolve_artist(c, "Noel Gallagher", None, TODAY)
        self.assertFalse(entry["matched"])
        self.assertEqual(entry["candidate"]["mbid"], "hfb")

    def test_transient_failure_is_not_cached(self):
        c, _ = client_for([FakeResponse(503)] * 4)
        self.assertIsNone(mb.resolve_artist(c, "Psychic Ills", None, TODAY))

    def test_malformed_isrcs_are_skipped_for_a_valid_one(self):
        playlists = [{"tracks": [{"artists": ["A"], "isrc": "us-abc-12-34567x"},
                                 {"artists": ["A"], "isrc": "usabc1234567"}]}]
        self.assertEqual(mb.isrc_refs(playlists), {"A": ("USABC1234567", 0)})

    def test_error_body_from_search_is_not_cached(self):
        c, _ = client_for([FakeResponse(400, {"error": "invalid query"})])
        self.assertIsNone(mb.resolve_artist(c, "X", None, TODAY))

    def test_refresh_policy(self):
        isrc_ok = {"matched": True, "genres": ["rock"], "identity": "isrc", "fetched": "2026-10-01"}
        self.assertTrue(mb.needs_fetch(None, None, TODAY))
        self.assertFalse(mb.needs_fetch(isrc_ok, "X", TODAY))
        self.assertTrue(mb.needs_fetch({"matched": True, "genres": ["rock"]}, None, TODAY))  # never checked
        named = {"matched": True, "genres": ["rock"], "identity": "name", "fetched": "2026-10-20"}
        self.assertFalse(mb.needs_fetch(named, "X", TODAY))  # re-proved by ISRC only after the TTL
        self.assertTrue(mb.needs_fetch({**named, "fetched": "2026-09-20"}, "X", TODAY))
        self.assertFalse(mb.needs_fetch({**named, "fetched": "2026-09-20"}, None, TODAY))
        empty = {"matched": False, "genres": [], "identity": None}
        self.assertFalse(mb.needs_fetch({**empty, "fetched": "2026-10-20"}, None, TODAY))
        self.assertTrue(mb.needs_fetch({**empty, "fetched": "2026-09-20"}, None, TODAY))


class ListenBrainzLines(unittest.TestCase):
    def index(self):
        playlists = [
            {"name": "Ether", "tracks": [{"artists": ["Slowdive"]}, {"artists": ["DIIV"]},
                                         {"artists": ["亜咲花"]}]},
            {"name": "Dreamy", "tracks": [{"artists": ["Slowdive"]}, {"artists": ["The Arcs"]}]},
            {"name": "Liked Songs", "tracks": [{"artists": ["Beach House"]}]},
        ]
        mb_cache = {"Slowdive": {"mbid": "s"}, "DIIV": {"mbid": "d"}, "The Arcs": {"mbid": "arcs-1"}}
        return lb.roster_index(playlists, mb_cache)

    def test_line_names_neighbour_homes_and_pull(self):
        by_mbid, by_name = self.index()
        entry = {"mbid": "t", "neighbours": [{"name": "Slowdive", "mbid": "s", "score": 186},
                                             {"name": "Beach House", "mbid": "b", "score": 156},
                                             {"name": "DIIV", "mbid": "d", "score": 144}]}
        self.assertEqual(
            lb.similar_line("Tamaryn", entry, by_mbid, by_name),
            "Tamaryn: Slowdive 186 [Dreamy, Ether]; Beach House 156; DIIV 144 [Ether]"
            " || roster pull: Ether 2, Dreamy 1")

    def test_ties_are_ordered_by_name(self):
        by_mbid, by_name = self.index()
        entry = {"mbid": "t", "neighbours": [{"name": "Slowdive", "mbid": "s", "score": 5}]}
        self.assertTrue(lb.similar_line("X", entry, by_mbid, by_name).endswith("Dreamy 1, Ether 1"))

    def test_non_latin_neighbour_matches_only_its_own_name(self):
        by_mbid, by_name = self.index()
        entry = {"mbid": "t", "neighbours": [{"name": "佐々木恵梨", "mbid": "x1", "score": 9},
                                             {"name": "亜咲花", "mbid": "x2", "score": 8}]}
        self.assertEqual(lb.similar_line("Y", entry, by_mbid, by_name),
                         "Y: 佐々木恵梨 9; 亜咲花 8 [Ether] || roster pull: Ether 1")

    def test_homonym_with_other_mbid_borrows_no_home_and_self_is_dropped(self):
        by_mbid, by_name = self.index()
        entry = {"mbid": "arcs-1", "neighbours": [{"name": "The Arcs", "mbid": "arcs-2", "score": 14},
                                                  {"name": "Arcs", "mbid": "arcs-3", "score": 9}]}
        self.assertEqual(lb.similar_line("The Arcs", entry, by_mbid, by_name), "The Arcs: -")
        other = {"mbid": "z", "neighbours": [{"name": "The Arcs", "mbid": "arcs-2", "score": 14}]}
        self.assertEqual(lb.similar_line("Z", other, by_mbid, by_name), "Z: The Arcs 14")

    def test_refetch_on_algorithm_or_identity_change(self):
        fresh = {"mbid": "m", "algorithm": lb.ALGORITHM, "fetched": "2026-10-25", "neighbours": [{}]}
        self.assertFalse(lb.needs_fetch(fresh, "m", TODAY))
        self.assertTrue(lb.needs_fetch({**fresh, "algorithm": "old"}, "m", TODAY))
        self.assertTrue(lb.needs_fetch(fresh, "corrected-mbid", TODAY))

    def test_error_body_is_not_cached_as_empty_graph(self):
        c, _ = client_for([FakeResponse(400, {"error": "invalid algorithm"})])
        self.assertIsNone(lb.fetch_similar(c, "m", TODAY))


class LastFmTags(unittest.TestCase):
    def test_filters_noise_and_personal_tags(self):
        body = tags(("Shoegaze", 100), ("seen live", 60), ("Tamaryn", 40), ("dream pop", 55), ("rare", 3))
        self.assertEqual(lfm.clean_tags(body, "Tamaryn"), ["shoegaze", "dream pop"])

    def test_not_found_is_empty_and_bad_shape_is_a_failure(self):
        self.assertEqual(lfm.clean_tags({"error": 6}, "A"), [])
        self.assertIsNone(lfm.clean_tags({"unexpected": 1}, "A"))

    def test_album_with_colon_parses_and_keys_are_per_entity(self):
        keys = lfm.entity_keys("Jimi Hendrix – Little Wing @ Axis: Bold As Love")
        self.assertEqual([k[0] for k in keys], ["artist", "album", "track"])
        self.assertEqual(keys[1][2], {"album": "Axis: Bold As Love"})
        same_artist = lfm.entity_keys("Jimi Hendrix – Spanish Castle Magic @ Axis: Bold As Love")
        self.assertEqual(keys[0][1], same_artist[0][1])
        self.assertEqual(keys[1][1], same_artist[1][1])

    def test_text_file_is_rebuilt_from_cache_without_stale_lines(self):
        with TemporaryDirectory() as d:
            out = Path(d)
            stale_key = lfm.entity_keys("Old Band")[0][1]
            (out / "lastfm_cache.json").write_text(json.dumps({
                stale_key: {"tags": ["rock"], "fetched": "2020-01-01"},
                "_queries": {"list": ["Old Band"], "tags": [], "fetched": "2026-10-26"}}))
            (out / "lastfm.txt").write_text("Old Band => artist [rock]\n")
            session = FakeSession([FakeResponse(200, tags(("shoegaze", 100))),
                                   FakeResponse(200, tags(("2010", 100), ("shoegaze", 60))),
                                   FakeResponse(200, {"toptags": {"tag": []}})])
            fake = OpenDataClient("lastfm", session=session, sleep=lambda s: None)
            with mock.patch.object(lfm, "OpenDataClient", return_value=fake), \
                    mock.patch.dict(os.environ, {"LASTFM_API_KEY": "k"}), \
                    mock.patch.object(sys, "argv", ["x", str(out), "Tamaryn – Love Fade @ The Waves"]):
                lfm.main()
            self.assertEqual((out / "lastfm.txt").read_text(),
                             "Tamaryn – Love Fade @ The Waves => artist [shoegaze] | album [2010; shoegaze]"
                             " | track [-]\n")

    def test_failed_level_leaves_query_unanswered(self):
        c, _ = client_for([FakeResponse(503)] * 4)
        self.assertIsNone(lfm.fetch_entity(c, "k", "A", "album", {"album": "B"}))


if __name__ == "__main__":
    unittest.main()
