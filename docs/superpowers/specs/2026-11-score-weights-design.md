# Adjustable couch-score weights: design

Status: draft for review, 2026-11-10. Nothing in this document is implemented yet.

Read against: steam-backlog-triage `7468859` (main), mcp-server-steam-backlog `bd9202a` (main). Every line
number below is from those commits.

## 1. Goal and success criteria

The couch score ranks Couch picks. Its weights are constants today. The goal is to let the user tune
them from the dashboard **without the page, Ask and the MCP server ever disagreeing about a ranking**.

Success means:

- With no saved preferences, every score, breakdown and ranking is exactly what it is today.
- With saved preferences, the page, Ask's tools and every MCP tool produce the same score for every
  game, bit for bit, and so the same order.
- A test fails if any code path that reads a weight ignores the saved preferences.
- Nothing about the formula changes: same terms, same bands, same thresholds.

Out of scope: new terms, changed bands or thresholds, per-device profiles, any write path for weights
other than the page.

## 2. What the code does today

The score is seven additive terms (SCORING.md §3). The table names them by `backlog_query.Term.term`:

| Term key | Today's points | Where the constants are |
|---|---|---|
| `play` | never +30, tried +22, played −20 | page `score()` L1167; `score_breakdown()` L434 |
| `deck` | Verified +30, Playable +14, Unknown 0, Unsupported −45 | page L1168; L435 |
| `length` | ≤5 h +24, ≤12 +18, ≤25 +9, ≤50 0, >50 −10; unknown 0 | page L1170; L440 |
| `metacritic` | (MC − 75) / 2; missing or 0 → 0 | page L1171; L444 |
| `deck_played` | any Deck playtime +5 | page L1172; L448 |
| `controller` | full controller support +4 | page L1173; L450 |
| `next` | lane Up next +100 | page L1174; L452 |

### 2.1 Where V3 and the code disagree

V3 lists the adjustable terms as "never played, tried, Deck rating, ProtonDB, length, Metacritic,
reviews, Deck playtime, Next bonus". Against the code:

- **ProtonDB and reviews are not score terms.** The score never reads `proton` or `reviews`. Making them
  adjustable means adding terms, which V3 itself rules out. This spec leaves them out (§9, Q2).
- **Controller (+4) is a term V3 doesn't list.** It is included.
- **Played (−20) is part of the play term** alongside never and tried. V3 lists never and tried as two
  weights; §3 explains why this spec uses one weight per term instead (§9, Q1).

### 2.2 Where V4 and the code disagree

V4 says "the score breakdown on a row uses the live weights". **The page has no numeric breakdown
today.** Pick cards show `reasons()` chips (L1239: "Never launched", "Played on Deck", "Metacritic 85",
...), which state facts, not points. Only `backlog_query.score_breakdown()` (Ask) and the MCP produce
point-by-point breakdowns. This spec adds a breakdown to the page's pick cards (§5.3).

### 2.3 Pre-existing drift found while reading (fixed by this work, not caused by it)

- The MCP's vendored `backlog_query.py` is locked at triage `4d0b53c`; triage main has two later commits
  to that file (`8eb89d6` history row, `2e6acd4` year bounds). The MCP PR's sync brings it current.
- SCORING.md §6 says both repos keep a **byte-identical** `score_vectors.json`. They don't: the
  `vectors` arrays are equal, but triage's `source` block was regenerated at `2e6acd4` (`normalize()`
  changed) and the MCP's still names `729fa93`. The MCP PR copies triage's regenerated file, which
  restores byte identity.

## 3. Design choice: one multiplier per term

Approaches considered:

**A. One multiplier per term (recommended).** Seven weights, one per term key, each multiplying that
term's points as computed today: `points = base × w`. Default `w = 1` for every term.

**B. Points per value.** About seventeen numbers (never, tried, played, Verified, Playable, Unsupported,
five length bands, the Metacritic pivot and divisor, Deck played, controller, next).

**C. A, plus never and tried split.** Eight weights: play's never/tried values scaled separately,
played scaled with tried.

Why A:

1. **Defaults are today's constants by construction.** With every `w = 1`, `base × 1 == base` exactly
   in IEEE-754, so no existing score, vector or test can move. There is no second table of default
   points to keep in sync across three implementations; the base tables stay where they are.
2. **It matches the code's structure.** `score_breakdown()` already emits one `Term` per key; the
   weight is keyed by `Term.term`, so the breakdown, the slider and the stored key are the same name.
3. **Exact arithmetic across JavaScript and Python** (with the step in §4.1). Every base point value is a
   multiple of 0.5 (Metacritic gives k/2 for an integer score). With weights in steps of 0.25, every
   weighted term is a multiple of 1/8, and a sum of seven such values under a few thousand is exactly
   representable in a double. So scores are equal bit for bit in both languages regardless of summation
   order, ties break identically, and rankings cannot diverge by rounding. This is why the step is a
   correctness requirement, not a UI nicety.
4. **Seven sliders fit beside Couch picks**; seventeen don't, at 390 px.

What A can't do: change the balance *inside* a term. Weight 0.5 on `deck` halves the Verified boost and
the Unsupported penalty together; making "tried" count as much as "never" isn't expressible. If that
matters, C adds one weight at the cost of a second play key; B is a bigger change to the spec and the
UI. Recommendation: ship A, revisit if the split is wanted.

## 4. Data model and API (V1, V5)

### 4.1 The weights

```
WEIGHT_KEYS = ("play", "deck", "length", "metacritic", "deck_played", "controller", "next")
WEIGHT_MIN = 0, WEIGHT_MAX = 3, WEIGHT_STEP = 0.25, WEIGHT_DEFAULT = 1
```

- Every weight is a number in [0, 3] that is a multiple of 0.25 (13 slider positions).
- No negative weights: a term never flips sign, so "unknown is never bad" (unknown adds 0) still holds
  and a penalty stays a penalty.
- Weight 0 removes a term's effect. `next` at 0 means Up next gets no boost in picks.

These constants live in `backlog_query.py` and in `backlog.html`. A parity test compares the page's copy
with the module's (§7, T3). The server validates with the module's.

### 4.2 `prefs.json` (data dir, owned by `backlog_server.py`)

```json
{"version": 1, "weights": {"play": 1, "deck": 1.5, "length": 1, "metacritic": 0, "deck_played": 1, "controller": 1, "next": 1}}
```

- Written only by `PUT /api/prefs`, under `App.lock`, with `atomic_write`, like `triage.json`. The server
  always writes all seven keys.
- Missing file means all defaults. The server never creates it on its own.
- Reading is tolerant: an unreadable or malformed file, an unknown key, or an out-of-range value falls
  back to the default for what's wrong, with one log line per load. The server keeps working.
- The file name is generic so later page preferences can join it under their own key.

### 4.3 Routes

`GET /api/prefs` → 200, `{"weights": {all seven}, "default": true|false}`, with an ETag over that body
and If-None-Match → 304, through the existing `small_resource()` helper. `default` is true when every
weight is 1. Missing file: the same body with all 1s and `default: true`.

`PUT /api/prefs` with `{"weights": {...}}`:
- Keys present replace the stored value; keys absent become 1. (A full replacement, so "reset" is
  `{"weights": {}}`.)
- Optional If-Match, strong comparison, 412 `{"error": "precondition failed", "etag": ...}` on
  mismatch, through the existing `if_match_ok()` / `precondition_failed()`.
- 200 `{"ok": true, "weights": {stored}, "previous": {before}}` with the new ETag, matching the other
  PUTs' `previous` convention.
- 400 for: a body that isn't an object, `weights` not an object, an unknown key, a value that isn't a
  finite JSON number (strings, booleans, null, NaN), out of range, or not a multiple of 0.25.
  Bodies over 4 KB are refused before parsing.
- 403 for a cross-site request (the existing `refuse_cross_site()` runs first, as for every PUT).

### 4.4 API version (V5): stays "2"

The route is an additive part of API 2. Reasons:

1. Contract §1: the version goes up only for changes a client must know about; additions don't bump it.
2. **A bump would break triage's own CI under V6's order.** Triage CI runs the MCP's contract tests at
   `MCP_PINNED_REF`, and those tests compare `c.api == 2` exactly in four places (`test_contract.py`
   L316, L348, L354, L372). An API 3 triage PR would be red until an MCP change merged, but the MCP
   change needs the triage PR merged to test against.
3. A bump wouldn't protect an old MCP anyway: it ranks with constants whatever the header says.

Feature detection is therefore by route, the way the export routes already work (404/405/501 →
unavailable). This is a stated exception to contract decision D5; the contract records it as D10.

### 4.5 MCP against a server without the route

`GET /api/prefs` answering 404, 405 or 501 (API 1, or API 2 before this PR) → all defaults. The MCP
says so where weights are reported (§6.3), and `doctor` lists "score weights: not on this server".
Any other failure is the usual generic upstream error; the MCP never falls back to defaults silently
on a 5xx or a timeout, because that would be exactly the disagreement this work exists to prevent.

## 5. Changes in steam-backlog-triage (one PR, `feature/score-weights`)

### 5.1 `backlog_query.py`

- Add the constants from §4.1, `DEFAULT_WEIGHTS`, and `clean_weights(raw) -> dict` (tolerant, for the
  file) and `check_weights(raw) -> dict` (strict, raises `QueryError`, for the PUT).
- `score_breakdown(g, lane, *, weights)`: each term's points become `base * weights[key]`. Base values,
  bands and labels are unchanged. Zero-point terms are still left out, except `play`, which stays
  (its existing rule, now also when its weight is 0).
- `score(g, lane, *, weights)` sums the breakdown, as now.
- `sort_games`, `picks`, `next_queue` take `*, weights` and pass it on.
- `Snapshot` gains `weights: dict`; `_row`, `_detail`, `_terms`, the picks output and `overview_out`
  read `snap.weights`. `overview_out` also reports `score_weights` and `score_weights_default`.
- **`weights` is keyword-only and required: no default.** A call site that forgets it raises
  `TypeError` the first time any test exercises it, and mypy strict flags it in the MCP before tests
  run. Defaults are always passed explicitly (`weights=DEFAULT_WEIGHTS`). This is the main guard
  against a path that "works" but ignores the preferences.

### 5.2 `backlog_server.py`

- `App.read_prefs()` and the two routes (§4.3). `ask_snapshot()` reads prefs under the same lock as
  triage, notes and order, and puts the weights in the `Snapshot`, so Ask uses the saved weights for
  every question with no other change.
- Docstring: the routes list and "PUTs honor If-Match" sentence gain `/api/prefs`.

### 5.3 `backlog.html`

- Constants `WEIGHT_KEYS`, `WEIGHT_MIN/MAX/STEP` (parity-tested against the module), and state
  `S.weights` (saved) and `S.wDraft` (while tuning).
- `scoreTerms(g)` returns `[[key, label, points], ...]` mirroring `score_breakdown()`; `score(g)` sums it.
  Both read one function, `liveWeights()`, which returns `S.wDraft || S.weights || DEFAULTS`. That is the
  page's single read point; nothing else reads a weight.
- `bootServer()` fetches `api/prefs` with the triage, notes and order, keeping its ETag. A 404 (an older
  server) means defaults and no Tune control.
- **Tune picks** (only when `canEdit()`; hidden in sample and artifact mode, which use defaults): a
  button in the Couch picks heading opens a panel with one range input per term (0 to 3, step 0.25).
  Each shows its multiplier and what it does in points ("Deck ×1.5: Verified +45, Playable +21,
  Unsupported −67.5"). Dragging sets `S.wDraft` and re-renders the picks (once per animation frame).
  **Save** PUTs with If-Match, then re-renders the list and Next order. **Reset** sets the draft to all
  1s (Save still needed). **Cancel** drops the draft. On 412 the panel says the weights changed
  elsewhere and reloads them. Keyboard accessible, labelled, works at 390 px with no horizontal scroll.
- A "Custom weights" badge beside the Couch picks heading when saved weights aren't the default.
- Pick cards gain a one-line breakdown under the chips ("never played +30 · Verified +45 · 8 h +18"),
  from `scoreTerms`, so the numbers on screen are the numbers that ranked the card.
- The picks subtitle stops saying "Games you mark Next come first", which was already inaccurate
  (SCORING.md §4); it becomes "Up next games get a large bonus".
- Unchanged: `reasons()` chips, `lenBucket`, the length presets, `PROG_RULE`.

### 5.4 `ask.py`

- The system prompt's "Up next games get +100" (L341) becomes "a large bonus; the breakdown has the
  actual points", since the number can now change. No tool or schema change; Ask stays read-only and
  gets no way to change weights.

### 5.5 Vectors and the generator

- `scripts/gen_score_vectors.mjs` moves from the MCP repo to triage as
  `tests/gen_score_vectors.mjs` (not a shipped file, not top-level). Triage owns the page, so it should
  own the script that turns the page into vectors; and under V6 triage must regenerate the vectors
  before the MCP PR exists. The MCP's CI runs triage's copy from its `BACKLOG_REF` checkout (§6.4).
- The generator also extracts `scoreTerms`, `liveWeights` and the weight constants, and stubs `S`.
- `score_vectors.json`: the existing `vectors` array is unchanged (only `source` changes, because
  `score()`'s text changes). A new top-level `weightSets` array is added; existing consumers ignore it:

```json
"weightSets": [
  {"name": "default", "weights": {"play": 1, ...},
   "expected": [{"id": 1, "score": 30, "terms": [["play", 30]]}, ...],
   "rank": [32, 30, 9, ...]},
  {"name": "mixed", "weights": {"play": 0.5, "deck": 2, "length": 1.5, "metacritic": 0,
                                 "deck_played": 3, "controller": 0.25, "next": 0.75}, ...},
  {"name": "zero", "weights": {all 0}, ...},
  {"name": "max", "weights": {all 3}, ...}
]
```

  `terms` are `[key, points]` pairs (labels are allowed to differ between page and module); `rank` is
  every vector's id sorted by score descending, ties by id (export order). The `zero` set makes every
  score 0, which exercises the play-stays rule and the tie-break.

### 5.6 Docs

SCORING.md: a §3a "Weights" (keys, range, step, the exactness argument, default 1, where they're
stored) and §6 updated for `weightSets` and the generator's new home. README: the Tune picks control,
`prefs.json`, the API table rows for `/api/prefs`. CLAUDE.md: "nothing writes prefs.json except
backlog_server.py".

No installer change; `prefs.json` is created by the server in the data dir it already writes.

## 6. Changes in mcp-server-steam-backlog (one PR, after triage merges)

### 6.1 Contract

`docs/backlog-api-contract.md` gains §10 "Preferences" (the routes, body shapes, status codes and
examples from §4.3, the file being server-owned, the 404 → defaults rule) and decision D10 (additive
under API 2; route detection as an exception to D5, with the `c.api == 2` reason). §6.3 gains the
weights paragraph from SCORING.md.

### 6.2 Client and view

- `BacklogClient.prefs() -> Versioned | None` (None on 404/405/501). Fetched on every `view()`, like
  triage and notes: one small loopback GET per tool call, so a change saved on the page reaches the
  next tool call with no cache to invalidate.
- `View` gains `weights` and `weights_source` (`"saved"`, `"default"`, `"default: server has no
  preferences"`).

### 6.3 Every score read passes `v.weights`

Vendored `backlog_query.py` is bumped with `sync_query.py --update` to the triage merge commit; the
required keyword makes mypy list every call site that needs changing. Known sites: `domain.py`
(`picks`, the `find` path through `sort_games`), `models.row()` L309, `models.terms()` L317, the detail
model L344–345, `server.py` suggest_picks L598–599, the four `next_queue` calls (L629, L644, L861,
L1152), and the `backlog://game/{appid}` resource.

- `backlog_get_overview` reports the weights and their source.
- `backlog://scoring` appends a "Current weights" section built per read from the live prefs (the
  static part stays `scoring.md`).
- Hard-coded points leave the instruction text: INSTRUCTIONS L66–67 and the suggest_picks description
  L558–567 say "a large bonus" and point at the breakdown.
- The MCP never writes prefs; there is no tool for it (§9, Q4).
- `doctor`: "score weights: saved / default / not on this server".

### 6.4 Vectors, pins, CI

- `tests/fixtures/score_vectors.json` copied byte for byte from triage.
- `scripts/gen_score_vectors.mjs` deleted; CI's "Score vectors still match backlog.html" step runs
  `node /tmp/sbt/tests/gen_score_vectors.mjs` and compares the whole file, both arrays.
- `BACKLOG_REF` (ci.yml L36) and `DEFAULT_BACKLOG_REF` (conftest.py L37) move to the triage merge commit.

## 7. Every weight read, and the test that proves it follows prefs.json

"Reads a weight" means: holds score constants, or computes a score or ranking from them.

| # | Repo | Where | Proving test |
|---|---|---|---|
| P1 | triage | page `scoreTerms()` / `score()` / `liveWeights()` (the only read point) | T1 node parity: every weight set's scores, terms and rank, from the page's own extracted functions |
| P2 | triage | page callers: `renderPicks` L1498, list sort L1529/1536, Next view tiebreak L1526, `nextIds` L2164, artifact panel L2385 | T7 browser: with `prefs.json` = `mixed`, picks order and the score-sorted list on `/` match the order `backlog_query` gives for the same fixture library and weights; differs from the default order (no vacuous pass) |
| P3 | triage | page Tune panel and badge | T7: drag reorders picks with no network write; Save sends PUT with If-Match and survives reload; Reset; 412 message; hidden in sample mode; 390 px; light and dark |
| P4 | triage | page weight constants | T3: page's `WEIGHT_*` equal `backlog_query`'s |
| Q1 | triage | `backlog_query.score_breakdown()` / `score()` | T2: every weight set's scores, terms and rank from the vectors |
| Q2 | triage | `sort_games`, `picks`, `next_queue`, `_row`, `_detail`, `_terms`, picks output, `overview_out` | T6: `weights` is keyword-only with no default on every scoring function (`inspect.signature`), so an omitted call site can't run |
| S1 | triage | `/api/prefs` and `prefs.json` | T4: GET with no file; PUT stores and returns `previous`; 304; 412; each 400; oversize; cross-site 403; malformed file → defaults plus a log line; README route table and its covered-routes set |
| A1 | triage | Ask (`ask_snapshot` → `Snapshot.weights` → every tool) | T5: data dir with `prefs.json` = `mixed`; dispatch `find_games`, `get_game`, `suggest_picks`, `get_next_queue` through `ask_snapshot`; scores and breakdowns equal `backlog_query` with `mixed` and differ from defaults |
| M1 | MCP | vendored `backlog_query` | M-T1: `test_domain.py` runs every weight set; `sync_query --check` holds the copy to the lock |
| M2 | MCP | `domain`, `models.row/terms`, detail, `server.py` picks, `next_queue` calls, `backlog://game` | M-T2: mypy strict (required keyword). M-T3: fake server serving `mixed`; each tool and the resource return the `mixed` scores, order and breakdown |
| M3 | MCP | client `prefs()` and the fallback | M-T4: 404 → defaults with the source stated; 500 → error, not defaults; API 1 simulation |
| M4 | MCP | `backlog://scoring`, overview, doctor | M-T5: live weights appear in each |
| M5 | both | the contract | Contract tests for §10 against the fake server and the real `backlog_server.py` at `BACKLOG_REF` |
| V1 | both | vector file and generator | Triage: T1/T2. MCP CI: regenerated file equals the committed one |

Not weight readers (checked, unchanged): `reasons()` chips (facts, not points), `lenBucket`, the length
presets, `PROG_RULE`, `stats_for`, `history_row`, the MCP's `deck.py`.

## 8. Order of work, verdicts and the gap between deploys (V6)

1. **Triage PR** `feature/score-weights`. Commits: C1 `backlog_query` weights (required keyword) and
   T2/T6; C2 the generator move, page `scoreTerms`/`liveWeights`, regenerated vectors with `weightSets`,
   T1/T3; C3 the server routes and Ask snapshot, T4/T5; C4 Tune panel, badge, breakdown line, T7;
   C5 docs. CI's contract job against the pinned MCP must stay green (the route is additive).
   **Deploy verdict A**: `backlog.html`, `backlog_query.py`, `backlog_server.py`, `ask.py` change, no
   installer change, no new top-level file. All four must be in the deploy registry's `files=` list.
2. **MCP PR** `feature/score-weights`: §6. **Verdict A** (no dependency, script or build-system change).
3. **Triage pin bump**: `MCP_PINNED_REF` to the MCP merge commit.

Between steps 1 and 2 deploying, the page and Ask follow saved weights and the deployed MCP doesn't.
Until both are deployed, don't save non-default weights (the badge makes it obvious if you have).

## 9. Open questions for review

- **Q1 (main decision).** One multiplier per term (A, recommended), or also split never/tried (C)?
- **Q2.** ProtonDB and reviews stay out, since they would be new terms. Agreed?
- **Q3.** Move the vector generator to triage (`tests/gen_score_vectors.mjs`)? The alternative is a
  generator change in the MCP repo first, which inverts V6's order.
- **Q4.** No MCP or Ask tool to change weights. Agreed? (Read-only for both, as the page owns tuning.)
- **Q5.** Range 0 to 3 for every term, including `next`? A narrower `next` range (say 0.5 to 2) would
  keep Up next meaningful, but 0 is a legitimate "rank purely on fit" choice.
