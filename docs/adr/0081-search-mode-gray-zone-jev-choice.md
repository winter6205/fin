# 0081. Search-mode gray-zone classify is Jev Choice

Date: 2026-09-21
Status: accepted

After Soft-Track has already chosen **grounded**, the search-mode ambiguous tier (rules miss, and inherit would not already decide) is a TypeSafe **Jev** closed-set Choice over `A | graph | global | D` on the same host client as ADR 0080. MiniMax `FIN_MODEL` JSON classify is removed from that tier and is not a fallback. Low confidence, missing key, or judge-down follows the existing cascade: **choice window** or typed 具名失败 — never silent **global**. Default pytest injects a Fake Choice. Landing this path **requires** a live Jev item (real key, assert a real 档 Choice); missing key blocks done — never a green Fake pass counted as live. Wire tokens follow `graph` / `global`, not lasting `B` / `C`. Ranking stays in iknow (ADR 0061). This amends ADR 0062’s “structured classify” clause only.

Considered Options: leave search-mode on MiniMax JSON until Soft-Track Jev ships alone; one fan-out that names track and 档 together.

## Amendment (2026-09-22): slot-migration evidence — discriminators moved from `instructions` to `criteria`

The per-option glosses that decide a 档 are now sent in the wire's
per-option `criteria` slot (`_JEV_MODE_TIER_GLOSSES`), and
`_JEV_MODE_INSTRUCTIONS` keeps only the routing sentence plus
"Answer with the token alone." The track lane mirrors the split
(`_JEV_TRACK_LEAF_CRITERIA` vs `_JEV_TRACK_INSTRUCTIONS`). This section is
the live evidence recorded before the old shape was replaced.

**Live A/B** (`/tmp/jev_ab/report.md`; 2026-09-22, real key, `POST
https://api.typesafe.ai/v1/systemone`, model `jev-latest`,
`question_id="search_mode"`, 4 utterances × 2 arms × 3 repeats = 24
serial calls, 1.2 s apart, 60 s timeout; credentials env-only and absent
from the artifacts):

- Arm I (old shape): `_JEV_MODE_INSTRUCTIONS` verbatim (four glosses in
  the prose), `criteria = {token: token}` identity.
- Arm C (migration target): task-only instructions, `_JEV_MODE_TIER_GLOSSES`
  verbatim.
- Outcome: 12/12 label hits in both arms (difference 0.0 pp; no
  transport or schema error). Arm I mean confidence 0.9883 (min 0.97),
  Arm C mean 0.9867 (min 0.97). Anchor utterance (the `graph`-expected
  ask the bare-label prompt used to miss): Arm I mean 0.9767 vs Arm C
  mean 0.9733, Δmean −0.0033. Pre-agreed gate — hit rate not lower, every
  per-utterance Δmean ≥ −0.05, Δmin ≥ −0.02, anchor mean ≥ 0.95 — all
  satisfied. The returned probability keys stay the four tokens in both
  arms, so `normalize_search_mode` and the port needed no change.
- Conclusion: **migratable** — no observable degradation and label
  correctness unchanged. At n = 3 a ±0.01 delta is not distinguishable
  from noise, so the claim is bounded to "no observed regression"; the
  opened-档 leg of `tests/host/test_live_jev_search_mode.py` stays the
  ongoing gate. The in-repo reproducible control for this A/B is the
  baseline probe arm in `tests/host/test_live_jev_track.py` (old
  instructions shape + identity criteria vs the shipped criteria arm,
  same utterance, values recorded via the warnings summary).

**Post-migration live distribution shift (honest record).** Re-running the
existing live suites after the slot switch moved every confidence leg
down while every label stayed the same:

| live leg (`via=`) | before | after |
| --- | --- | --- |
| raw choice (`raw`) | 0.980 | 0.840 |
| product seam (`decide_search_mode`) | 0.970 | 0.820 |
| seam #2 (`decide_search_mode#2`) | 0.790 | 0.720 |
| opened-档 leg (`opened_tier`) | 0.980 | 0.770 |
| judged 档 reaches wire (`live_graph`) | 0.980 | 0.850 |

Labels unchanged on all five legs; the smallest remaining margin above the
0.6 confidence floor narrowed to 0.72 (seam #2). Whether the floor should
move is an **operator decision** — this ticket does NOT change
`_MIN_JEV_CONFIDENCE`.
