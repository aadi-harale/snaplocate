# SnapLocate

**A GitHub issue with a screenshot and no file names goes in. The likely source
files, the call path, an S/M/L difficulty rating and the blast radius come out.**

A first-time contributor opens an issue that says *"it's broken"* and attaches a
screenshot. There is no stack trace and no file name. SnapLocate reads the
pixels, pulls the on-screen strings, finds them in the repo, and expands the
hits over a code graph.

Python 3.10+, **standard library only**. No pip install, no npm, no build step.
It runs on a bare Droplet with `git` and `python3` and survives the venue wifi
dying.

---

## Model, licence and dependencies

| | |
|---|---|
| **Model** | `gemma-4-26b-a4b-it` (MoE). `gemma-4-31b-it` also supported. |
| **Weights** | **Open-weight**, not OSI open-source. Released under the [Gemma Terms of Use](https://ai.google.dev/gemma/terms) and the [Gemma Prohibited Use Policy](https://ai.google.dev/gemma/prohibited_use_policy) — read those before redistributing, they carry use restrictions an OSI licence does not. |
| **Size class** | 26B / 31B parameters, so a **large** language model (>10B). |
| **Access** | Served via the Google AI `generativelanguage` API in this build. Weights are downloadable, so the same JSON contract can be pointed at a self-hosted runtime without changing anything downstream. |
| **This project's licence** | MIT — see [LICENSE](LICENSE). |
| **Runtime dependencies** | **None.** Python 3.10+ standard library only. No `requirements.txt`, because there is nothing to install. |
| **Build dependencies** | None. No npm, no bundler, no build step. |
| **Optional, not required** | [`graphifyy`](https://pypi.org/project/graphifyy/) for the primary code graph. If it is absent, `snaplocate/mkgraph.py` builds a compatible graph from the standard library alone, and says so in the output. |

Verified on 2026-10-22: both model IDs are live on the
`generativelanguage` API, and the bounding-box convention was confirmed against
measured ground truth (see `snaplocate/gemma.py`).

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
