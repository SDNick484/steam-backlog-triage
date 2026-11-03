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
`score`, `playState` and `lengthHours` (`L`). It is generated from the page's own `normalize()`
and `score()` by `scripts/gen_score_vectors.mjs` in
[mcp-server-steam-backlog](https://github.com/SDNick484/mcp-server-steam-backlog), and both repos
keep a byte-identical copy. `tests/test_score_vectors.py` checks the page against it (it needs
`node` and skips without it). When `score()` changes, regenerate the vectors and update both repos.
