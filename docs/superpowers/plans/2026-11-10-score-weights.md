# Adjustable couch-score weights (triage PR) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user tune the couch score's seven term weights from the dashboard, stored server-side, with the page, `backlog_query.py` and Ask always computing identical scores.

**Architecture:** One multiplier per score term, multiples of 0.25, stored in `<data>/prefs.json` and served by `GET/PUT /api/prefs` (additive under API 2). `backlog_query.py`'s scoring functions take a required keyword-only `weights`; the page reads weights through one function, `liveWeights()`. Parity is pinned by `score_vectors.json`, which gains `weightSets` generated from the page by a generator that moves into this repo.

**Tech Stack:** Python 3.11+ standard library, single-file `backlog.html` (vanilla JS), node for parity tests, Playwright for browser tests.

**Spec:** `docs/superpowers/specs/2026-11-score-weights-design.md` (read §3–§5 and §7 before starting).

This plan covers the steam-backlog-triage PR only (spec §8 step 1). The MCP PR gets its own plan after this one merges.

## Global Constraints

- Python standard library only; `backlog.html` stays one file with no build step and no new CDN script.
- `API_VERSION` stays `"2"`. `SCHEMA_VERSION` unchanged. No installer change. No new top-level file.
- `WEIGHT_KEYS = ("play", "deck", "length", "metacritic", "deck_played", "controller", "next")`.
- `WEIGHT_RANGE`: `(0, 3)` for every key except `next`: `(0.5, 2)`. `WEIGHT_STEP = 0.25`. Default weight `1`.
- Weighted term points are `base * weight`; base values, bands, thresholds and labels are unchanged.
- A weighted zero is `0`, never `-0`: compute `base * w + 0.0` (Python) / `base * w + 0` (JS).
- Zero-point terms are left out of a breakdown, except `play`, which is always present.
- The existing `vectors` array in `tests/score_vectors.json` must not change; only `source` and the new `weightSets` key do.
- `prefs.json` is written only by `backlog_server.py`, under `App.lock`, with `atomic_write`.
- Ask stays read-only: no tool, schema or prompt change that lets it write weights.
- Public repo: no IPs, hostnames, domain, container ID, time zone, SteamID, notify names, real name, email or keys. Grep every diff. Author `SDNick484 <SDNick484@users.noreply.github.com>`. Branch `feature/score-weights`.

## Review Focus

1. **Weight 0 on a negative term** (played −20, Unsupported −45, length −10, Metacritic below 75): the term must disappear from the breakdown and contribute `0`, not `-0`, in both languages. Test in Task 1 and in the `floor` vector set (Task 2).
2. **A JSON boolean as a weight** (`{"deck": true}`): Python's `bool` is an `int`, so a naive number check accepts it as 1. Must be a 400. Test in Task 1.
3. **A hand-edited `prefs.json` with one bad value** (`"next": 0`, `"deck": "2"`, an unknown key, or invalid JSON): the server keeps serving, uses the default for just that key (or all defaults for unparseable JSON), and logs one line. Test in Task 3.
4. **An Ask tool that forgets `weights`**: `ask.run_tool` turns a `TypeError` into "missing or invalid arguments", so the bug would look like a model mistake. The Ask test must assert the error is `None` for every scoring tool. Test in Task 3.
5. **Dragging, then Cancel; and saving from two tabs**: Cancel must restore the saved order exactly, and a stale tab's Save must hit 412 and reload the weights instead of overwriting. Test in Task 4.

---

### Task 1: Weights in `backlog_query.py`

**Files:**
- Modify: `backlog_query.py` (constants near L31–L69; `score_breakdown`/`score` L429–L460; `sort_games` L751; `find` L779; `picks` L805; `next_queue` L813; `Snapshot` L1175; `_row`/`_detail`/`_terms` L1311–L1348; `overview_out` L1361; `find_out`/`game_out`/`picks_out`/`next_queue_out` L1380–L1438)
- Modify: `tests/test_backlog_query.py` (existing calls to `q.score`, `q.score_breakdown`, `q.picks`, `q.sort_games`, `q.next_queue`, `q.find` gain `weights=q.DEFAULT_WEIGHTS`)
- Create: `tests/test_weights.py`
- Modify: `.github/workflows/ci.yml` (unit-test module list ~L55: add `tests.test_weights`)

**Interfaces:**
- Produces:
  - `WEIGHT_KEYS: tuple[str, ...]`, `WEIGHT_RANGE: dict[str, tuple[float, float]]`, `WEIGHT_STEP: float = 0.25`
  - `Weights = Mapping[str, float]`; `DEFAULT_WEIGHTS: Weights` (a `types.MappingProxyType`, every key `1`)
  - `check_weights(raw: Any) -> dict[str, float]`: strict, for PUT. `raw` must be a dict; absent keys become `1`; raises `QueryError` naming the key on an unknown key, a non-number (including `bool`), a non-finite number, a value outside `WEIGHT_RANGE[key]`, or a value that isn't a multiple of 0.25. Returns all seven keys.
  - `clean_weights(raw: Any) -> tuple[dict[str, float], list[str]]`: tolerant, for reading the file. Returns all seven keys (bad or missing ones at `1`) and one short problem string per thing it fixed (e.g. `"next: out of range"`).
  - `is_default_weights(w: Weights) -> bool`
  - `score_breakdown(g, lane, *, weights: Weights) -> list[Term]`, `score(g, lane, *, weights: Weights) -> float`
  - `sort_games(games, sort, triage, *, weights)`, `find(games, f, triage, notes, *, sort="score", relax=True, weights)`, `picks(games, f, triage, notes, *, exclude_next=False, weights)`, `next_queue(games, triage, order, *, weights)`: `weights` keyword-only with **no default**.
  - `Snapshot.weights: Weights = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))` (a default here only because `Snapshot` is built positionally in tests and the MCP; Task 3's Ask test is the guard for this path). Every `*_out` function passes `snap.weights` on.
  - `overview_out` adds `"score_weights": dict(snap.weights)` and `"score_weights_default": bool`.

- [ ] **Step 1: Write the failing tests** in `tests/test_weights.py`:

```python
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
                    {"deck": 3.25}, {"deck": -0.25}, {"deck": 0.3}, {"next": 0}, {"next": 2.25}, [], "x"):
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
```

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest tests.test_weights -v`
Expected: FAIL / ERROR (`AttributeError: ... WEIGHT_KEYS`).

- [ ] **Step 3: Implement the interfaces above in `backlog_query.py`.** In `score_breakdown`, compute each term's base exactly as today, then `pts = base * weights[key] + 0.0`, and append only if `pts` is non-zero (always for `play`). Thread `weights` through every listed function and every `*_out` caller. `check_weights` tests numbers with `type(v) in (int, float)` (excludes `bool`) and the step with `(v * 4).is_integer()`. Update the module docstring's function list.

- [ ] **Step 4: Update every existing caller in `tests/test_backlog_query.py`** to pass `weights=q.DEFAULT_WEIGHTS`; expectations don't change.

- [ ] **Step 5: Run the tests**

Run: `REQUIRE_NODE=1 python3 -m unittest tests.test_weights tests.test_backlog_query tests.test_stats tests.test_ask tests.test_history -v`
Expected: PASS, nothing skipped. (`test_ask` and `test_history` prove no other caller was missed.)

- [ ] **Step 6: Commit**

```bash
git add backlog_query.py tests/test_weights.py tests/test_backlog_query.py .github/workflows/ci.yml
git commit -m "backlog_query: per-term score weights, required at every scoring call"
```

---

### Task 2: Page scoring, the generator and weighted vectors

**Files:**
- Modify: `backlog.html` (constants beside `TRIED_MIN` L560; `score()` L1164–L1176; `S` state ~L661)
- Create: `tests/gen_score_vectors.mjs` (start from mcp-server-steam-backlog `bd9202a` `scripts/gen_score_vectors.mjs`; record that commit in its header)
- Modify: `tests/score_vectors.json` (regenerated)
- Modify: `tests/test_score_vectors.py`, `tests/test_backlog_query.py` (vector test ~L313)
- Modify: `SCORING.md` (new §3a "Weights"; §6 for `weightSets` and the generator's new path)

**Interfaces:**
- Consumes: Task 1's `WEIGHT_KEYS`, `WEIGHT_RANGE`, `WEIGHT_STEP`, `score_breakdown(..., weights=)`.
- Produces (page globals, used by Task 4):
  - `const WEIGHT_KEYS = [...]`, `const WEIGHT_RANGE = {...}` (key → `[min, max]`), `const WEIGHT_STEP = 0.25;`, `const DEFAULT_W = Object.freeze({...all 1})`
  - `S.weights` (saved weights, `null` until loaded) and `S.wDraft` (`null` unless tuning)
  - `liveWeights()` → `S.wDraft || S.weights || DEFAULT_W`. **The only place the page reads a weight.**
  - `scoreTerms(g)` → `[[key, label, points], ...]`, same term order, omission rule and `+ 0` as Task 1; labels the same strings as `backlog_query`'s `Term.label`.
  - `score(g)` → sum of `scoreTerms(g)` points. Its callers don't change.
- Produces (vector file): top-level `weightSets: [{name, weights, expected: [{id, score, terms: [[key, points], ...]}], rank: [id, ...]}]` with sets `default` (all 1), `mixed` (`{"play": 0.5, "deck": 2, "length": 1.5, "metacritic": 0, "deck_played": 3, "controller": 0.25, "next": 0.75}`), `floor` (every key at its minimum) and `ceiling` (every key at its maximum). `rank` is every vector id sorted by score descending, ties by id ascending.

- [ ] **Step 1: Write the failing tests.** In `tests/test_score_vectors.py`:
  - `extracted()` also pulls, in this order after `TRIED_MIN`: `const WEIGHT_KEYS`, `const WEIGHT_RANGE`, `const WEIGHT_STEP`, `const DEFAULT_W`, `function liveWeights`, `function scoreTerms`; the runner stubs `const S = {weights: null, wDraft: null};`. The generator must extract the identical list so `functionsSha256` matches.
  - `test_weight_sets_present`: names are exactly `["default", "mixed", "floor", "ceiling"]`, each has 33 `expected` rows and a 33-id `rank`.
  - `test_page_matches_every_weight_set`: for each set, set `S.weights = set.weights`, and assert each vector's `score` (tolerance 1e-9), `terms` (as `[key, points]`, exact) and the set's `rank` (exact).
  - `test_page_weight_constants_match_backlog_query`: evaluate the four page constants under node and compare with `q.WEIGHT_KEYS`, `q.WEIGHT_RANGE`, `q.WEIGHT_STEP`, `dict(q.DEFAULT_WEIGHTS)`.
  - `test_default_set_equals_the_unweighted_vectors`: the `default` set's scores equal `vectors[i].expected.score` exactly.

  In `tests/test_backlog_query.py`, `test_weight_sets`: the same score, terms and rank checks through `q.score_breakdown(..., weights=set["weights"])`.

- [ ] **Step 2: Run to verify they fail**

Run: `REQUIRE_NODE=1 python3 -m unittest tests.test_score_vectors tests.test_backlog_query -v`
Expected: FAIL (`not found in backlog.html: const WEIGHT_KEYS`, missing `weightSets`).

- [ ] **Step 3: Implement the page globals** listed under Produces, and rewrite `score()` to sum `scoreTerms()`.

- [ ] **Step 4: Create `tests/gen_score_vectors.mjs`.** Same 33 cases and output as the MCP copy, plus: the extended extraction list from Step 1, the `S` stub, and the four `weightSets` computed from the page. Default output path `tests/score_vectors.json`.

- [ ] **Step 5: Regenerate and check the `vectors` array didn't move**

Run: `git show HEAD:tests/score_vectors.json > /tmp/old.json && node tests/gen_score_vectors.mjs backlog.html tests/score_vectors.json --source-ref "$(git rev-parse --short HEAD)" && python3 -c "import json;a=json.load(open('/tmp/old.json'));b=json.load(open('tests/score_vectors.json'));assert a['vectors']==b['vectors'];print('vectors unchanged')"`
Expected: `wrote 33 vectors ...` then `vectors unchanged`.

- [ ] **Step 6: Run the tests**

Run: `REQUIRE_NODE=1 python3 -m unittest tests.test_score_vectors tests.test_backlog_query tests.test_weights -v`
Expected: PASS, nothing skipped.

- [ ] **Step 7: Update SCORING.md**: §3a lists the keys, ranges, step, default, where they're stored (`prefs.json`, owned by the server) and the exactness argument (spec §3 point 3); §6 describes `weightSets` and the generator at `tests/gen_score_vectors.mjs`, and no longer claims the MCP owns it.

- [ ] **Step 8: Commit**

```bash
git add backlog.html tests/gen_score_vectors.mjs tests/score_vectors.json tests/test_score_vectors.py tests/test_backlog_query.py SCORING.md
git commit -m "page: weighted scoreTerms; vector generator moves here and gains weight sets"
```

---

### Task 3: `/api/prefs`, `prefs.json` and Ask

**Files:**
- Modify: `backlog_server.py` (docstring L30–L50; `App.__init__` ~L290; new `read_prefs`/`load_weights` beside `read_triage` L423; `ask_snapshot` L359–L376; `do_GET` ~L918; `do_PUT` ~L955)
- Modify: `ask.py` (system prompt L340–L342)
- Modify: `README.md` (API table ~L170–L180: rows for `GET /api/prefs` and `PUT /api/prefs`; a sentence that `prefs.json` holds the weights)
- Modify: `CLAUDE.md` (the "only `backlog_server.py` writes" rule names `prefs.json`)
- Modify: `tests/test_server.py` (covered-routes set L743), `tests/test_ask.py`
- Create: `tests/test_prefs.py`
- Modify: `.github/workflows/ci.yml` (unit-test module list: add `tests.test_prefs`)

**Interfaces:**
- Consumes: Task 1's `check_weights`, `clean_weights`, `is_default_weights`, `DEFAULT_WEIGHTS`, `Snapshot.weights`.
- Produces:
  - `PREFS_MAX_BODY = 4096`; `App.prefs` (path `<data>/prefs.json`)
  - `App.load_weights() -> dict[str, float]`: reads the file through `clean_weights`; missing file → defaults silently; problems → one log line naming them (no values).
  - `App.read_prefs() -> dict`: `{"weights": load_weights(), "default": is_default_weights(...)}` (the GET body).
  - `GET /api/prefs` via `small_resource(app.read_prefs)` (ETag, 304).
  - `PUT /api/prefs` body `{"weights": {...}}`: 400 when `Content-Length` > `PREFS_MAX_BODY` (checked before `self.body()` reads), when the body isn't an object, or when `check_weights` raises (error text from the `QueryError`). Under `app.lock`: current ETag over `dump(read_prefs())`, `if_match_ok` → else `precondition_failed`; write `{"version": 1, "weights": stored}` with `atomic_write`; 200 `{"ok": true, "weights": stored, "previous": previous_weights}` with the new ETag.
  - `ask_snapshot()` reads `load_weights()` inside the same `with self.lock:` as triage/notes/order and passes `weights=` to `Snapshot`.
  - Ask prompt: "Up next games get +100, so they usually come first." → "Up next games get a large bonus, so they usually come first; the breakdown has the actual points."

- [ ] **Step 1: Write the failing tests** in `tests/test_prefs.py` using `_support.Server`:
  - `test_get_without_a_file`: 200, `{"weights": {all 1}, "default": true}`, ETag present, no `prefs.json` created.
  - `test_put_stores_and_returns_previous`: PUT `{"weights": {"deck": 2}}` → 200, `weights.deck == 2`, other keys 1, `previous` all 1; GET returns it with `default: false`; the file on disk has `"version": 1` and all seven keys.
  - `test_put_empty_resets`: after a custom PUT, PUT `{"weights": {}}` → all 1, `default: true`.
  - `test_if_none_match_304` and `test_if_match_412`: a stale If-Match → 412 with `etag`, file unchanged; the current one → 200.
  - `test_put_400s`: each of `[]`, `{"weights": []}`, `{"weights": {"nope": 1}}`, `{"weights": {"deck": true}}`, `{"weights": {"next": 0}}`, `{"weights": {"deck": 0.3}}` → 400; a 5,000-byte body → 400; none of them creates the file.
  - `test_cross_site_put_403`: `Sec-Fetch-Site: cross-site` → 403.
  - `test_bad_file_falls_back_per_key`: write `{"version":1,"weights":{"deck":2,"next":0,"zzz":1}}` → GET gives `deck` 2 and `next` 1; then write `not json` → all defaults; server still answers `/healthz`; `server.log_text()` mentions `prefs.json` and contains neither `0` value nor the word `zzz`'s value.
  - `test_api_version_still_2`: `X-Backlog-Api` is `2` on `/api/prefs` responses.

  In `tests/test_server.py`, add `("GET", "/api/prefs")` and `("PUT", "/api/prefs")` to `covered`.

  In `tests/test_ask.py` (an `AskServerBase` subclass), `test_tools_use_saved_weights`: write `prefs.json` with `MIXED` (Task 1's dict) into `self.data`; `snap = self.app.ask_snapshot()`; assert `snap.weights == MIXED`; for each of `find_games {}`, `get_game {"game": <a fixture name>}`, `suggest_picks {}`, `get_next_queue {}` via `ask.run_tool(snap, name, args)`: the error is `None`, and every returned score equals `bq.score(g, lane, weights=MIXED)`; and at least one differs from `bq.score(g, lane, weights=bq.DEFAULT_WEIGHTS)` (so the test can't pass vacuously). Also `test_system_prompt_has_no_fixed_next_bonus`: `"+100"` not in the system prompt.

- [ ] **Step 2: Run to verify they fail**

Run: `python3 -m unittest tests.test_prefs tests.test_server tests.test_ask -v`
Expected: FAIL (404 on `/api/prefs`; README table check fails; Ask test fails).

- [ ] **Step 3: Implement the server, Ask and doc changes** listed under Produces. The README rows: `GET | /api/prefs | The couch-score weights: {"weights": {...}, "default": bool}` and `PUT | /api/prefs | {"weights": {"deck": 1.5}}; absent keys reset to 1; If-Match honored`.

- [ ] **Step 4: Run the tests**

Run: `REQUIRE_NODE=1 REQUIRE_OPENSSL=1 python3 -m unittest tests.test_prefs tests.test_server tests.test_ask tests.test_hardening tests.test_history -v`
Expected: PASS, nothing skipped.

- [ ] **Step 5: Commit**

```bash
git add backlog_server.py ask.py README.md CLAUDE.md tests/test_prefs.py tests/test_server.py tests/test_ask.py .github/workflows/ci.yml
git commit -m "server: GET/PUT /api/prefs in prefs.json; Ask ranks with the saved weights"
```

---

### Task 4: Tune picks panel, badge and breakdown

**Files:**
- Modify: `backlog.html` (Couch picks markup L434–L441; `pickCard` L1402; `renderPicks` L1497; `bootServer` L2525; the delegated click handler; CSS near the `.picks` rules)
- Modify: `README.md` (a "Tuning the couch picks" paragraph under the Couch picks bullet L15)
- Create: `tests/browser/test_weights_page.py`
- Modify: `.github/workflows/ci.yml` (page-smoke job L102: add `tests.browser.test_weights_page`)

**Interfaces:**
- Consumes: Task 2's `WEIGHT_KEYS`, `WEIGHT_RANGE`, `WEIGHT_STEP`, `DEFAULT_W`, `S.weights`, `S.wDraft`, `liveWeights()`, `scoreTerms()`; Task 3's routes.
- Produces (page): `S.prefsOn` (bool), `S.prefsEtag`; `loadPrefs()` (called in `bootServer` beside triage/notes/order; a 404 or network error leaves `S.prefsOn = false` and defaults); `savePrefs()`; element ids `tuneBtn`, `tunePanel`, `tuneSave`, `tuneReset`, `tuneCancel`, `tuneMsg`, `wBadge`, sliders `w-<key>`.

Behavior (spec §5.3):
- `tuneBtn` in the Couch picks heading, shown only when `canEdit() && S.prefsOn`. Opens `tunePanel`: one `<input type="range">` per key with its `WEIGHT_RANGE` and step 0.25, a visible label, `aria-valuetext` like `"×1.5"`, and a line of effective points ("Verified +45, Playable +21, Unsupported −67.5"; for play "never +15, tried +11, played −10").
- `input` on a slider sets `S.wDraft` and re-renders only the picks, at most once per animation frame. No network request.
- `tuneSave`: PUT `{"weights": S.wDraft}` with `If-Match: S.prefsEtag`. On 200: `S.weights` = response weights, store the new ETag, clear `S.wDraft`, close, re-render the list, Next order and picks. On 412: show "The weights changed in another window; reloaded them." in `tuneMsg`, reload with `loadPrefs()`, reset the sliders. Other errors: a short message, draft kept.
- `tuneReset`: `S.wDraft = {...DEFAULT_W}`, sliders and picks update; still needs Save.
- `tuneCancel` (and Escape): `S.wDraft = null`, close, re-render picks.
- `wBadge` "Custom weights" beside the heading when `S.weights` isn't the default.
- `pickCard` gains a `<p class="terms">` line: `scoreTerms(g)` as `"<label> <signed points>"` joined by ` · `, through `esc()`.
- The picks subtitle's "Games you mark <b>Next</b> come first." becomes "Up next games get a large bonus."

- [ ] **Step 1: Write the failing browser tests** in `tests/browser/test_weights_page.py`, following `test_page.py`'s setup (Playwright, `_support.Server`, `load_fixture_library`, `REQUIRE_BROWSER`), at 390×844 in light and dark:
  - `test_saved_weights_rank_the_picks`: seed `prefs.json` with weights `W` (chosen so the fixture's default and weighted pick orders differ; the test computes both with `backlog_query.picks(..., weights=...)` and asserts `assertNotEqual` first). The six pick names on `/` equal the weighted order; `wBadge` visible; each card's `.terms` line equals `backlog_query.explain(score_breakdown(...))` rendered with ` · ` separators. With the list's sort set to Score (view All), the row order equals
    `backlog_query.sort_games(..., "score", ..., weights=W)` for the same games.
  - `test_drag_reorders_without_saving_then_cancel_restores`: record requests; open the panel, set a slider through `fill`/keyboard; picks order changes; no PUT was sent; Cancel → order equals the saved order again.
  - `test_save_persists_and_reset_needs_save`: change a slider, Save → exactly one PUT with an `If-Match` header; reload → the slider value and order persist. Reset → sliders at 1, no PUT until Save.
  - `test_stale_tab_gets_412`: two pages; save in page A; save in page B → `tuneMsg` shows the reload message and B's sliders show A's values; the file holds A's weights.
  - `test_hidden_without_prefs_route`: `page.route("**/api/prefs", lambda r: r.fulfill(status=404, body='{"error":"not found"}'))` → `tuneBtn` hidden, picks in default order, no console errors.
  - Every test: no console errors, `document.documentElement.scrollWidth <= 390`.

- [ ] **Step 2: Run to verify they fail**

Run: `REQUIRE_BROWSER=1 python3 -m unittest -v tests.browser.test_weights_page`
Expected: FAIL (`tuneBtn` not found).

- [ ] **Step 3: Implement the page changes** listed under Produces and Behavior, using the page's existing theme tokens and button styles; keyboard reachable, labelled controls.

- [ ] **Step 4: Run all browser tests**

Run: `REQUIRE_BROWSER=1 python3 -m unittest -v tests.browser.test_page tests.browser.test_filters tests.browser.test_stats_page tests.browser.test_ask_page tests.browser.test_health_banner tests.browser.test_weights_page`
Expected: PASS, nothing skipped (the existing ones prove the default ranking and the artifact-mode panel didn't change).

- [ ] **Step 5: Add the test module to CI's page-smoke list and the README paragraph**, then commit.

```bash
git add backlog.html README.md tests/browser/test_weights_page.py .github/workflows/ci.yml
git commit -m "page: Tune picks panel, custom-weights badge and a score breakdown on each pick"
```

---

### Task 5: Full local CI, PR and deploy verdict

**Files:** none new.

- [ ] **Step 1: Run everything CI runs, locally, with nothing skipped**

Run: the unit-test command from `.github/workflows/ci.yml` (Tasks 1 and 3 added their modules to it) under `REQUIRE_NODE=1 REQUIRE_OPENSSL=1`; the browser job's command under `REQUIRE_BROWSER=1`; the lint and privacy-scan jobs; and the MCP contract tests at `MCP_PINNED_REF` against this checkout (the CI job's steps).
Expected: all green; `grep -Ei 'skipped' unittest.log page.log` finds nothing. The contract job passing proves the route is additive.

- [ ] **Step 2: Privacy grep the whole diff**

Run: `git diff main...HEAD | grep -nEi '192\.168|7656119[0-9]{10}|@gmail|cloudflareaccess\.com|America/' || echo clean`
Expected: `clean` (placeholders such as `your-team.cloudflareaccess.com` are fine if any appear; check them by eye).

- [ ] **Step 3: Push and open the PR** following the SHIP PROCEDURE in `CLAUDE.md` (PR body: what changed, the exact commands with pass counts, the pending MCP PR; no deploy commands or server details). `gh pr create` may fail on GraphQL; then use `gh api repos/SDNick484/steam-backlog-triage/pulls --method POST --input body.json`.

- [ ] **Step 4: Watch checks until green**, fixing causes, never skipping or loosening a test; stop after three failed rounds.

- [ ] **Step 5: Final message**: PR URL, check summary, and **Deploy verdict A**: only `backlog.html`, `backlog_query.py`, `backlog_server.py` and `ask.py` ship; all four must be in the registry's `files=`. After deploying: from inside the CT, `curl -s http://127.0.0.1:8484/api/prefs` shows all 1s and `"default": true`; the page shows Tune picks. Remind that custom weights shouldn't be saved until the MCP PR deploys (spec §8). No tunnel or Access change.
