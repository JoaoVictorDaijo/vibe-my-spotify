"""Polite HTTP client for the open metadata services (MusicBrainz,
ListenBrainz, Last.fm). Spotify never goes through here: its calls belong to
apply_plan_runner.Api and its per-account daily quota.

Each service is paced at one request per 1.1 s — MusicBrainz and ListenBrainz
both demand <= 1/s per client and Last.fm publishes no number, so one pace
fits all. The pace is per client instance, not machine-wide: never run two
enrichment scripts against the same service at once. Limits and the evidence
behind them: docs/genre-sources.md.

Failure contract, so callers never cache a transient failure as "no data":
- returns parsed JSON on a 2xx success, or on Last.fm's "not found" (error 6),
  which is a real answer;
- returns None for anything else that went wrong — network, 5xx, 404, an
  error body, a temporary Last.fm error — and the caller leaves that entity
  uncached so the next run retries it;
- raises ServiceStopped when the service says stop (a long or unreadable
  429 wait, an exhausted window longer than we will sleep, a Last.fm
  rate-limit or key error, a failure streak) — the caller ends the run for
  that service and keeps what it has.
"""

from __future__ import annotations

import fcntl
import math
import time
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import requests

USER_AGENT = "VibeMySpotify/0.2 (https://github.com/JoaoVictorDaijo/vibe-my-spotify)"
PACE_SECONDS = 1.1
# Same split as the Spotify doctrine: a short wait is a rolling window worth
# sleeping out; anything longer means this service wants us gone for a while.
MAX_SLEEP_SECONDS = 300
MAX_RETRIES = 4
MAX_FAILURE_STREAK = 5
# Last.fm reports errors inside the JSON body, often with HTTP 200.
LASTFM_NOT_FOUND = 6
LASTFM_STOP_ERRORS = {
    4: "authentication failed",
    9: "invalid session",
    10: "invalid API key",
    26: "API key suspended",
    29: "rate limit exceeded",
}
LASTFM_TEMPORARY_ERRORS = {8: "operation failed", 11: "service offline", 16: "temporarily unavailable"}


class ServiceStopped(Exception):
    pass


@contextmanager
def service_lock(out_dir: Path, service: str):
    """Refuse to run while another process uses this service from the same
    cache dir: two clients double the request rate and overwrite each
    other's cache saves."""
    path = Path(out_dir) / f".lock-{service}"
    with open(path, "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit(f"another run is already using {service} with {out_dir} — wait for it to finish")
        yield


def parse_wait(value: str | None) -> float | None:
    """Seconds from a Retry-After / X-RateLimit-Reset-In value; None if unreadable."""
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - datetime.now(timezone.utc)).total_seconds()
        seconds = max(seconds, 0.0)
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


class OpenDataClient:
    def __init__(self, service: str, session=None, sleep=time.sleep, clock=time.monotonic):
        self.service = service
        self.session = session or requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT
        self.sleep = sleep
        self.clock = clock
        self.next_at = 0.0
        self.failure_streak = 0
        self.requests = 0

    def _pace(self) -> None:
        wait = self.next_at - self.clock()
        if wait > 0:
            self.sleep(wait)
        self.next_at = self.clock() + PACE_SECONDS

    def _wait_hint(self, r) -> float | None:
        for header in ("Retry-After", "X-RateLimit-Reset-In"):
            if r.headers.get(header) is not None:
                return parse_wait(r.headers.get(header))
        return None

    def _respect_window(self, r) -> None:
        # ListenBrainz announces an exhausted window before it 429s.
        if r.headers.get("X-RateLimit-Remaining") != "0":
            return
        hint = self._wait_hint(r)
        if hint is None or hint > MAX_SLEEP_SECONDS:
            raise ServiceStopped(f"{self.service}: rate window exhausted, resets in {hint} s — stop for today")
        self.next_at = max(self.next_at, self.clock() + hint)

    def _fail(self, why: str) -> None:
        self.failure_streak += 1
        print(f"{self.service}: transient failure ({why})", flush=True)
        if self.failure_streak >= MAX_FAILURE_STREAK:
            raise ServiceStopped(f"{self.service}: {self.failure_streak} failures in a row, last: {why}")

    def get_json(self, url: str, params: dict | None = None, not_found=None):
        """`not_found`, when given, is returned for a 404: pass it only where a
        404 is a real answer (an unknown ISRC), never a broken URL."""
        last = "no attempt"
        for attempt in range(MAX_RETRIES):
            self._pace()
            self.requests += 1
            try:
                r = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                self.sleep(2 * (attempt + 1))
                last = type(e).__name__
                continue
            if r.status_code == 429:
                hint = self._wait_hint(r)
                if hint is None or hint > MAX_SLEEP_SECONDS:
                    raise ServiceStopped(f"{self.service}: 429 with wait {hint} s — stop for today")
                self.sleep(hint)
                last = "429"
                continue
            if r.status_code >= 500:
                # MusicBrainz signals its per-IP rate limit with 503.
                self.sleep(5 * 2 ** attempt)
                last = str(r.status_code)
                continue
            self._respect_window(r)
            if r.status_code == 404 and not_found is not None:
                self.failure_streak = 0
                return not_found
            try:
                body = r.json()
            except ValueError:
                last = f"{r.status_code} non-JSON"
                break
            error = body.get("error") if isinstance(body, dict) else None
            if error in LASTFM_STOP_ERRORS:
                raise ServiceStopped(f"{self.service}: error {error} ({LASTFM_STOP_ERRORS[error]})")
            if error in LASTFM_TEMPORARY_ERRORS:
                self.sleep(5 * 2 ** attempt)
                last = f"error {error} ({LASTFM_TEMPORARY_ERRORS[error]})"
                continue
            if error == LASTFM_NOT_FOUND or (error is None and 200 <= r.status_code < 300):
                self.failure_streak = 0
                return body
            last = f"{r.status_code} error {error!r}"
            break
        self._fail(last)
        return None
