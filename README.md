# SnapLocate

**A GitHub issue with a screenshot and no file names goes in. The source files
behind it come out.**

[![License: MIT](https://img.shields.io/badge/License-MIT-d97757.svg)](LICENSE)
&nbsp;[![Model: Gemma 4 (open weights)](https://img.shields.io/badge/Model-Gemma%204%20(open%20weights)-9d8df1.svg)](https://ai.google.dev/gemma/terms)
&nbsp;![Dependencies: none](https://img.shields.io/badge/Runtime%20deps-none%20(stdlib%20only)-7fa88a.svg)

---

## Try it in 30 seconds — no API key, no install, no network

Everything needed is committed. Clone and run:

```bash
git clone <this-repo> && cd snaplocate
python3 scripts/selftest.py                       # 116 checks, offline
python3 -m snaplocate.cli serve --offline     --repo demo-repo --graph demo-repo/graphify-out/graph.json
```

Open <http://localhost:8000> and click the cached issue. You get the full
pipeline — the screenshot with Gemma's bounding boxes, the ranked files, the
Graphify call graph, difficulty and blast radius — replayed from a recorded
real run, and labelled as a replay.

To run it **live** on any GitHub issue you need one free key:

```bash
export GEMMA_API_KEY=...        # https://aistudio.google.com/apikey
python3 -m snaplocate.cli locate https://github.com/owner/repo/issues/123     --repo /path/to/that/repo --graph /path/to/that/repo/graphify-out/graph.json
```

---

## What we built

A first-time contributor opens an issue that says *"it's broken"* and attaches
a screenshot. There is no stack trace and no file name, so nobody knows where
to start. SnapLocate turns that screenshot into a ranked list of source files.

Seven stages: fetch the issue → **Gemma 4 reads the screenshot** into
bounding-box regions → those exact strings are searched in the repo →
**Graphify's call graph** expands the hits two hops → the two signals are fused
→ Gemma reranks the shortlist → out come files, a call path, an S/M/L
difficulty and a blast radius.

## The AI contribution, specifically

| Stage | What the open-weight model does |
|---|---|
| 2 · XRAY | **Gemma 4 vision** reads the screenshot and returns `{surface, symptom, regions[]}` — each region a string copied **verbatim** off the screen with a bounding box. This is the thing that makes the product possible: without it there is no query. |
| 6 · RERANK | **Gemma 4** re-orders the top-10 shortlist with reasons and a call path, locked to paths that already exist in the ranking so it cannot invent a file. |

Measured on `psf/black#2531`: search alone ranked two test fixtures first;
Gemma's rerank promoted `src/black/__init__.py`, the file that actually prints
the error, to #1.

## Model and licence

| | |
|---|---|
| **Model** | `gemma-4-26b-a4b-it` (default) · `gemma-4-31b-it` also supported |
| **Licence / terms** | **[Gemma Terms of Use](https://ai.google.dev/gemma/terms)** and the **[Gemma Prohibited Use Policy](https://ai.google.dev/gemma/prohibited_use_policy)** |
| **Open-weight, not OSI open-source** | The weights are downloadable, but the Gemma Terms carry use restrictions an OSI licence does not. We are not claiming it is OSI open-source. |
| **Size class** | 26B / 31B parameters → a **large** language model (>10B) |
| **Served by** | Google AI `generativelanguage` API (verified working), or DigitalOcean Serverless Inference (`SNAPLOCATE_PROVIDER=digitalocean`, **untested**) |
| **This project** | MIT — [LICENSE](LICENSE) |

## Key dependencies

| | |
|---|---|
| **Runtime** | **None.** Python 3.10+ standard library only — `urllib` for HTTP, `http.server` for the API, `json`, `ast`, `re`. There is no `requirements.txt` because there is nothing to install. |
| **Frontend** | None. One HTML file, vanilla CSS and JS. No npm, no bundler, no CDN, no web font. |
| **Optional, build-time** | [`graphifyy`](https://pypi.org/project/graphifyy/) builds the code graph (`pip install graphifyy`). It writes `graph.json`; our code reads that file with the stdlib. If it is absent, `snaplocate/mkgraph.py` builds a compatible graph and **says so in the output**. |

Verify for yourself — this prints nothing but stdlib module names:

```bash
python3 - <<'EOF'
import ast, os, sys
std = set(sys.stdlib_module_names); bad = []
for dp, dn, fns in os.walk("snaplocate"):
    for fn in (f for f in fns if f.endswith(".py")):
        for n in ast.walk(ast.parse(open(os.path.join(dp, fn)).read())):
            mods = ([a.name.split(".")[0] for a in n.names] if isinstance(n, ast.Import)
                    else [n.module.split(".")[0]] if isinstance(n, ast.ImportFrom) and not n.level and n.module
                    else [])
            bad += [m for m in mods if m not in std and m != "snaplocate"]
print("third-party runtime imports:", sorted(set(bad)) or "NONE")
EOF
```

## Agent skill

`skills/snaplocate/SKILL.md` follows the **Agent Skills open standard** —
frontmatter on line 1, only spec fields (`name`, `description`, `license`,
`compatibility`). It documents when to use SnapLocate and, just as explicitly,
**when not to**: no text in the screenshot, interpolated or translated
strings, or the wrong commit checked out.

---

## Pipeline

| # | Stage | What it does | If it fails |
|---|-------|--------------|-------------|
| 1 | FETCH | GitHub REST → title, body, comments, first image | clean error |
| 2 | XRAY | Gemma 4 vision → `{surface, symptom, regions[]}` with boxes | clean error |
| 3 | SEARCH | each region string → files, scored by length × exactness × rarity | clean error naming the strings tried |
| 4 | GRAPH | map hits to graph nodes, propagate 2 hops with 0.5 decay | **→ search-only**, says so |
| 5 | FUSE | `0.6*search + 0.4*graph`, deterministic tie-break | — |
| 6 | RERANK | top 10 → Gemma → top 3 + call path, shortlist-locked | **→ fused ranking**, says so |
| 7 | OUTPUT | difficulty, blast radius, region→file map | difficulty = `M (not confident)` |

Every stage degrades instead of crashing. `scripts/selftest.py` exercises each
of those failure paths explicitly.

---

## Quick start

```bash
python3 scripts/selftest.py          # no network, no API key needed
```

```bash
export GEMMA_API_KEY=...             # https://aistudio.google.com/apikey
export GITHUB_TOKEN=...              # optional; 60 req/hr -> 5000
```

**Get a demo target ready in one command** — clones at the commit you want and
builds the graph:

```bash
python3 scripts/setup_demo.py --repo https://github.com/owner/name --commit abc1234
```

It prints the exact `doctor` / `locate` / `serve` commands for that checkout.
Other modes:

```bash
# let it find the base commit from the fix PR, and propose gold_files
python3 scripts/setup_demo.py --repo https://github.com/owner/name --pr 456

# set up EVERY repo in the eval CSV, each at its own base commit
python3 scripts/setup_demo.py --from-csv data/issues.csv
```

`--pr` resolves the PR's base commit, lists the files it changed, and splits
them into keep/drop against the gold-file exclusion rule — if nothing is left
after exclusions it says the PR is unusable as an eval row rather than letting
you add a junk one.

It **refuses to fall back to the default branch** if the commit you asked for
cannot be resolved. Silently checking out main is how you end up evaluating
against code that already contains the fix.

Then, by hand if you prefer:

```bash
pip install graphifyy && graphify /path/to/repo     # -> graphify-out/graph.json
python3 -m snaplocate.cli doctor --repo /path/to/repo \
    --graph /path/to/repo/graphify-out/graph.json
python3 -m snaplocate.cli locate https://github.com/owner/repo/issues/123 \
    --repo /path/to/repo --graph /path/to/repo/graphify-out/graph.json
python3 -m snaplocate.cli serve --repo /path/to/repo \
    --graph /path/to/repo/graphify-out/graph.json --port 8000
```

### `doctor` first, always

`graph.json`'s field names are not something this project hard-codes. `doctor`
**sniffs** the schema — it scores every candidate key on how path-like its
values are — and prints what it picked:

```
  path field     : path
      path           coverage=1.00 quality=1.00 score=1.000
      id             coverage=1.00 quality=0.95 score=0.950
  community field: community
  resolved     : 140 of 142 files map to a graph node
```

If it reports **0 files resolved**, open `graph.json`, find the field holding
the file path, and add its name to `PATH_KEYS` in `snaplocate/graph.py`.

---

## Commands

| Command | Purpose |
|---|---|
| `doctor --graph <p> --repo <p>` | print the sniffed graph schema and resolution rate |
| `locate <issue_url> --repo --graph` | run the pipeline; `--json`, `--variant search-only` |
| `verify-boxes <image> --out b.html` | **render real Gemma boxes over a real screenshot** |
| `mkgraph --repo --out` | fallback graph builder (insurance only) |
| `eval <csv> --split test --ablation` | Top-1 / Top-3 / MRR / median latency |
| `serve --repo --graph --port 8000` | web UI; `--record`, `--offline` |
| `cache` | list cached runs, pre-flight offline mode |

---

## Things this project will not pretend about

**The bounding-box convention is unverified.** `[ymin,xmin,ymax,xmax]` on a
0–1000 scale is an *assumption* carried over from the Gemini/PaliGemma family;
it has not been confirmed for Gemma 4. It is defined in exactly one place —
`BOX_ORDER` / `BOX_SCALE` in `snaplocate/gemma.py` — and the web UI reads it
off the API response rather than hard-coding it, so correcting it there fixes
the overlay too. Run `cli verify-boxes` on a real screenshot and look at the
result before trusting any box.

**Every weight is untuned.** `0.6/0.4`, the `0.5` hop decay, the S/M/L
thresholds, the `1.5x` suspect boost — all hand-picked before any data existed,
all labelled as such in `snaplocate/config.py`. They have not been fitted and
must not be described as tuned or validated.

**The accuracy numbers come from a tiny set.** The eval is a handful of
hand-built issues. At that size a single issue moves Top-1 by double-digit
percentage points. These numbers are **not comparable to MM-IssueLoc, SWE-bench
M or any published benchmark** — different issues, different repos, a different
gold definition, and orders of magnitude fewer instances. Do not put them on the
same axis.

**The graph has to earn its place.** `--variant search-only` is the ablation.
If the graph does not improve the ranking, that gets reported, and the argument
for it becomes the call path and blast radius — not a number that was not there.

**Which graph is in use is stated, not implied.** Graphify is primary. If
`snaplocate/mkgraph.py` built the graph instead, it stamps its own caveats into
`graph.json` and `doctor` prints them in capitals. The fallback is file-level
*import* edges (Python via `ast`, JS/TS via regex) and connected components, not
call edges and not Leiden.

---

## Eval

```bash
python3 -m snaplocate.cli eval data/issues.csv --check     # validate first
python3 -m snaplocate.cli eval data/issues.csv --split tune
python3 -m snaplocate.cli eval data/issues.csv --split test \
    --ablation --i-am-done-tuning --out eval-test.json
```

`data/issues.csv` holds closed issues that have a screenshot **and** a linked
merged fix PR. `gold_files` are the source files that PR changed, excluding
tests, docs, lockfiles and snapshots. Rows run against the PR's **base commit** —
`--check` refuses to run if a repo is sitting on HEAD, because there the fix is
already present and the measurement is meaningless.

The first rows are the `tune` split; the rest are `test`. `--split test`
requires `--i-am-done-tuning`. An issue that errors counts as **0**, it is not
dropped — otherwise the figure would be accuracy over only the issues that
happened to work.

---

## DigitalOcean

Two independent ways DigitalOcean carries this project:

**1. The model itself runs on DigitalOcean.** Gemma 4 is open-weight, so the
same model is served by DigitalOcean Serverless Inference. SnapLocate treats
the provider as swappable behind one JSON contract — nothing downstream knows
which is in use:

```bash
export SNAPLOCATE_PROVIDER=digitalocean
export DIGITALOCEAN_INFERENCE_KEY=...
python3 -m snaplocate.cli locate <issue_url> --repo <r> --graph <g>
```

`https://inference.do-ai.run/v1`, OpenAI-compatible, model `gemma-4-31B-it`.
**This path is written but NOT TESTED** — no DigitalOcean key was available.
The Google path is verified working; try the DO one before relying on it.

**2. The app runs on a Droplet.** `deploy/droplet.sh` — Ubuntu, systemd, ufw.
Because the backend is stdlib-only there is nothing to `pip install`: git +
python3 + the repo and it serves.

---

## Offline demo mode

The venue wifi dies, or the API rate-limits you mid-demo. Record a real run
beforehand and it replays from disk.

```bash
# before the demo, with the network up
python3 -m snaplocate.cli locate <issue_url> --repo <r> --graph <g> --record

# pre-flight: would it actually replay?
python3 -m snaplocate.cli cache

# the safety net, live
python3 -m snaplocate.cli serve --repo <r> --graph <g> --record
```

With `--record`, every successful live run is cached. If a later run fails —
no network, no API key, rate limit, GitHub down — a cached run for that issue
**replays automatically** instead of showing the audience a stack trace.
`serve --offline` never touches the network at all.

**A replay is always labelled as a replay.** The UI shows an amber banner with
the recording timestamp and age, the CLI prints a boxed notice, and
`degraded[0]` says so in the JSON. It is the same rule as the fallback graph:
the demo has to survive the wifi, not the audience's trust. A failed run is
never cached — replaying an error is just a second way to fail on stage.

---

## Deploy (DigitalOcean)

```bash
ssh root@<droplet-ip>
GEMMA_API_KEY=xxx REPO_URL=https://github.com/you/snaplocate bash droplet.sh
```

Ubuntu Droplet, systemd service (survives the SSH session closing), `ufw`
allowing 22 and 8000. The script installs **git and python3 only** — there is
nothing to pip install, by design — and refuses to start the service if
`scripts/selftest.py` fails.

Plain HTTP, no TLS, no auth: fine for a demo, not for leaving up.

---

## Layout

```
snaplocate/
  config.py    all weights, every one labelled UNTUNED
  gh.py        stage 1  GitHub REST via urllib
  gemma.py     stages 2+6  vision and rerank; BOX_ORDER lives here
  jsonx.py     defensive JSON extraction from free-form model text
  search.py    stage 3  string matching and seed scoring
  graph.py     stage 4  schema sniffing, PATH_KEYS, hop propagation
  fuse.py      stages 5+7  fusion, difficulty, blast radius
  pipeline.py  the seven stages and the degradation contract
  mkgraph.py   fallback graph builder (insurance only)
  evalh.py     metrics and CSV validation
  cli.py       locate / doctor / mkgraph / verify-boxes / eval / serve
  server.py    stdlib http.server
  cache.py     offline demo mode: record a run, replay it when the net dies
web/static/index.html    the whole frontend, vanilla JS and CSS
scripts/selftest.py      111 checks, no network, no API key
scripts/setup_demo.py    clone a repo at a commit + build its graph
deploy/droplet.sh        Ubuntu + systemd + ufw
skills/snaplocate/       agent skill, including when NOT to use it
```
