import inspect, unittest
import backlog_query as q

MIXED = {"play": 0.5, "deck": 2, "length": 1.5, "metacritic": 0, "deck_played": 3, "controller": 0.25, "next": 0.75}

class WeightTests(unittest.TestCase):
    def test_scoring_functions_require_weights(self):
        for fn in (q.score_breakdown, q.score, q.sort_games, q.find, q.picks, q.next_queue):
            p = inspect.signature(fn).parameters["weights"]
            self.assertEqual(p.kind, inspect.Parameter.KEYWORD_ONLY, fn.__name__)
            self.assertIs(p.default, inspect.Parameter.empty, fn.__name__)

    def test_constants(self):
        self.assertEqual(q.WEIGHT_KEYS, ("play", "deck", "length", "metacritic", "deck_played", "controller", "next"))
        self.assertEqual(q.WEIGHT_RANGE["next"], (0.5, 2))
        self.assertEqual({k for k, r in q.WEIGHT_RANGE.items() if r == (0, 3)}, set(q.WEIGHT_KEYS) - {"next"})
        self.assertEqual(dict(q.DEFAULT_WEIGHTS), {k: 1 for k in q.WEIGHT_KEYS})

    def test_check_weights_fills_defaults(self):
        self.assertEqual(q.check_weights({}), {k: 1 for k in q.WEIGHT_KEYS})
        self.assertEqual(q.check_weights({"deck": 2.5})["deck"], 2.5)

    def test_check_weights_rejects(self):
        for bad in ({"nope": 1}, {"deck": True}, {"deck": "2"}, {"deck": None}, {"deck": float("nan")},
                    {"deck": 3.25}, {"deck": -0.25}, {"deck": 0.3}, {"next": 0}, {"next": 2.25}, [], "x",
                    {"deck": float("inf")}, {"deck": 10**400}, {"play": True}, {1: 1}):
            with self.subTest(bad=bad), self.assertRaises(q.QueryError):
                q.check_weights(bad)

    def test_clean_weights_keeps_good_keys(self):
        w, problems = q.clean_weights({"deck": 2, "next": 0, "zzz": 1, "length": "x"})
        self.assertEqual((w["deck"], w["next"], w["length"]), (2, 1, 1))
        self.assertEqual(len(problems), 3)
        self.assertEqual(q.clean_weights("garbage")[0], dict(q.DEFAULT_WEIGHTS))

    def test_zero_weight_on_a_negative_term_is_dropped_and_not_negative_zero(self):
        g = q.normalize({"appid": 1, "name": "x", "playtime": 3000, "deck": 1, "metacritic": 41,
                         "hltb": {"main": 80, "extra": None, "complete": None}})
        w = {**q.DEFAULT_WEIGHTS, "play": 0, "deck": 0, "length": 0, "metacritic": 0}
        terms = q.score_breakdown(g, "", weights=w)
        self.assertEqual([t.term for t in terms], ["play"])
        self.assertEqual(str(terms[0].points), "0.0")
        self.assertEqual(str(q.score(g, "", weights=w)), "0.0")

    def test_weights_scale_each_term(self):
        g = q.normalize({"appid": 1, "name": "x", "deck": 3, "metacritic": 86,
                         "hltb": {"main": 8, "extra": None, "complete": None}})
        pts = {t.term: t.points for t in q.score_breakdown(g, "next", weights=MIXED)}
        self.assertEqual(pts, {"play": 15, "deck": 60, "length": 27, "next": 75})

    def test_overview_reports_weights(self):
        snap = q.Snapshot(games=[], weights=MIXED)
        out = q.overview_out(snap)
        self.assertEqual((out["score_weights"], out["score_weights_default"]), (MIXED, False))

    def test_huge_int_is_a_query_error_not_overflow(self):
        with self.assertRaises(q.QueryError):
            q.check_weights({"deck": 10**400})
        w, problems = q.clean_weights({"deck": 10**400})
        self.assertEqual((w["deck"], problems), (1, ["deck: not a finite number"]))

    def test_problem_text_carries_no_raw_key(self):
        raw = "evil\nkey\x1b[31m"
        with self.assertRaises(q.QueryError) as cm:
            q.check_weights({raw: 1})
        self.assertNotIn("evil", str(cm.exception))
        self.assertIn("unknown weight key", str(cm.exception))
        self.assertEqual(q.clean_weights({raw: 1, 7: 1})[1], ["unknown weight key"] * 2)

    def test_negative_zero_is_normalised(self):
        self.assertEqual(str(q.check_weights({"deck": -0.0})["deck"]), "0.0")
        self.assertEqual(str(q.clean_weights({"deck": -0.0})[0]["deck"]), "0.0")
