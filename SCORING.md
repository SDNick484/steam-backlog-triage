# The couch score

The couch score ranks backlog games for playing on the couch with a Steam Deck. It is
`score()` in `backlog.html`. Other tools that need the same ranking (for example an MCP server)
implement the rules below and check themselves against `tests/score_vectors.json`.

## 1. Normalizing a game

The page normalizes every export game before scoring (`normalize()` in `backlog.html`). The rules
that affect the score:

- `playtime`, `deckPlaytime`: number, else 0; negative becomes 0.
- `deck`: one of 0, 1, 2, 3, else **0** (Unknown).
- `hltb`: kept only if it is an object whose `main` or `extra` is a non-zero finite number;
  otherwise `null`. Each of `main`, `extra`, `complete` is a finite number or `null`.
- `metacritic`: finite number or `null`.
- `controller`: `"full"` or `"partial"`, else `null`.

## 2. Derived values

- `TRIED_MIN = 120` minutes.
- **Play state:** `never` if `playtime == 0`; `tried` if `playtime < 120`; else `played`.
- **Length** `L = hltb.main || hltb.extra` (JavaScript truthiness: a `main` of 0 falls through
  to `extra`); `null` if `hltb` is null. Completionist time is never used for `L`.
- **Lane:** the triage lane, `""` if none.
- **Backlog:** play state is not `played` **and** lane is not `skip` or `done`.

## 3. The score

`score = play + deck + length + metacritic + deckPlayed + controller + next`

| Term | Value |
|---|---|
| `play` | `never` **+30**, `tried` **+22**, `played` **−20** |
| `deck` | Verified (3) **+30**, Playable (2) **+14**, Unknown (0) **0**, Unsupported (1) **−45** |
| `length` | if `L` is null: **0**. Else `L ≤ 5` **+24**, `≤ 12` **+18**, `≤ 25` **+9**, `≤ 50` **0**, `> 50` **−10** |
| `metacritic` | if `metacritic` is truthy (not null, not 0): **(metacritic − 75) / 2**. Else **0** |
| `deckPlayed` | `deckPlaytime > 0`: **+5** |
| `controller` | `controller == "full"`: **+4** |
| `next` | lane `next`: **+100** |

Unknown is never bad: a missing length, Metacritic score or Deck rating adds 0.

Plain IEEE-754 double arithmetic, no rounding. Implementations compare with an absolute tolerance
of `1e-9`.

**Example.** Outer Wilds, never played, Deck Verified, 17 h main story, Metacritic 85:
never played +30, Verified +30, 17 h +9, Metacritic +5 = **74**.

## 3a. Weights

Each term's points are multiplied by a weight before they are summed, so
`score = Σ base_points(term) × weight(term)`. The all-1 default reproduces section 3 exactly.

| Key | Range | Default |
|---|---|---|
| `play`, `deck`, `length`, `metacritic`, `deck_played`, `controller` | 0 – 3 | 1 |
| `next` | 0.5 – 2 | 1 |

Weights move in steps of **0.25** (`WEIGHT_STEP`), so every allowed weight is a dyadic rational. They
are stored in `prefs.json`, which the server owns; the page and the Python mirror (`backlog_query.py`)
both take them as input and never persist them. A term whose weighted points are zero is left out of
the breakdown (`play` always stays), and `base × weight + 0` turns a `-0` into `0`.

Exactness: base points are small integers or halves (`(metacritic − 75) / 2`), and weights are multiples
of 0.25, so each product is exactly representable in IEEE-754 doubles and both implementations compute
bit-identical terms. Sums can still differ in the last bits by order, which is why scores are compared
with the `1e-9` tolerance and the order of terms is fixed.

## 4. Ranking

- **Couch picks** (page: `renderPicks`): games in the backlog that pass the active filters,
  sorted by score descending; ties keep **export order** (the page's sort is stable, and the
  exporter writes games sorted by lower-cased name). Top 6.
- **List sort "score":** score descending, ties by name (`localeCompare`).
- The `next` bonus makes Up-next games rank first **in practice, not by rule**. The highest
  score without it is 30 + 30 + 24 + 12.5 + 5 + 4 = 105.5 (never played, Verified, ≤ 5 h,
  Metacritic 100, played on the Deck, full controller). The lowest Up-next game still in the
  backlog is 22 − 45 − 10 + (MC − 75)/2 + 100, which is 67 at Metacritic 75. So a weak Up-next
  game can rank below a strong untriaged one. Implementations MUST sort by score alone, exactly
  as the page does, and MUST NOT force lane `next` to the top.

## 5. Buckets used elsewhere (not part of the score)

These use different boundaries from the score's length bands:

- **Overview grid buckets** (`lenBucket`): on `L`: `< 5` short, `< 15` mid, `< 40` long, else
  epic; `null` → none.
- **Length filter presets** (filter panel): on `hltb[measure]` with **no fallback** (`measure` is
  `main`, `extra` or `complete`): short `≤ 4.9`, mid `5 – 14.9`, long `15 – 39.9`, epic `≥ 40`;
  "no data" means `hltb[measure]` is null.

## 6. Parity vectors

`tests/score_vectors.json` holds 33 export-shaped games with a lane each and the expected
`score`, `playState` and `lengthHours` (`L`) under the default weights (`vectors`), plus `weightSets`:
four named weight sets (`default`, `mixed`, `floor`, `ceiling`), each with every vector's `score`,
its `terms` as `[key, points]` pairs, and `rank` (all vector ids by score descending, ties by id
ascending). The file is generated from the page's own `normalize()`, `scoreTerms()` and `score()` by
`tests/gen_score_vectors.mjs` in this repo (copied from mcp-server-steam-backlog `bd9202a`). The MCP
repo keeps a byte-identical copy of the JSON. `tests/test_score_vectors.py` checks the page against it
and `tests/test_backlog_query.py` checks `backlog_query.py` (the page test needs `node` and skips without
it unless `REQUIRE_NODE=1`). When the scoring code changes, regenerate with

    node tests/gen_score_vectors.mjs backlog.html tests/score_vectors.json --source-ref "$(git rev-parse --short HEAD)"

and update the MCP repo's copy.
