# Local setup on Windows

The official kit is in this folder. Docker Desktop's Linux engine is installed and running. Python 3.11 is available. GNU Make is not installed, so use the wrapper below instead of `make sample`.

## Run the selector against the official sample

From PowerShell in this folder:

```powershell
python .\run_sample.py
```

This builds the `linux/amd64` image, downloads only the pinned SQLGlot source archive, installs the exact lockfile plus pinned coverage tools, runs the test suite at image-build time, creates the impact map, runs all 30 public sample patches under the published limits, and scores the output. The map stays in the image; it is not added to this repository. Raw output is written to `output.jsonl`.

The selector combines per-test line coverage, AST scopes, fixture-to-test references, dialect-specific references, and child-process test detection. It runs the whole suite for dynamic dispatch hubs when the source evidence is insufficient. It also uses a narrow, source-derived equivalence check for enum identity comparisons and ignores only tokenizer-verified safe comment insertions. Read `SCORING.md` before changing it: each missed failing test adds 300 seconds, while selected test runtime, worker count, and selector CPU time also count.

Latest local result: **141.569 simulated seconds, zero misses** on all 30 public practice patches. I measured **0.407 seconds of selector CPU**, for **141.976 seconds total** in that run. The live board's best visible score was **136.1 seconds** when checked on November 21, 2026; this result was about 5.9 seconds behind and around third in that snapshot. The board uses the middle of three runs; this is one local run. The final set is unseen, so the practice score does not establish final performance.

The current impact map also narrows edits to numeric escape entries and `WEEK_OFFSET` constants using source-derived dialect/key and week-test references. Keep those indexes grounded in the pinned source and test fixtures; never derive them from the practice patch answers.

A pruning experiment that removed an optimizer regression-test family missed a practice failure and scored 530.130 seconds. A separate attempt to remove the whole BigQuery parser suite missed three optimizer regressions; that change was revised to keep cross-component dialect tests. The current parser, generator, dialect, and typing fallbacks use enclosing-function/class evidence where available, while preserving broader coverage when that evidence is missing. Recent iterations reduced the sample total while retaining zero misses. Do not drop a selected family without rechecking all 30 patches against the local scorer.

## Important challenge constraints

- Keep the submission repository public and at most 20 MB. Do not include sample output or generated artifacts.
- Build maps from the pinned SQLGlot commit (`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`) only. The rules allow coverage, import, and fixture maps from that pinned checkout.
- Do not build a lookup table from sample patches (or edit types) to the tests they fail.
- Final scoring uses unseen patches. The practice score is useful feedback, but only the final decides the prize.
- The competition cutoff is Sunday, November 22 at 11:00 a.m. ET.

The online submission still needs a public GitHub repository and a selected GitHub owner. This local folder has not been published or connected to a remote. The active `gh` account and local Git author differ, so choose the owner before creating or pushing the public repository.
