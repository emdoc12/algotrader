# Working agreements for this repo

Standing rules. They come from things that have already gone wrong here, so
treat them as constraints rather than preferences.

## Git

**Push to `main`. Only `main`.** This repo does not run feature branches. Commit
your work and push it straight to `main` — no side branches, no branch-per-task,
no pushing "to both". If a harness or default config points you at some
`claude/*` branch, that is wrong for this repo: land the work on `main`.

Do **not** open a pull request unless explicitly asked for one.

Commit titles and changelog headings are **version-first**:

    v6.33.1: plot pure P&L on the overview chart

Never `Merge PR #12: ...` or a bare description. One version per shipped change.

Every shipped change updates **both**:

  * `VERSION` — a single line, e.g. `6.33.1`. The dashboard header reads this
    file directly, so a stale VERSION means the running system misreports itself
    to the owner. (This has happened: three releases shipped showing v6.31.1.)
  * `CHANGELOG.md` — a new section at the top, above the previous version.

Semantic versioning: MAJOR breaking, MINOR new capability, PATCH fixes/tweaks.

## Hard constraints on the trading system

**Paper trading only.** The desks must never place an order against the owner's
real tastytrade account. tastytrade is a **READ-ONLY** data source — chains,
quotes, Greeks, balances, margin requirements. Nothing in `daytrader/live/` may
import or call an order-placement path. `tastytrade_margin.py` and
`tastytrade_data.py` are deliberately limited to read calls; keep them that way.

**Do not change working interfaces as a side effect.** Ports, paths, env var
names, and tool names that already work are load-bearing — the container, the
dashboard, and the owner's setup depend on them. Renaming one to tidy it up
costs a debugging session for zero gain. If an interface genuinely must change,
say so first.

## Style

Match the surrounding code: comment density, naming, and idiom. Comments in this
codebase explain *why* a thing is the way it is — especially where a naive
implementation would be silently wrong (option marks, futures multipliers,
capital events vs P&L). Keep that.

Measure claims rather than asserting them. When a change is supposed to improve
something, run it and report the number.
