import json
import random
import unittest
from unittest import mock

import auto_reserve as ar


def sess(sid, time="10:00", length=60, date="2026-11-30", title=None):
    return {"sessionId": sid, "seatAvailability": "available", "title": title or sid.upper(), "sessionTime": {"date": date, "time": time, "length": length}}


class Fake:
    """Stub Events API; records calls. Never touches the network."""

    def __init__(self, favorites, reserved=(), personal=(), catalog=(), results=None):
        self.schedule = {"favorites": list(favorites), "reserved": list(reserved), "personalTime": list(personal)}
        self.catalog = list(catalog)
        self.results = results or {}  # sid -> failure code
        self.reserve_calls = []

    def __call__(self, method, path, body):
        if path.endswith("/schedule"):
            return 200, json.dumps({"schedule": self.schedule}).encode(), {}
        if "/sessions" in path:
            return 200, json.dumps({"items": self.catalog}).encode(), {}
        ids = json.loads(body)["sessionIds"]
        self.reserve_calls.append(ids)
        ok = [i for i in ids if i not in self.results]
        bad = [{"sessionId": i, "code": self.results[i]} for i in ids if i in self.results]
        return 200, json.dumps({"result": {"successful": ok, "failed": bad}}).encode(), {}


def make(api, signed_in=True):
    logs = []
    w = ar.AutoReserver(api, lambda: signed_in, lambda: None, log=logs.append, wait=lambda s: False)
    return w, logs


class AutoReserveTest(unittest.TestCase):
    def test_jitter_bounds_and_not_fixed(self):
        rng = random.Random(1)
        vals = [ar.next_delay(rng) for _ in range(500)]
        self.assertTrue(all(ar.AUTO_MIN_MINUTES * 60 <= v <= ar.AUTO_MAX_MINUTES * 60 for v in vals))
        self.assertGreater(len(set(vals)), 100)
        self.assertEqual((ar.AUTO_MIN_MINUTES, ar.AUTO_MAX_MINUTES), (10, 20))

    def test_skips_reserved_and_clashing(self):
        api = Fake(["a", "b", "c", "d"], reserved=["a"],
                   personal=[{"startDateTime": "2026-11-30T14:00:00", "endDateTime": "2026-11-30T15:00:00", "title": "Lunch"}],
                   catalog=[sess("a"), sess("b", "10:30"), sess("c", "14:30"), sess("d", "16:00")])
        w, logs = make(api)
        w.cycle()
        self.assertEqual(api.reserve_calls, [["d"]])
        self.assertTrue(any("ERROR" in l and "b" in l and "clashes" in l for l in logs))
        self.assertTrue(any("ERROR" in l and "Lunch" in l for l in logs))

    def test_batches_of_ten(self):
        ids = ["s%02d" % i for i in range(25)]
        api = Fake(ids)
        w, _ = make(api)
        w.cycle()
        self.assertEqual([len(c) for c in api.reserve_calls], [10, 10, 5])

    def test_refusal_logged_and_retried_next_cycle(self):
        api = Fake(["a", "b"], catalog=[sess("a", title="Cool Talk"), sess("b", "13:00")], results={"a": "sessionFull"})
        w, logs = make(api)
        w.cycle()
        self.assertFalse(any(l.startswith("ERROR") for l in logs))
        full = [l for l in logs if "Cool Talk" in l]
        self.assertEqual(len(full), 1)
        self.assertIn("full", full[0])
        self.assertTrue(any("RESERVED b" in l for l in logs))
        api.results = {}
        w.cycle()
        self.assertEqual(api.reserve_calls, [["a", "b"], ["a"]])

    def test_stops_after_reserved(self):
        api = Fake(["a"])
        w, _ = make(api)
        w.cycle()
        api.schedule["reserved"] = []  # even if the schedule lags, we remember
        w.cycle()
        self.assertEqual(api.reserve_calls, [["a"]])

    def test_skips_walk_in_once_and_gives_up_on_no_access(self):
        walk = dict(sess("w", "09:00"), seatAvailability="walkUp")
        nores = dict(sess("n", "11:00"), isReservable=False)
        api = Fake(["w", "n", "x", "a"], catalog=[walk, nores, sess("x", "12:00"), sess("a", "13:00")],
                   results={"x": "insufficientAccess"})
        w, logs = make(api)
        w.cycle()
        w.cycle()
        self.assertEqual(api.reserve_calls, [["x", "a"]])
        errors = [l for l in logs if l.startswith("ERROR")]
        self.assertEqual(len(errors), 1)  # only the no-access one, once
        self.assertIn("x", errors[0])
        self.assertEqual(sum("skipping w" in l for l in logs), 1)

    def test_colours(self):
        api = Fake(["ok", "no", "x"], catalog=[sess("ok"), sess("no", "11:00"), sess("x", "12:00")],
                   results={"no": "sessionFull", "x": "insufficientAccess"})
        logs = []
        w = ar.AutoReserver(api, lambda: True, lambda: None, log=logs.append, wait=lambda s: False, use_colour=True)
        w.cycle()
        self.assertTrue(any(l.startswith("\033[32m") and "RESERVED ok" in l for l in logs))
        self.assertTrue(any(l.startswith("\033[97m") and "full" in l for l in logs))
        self.assertTrue(any(l.startswith("\033[31mERROR") and l.endswith("\033[0m") for l in logs))

    def test_warn_is_yellow(self):
        logs = []
        w = ar.AutoReserver(mock.Mock(), lambda: False, lambda: None, log=logs.append, wait=lambda s: False, use_colour=True)
        w.cycle()
        self.assertTrue(logs[0].startswith("\033[33mWARN [auto-reserve]"))

    def test_no_colour_unless_tty_and_no_no_color(self):
        with mock.patch.object(ar.sys, "stdout") as out, mock.patch.dict(ar.os.environ, {}, clear=True):
            out.isatty.return_value = True
            self.assertTrue(ar.colour_enabled())
            out.isatty.return_value = False
            self.assertFalse(ar.colour_enabled())
            out.isatty.return_value = True
            ar.os.environ["NO_COLOR"] = "1"
            self.assertFalse(ar.colour_enabled())

    def test_full_is_retried(self):
        api = Fake(["f"], catalog=[dict(sess("f"), seatAvailability="unavailable")], results={"f": "sessionFull"})
        w, _ = make(api)
        w.cycle()
        w.cycle()
        self.assertEqual(api.reserve_calls, [["f"], ["f"]])

    def test_talk_key(self):
        k = ar.talk_key
        self.assertEqual(k({"abbreviation": "ARC320-R2"}), "ARC320")
        self.assertEqual(k({"abbreviation": "arc320-r1"}), "ARC320")
        self.assertEqual(k({"abbreviation": "ARC320R"}), "ARC320")
        self.assertEqual(k({"abbreviation": "ARC320"}), "ARC320")
        self.assertNotEqual(k({"abbreviation": "ARC321"}), k({"abbreviation": "ARC320-R1"}))
        self.assertEqual(k({"title": "  Cell  Based "}), k({"title": "cell based"}))

    def test_open_repeat_announced_once_and_reserved(self):
        orig = dict(sess("o", "09:00", title="Cells"), abbreviation="ARC320", seatAvailability="unavailable")
        rep = dict(sess("r", "13:00", title="Cells"), abbreviation="ARC320-R1", seatAvailability="available")
        api = Fake(["o"], catalog=[orig, rep], results={"o": "sessionFull"})
        w, logs = make(api)
        w.cycle()
        self.assertEqual(api.reserve_calls, [["r"]])  # open repeat first, one per talk
        self.assertFalse(any("open seats" in l for l in logs))  # just reserved: redundant
        self.assertTrue(any("RESERVED ARC320-R1 (Cells) - 2026-11-30 13:00" in l for l in logs))
        self.assertTrue(any("1 favourites, 0 with open repeats" in l for l in logs))
        api.schedule["reserved"] = ["r"]
        w.cycle()
        w.cycle()
        self.assertEqual(api.reserve_calls, [["r"]])  # talk done: original not tried
        self.assertFalse(any(l.startswith("ERROR") for l in logs))

    def test_open_repeat_that_clashes_announced_once_with_labels(self):
        orig = dict(sess("o", "09:00", title="Cells"), abbreviation="ARC320", seatAvailability="unavailable")
        rep = dict(sess("r", "13:00", title="Cells"), abbreviation="ARC320-R1")
        mine = dict(sess("m", "13:30", title="Other"), abbreviation="DEV100")
        api = Fake(["o"], reserved=["m"], catalog=[orig, rep, mine], results={"o": "sessionFull"})
        w, logs = make(api)
        w.cycle()
        w.cycle()
        self.assertEqual(sum("open seats: ARC320-R1 (Cells)" in l for l in logs), 1)
        self.assertEqual(api.reserve_calls, [["o"], ["o"]])
        self.assertTrue(any("ARC320 (Cells): full" in l for l in logs))

    def test_clash_label_uses_code_and_title(self):
        a = dict(sess("a", "10:00", title="Mine"), abbreviation="DEV100")
        b = dict(sess("b", "10:30", title="Wanted"), abbreviation="ARC1")
        api = Fake(["b"], reserved=["a"], catalog=[a, b])
        w, logs = make(api)
        w.cycle()
        self.assertTrue(any("ARC1 (Wanted): clashes with DEV100 (Mine)" in l for l in logs))
        self.assertEqual(ar.session_label("x", {}), "x")
        self.assertEqual(ar.session_label("x", {"title": "T"}), "x (T)")

    def test_new_repeat_announced_once(self):
        orig = dict(sess("o", "09:00"), abbreviation="ARC320", seatAvailability="unavailable")
        api = Fake(["o"], catalog=[orig], results={"o": "sessionFull"})
        w, logs = make(api)
        w.cycle()
        api.catalog.append(dict(sess("r", "13:00"), abbreviation="ARC320-R2", seatAvailability="unavailable"))
        w.cycle()
        w.cycle()
        self.assertEqual(sum("new repeat" in l for l in logs), 1)
        self.assertEqual(api.reserve_calls, [["o"], ["o"], ["o"]])  # full repeat is left alone

    def test_one_per_talk_per_batch_earliest_first(self):
        a = dict(sess("a", "15:00"), abbreviation="X100-R2")
        b = dict(sess("b", "09:00"), abbreviation="X100")
        c = dict(sess("c", "11:00"), abbreviation="X100-R1")
        api = Fake(["a", "b"], catalog=[a, b, c])
        w, _ = make(api)
        w.cycle()
        self.assertEqual(api.reserve_calls, [["b"]])

    def test_clashing_repeat_skipped(self):
        orig = dict(sess("o", "09:00"), abbreviation="Y200", seatAvailability="unavailable")
        rep = dict(sess("r", "13:00"), abbreviation="Y200-R1")
        mine = sess("m", "13:30")
        api = Fake(["o"], reserved=["m"], catalog=[orig, rep, mine], results={"o": "sessionFull"})
        w, logs = make(api)
        w.cycle()
        self.assertEqual(api.reserve_calls, [["o"]])
        self.assertFalse(any(l.startswith("ERROR") for l in logs))

    def test_not_signed_in_logs_error_and_calls_nothing(self):
        api = mock.Mock()
        w, logs = make(api, signed_in=False)
        w.cycle()
        api.assert_not_called()
        self.assertTrue(any(l.startswith("WARN") and "sign in" in l for l in logs))

    def test_refresh_failure_on_401(self):
        state = {"in": True}

        def api(method, path, body):
            state["in"] = False  # call_api clears tokens after a failed refresh
            return 401, b"{}", {}

        logs = []
        w = ar.AutoReserver(api, lambda: state["in"], lambda: None, log=logs.append, wait=lambda s: False)
        w.cycle()
        self.assertTrue(any(l.startswith("WARN") and "sign in again" in l for l in logs))

    def test_429_honours_retry_after_once(self):
        waits, calls = [], []

        def api(method, path, body):
            calls.append(path)
            if len(calls) == 1:
                return 429, b"", {"Retry-After": "7"}
            return 200, json.dumps({"schedule": {"favorites": []}}).encode(), {}

        logs = []
        w = ar.AutoReserver(api, lambda: True, lambda: None, log=logs.append, wait=lambda s: waits.append(s) or False)
        w.cycle()
        self.assertEqual(waits, [7])
        self.assertTrue(any(l.startswith("WARN") and "429" in l for l in logs))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
