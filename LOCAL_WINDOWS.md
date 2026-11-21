# Local setup on Windows

The official kit is in this folder. Docker Desktop's Linux engine is installed and running. Python 3.11 is available. GNU Make is not installed, so use the wrapper below instead of `make sample`.

## Run the selector against the official sample

From PowerShell in this folder:

```powershell
python .\run_sample.py
```

This builds the `linux/amd64` image, downloads only the pinned SQLGlot source archive, installs the exact lockfile plus pinned coverage tools, runs the 1,620-test suite once at image-build time, creates the impact map, runs all 30 public sample patches under the published limits, and scores the output. The map stays in the image; it is not added to this repository. Raw output is written to `output.jsonl`.

The selector combines per-test line coverage, innermost AST scopes, fixture-to-test references, dialect-specific references, and child-process test detection. It runs the whole suite for the dynamic dialect registry files. Read `SCORING.md` before changing it: each missed failing test adds 300 seconds, while selected test runtime, worker count, and selector CPU time also count.

Latest local result: **228.314 simulated seconds, zero misses** on all 30 public practice patches. I measured **0.388 seconds of selector CPU**, for **228.702 seconds total** in that single run. The official board takes the middle of three runs. The lowest visible practice score was **138.1 seconds** when checked, so this is a validated starting point rather than a leaderboard contender. The practice set is public and the final set is unseen; a sample score does not establish final performance.

A pruning experiment that removed one optimizer regression-test family missed a real practice failure and scored 530.130 seconds. The current selector keeps that broader coverage. A more limited fallback now expands to enclosing-function tests only when a changed source line lacks baseline coverage; insertion hunks check their own neighboring lines independently. This reduced the sample total from 229.386 to 228.314 seconds without misses. Any future cost reduction needs to preserve zero misses and should be checked against the local scorer before it replaces this version.

## Important challenge constraints

- Keep the submission repository public and at most 20 MB. Do not include sample output or generated artifacts.
- Build maps from the pinned SQLGlot commit (`eb11aec4be8cb2122afe713a4816662c3f0fb2fa`) only. The rules allow coverage, import, and fixture maps from that pinned checkout.
- Do not build a lookup table from sample patches (or edit types) to the tests they fail.
- Final scoring uses unseen patches. The practice score is useful feedback, but only the final decides the prize.
- The competition cutoff is Sunday, November 22 at 11:00 a.m. ET.

The online submission still needs a public GitHub repository. This local folder has not been published or connected to a remote.
