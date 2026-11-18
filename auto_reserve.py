"""Opt-in background watcher: tries to reserve your favorited sessions.

Off unless the server is started with --auto-reserve (or OPEN_SEATS_AUTO_RESERVE=1).
It never signs in for you: it only uses the token the server already holds in
memory after you signed in in the browser. All I/O goes through the injected
`api(method, path, body) -> (status, bytes, headers)` so tests stay offline.
"""

import json
import os
import random
import re
import sys
import threading
import time
import urllib.parse

EVENT_ID = "reinvent2026"
AUTO_MIN_MINUTES = 10  # uniform random wait between cycles
AUTO_MAX_MINUTES = 20
BATCH_SIZE = 10  # ReserveSessions limit
MAX_RETRY_AFTER = 120  # seconds; a longer Retry-After skips the rest of this cycle
SIGNIN_POLL_SECONDS = 15

REASONS = {
    "sessionFull": "session is full",
    "scheduleConflict": "clashes with something on your schedule",
    "alreadyScheduled": "already on your schedule",
    "sessionNotReservable": "session does not take reservations",
    "insufficientAccess": "your registration does not include this session",
    "timePassed": "session has already started",
}


def next_delay(rng=random):
    """Seconds until the next cycle: no fixed cadence."""
    return rng.uniform(AUTO_MIN_MINUTES * 60, AUTO_MAX_MINUTES * 60)


def _minutes(hms):
    h, m = hms.split(":")[:2]
    return int(h) * 60 + int(m)


def _session_interval(s):
    t = s.get("sessionTime") or {}
    if not t.get("date") or not t.get("time"):
        return None
    start = _minutes(t["time"])
    return t["date"], start, start + (int(t.get("length") or 60))


def _wall_interval(start_iso, end_iso):
    day, s = start_iso.split("T")
    end_day, e = end_iso.split("T")
    return day, _minutes(s), (_minutes(e) if end_day == day else 24 * 60)


def session_label(sid, session):
    """CODE (Title); falls back to the ID (or just the title) when the code is missing."""
    session = session or {}
    code, title = session.get("abbreviation"), session.get("title")
    if code and title:
        return "%s (%s)" % (code, title)
    return code or ("%s (%s)" % (sid, title) if title else sid)


def busy_intervals(schedule, sessions_by_id):
    """(day, start, end, label) for reservations and personal time."""
    out = []
    for sid in schedule.get("reserved") or []:
        iv = _session_interval(sessions_by_id.get(sid) or {})
        if iv:
            out.append(iv + (session_label(sid, sessions_by_id.get(sid)),))
    for pt in schedule.get("personalTime") or []:
        try:
            out.append(_wall_interval(pt["startDateTime"], pt["endDateTime"]) + (pt.get("title") or "personal time",))
        except (KeyError, ValueError):
            continue
    return out


def clash_with(session, busy):
    """Label of the first busy block overlapping the session (travel time ignored), else None."""
    iv = _session_interval(session)
    if not iv:
        return None
    for day, start, end, label in busy:
        if day == iv[0] and start < iv[2] and iv[1] < end:
            return label
    return None


COLOURS = {"info": "97", "warn": "33", "error": "31", "success": "32"}  # bright white, red, green


def colour_enabled():
    return sys.stdout.isatty() and "NO_COLOR" not in os.environ


_REPEAT_SUFFIX = re.compile(r"[-\s]?R\d*$", re.IGNORECASE)
OPEN_BANDS = ("available", "limited", "veryLimited")


def talk_key(session):
    """Identifies a talk across its repeats: the session code without an -R1/-R2 suffix
    (ARC320-R2 -> ARC320); sessions without a code fall back to their normalised title."""
    code = (session.get("abbreviation") or "").strip()
    if code:
        return _REPEAT_SUFFIX.sub("", code).upper()
    title = " ".join((session.get("title") or "").lower().split())
    return "title:" + title if title else None


def is_open(session):
    return (session.get("seatAvailability") or ("available" if session.get("isReservable") else "none")) in OPEN_BANDS


def not_reservable(session):
    """Walk-in / walk-up / non-reservable, mirroring band() in web/js/model.js. Unknown sessions are not skipped."""
    if not session:
        return False
    band = session.get("seatAvailability") or ("available" if session.get("isReservable") else "none")
    return band in ("none", "walkUp") or session.get("isReservable") is False


class AutoReserver:
    def __init__(self, api, signed_in, clear_tokens, log=print, rng=random, wait=None, use_colour=None):
        self.api = api
        self.signed_in = signed_in  # () -> bool
        self.clear_tokens = clear_tokens
        self.log = log
        self.rng = rng
        self.use_colour = colour_enabled() if use_colour is None else use_colour
        self._stop = threading.Event()
        self.wait = wait or self._stop.wait  # wait(seconds) -> True if stopped
        self.reserved = set()  # never tried again once reserved
        self.reserved_talks = set()  # talk keys with a reserved session: stop trying the others
        self.key_of = {}
        self.seen_ids = set()  # catalog ids from the previous cycle
        self.announced = set()  # (kind, session id) already announced
        self.skipped = set()  # walk-in or not allowed: never tried, logged once
        self.thread = None

    def _emit(self, level, text):
        if self.use_colour:
            text = "\033[%sm%s\033[0m" % (COLOURS[level], text)
        self.log(text)

    def warn(self, msg):
        self._emit("warn", "WARN [auto-reserve] " + msg)

    def error(self, msg):
        self._emit("error", "ERROR [auto-reserve] " + msg)

    def info(self, msg):
        self._emit("info", "[auto-reserve] " + msg)

    def success(self, msg):
        self._emit("success", "[auto-reserve] " + msg)

    def start(self):
        self.thread = threading.Thread(target=self.run, name="auto-reserve", daemon=True)
        self.thread.start()

    def stop(self):
        self._stop.set()
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=5)

    def run(self):
        self.info("enabled. Waiting for you to sign in in the browser; nothing happens before that.")
        warned = False
        while not self._stop.is_set():
            if self.signed_in():
                break
            if not warned:
                self.warn("not signed in. Sign in in the browser; I will not sign in for you.")
                warned = True
            if self.wait(SIGNIN_POLL_SECONDS):
                return
        while not self._stop.is_set():
            try:
                self.cycle()
            except Exception as exc:  # keep the watcher alive; surface the problem
                self.error("cycle failed: %s" % exc)
            delay = next_delay(self.rng)
            self.info("next attempt in %d min at %s" % (
                round(delay / 60), time.strftime("%H:%M:%S", time.localtime(time.time() + delay))))
            if self.wait(delay):
                return

    def _call(self, method, path, body=None):
        """One API call; honours a single 429 Retry-After. Returns (status, parsed JSON or None)."""
        raw = json.dumps(body).encode() if body is not None else None
        for attempt in (0, 1):
            status, payload, headers = self.api(method, path, raw)
            if status == 429 and attempt == 0:
                try:
                    secs = int((headers or {}).get("Retry-After") or 0)
                except ValueError:
                    secs = 0
                if secs <= 0 or secs > MAX_RETRY_AFTER:
                    return 429, None
                self.warn("rate limited (429); waiting %ds as asked" % secs)
                if self.wait(secs):
                    return 0, None
                continue
            try:
                return status, (json.loads(payload) if payload else None)
            except ValueError:
                return status, None
        return 429, None

    def _all_sessions(self):
        """{id: session} for clash checks; {} if the catalog can't be read."""
        found, token = {}, None
        while True:
            qs = {"includeAbstracts": "false"}
            if token:
                qs["nextToken"] = token
            status, page = self._call("GET", "/events/%s/sessions?%s" % (EVENT_ID, urllib.parse.urlencode(qs)))
            if status != 200 or not page:
                return {}
            found.update({s["sessionId"]: s for s in page.get("items", []) if "sessionId" in s})
            token = page.get("nextToken")
            if not token:
                return found

    def cycle(self):
        if not self.signed_in():
            return self.warn("not signed in (or refresh failed). Please sign in again in the browser.")
        status, data = self._call("GET", "/events/%s/schedule" % EVENT_ID)
        if status == 401 or not self.signed_in():
            return self.warn("session expired and could not be refreshed. Please sign in again in the browser.")
        if status != 200 or not data:
            return self.error("could not read your schedule (HTTP %s); will retry next cycle." % status)
        schedule = data.get("schedule") or {}
        reserved = set(schedule.get("reserved") or [])
        self.reserved |= reserved
        favorites = schedule.get("favorites") or []
        if not favorites:
            return self.info("no favorites to reserve.")

        catalog = self._all_sessions()
        if not catalog:
            self.info("session times unavailable; skipping local clash check and repeat detection.")
        key_of = {sid: talk_key(s) or sid for sid, s in catalog.items()}
        key = lambda sid: key_of.get(sid) or sid
        self.reserved_talks |= {key(sid) for sid in self.reserved}

        fav_keys = {key(f) for f in favorites}
        fav_set = set(favorites)
        repeats = [s for sid, s in catalog.items() if sid not in fav_set and key(sid) in fav_keys]

        busy = busy_intervals(schedule, catalog)
        todo = []
        for sid in favorites:
            if sid in self.reserved or sid in self.skipped or key(sid) in self.reserved_talks:
                continue
            if not_reservable(catalog.get(sid)):
                self.skipped.add(sid)
                self.info("skipping %s: walk-in / no reservation needed." % self._name(sid, catalog))
                continue
            label = clash_with(catalog.get(sid) or {}, busy)
            if label:
                self.error("%s: clashes with %s; not trying." % (self._name(sid, catalog), label))
            else:
                todo.append(sid)
        for s in repeats:  # repeats are only tried when open and free; otherwise silently left alone
            sid = s["sessionId"]
            if (sid not in self.reserved and sid not in self.skipped and key(sid) not in self.reserved_talks
                    and is_open(s) and not clash_with(s, busy)):
                todo.append(sid)

        # One session per talk: open ones first, then earliest start.
        def rank(sid):
            s = catalog.get(sid) or {}
            iv = _session_interval(s) or ("~", 0, 0)
            return (0 if is_open(s) else 1, iv[0], iv[1])

        picked, seen = [], set()
        for sid in sorted(todo, key=rank):
            if key(sid) not in seen:
                seen.add(key(sid))
                picked.append(sid)
        self.key_of = key_of
        for i in range(0, len(picked), BATCH_SIZE):
            if self._stop.is_set():
                return
            self._reserve_batch(picked[i:i + BATCH_SIZE], catalog)
        # After reserving, so nothing just booked is announced as merely "open".
        self._announce(repeats, catalog, key, favorites)
        self.seen_ids = set(catalog) or self.seen_ids

    def _announce(self, repeats, catalog, key, favorites):
        """INFO lines, once per session per run, plus a one-line summary."""
        first_cycle = not self.seen_ids
        open_talks = set()
        for s in repeats:
            sid = s["sessionId"]
            if sid in self.reserved or key(sid) in self.reserved_talks:
                continue
            if not first_cycle and sid not in self.seen_ids and ("new", sid) not in self.announced:
                self.announced.add(("new", sid))
                self.info("new repeat of a favourite appeared: %s" % self._name(sid, catalog))
            if is_open(s):
                open_talks.add(key(sid))
                if ("open", sid) not in self.announced:
                    self.announced.add(("open", sid))
                    self.info("repeat of a favourite has open seats: %s" % self._name(sid, catalog))
        self.info("%d favourites, %d with open repeats" % (len(favorites), len(open_talks)))

    def _name(self, sid, catalog):
        return session_label(sid, catalog.get(sid))

    def _when(self, sid, catalog):
        iv = _session_interval(catalog.get(sid) or {})
        return " - %s %02d:%02d" % (iv[0], iv[1] // 60, iv[1] % 60) if iv else ""

    def _reserve_batch(self, batch, catalog):
        status, data = self._call("POST", "/events/%s/reservations" % EVENT_ID, {"sessionIds": batch})
        if status != 200 or not data:
            reason = "rate limited (429)" if status == 429 else "HTTP %s" % status
            for sid in batch:
                self.error("%s: not reserved, %s; will retry next cycle." % (self._name(sid, catalog), reason))
            return
        result = data.get("result") or {}
        for item in result.get("successful") or []:
            sid = item.get("sessionId") if isinstance(item, dict) else item
            self.reserved.add(sid)
            self.reserved_talks.add(self.key_of.get(sid) or sid)
            self.success("RESERVED %s%s" % (self._name(sid, catalog), self._when(sid, catalog)))
        failed = {}
        for f in result.get("failed") or []:
            failed[f.get("sessionId")] = f
        for sid in batch:
            if sid in self.reserved:
                continue
            f = failed.get(sid) or {}
            code = f.get("code")
            reason = REASONS.get(code) or code or "no result returned"
            if code == "alreadyScheduled":
                self.reserved.add(sid)
                self.info("%s is already on your schedule." % self._name(sid, catalog))
                continue
            if code in ("insufficientAccess", "sessionNotReservable"):
                self.skipped.add(sid)
                self.error("%s: cannot be reserved, %s (won't retry this run)." % (self._name(sid, catalog), reason))
                continue
            if code == "sessionFull":
                self.info("%s: full right now; will retry next cycle." % self._name(sid, catalog))
                continue
            self.error("%s: not reserved, %s; will retry next cycle." % (self._name(sid, catalog), reason))
