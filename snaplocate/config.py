"""Tunable constants.

!!! EVERY WEIGHT IN THIS FILE IS AN UNTUNED STARTING VALUE. !!!

None of these numbers have been fitted to data. They were chosen by hand before
any eval was run. Do not describe them as "tuned", "optimised" or "validated"
anywhere -- not in the README, not on a slide, not in conversation. If you tune
them, tune on the `tune` split of data/issues.csv only and record what changed.
"""

# ---- stage 3: SEARCH -------------------------------------------------------
MIN_STRING_LEN = 4      # UNTUNED. strings shorter than this are dropped
MAX_DOC_FREQ = 40       # UNTUNED. a string in more files than this is boilerplate
WEIGHT_LEN_CAP = 40     # UNTUNED. length past which a string gets no more weight
SUSPECT_BOOST = 1.5     # UNTUNED. multiplier for files hit by a suspect region

EXACTNESS = {           # UNTUNED. match-quality tiers
    "exact": 1.0,
    "icase": 0.6,
    "wsnorm": 0.4,
}

# How seed/graph scores are normalised to 0..1.
#
#   "max"    divide by the maximum. Preserves ratios; the weakest match keeps
#            a non-zero score.
#   "minmax" (x-min)/(max-min), as the original spec said. Forces the LOWEST
#            scorer to exactly 0.0 even when it matched perfectly -- measured
#            doing real damage on a 2-file match, see search.minmax().
#
# Changed from "minmax" to "max" on evidence, not taste. Set it back to
# "minmax" to reproduce the spec's behaviour.
SEED_NORM = "max"

# ---- stage 4: GRAPH --------------------------------------------------------
GRAPH_MAX_HOPS = 2      # UNTUNED
GRAPH_DECAY = 0.5       # UNTUNED. multiplier per hop

# Whether the seeded file itself (hop 0) contributes to its own graph score.
#
# This is a JUDGEMENT CALL, not something the spec settled. With it True the
# graph score partly restates the seed score, so 0.4*graph adds less new
# information. With it False the graph score is pure expansion -- a file only
# scores if a *neighbour* was hit. Both are defensible. It is exposed here so
# the eval can measure the difference instead of us guessing.
GRAPH_INCLUDE_HOP0 = True   # UNTUNED

# ---- stage 5: FUSE ---------------------------------------------------------
W_SEED = 0.6            # UNTUNED
W_GRAPH = 0.4           # UNTUNED

# ---- stage 7: OUTPUT -------------------------------------------------------
DIFF_S_MAX_NEIGHBOURS = 5   # UNTUNED. S requires 1 subsystem AND <= this
DIFF_L_MIN_SUBSYSTEMS = 3   # UNTUNED. L requires >= this many subsystems
BLAST_HOPS = 2              # UNTUNED

# ---- stage 2/6: MODEL ------------------------------------------------------
# Verified model ids (given, not independently confirmed by this code).
MODEL_DEFAULT = "gemma-4-26b-a4b-it"
MODEL_FALLBACK = "gemma-4-31b-it"
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
API_TIMEOUT = 90          # per-socket-operation timeout

# TOTAL wall-clock budget for one model call, retries and backoff included.
# urlopen's timeout is per socket operation, not total: a measured rerank ran
# 3127s (52 min) before failing. On a demo clock a prompt failure beats a slow
# answer, because the cache can rescue a failure but not a hang.
API_BUDGET = 150          # UNTUNED
API_BUDGET_RERANK = 75    # UNTUNED. rerank is optional; fail fast and degrade
MAX_REGIONS = 15
RERANK_SHORTLIST = 10
RERANK_TOPK = 3

# ---- repo scanning ---------------------------------------------------------
SOURCE_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".vue", ".svelte",
    ".go", ".rs", ".rb", ".java", ".kt", ".cs", ".php", ".c", ".h", ".cc",
    ".cpp", ".hpp", ".m", ".swift", ".scala", ".ex", ".exs", ".dart",
    ".html", ".htm", ".css", ".scss", ".sass", ".less",
}
SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "out", "target", "vendor", "third_party",
    ".next", ".nuxt", ".svelte-kit", "coverage", ".tox", ".mypy_cache",
    ".pytest_cache", "site-packages", ".idea", ".vscode",
}
MAX_FILE_BYTES = 1_000_000

# Patterns excluded from gold_files in the eval (tests/docs/lockfiles/snapshots).
NON_SOURCE_HINTS = (
    "test", "tests", "spec", "specs", "__tests__", "e2e", "fixtures",
    "docs", "doc", "examples", "example", "snapshots", "__snapshots__",
)
NON_SOURCE_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock",
    "Cargo.lock", "Gemfile.lock", "go.sum", "composer.lock",
}
