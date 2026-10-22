---
name: snaplocate
description: Locate the source files behind a screenshot in a GitHub issue. Use when an issue has a screenshot of a UI problem and no file names, and you need a starting point for where in the repo to look. Reads the screenshot with Gemma 4 vision, greps the on-screen strings against the repo, and expands the hits over a code graph.
---

# SnapLocate

Turns "it's broken [screenshot]" into a ranked list of source files, a call
path, an S/M/L difficulty label and a blast radius.

## When to use this

- A GitHub issue has a **screenshot** and names **no files**.
- The screenshot contains **text** — an error message, a label, a wrong value.
- You have a **local clone** of the repo at the commit the issue refers to.
- You want a starting point for a first-time contributor.

## When NOT to use this

Each of these is a case where SnapLocate will look confident and be wrong.

- **No text in the screenshot.** A purely visual bug — misaligned button,
  wrong shade of blue, broken animation — gives the search stage nothing to
  grep. SnapLocate finds files by matching on-screen *strings*; no strings, no
  signal.
- **The string is generated, not literal.** `Error: 3 items failed` is built by
  interpolation and appears in no source file. Templated, pluralised,
  translated and i18n-key-driven text will miss.
- **The app is localised** and the screenshot is not in the source language.
  The string in the image will not be the string in the code.
- **The repo is not at the right commit.** At HEAD the bug may already be
  fixed and the strings changed or gone.
- **You already know the file.** This is a search tool, not a reviewer. If the
  issue says "crash in `parser.py`", just open `parser.py`.
- **You need to know the cause.** SnapLocate tells you *where the text lives*,
  which is usually near the symptom and is not always near the cause. The call
  path is a model's guess over a shortlist, not a traced execution.
- **As evidence that a file is at fault.** It ranks candidates. It does not
  diagnose.

## How to run it

```bash
# once per repo: build the code graph (Graphify is primary)
pip install graphifyy && graphify <repo>          # -> graphify-out/graph.json
# check the graph is actually usable BEFORE trusting any ranking
python3 -m snaplocate.cli doctor --repo <repo> --graph <repo>/graphify-out/graph.json

export GEMMA_API_KEY=...
python3 -m snaplocate.cli locate https://github.com/owner/repo/issues/123 \
    --repo <repo> --graph <repo>/graphify-out/graph.json
```

Add `--variant search-only` to see what the graph contributed, and `--json`
for machine-readable output.

## Reading the output honestly

- **`score`** is `0.6*search + 0.4*graph`. Those weights are **untuned
  hand-picked values**. A score of 0.91 does not mean 91% confidence; it means
  this file scored highest among the files in this repo for these strings.
- **`search` vs `graph` bars.** If the graph bar is empty across the board, the
  graph contributed nothing and you are looking at grep results.
- **"DEGRADED" lines are not cosmetic.** They say which stage did not run.
  Ranking with `graph=0.000` everywhere is a search-only result whatever the
  header says.
- **Difficulty is S/M/L from subsystem and neighbour counts**, with untuned
  thresholds. It reports `M (not confident)` when there is no graph or no
  community field rather than guessing — treat an unconfident `M` as "unknown".
- **Blast radius is reachability**: what a change here *could* affect. It is an
  upper bound, not a prediction of the diff.

## Known limits

- The bounding-box convention for Gemma 4 (`[ymin,xmin,ymax,xmax]`, 0–1000) is
  an **assumption**. Run `cli verify-boxes <image>` and look at the output
  before trusting the overlay.
- The model is told to copy on-screen text **verbatim**. When it "helpfully"
  corrects a typo, that string matches nothing — look at the dropped-strings
  line to spot it.
- Accuracy figures for this tool come from a **small, hand-built issue set**.
  They are not comparable to MM-IssueLoc, SWE-bench M, or any published
  benchmark, and should not be placed on the same axis as one.
- If `graph.json` came from the fallback builder rather than Graphify, edges
  are file-level imports found partly by regex, not call edges. `doctor` prints
  this; do not let a demo imply otherwise.
