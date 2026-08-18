# Changelog

All notable changes to AlgoTrader are documented here.
Format follows [Semantic Versioning](https://semver.org): MAJOR.MINOR.PATCH

- **MAJOR** — breaking changes (schema migrations, API redesigns)
- **MINOR** — new features, new strategies, new broker integrations
- **PATCH** — bug fixes, tweaks, performance improvements

---

## [6.36.7] — 2026-08-18

### Fixed — the auto-fix pipeline is verified live, end to end
The failing credential turned out to be a line break at character 80 of the
pasted OAuth token — the terminal wrapped it and the copy kept the wrap. With a
cleanly pasted token, run #5 completed the whole loop against the real test
issue: Claude read the repo, reported the VERSION, confirmed CLAUDE.md's hard
constraints, deliberately changed nothing (as the test demanded), commented, and
closed the issue as `claude[bot]`. Every link is now proven in production:
desk files → GitHub issue → auto-fix run → push to main → image build →
Watchtower pull → closed-issue broadcast to all desks.

The `show_full_output` diagnostic that exposed the token error is removed again.

## [6.36.6] — 2026-08-18

### Changed — surface the auto-fix workflow's real error text
Runs die in ~80ms with `is_error: true` at zero cost, on two different models —
so the failure is local and happens before any API work, and the action hides
the underlying message by default. `show_full_output: true` is enabled on the
dev-request workflow as a diagnostic; to be removed once the pipeline is green.

## [6.36.5] — 2026-08-18

### Fixed — the auto-fix run died instantly under a subscription token
With `CLAUDE_CODE_OAUTH_TOKEN` finally in place, the run failed in 77ms with
`is_error: true` at zero cost: the workflow pinned `--model claude-opus-5`, and
a subscription OAuth token can only use the models its plan serves, so the CLI
rejected the pin before doing any work. The pin is removed from both workflows;
the Claude Code default model tracks the best model the credential is entitled
to, which is also the right behaviour if the owner later swaps to an API key.

## [6.36.4] — 2026-08-18

### Fixed — the GitHub sync only ran during trading hours
`sync_github_resolutions` — the backfill of stranded requests AND the
closed-issue broadcasts — was called from inside `trade_all()`, which runs only
09:45–15:30 ET on market days. Consequences: the stranded backlog could not
backfill after the close (observed live: a 15:40 deploy left it waiting for the
next trading morning), a fix shipped in the evening or on a weekend would not
reach the desks' journals until the next session, and a dev request whose direct
GitHub post failed (e.g. filed during a container-restart window) stayed
invisible for just as long.

The sync now runs from the top of the always-on `run_forever` loop — every pass,
year-round, weekends included — still internally throttled to ~15 minutes and
never allowed to break the trading path. The market gates what trades; it has no
business gating a conversation with GitHub.

## [6.36.3] — 2026-08-18

### Fixed — option chains timed out on every call (the real fix this time)
Five desks independently reported the same thing: after v6.34.2, `get_option_chain`
stopped saying `no_chain` and started saying `stream_timeout` — "chain fetch
exceeded its 45s budget" — on EVERY call, across SPY, AMZN, NVDA, CRM, CSCO and
MU. One desk counted 12 consecutive failures over three sessions. v6.34.2 fixed
the budget arithmetic but not the disease.

The disease: the REST download of a symbol's ENTIRE chain (~10k contracts for
SPY) exceeds its slice on a host already running seven desks — and the design
made that pathological. `asyncio.wait_for` cannot cancel a thread, so a download
that blew its budget kept running while its result was thrown away; the retry
started a SECOND concurrent download of the same chain; and every desk retrying
every cycle piled more on. The same expensive work was done over and over,
always discarded, never once kept.

Now:

* **The chain is cached** (`OPTION_CHAIN_CACHE_TTL`, 6h) — strikes and
  expirations change about daily, and re-downloading them per call was the
  whole cost.
* **An over-budget download completes INTO the cache.** The orphaned thread's
  work is kept, so the desk's next cycle succeeds instantly instead of starting
  over. Worst case is now: first call warms, second call works.
* **An in-flight registry prevents duplicate downloads** of one symbol, with a
  staleness rescue so a genuinely hung socket cannot wedge the symbol forever.
* **Failures are fast and phase-precise**: `rest_chain_timeout` ("keeps
  downloading, retry in a minute") and `chain_downloading` ("do not stack
  another") return in seconds instead of eating the whole 45s, and a
  download-timeout is not retried within the same call. If timeouts persist
  after this fix, the code now names the failing phase, so the next desk report
  is decisive rather than opaque.
* The download budget default rises 25s → 40s (`OPTION_CHAIN_FETCH_TIMEOUT`),
  affordable now that it is paid once per symbol per session rather than per
  call.

Verified offline against a faked tastytrade SDK: a 5s download against a 2s
budget fails fast with `rest_chain_timeout`, a concurrent retry gets
`chain_downloading` without starting a second download, the background download
lands in the cache, the next call returns a full chain with Greeks in
milliseconds, and a fast fresh symbol succeeds first try.

Note for the owner: the "snapshot fallback" the desks asked for already exists —
the Alpha Vantage historical chain with a stale-quote warning — but it activates
only once `ALPHAVANTAGE_API_KEY` is set in Settings, which it still is not.

## [6.36.2] — 2026-08-18

### Added — the stranded dev-request backlog backfills itself to GitHub
Requests filed while the GITHUB_TOKEN was broken exist only in the desks' local
databases — invisible to the auto-fix pipeline, which is exactly the backlog the
owner kept relaying by hand. The resolution sync now also mirrors every open
local request that has no issue URL: it files the issue (with provenance — which
desk, when, how many times re-reported), links the local row to it, and the
normal pipeline takes over from there. Capped at 5 per pass
(`DEV_REQUEST_BACKFILL_PER_PASS`) so a large backlog trickles into the fix queue
instead of flooding it; idempotent, so a filed request is never filed twice.

Verified with a mocked GitHub: five stranded requests across three desks filed
and linked in one pass, a second pass files nothing, and a backfilled issue that
the Action later closes broadcasts to all seven desks exactly like any other.

The auto-fix workflow also triggers on `reopened` now — a run that failed
(missing secret, outage) does not re-fire on its own, but closing and reopening
the issue does, which makes retries a two-click operation instead of a manual
workflow dispatch.

## [6.36.1] — 2026-08-18

### Changed — the auto-fix workflows accept either billing credential
Both Claude workflows now pass `anthropic_api_key` and
`claude_code_oauth_token`, so the owner sets exactly one repo secret —
`ANTHROPIC_API_KEY` (API credit) or `CLAUDE_CODE_OAUTH_TOKEN` (a Claude
Pro/Max subscription, from `claude setup-token`) — and the pipeline works
without another workflow edit. The first live run confirmed everything up to
authentication: trigger, label filter, actor checks, checkout, app token, and
Claude Code install all passed, failing only on the absent key.

## [6.36.0] — 2026-08-18

### Added — dev requests now flow straight to Claude and back, no middleman
A desk's request previously waited for the owner to notice it, relay it, and
relay the fix back. The full pipeline is now:

1. A desk calls `request_dev_help` → a GitHub issue is filed (label
   `dev-request`).
2. **`.github/workflows/claude-dev-requests.yml`** runs Claude Code on the
   issue: it reads CLAUDE.md, reproduces the problem, fixes it, verifies by
   running the code, bumps VERSION + CHANGELOG, pushes to main, comments the
   resolution on the issue, and closes it. Unsafe requests (real-money trading,
   tastytrade write access, weakened rails) are refused and closed
   `not planned`; judgment calls are analysed and left open for the owner.
   Runs queue one at a time so concurrent fixes cannot race their pushes.
3. The push triggers the existing image build.
4. **`sync_github_resolutions()`** in the running container notices the closed
   issue (polled ~15 min, a few API calls a day) and routes it through the same
   `close_dev_request` broadcast as the owner's "Fixed — tell them" button —
   every desk's journal gets the resolution, `wont_fix` mapped from GitHub's
   `not_planned`. Verified end to end with a mocked GitHub: a closed issue
   closed the local request and notified all seven desks, throttling and the
   no-token case behave, and `not_planned` reads as WILL NOT BE BUILT.

Also adds **`.github/workflows/claude.yml`**: mention `@claude` in any issue
comment to discuss or amend a fix from a phone.

Two one-time setups only the owner can do: install the Claude GitHub App
(github.com/apps/claude) and add an `ANTHROPIC_API_KEY` repo secret.

### Fixed — the desk→GitHub bridge was silently dead, and misdiagnosed
The repo has **zero issues ever created** despite weeks of desk dev requests:
the POST leg has never worked on the host. Worse, the tool reported every
failure as "no GITHUB_TOKEN set" even when a token existed and the POST was
rejected — sending the owner to look for a missing setting instead of a bad
one. The message now carries the actual error. A **github row in Test
Connections** verifies the token can see the repo *and* carries write access
(what creating an issue requires), so the bridge's health is as visible as a
model key's.

## [6.35.3] — 2026-08-18

### Added — the owner's real icon, recovered and converted
The supplied icon was a JPEG. Renaming it to `.png` in the GitHub UI does not
convert it — the committed file still began `ff d8 ff e1`, and Safari would have
kept showing the generated letter square. The file was then deleted, which also
removed `daytrader/live/static/` (git does not keep empty directories), leaving
the endpoint with nothing to serve.

The artwork was recovered from git history and converted here. That needed a
**baseline JPEG decoder written from scratch** (`tools/jpeg_to_icon.py`) —
Huffman → dequantise → IDCT → chroma upsample → YCbCr→RGB, with numpy doing the
IDCT in one batched einsum — because this project has no Pillow (adding it once
clobbered pandas) and the container has no ImageMagick, djpeg or ffmpeg either.

`tools/find_tile.py` then locates the icon tile inside the render. The supplied
file is a presentation mockup — the tile floating on a backdrop with margin and
a vignette — and uploading that whole frame leaves iOS masking an already-inset
image, so the home-screen icon looks small and ringed. Brightness thresholding
does not find the tile (the backdrop gradient crosses any global threshold, and
a first attempt cropped the vignette instead of the artwork), so it detects the
tile's **edges** as the strongest sustained gradient on each axis. It found the
tile at x 8.6–83.8%, y 25.3–75.5% of the frame; the result is centred to within
5% on both axes with all four corners dark, i.e. genuinely full-bleed.

### Changed — the PNG encoder now uses adaptive row filtering
Every row was written unfiltered, which is valid but compresses badly on
photographic content. Per-row filter selection by the standard
minimum-sum-of-absolute-differences heuristic takes the icon from 416KB to
300KB, 28% smaller. Verified by unfiltering the served bytes back to pixels.

## [6.35.2] — 2026-08-18

### Added — the icon uploader now accepts a JPEG (or anything else) and converts it
Safari renders **only** a PNG as an apple-touch-icon, so a JPEG is exactly the
input that leaves the home screen showing the generated letter square. v6.35.1
refused non-PNG uploads with a clear message, which was honest but unhelpful:
most people's icon is a JPEG.

The Settings uploader now converts in the browser. The file picker accepts any
image the browser can decode — JPEG, HEIC, WebP — draws it to a canvas and
exports a PNG. Doing it client-side avoids adding an image library to the
container purely to accept a photo (Pillow is deliberately absent here), and the
browser's decoder handles formats a server-side library would not.

It also **centre-crops to a square** rather than squashing a non-square source,
and scales to 512x512 when the source allows, never upscaling a small image past
180x180. Verified across six source shapes: a 1600x900 image crops a 900px square
centred at x=350, a 3024x4032 phone photo crops centred vertically, a 120px
source lands at 180x180, and an undecodable file is rejected.

Server-side PNG validation is unchanged, so anything reaching disk is still a
real PNG.

## [6.35.1] — 2026-08-18

### Added — upload the app icon from the Settings page
Getting a new icon into the container previously meant editing the source tree
and rebuilding the image. Settings now has a **Home screen icon** card: pick a
PNG, upload, done. It is written to the **data volume**, so it survives container
updates and needs no rebuild — which matters because redeploying is the slow
step in this setup.

Validation is by PNG magic bytes rather than by trusting the filename: Safari
will not render a JPEG or SVG as an apple-touch-icon, so silently accepting one
would reproduce the original bug with an icon that looks installed but still
shows the generated letter square. Non-square and under-180px uploads are
accepted with a warning about how iOS will treat them.

Resolution order when serving `/apple-touch-icon.png` is `APP_ICON_PATH`, then
the uploaded file, then the bundled default.

### Added — Unraid picks the icon up automatically
`net.unraid.docker.icon` and `net.unraid.docker.webui` labels on the image point
at the same PNG the app serves, hosted from this repo's raw URL. So the Docker
page, the browser tab and the iOS home screen all show one file, and the Docker
page still resolves it when the container is down.

Verified end to end over HTTP: the bundled 180x180 icon serves, a 512x512 upload
replaces it in place, a JPEG is refused with an actionable message, and the good
icon still serves afterwards.

## [6.35.0] — 2026-08-18

### Added — a real home-screen icon on iOS
Added to an iPhone home screen, the dashboard showed a generated white letter on
a coloured square. The page had **no icon of any kind** — no `apple-touch-icon`,
no favicon — and Safari falls back to that placeholder when it finds none.

* A 180x180 PNG is served **by the app itself** at `/apple-touch-icon.png`, read
  off local disk on each request. Not a CDN and not GitHub: this box runs on the
  LAN with no internet, and an icon that only resolves when the WAN is up is an
  icon that intermittently disappears. `/apple-touch-icon-precomposed.png`,
  `/favicon.png` and `/favicon.ico` serve the same bytes, since iOS and browsers
  ask for several names.
* The route sits **above the `/api/` auth gate** and needs no token. iOS fetches
  the icon with a bare request, and a 401 there is precisely what produces the
  white-letter square.
* Head tags: `apple-touch-icon`, `apple-mobile-web-app-title` ("AlgoTrader"),
  `apple-mobile-web-app-capable`, `mobile-web-app-capable`, and
  `apple-mobile-web-app-status-bar-style`, plus a `rel="icon"` so desktop
  browsers stop 404ing on `/favicon.ico`.
* `APP_ICON_PATH` (Settings-editable) overrides the bundled file, so a different
  icon can be dropped onto a mounted volume and served immediately — no image
  rebuild, which matters because redeploying is the slow step here.

The icon is generated by `tools/make_icon.py`, which draws it and encodes the PNG
using only `zlib` and `struct`. Pillow is not installed in this project and
adding it once already cost a debugging session by clobbering pandas, so the
icon has no build dependency. It renders at 4x and box-downsamples for clean
anti-aliased edges, and deliberately leaves the corners square — iOS applies its
own squircle mask, and pre-rounding shows as a dark halo inside it.

Verified over HTTP: `200`, `Content-Type: image/png`, 180x180 decoded from the
bytes actually served, and present in a simulated image layer built from only
what the Dockerfile copies.

## [6.34.7] — 2026-08-17

### Fixed — the Grok desk's API cost was understated by ~2.5x
The pricing table carried `grok: (1.25, 2.50)`, which is **grok-4.3's** rate.
The desk runs grok-4.5, which bills $2.00 / $6.00 — verified against xAI's own
model-pricing endpoint rather than from memory. On a representative cycle
(40k input, 35k of it cached, 3k output) the dashboard reported $0.0181 against
an actual $0.0455.

Cached input is now priced from the provider's published rate where we have it,
instead of a flat "10% of input" assumption. That assumption is well off for
xAI — grok-4.6 caches at 25% of its input rate — and cached tokens are most of
the volume here, because every cycle resends the same ~29k-character mission.
Override per team with `<TEAM>_PRICE_CACHED`.

### Note — Grok is pinned to grok-4.5; grok-4.6 shipped 2026-08-12
Model ids are pinned deliberately, so nothing moves on its own. Switching is one
field: Settings → `XAI_MODEL` → `grok-4.6`. Input and output pricing are
identical between 4.5 and 4.6 ($2.00 / $6.00), so the switch is cost-neutral
apart from a slightly dearer cache rate.

## [6.34.6] — 2026-08-17

### Fixed — a closed dev request kept coming back
`add_dev_request()` had no duplicate check, so every cycle that re-hit the same
bug inserted another row. Closing one did nothing about the six identical ones
behind it, and the next cycle filed another — the owner closed the same
complaint repeatedly and watched it reappear each time.

A re-report now collapses onto the existing open row, incrementing
`report_count` instead of multiplying rows. Matching is exact-normalized, or
Jaccard ≥ 0.6, or containment ≥ 0.9 on titles of five words or more — the last
one catches the same bug restated more briefly (two wordings of the SPY chain
report score only 0.69 on word overlap but are plainly one bug). Length-gated
deliberately: wrongly fusing two real requests hides one, which is worse than a
duplicate. Verified that four wordings of the chain bug collapse to one row while
four genuinely different requests stay distinct.

Two new signals in the queue: **"reported Nx"** when a desk keeps hitting it, and
**"back after a fix"** when a closed complaint is filed again — which means the
fix did not reach that desk and reads very differently from a first report.

### Changed — "Fixed — tell them" is one click, no typing
Closing a request required expanding a note field and clicking Confirm. The
common case is "this is fixed, tell them to try again", which should not require
writing a justification. One click now broadcasts to all seven desks: *"FIXED —
this has been shipped. TRY IT AGAIN before assuming it is still broken, and drop
any workaround you built for it."* **Won't fix** sits beside it, and an optional
`+ note` link remains for the rare case a note adds something.

## [6.34.5] — 2026-08-17

### Fixed — rejected orders were logged nowhere
`_fail()` returned a rejection to the caller and recorded nothing. So a desk that
TRIED to trade every cycle and was refused by a risk rail was indistinguishable
from a desk that deliberately stood aside: both showed no positions and an empty
activity feed. Every rejection now lands in the agent log with its reason.

### Added — "What each desk is doing" on the overview
Six flat desks rendered identically to six idle ones, with no way to tell which.
A desk with no open position has at least seven distinct causes, and they demand
completely different responses:

| State | Meaning |
|---|---|
| `no_api_key` | never runs — no key configured |
| `provider_down` | paused: out of credit / bad key / bad model |
| `halted_daily_loss` | tripped the daily circuit breaker |
| `cooling_off` | past the drawdown threshold, new entries blocked |
| `orders_rejected` | **tried to trade and was refused** — with the top reasons and counts |
| `no_cycles_today` | no trader cycle ran at all |
| `chose_not_to_trade` | ran, judged nothing worth taking — the only benign one |

The card shows cycles run today, trades placed, rejections, and the three most
common rejection reasons with counts, so "40x trade risk exceeds the 1.5% cap"
reads as a sizing bug rather than as inactivity. The four states only the owner
can clear are red.

Verified by constructing all seven states across seven desks and asserting each
is classified correctly, including a desk rejected 7 times whose reasons group as
5x risk-cap and 2x missing-stop.

## [6.34.4] — 2026-08-17

### Added — get_platform_updates: the complete history, not just the recent few
v6.34.3 broadcast each fix to every desk, but the snapshot can only carry the
newest handful per cycle, and there was no journal READ tool — so an update
older than that window became permanently invisible. The failure case is
specific: a desk paused for days (an out-of-credit provider) could miss the very
fix that unblocks it, and never know to look.

`get_platform_updates` returns the full history, newest first, to all three
roles. The snapshot still shows the newest 8 for immediacy and now reports the
total when more exist, pointing at the tool for the rest. Verified against 12
shipped fixes: the snapshot surfaces 8 and says so, while the tool reaches all
12 including the oldest, from a desk that filed none of them.

**Rules are a separate mechanism and were never subject to this.** The mission —
risk rails, the strategy menu, what is tradeable — is rebuilt from code and sent
in full to every desk on every cycle, so all seven always hold the complete,
current rule set with no dependence on memory or notification. Updates are the
rolling record of what CHANGED; the mission is the standing statement of what IS.

## [6.34.3] — 2026-08-17

### Added — marking a dev request done now notifies EVERY desk
Closing a request used to be silent. The row's status changed and it simply
disappeared from the filing desk's next snapshot — so a desk could not tell
"shipped" from "rejected" from "lost", and, expensively, a desk that had written
a capability off had no signal that it now worked and should be retried. The
other six desks were never told at all.

A fix is almost never useful only to whoever reported it. When the option-chain
fetch was broken, one desk filed it while the other six had equally abandoned
premium-selling; delivering that fix to one inbox would have left six desks
avoiding a lane that works.

So a resolution is now **broadcast to every desk's journal** — their durable
memory, which outlives any snapshot window — attributed to the desk that filed
it, and surfaced in each desk's snapshot as `platform_updates`. A closed request
carries an explicit instruction to re-test anything previously abandoned;
`wont_fix` reads as "WILL NOT BE BUILT — stop planning around it". The mission
now tells all three roles that platform updates are not optional reading, since
carrying a workaround for a bug fixed last week is how a desk quietly excludes
itself from a whole strategy lane.

The dashboard's "Mark done" button now opens an inline note field, because
"closed" tells a desk nothing while "fixed in v6.34.2, retry get_option_chain"
tells it exactly what to do. There is a **Won't fix** button beside it. Leaving
the note blank still produces a useful default rather than a bare status.
Verified: closing one request wrote the update to all seven desks' journals and
reached the snapshot of a desk that never filed it.

## [6.34.2] — 2026-08-17

### Fixed — get_option_chain returned "no_chain" for SPY on every call
A desk reported this as blocking five of the eight menu strategies, and it was
three compounding bugs plus a reporting failure that hid all of them.

**The timeout budget could not cover its own phases.** The outer bound was
`timeout + 4` = 12s. Inside it: a spot quote allowed 4s, then the Greeks/quote
stream allowed the full `timeout` = 8s. 4 + 8 = 12s — the entire budget — which
left **zero seconds** for the full-chain HTTP download that has to happen first.
For an underlying with thousands of listed contracts that download cannot finish
in no time, so the outer `wait_for` cancelled, `_run` returned `None`, and the
caller reported "no chain". This was deterministic for SPY, which is why
narrowing the strike window never helped: narrowing strikes does not shrink the
initial chain fetch. Each phase now has its own budget and the total is their
sum.

**The blocking fetch ran on the event loop.** `get_option_chain(session, symbol)`
is synchronous HTTP called from inside a coroutine, so it stalled the same loop
the streamer needed. It now runs via `asyncio.to_thread`.

**The stream waited for every contract.** `while need_greeks:` only exited once
*all* subscribed contracts had reported, so one illiquid strike that never
publishes burned the whole allowance while usable data for the liquid strikes
sat already collected. It now returns on quiescence
(`OPTION_STREAM_QUIET_SEC`, 4s), treats partial data as success, and caps the
subscription at `OPTION_MAX_STREAM_SYMBOLS` (80).

**Every failure returned `{}`.** That is why one message covered "this symbol has
no options", "the session expired" and "the stream timed out" — and why
`data_providers.degraded` had nothing to report. `fetch_option_chain()` now
returns a diagnosed envelope with an `error_code` of `not_configured` /
`no_session` / `empty_chain` / `no_contracts` / `no_quotes` / `stream_timeout`,
each with specific advice, and `_run` re-raises a diagnosed error instead of
flattening it to a timeout. Live fetches retry twice.

### Added — historical-chain fallback, and degraded reporting for options data
When live quotes are unavailable, the chain is served from the most recent
HISTORICAL data (Alpha Vantage) flagged `stale: true` with its `as_of` date.
Strikes, deltas and IV remain sound for choosing a structure; the premiums are
explicitly not executable prices. `place_option_trade` accepts leg prices from
that chain rather than refusing the lane, and returns a `quote_warning` when no
live quote could cross-check them. Mark-to-market still holds such a position
flat instead of inventing a value.

Both an outright failure **and** a stale fallback now register in
`snapshot.data_providers.degraded` (`record_named_error`, for sources that are
SDK streams rather than plain HTTP GETs), and clear automatically when live
quotes return — so a desk learns the state before planning a premium lane
around it.

### Fixed — declare_strategy contradicted the mandate about options
Its tool description still said options lanes "are on the menu but NOT
executable yet and will be refused", written when that was true in v6.32.0 and
never updated when v6.33.0 shipped the engine. Meanwhile
`mandate.executable_strategies` correctly listed all five. The description was
the wrong one: **all eight lanes are executable.** It now says so and points at
`get_risk_state().mandate.executable_strategies` as authoritative.

## [6.34.1] — 2026-08-13

### Fixed — an out-of-credit provider was treated as a rate limit and retried forever
GLM returned `429 code 1113 — "Insufficient balance or no resource package.
Please recharge."` The `429` made the SDK raise `RateLimitError`, so the runner
treated an empty account as a transient throttle: it retried every cycle, filed
the same SDK repr into the activity feed each time, and nothing anywhere said
the one thing that would fix it. **No retry can succeed against an account with
no money in it.**

`classify_provider_error()` now separates the two meanings of 429 — and the
other overloaded statuses — into a code and a `terminal` flag:

| Classified as | Terminal | Behaviour |
|---|---|---|
| `out_of_credit` (insufficient balance, quota exhausted, low credit) | yes | pause the desk |
| `bad_api_key`, `no_access`, `bad_model` | yes | pause the desk |
| `rate_limited`, `network`, `provider_down` | no | retry next cycle, as before |

A terminal failure **pauses that desk**: the runner stops calling the API from
the Strategist, Trader and Reviewer alike, rather than burning cycles and
tokens on a call that cannot succeed. The desk's positions keep running their
stops — `manage_positions` was already outside the agent path and stays there.
The pause persists across restarts, and the day is not marked planned or
reviewed on a dead provider, so those run properly once it is back.

**Recovery needs no intervention beyond fixing the account.** A paused desk is
probed once every `RETRY_PROVIDER_MINUTES` (20); the first probe that succeeds
clears the pause and announces the desk resumed. Verified: 20 consecutive
cycles skipped with zero API calls, exactly one probe allowed at the 21-minute
mark, automatic recovery on success, and a genuine rate limit correctly NOT
pausing the desk. The retry clock is stamped at the moment of failure — without
that, the timer read as "never probed" and let the very next cycle straight back
through to the dead endpoint.

**The dashboard now says so.** A blocked desk gets a red banner at the top of
the overview naming the desk, what is wrong, and where to fix it, plus a marker
in the leaderboard row. Previously such a desk simply looked idle.

## [6.34.0] — 2026-08-13

### Added — the overview page now shows every desk's work in one place
Seven desks meant seven tabs, and anything that lived only on a team page was
seen a seventh at a time. Three sections now aggregate across all desks:

* **Dev requests** — every desk's open requests in one queue, newest first, each
  tagged with the desk in its chart colour and closable in place with "Mark
  done". This is the one thing that needs the owner personally; requests were
  sitting unread simply because nobody opened that tab.
* **Who is running what** — each desk's declared strategy, when it was declared,
  its allocation, and how many share and options positions it currently holds.
  This is what the P&L chart is actually testing, so it belongs next to it.
* **Open positions** — every desk's open risk in one table: side, quantity,
  entry, live mark, unrealized P&L, stop, target, strategy and horizon, plus a
  net-unrealized total across all desks, and a second table for open options
  structures with their credit/debit, max loss and collateral.

Marks come from the 30-second quote cache the trading loop already fills, so a
dashboard refresh is a dictionary lookup rather than a burst of network calls.
A symbol that cannot be priced shows a blank mark rather than falling back to
its entry price, which would render a losing position as flat. Futures P&L
applies the contract multiplier: a 15-point adverse move on 2 MES shows −$150,
not −$30.

The team pages now render their dev requests through the same component, so the
two views cannot drift apart.

## [6.33.3] — 2026-08-13

### Fixed — PROJECT_NOTES.md described a system that no longer existed
It still said "four AI desks" with "$10,000" accounts and options "once a
brokerage is connected" — against seven desks on a $50,000 capital base with a
working options engine. Rewritten and, more importantly, **verified**: every
factual claim in it (desk names, starting capital, all seven risk-rail defaults,
the micro futures list, the strategy count, the commitment period, and the tool
names) is now asserted against the live code rather than written from memory.

Adds sections the file never had: what the desks can actually trade, the
broker-enforced risk rails with their defaults and env vars, the data sources
and which of them are read-only, and the research loop.

## [6.33.2] — 2026-08-13

### Fixed — VERSION was stale, so the dashboard misreported the running build
`VERSION` had sat at 6.31.1 across three shipped releases. The dashboard header
reads that file directly, which meant a container running the options engine
still badged itself v6.31.1. Bumped to match, and `CLAUDE.md` now records that
VERSION and CHANGELOG are updated together on every shipped change.

### Added — CLAUDE.md with the repo's standing working agreements
Chiefly: **all work is pushed to `main`** — this repo does not run feature
branches, and no pull request is opened unless asked. Also records the
paper-trading-only constraint (tastytrade stays READ-ONLY), the rule against
changing working interfaces as a side effect, and the version-first commit and
changelog convention.

## [6.33.1] — 2026-08-13

### Changed — the overview chart plots pure profit and loss
Account VALUE is a poor comparison chart. Every desk's line starts at the same
large number, so the only thing being judged — what each desk actually earned —
was a few hundred dollars of wiggle inside a $50,000 axis. The chart now plots
P&L from zero: every desk shares an origin, break-even is always on screen, and
the full vertical range goes to the difference between them. Each line is
labelled with its current P&L at the right edge, because with seven desks
bunched within a few hundred dollars the legend colour alone does not say who
is ahead.

The series is still `equity_adj` (owner-contributed capital removed), measured
against the original starting equity, so the +$25k deposit remains invisible:
verified on a series where raw equity steps $25,320 between two points while the
plotted step is $320.

The page header was stale in two ways and is now correct — it claimed "Four AI
desks" against seven, and "$25,000" against accounts that hold $50,000.

### Added — Alpha Vantage key field in Settings
`ALPHAVANTAGE_API_KEY` is now a masked field on the Settings page under research
data providers, with `ALPHAVANTAGE_DAILY_LIMIT` alongside it, so enabling
historical option chains does not require an env edit and a container restart.
Verified through the real save path: the key persists, masks in the status
payload, applies to the environment, and flips the provider to configured.

## [6.33.0] — 2026-08-13

### Added — the options engine: desks can now trade what the owner trades
Five of the six strategies on the v6.32.0 menu were gated because options could
not be executed. They execute now, properly — 100x multiplier, premium,
collateral, assignment and exercise — so a desk that declares the wheel can
actually run the wheel.

**`daytrader/core/options.py`** models contracts and structures. A position is a
list of signed legs (+long, −short), so one model prices a cash-secured put, a
vertical and an iron condor without per-strategy special casing. Max profit,
max loss and breakevens are **computed from the payoff**, not asserted: the
expiration payoff is piecewise linear with breaks only at strikes, so evaluating
those points is exact. That is what makes "defined risk only" enforceable —
a naked short call is rejected by arithmetic, not by pattern-matching its name.

**`daytrader/live/options_book.py`** runs the book against the paper account:

* **Collateral is really held.** A cash-secured put ties up the strike, a
  vertical its width. It leaves buying power until the position closes, so a
  desk cannot sell ten puts it could never be assigned on.
* **Expiration actually happens.** ITM shorts are assigned into real share
  positions, longs exercise, OTM expires worthless. Assigned stock arrives at
  the STRIKE as its cost basis and becomes an ordinary position — markable,
  closable, and eligible to have covered calls written against it. Shares
  already pledged to one short call cannot cover a second.
* **Management runs every cycle**, inside `manage_positions`, so a 50%-profit
  rule and a 21-DTE exit are rules rather than journal notes — and an expiring
  short put settles whether or not an agent ran that cycle.
* **P&L lands in the shared `trades` table**, so the leaderboard, profit factor
  and per-strategy breakdown include options without knowing they exist.

**Risk is modelled honestly, and it forced a real decision.** A cash-secured
put's true max loss is strike-to-zero, which no 1.5%-of-equity cap can ever
accommodate — under that rule the wheel is not safe, it is unrunnable, and an
impossible rule gets bypassed rather than obeyed. So width-bounded structures
(spreads, condors) are sized on their genuine max loss, while structures exposed
to the underlying itself are sized on a stress loss at a 20% adverse move
(`OPTION_STRESS_MOVE_PCT`), against a separate `MAX_OPTION_RISK_PCT` (5%)
— with the full obligation still held as collateral, capped per structure at
`MAX_OPTION_COLLATERAL_PCT` (25%). Options risk feeds the same portfolio-heat
rail as shares and futures.

**Data.** Live chains with real bid/ask and streaming Greeks come from the
owner's tastytrade account, still strictly READ-ONLY — `get_option_chain` now
selects expirations by DTE window and strikes by distance from spot, because
"the next N expirations" hands a desk asking for a 35-DTE spread a fistful of
weeklies, and a count-based strike window misses the 20-30 delta strikes every
premium strategy needs.

New provider **`daytrader/data/feeds/alphavantage.py`** adds HISTORICAL chains
(`av_historical_option_chain`): every strike and expiration on a past date with
bid/ask, volume, open interest, IV and full Greeks, ~20 years back. This makes
options *research* possible at all — a live chain can only say what is true now.
History never changes, so every chain is cached to disk permanently and each
(symbol, date) is fetched once, ever, shared across all seven desks; the free
tier's 25 requests/day is tracked and reported rather than failing opaquely.
Requires `ALPHAVANTAGE_API_KEY` (free).

Verified end to end, including a full wheel: sell the put, get assigned at the
strike, own the shares, write a covered call, get called away. **The books
reconcile exactly** — sum of recorded trade P&L equals the actual change in
account equity to the cent. Getting there caught three bugs worth naming: an
intrinsic-value mark that booked an entire credit as profit the instant a
position opened; an assignment that recorded the share transfer as an option
loss *and* left the shares at strike basis, double-counting it (reported −$811
of trades against a −$14 account move); and an uncovered-call check that
rejected iron condors by ignoring their own long wing.

## [6.32.0] — 2026-08-13

### Added — hard risk rails and a declared strategy per desk
Seven desks trading with an open mandate all landed within a few hundred dollars
of each other. That is not seven strategies competing; it is one undifferentiated
discretionary approach, run seven times. Two things change.

**The risk rules are now enforced by the broker, not suggested in the prompt.**
An order that breaks one is rejected with a message the desk can act on:

| Rail | Default | Env |
|---|---|---|
| Per-trade risk (entry→stop) | 1.5% of equity (was 2.0%) | `MAX_TRADE_RISK_PCT` |
| Portfolio heat — Σ open risk across ALL positions | 8% of equity | `MAX_PORTFOLIO_HEAT_PCT` |
| Daily realized loss before new entries stop | 3% of equity | `DAILY_LOSS_LIMIT_PCT` |
| Drawdown from peak that triggers cooling-off | 8% | `COOLDOWN_DRAWDOWN_PCT` |
| Averaging down (adding while underwater) | blocked | `ALLOW_AVERAGE_DOWN` |

Portfolio heat is the one that actually bounds the account: per-trade sizing
alone permits six "small" 1.5% trades that all fail together, which is a 9% day.
Cooling-off and the daily loss limit cover different failure modes on purpose —
an account can lose 3% today, recover, and lose 3% tomorrow without ever tripping
a peak-to-trough threshold. Both block *new* positions only; open positions keep
running their stops.

`get_risk_state` and the snapshot's `risk_state` report `risk_budget_remaining`
and `daily_loss_remaining`, so a desk sizes against the budget that is actually
left instead of discovering it through a rejection.

**Desks must declare a strategy and stay in it** (`declare_strategy`, new module
`daytrader/live/mandate.py`). A lane can be switched at most once every
`STRATEGY_COMMIT_DAYS` (5) and may consume at most `MAX_STRATEGY_ALLOCATION_PCT`
(50%) of buying power. The menu is in the mission: wheel/CSP, bull put spreads,
iron condors, LEAPs/debit spreads, constrained share momentum, and earnings vol
crush. The options lanes are listed but **gated** — `declare_strategy` refuses
them until the options engine lands, because declaring a strategy the engine
would reject on every order is worse than not offering it.

All six rails verified end to end: the heat cap fires on the 6th of six 1.4%
entries at 7.0% heat; a 2.0% order is rejected against the 1.5% cap; a 9.0%
drawdown blocks new entries and clears on recovery; the daily limit fires after
three ~$700 losses on a $50k account; adds are blocked while underwater and
allowed on strength; and `add_to_position` re-checks heat on the blended
position rather than double-counting the leg it replaces.

New settings are editable in the dashboard Settings tab under "Trading rails".

## [6.31.1] — 2026-08-05

### Fixed — the capital top-up put a $25k cliff in the equity chart
v6.31.0 kept `return_pct` and `pnl` honest but left the **equity curve** plotting
raw account value, so the deposit drew a vertical $25,000 step — reading as an
enormous gain on the one view that is meant to show trading skill.

`equity_curve()` now carries `contributed` (capital added up to that point) and
`equity_adj` = equity − contributed, and the dashboard chart plots `equity_adj`.
The line is continuous across the deposit and moves only on realized/unrealized
P&L, which also keeps the seven desks comparable across the boundary. Raw
`equity` is untouched for anything that needs true account value (standings,
stat tiles, risk checks). Verified: a deposit that stepped the raw series
$25,000 leaves a maximum plotted step of $400 — the amount actually earned.

## [6.31.0] — 2026-08-05

### Added — one-time capital top-up, booked as capital and not as profit
Every desk receives a **+$25,000** owner deposit, taking the capital base from
$25k to $50k (`CAPITAL_TOPUP`, guarded by `CAPITAL_TOPUP_ID` so a restart can
never repeat it).

- New `capital_events` table and `broker.deposit()`. A deposit is explicitly
  **not** P&L: cash rises, and the drawdown peak and the day's risk anchor rise
  by the same amount — otherwise the injection would read as a winning day and
  quietly hand the desk a fresh daily-loss budget.
- **Return is now measured against the capital base** (`START_CASH` + net
  deposits) in both the leaderboard and the dashboard standings, so the transfer
  cannot show up as performance. Dollar `pnl` (equity − capital base) is now
  reported alongside, and is completely immune to the deposit.
- Every desk gets the same injection at the same moment, so relative standings
  are preserved. Note the arithmetic side effect: a past gain now divides by a
  larger base, so historical *return %* compresses (a $1,200 profit reads 2.4%
  rather than 4.8%). Dollar P&L is unchanged — that is the undistorted number.

### Changed — the desks' mandate is now explicitly open
The mission read "you DAY-TRADE liquid US stocks and ETFs" and hardcoded $25k.
It now states the mandate is broad and theirs: any horizon, any approach they can
justify and measure, with their own realized breakdown — not habit — deciding.
Spells out what the engine actually settles correctly (stocks/ETFs incl.
leveraged and inverse, listed futures in contracts with real margin, day/swing/
long horizons, tranche building, trailing stops, ADX-decay exits) and drops the
stale "PREFER DAY TRADING" default and the $25k sizing example.

### Fixed — option symbols could have been ordered as shares
`unsupported_instrument` did not recognise OCC/dxfeed option symbols, so
`place_trade("PLTR  260116C00150000", ...)` would have been priced as **shares** —
the same silent-wrong-numbers trap futures had before v6.26. Now blocked with an
explanation, across `place_trade`, `stage_order` and `broker.open`.

**Options are not executable in this system.** There is no contract-multiplier,
premium, assignment or exercise model, so wheels, covered calls, cash-secured
puts, spreads and premium selling cannot be run — the mission now says so
explicitly rather than letting desks write plans around them. Option chain data
and Greeks remain available for analysis. Building real options support is a
substantial piece of work (100x multiplier, strike/expiry, premium credit/debit,
assignment and exercise, CSP collateral, covered-call share linkage, naked-short
margin, expiry handling) — comparable to or larger than the futures layer.

## [6.30.0] — 2026-08-05

### Added — dev request: breadth / sector confirmation across research and review
The failing SPY `ema_pullback` short (-$78.25) was technically clean on the chart
and taken at breadth 13/21 when the plan wanted <=7. That mistake was
un-analysable and un-testable: breadth existed only as a snapshot scalar, so
`get_performance_breakdown` could not group by it and the rule grammar could not
express it.

New `daytrader/core/breadth.py` produces breadth and sector-cluster context as
**time series over history**, not snapshot scalars — which is what lets the same
numbers serve three places that previously could not agree.

- **Rule grammar** — `backtest_custom_strategy` / `propose_hypothesis` accept
  `breadth_pct`, `breadth_advancers`, `breadth_total`, `breadth_change_20m`,
  `sector_avg_adx`, `sector_avg_adx_slope`, `sector_pct_down`,
  `sector_breadth_pct`. A rule can now require the TAPE to agree, not just the
  chart. Measured immediately on 45d/5m over 12 names: EMA short **PF 0.81** →
  **0.90** with `breadth_pct <= 35` → **1.06** (alpha +2.05) with
  `breadth_change_20m < 0`. Breadth is universe-wide and shared; sector series
  are per-symbol, resolved from the ticker the loader stamps on each frame.
  Absent context leaves the features NaN, so a breadth rule simply never fires
  rather than evaluating against garbage.
- **Entry-time recording** — every trade now stores the tape as it was at fill:
  `breadth_pct`, `breadth_advancers`, `breadth_total`, `breadth_bucket`,
  `breadth_change_20m`, `sector`, `sector_avg_adx`, `sector_pct_down`. This is
  unrecoverable after the fact — breadth is gone by the time a trade closes.
- **`get_performance_breakdown`** gains `breadth_bucket` (weak <=35% / mixed /
  strong >=65%), `breadth_trend` (deteriorating / flat / improving), `sector`
  and `sector_adx_bucket`, plus a `filters` argument taking exact values, ranges
  (`{"breadth_pct":{"max":35}}`) or allowed-lists. Trades predating capture group
  as `unknown` rather than being silently bucketed — an unlabelled trade is not
  evidence.
- **Live snapshot** breadth block now carries `breadth_pct`, `breadth_bucket`
  and `breadth_change_20m` alongside the raw counts.

### Fixed
- `get_performance_breakdown` echoed a **stale** `group_by`: an allowlist of
  `("strategy","tod_bucket")` meant a request grouped by `direction` or
  `with_trend` was reported back as `strategy`. It now echoes the dimensions
  actually applied and returns `ignored_group_by` + `valid_group_by` for any it
  did not recognise, instead of silently substituting.

## [6.29.1] — 2026-08-05

### Fixed — the options-flow 403 was ours, not the provider's
The 403 was **not** a credential or plan problem. `http_json`/`http_text` sent no
User-Agent, so requests went out as `Python-urllib/3.x` — a signature Cloudflare
bans outright (**error 1010, `browser_signature_banned`**). The block happened at
the CDN, before the request ever reached the API, and presented as a plain 403
that looked exactly like an auth failure.

- Both helpers now send a real User-Agent (the same one `data/loader.py` has
  always used, which is why bar data was never affected). Verified against the
  live endpoint: default UA → `403 Cloudflare 1010`; with a UA → `401
  authentication_required` from the actual Unusual Whales API, i.e. the request
  now arrives and the only thing missing is the key.
- **Two providers were affected, not one**: Unusual Whales and Quiver were both
  fully unreachable. Polygon and BullFlow were unaffected (their CDNs allow the
  default UA), which is why the failure looked provider-specific.
- A CDN block is now classified as **`blocked_by_cdn`** and states plainly that
  it is not an auth or plan problem, so this cannot be misdiagnosed as a bad key
  again — which is exactly what v6.29.0's `auth_or_plan` diagnosis did.

## [6.29.0] — 2026-08-05

### Added — dev request: scale into an existing position
"One position per symbol" made every pullback-add plan silently un-executable —
the 08-04 plan ("hold 6.0 sh SPY core, add +2.0 on a pullback to <=758.5, same
734.50 stop") could only be done by closing and re-entering, which surrenders
the runner, pays two extra sets of spread and slippage, and resets the trailing
ratchet.

- **`add_to_position(symbol, qty, stop=, target=, auto_scale_frac=, auto_scale_r=)`**
  blends into ONE position: volume-weighted average entry, summed quantity, a
  single stop/target. Verified on the desk's own numbers —
  `(755.42x6 + 758.50x2)/8 = 756.19`.
- **Position-level risk, returned for verification.** The response carries
  `blended_entry`, `total_qty`, `total_planned_risk` and
  `risk_pct_of_equity`, and the risk cap is re-checked against the BLENDED entry
  rather than the tranche — that is what is actually at risk. A stop left on the
  wrong side of the new average is rejected explicitly, since adding moves the
  average toward price.
- **Auto-scale is re-measured from the blended entry** (`init_stop` follows the
  stop in force after the add), so every R-based mechanism reflects the position
  that exists. Pass `auto_scale_frac=0` to keep a core hold unmanaged; a position
  that had already scaled is re-armed at the new level rather than silently
  losing its protection.
- **`max_adds`** on `place_trade` encodes a plan like "core + up to 2 tranches"
  and the engine enforces it (`max_adds_reached`). Survives restarts alongside
  `adds_used`.
- **`place_trade` on a held symbol now returns `duplicate_symbol_position`** and
  names the tool that can do the job — same-direction points at
  `add_to_position`, opposite-direction at `take_partial`/`close_position`,
  because a reduce or flip must stay explicit rather than become an implicit
  side effect of an add.

### Fixed — dev request: options flow returned an opaque 403
A bare `HTTP 403` could not tell the desk whether the key was missing, the plan
lacked the endpoint, or it was rate-limited — so it could not choose between
waiting, switching source, or trading without the confluence.

- Provider errors are now **self-diagnosing**: `missing_credentials` (403 with
  no key set — names the env var and the Settings tab), `auth_or_plan` (403 *with*
  a key — invalid/expired or the plan lacks the endpoint, and explicitly flagged
  as NOT transient so retrying is not attempted), `rate_limited`, `not_found`,
  `provider_down`, `network_error`. Each carries `provider`, `http_status` and an
  actionable `hint`.
- **Availability is surfaced BEFORE it is needed**: `data_providers` in the
  market snapshot and on the Health tab lists configured providers plus any that
  failed on their last call, with guidance not to gate an entry on confluence
  that cannot be fetched — trade the setup on its own merits and say so, or stand
  aside.

Note: the underlying 403 is a credential/plan matter on the provider side and
cannot be resolved from here — this makes it diagnosable and visible rather than
a silent dead end.

## [6.28.0] — 2026-08-03

### Added — dev request #12: swing horizons are testable (was a hard blocker)
The engine force-flattened at every session close regardless of interval, so a
"swing" config was silently re-tested as another intraday one — a 365-day 1h
trend-continuation run exited every trade on `eod_flat` at ~205min. With six
intraday configs measured negative, this made the desk's only remaining
hypothesis untestable.

- **`horizon: "swing"`** on `backtest_custom_strategy`, `backtest_strategy` and
  `propose_hypothesis` disables the EOD flat: positions carry across sessions
  until stop / target / `max_hold_days`. Overnight gap risk was already priced
  honestly (`CostModel.gap_through_stop` fills a gapped open at the OPEN, not at
  the stop level) — it simply never got exercised because nothing survived the
  close. Verified on real data: the same rule goes **PF 0.88 → 1.32**, avg hold
  **3.7h → 75.9h**, exits **100% `eod_flat` → `stop`/`max_hold`**.
- **`max_hold_days`** time stop. Required for a swing hypothesis: without it a
  rule that never hits stop or target degenerates into buy-and-hold and measures
  the market rather than the rule.
- **Trailing stops in the backtest** — `trail_atr_mult` (already implemented in
  the engine but never exposed) plus a new `trail_pct`, matching what
  `place_trade` offers live. A runner-based edge is no longer capped by a fixed
  rr target. `breakeven_at_r` exposed alongside them.
- **`1d` interval** (3650d history) for multi-week position tests.
- Two engine fixes that swing mode depends on: the last-bar ENTRY block now
  applies **only** in intraday mode (it existed precisely because overnight
  holds were forbidden, and would otherwise silently drop swing entries), and
  the time stop runs before the EOD check.
- Execution is part of a hypothesis's **identity hash**, so the swing variant of
  a rejected intraday rule is a genuinely new hypothesis rather than a blocked
  re-proposal.

### Added — dev request: rollover_short_trigger sector/RS alignment
The SPY gate required down-direction AND rising ADX simultaneously, which almost
never coincides in the 10:00-14:00 window — the block reported `near_miss_count:
5` while the real structure was a sector selloff under a flat index.

- A candidate now qualifies via **either** the existing SPY gate **or** a sector
  path: its `sector_cluster` averaging ADX>=22 and rising with >=60% of members
  EMA-down, **and** the name itself lagging (`rs_vs_spy_pct < 0`) on a
  non-improving `rs_slope_20m`. Each candidate reports which path qualified it
  (`alignment: "spy"|"sector"`) plus the cluster evidence.
- Per-candidate fields the desk was hand-deriving every cycle are now exposed:
  `ema_stack_down` (full ema9<ema21<ema50), `ema9/21/50`, `vs_vwap_pct`, `rsi14`,
  `dist_from_ema9_atr`, `macd_hist` vs `macd_hist_prev` plus a
  `macd_hist_expanding_down` flag, `adx_rising_nbars`, `rs_vs_spy_pct`,
  `rs_slope_20m`.
- New per-symbol snapshot fields backing them: `ema50`, `ema_stack_down`,
  `ema_stack_up`, `adx_rising_nbars`, `adx_decaying_nbars` — the last two using
  the same definition the custom DSL exposes, so a snapshot read and a backtest
  condition agree.
- Guards verified: an improving RS slope, positive RS vs SPY, or a decaying
  sector ADX all fail to qualify; the SPY-only path still works unchanged.

## [6.27.1] — 2026-08-01

### Fixed
- **The new trading settings had no UI.** `USE_TASTYTRADE_MARGIN` (6.27.0) and
  `FUTURES_SYMBOLS` (6.26.0) shipped as environment variables only, which on a
  container means editing the Unraid template and restarting — not the Settings
  tab where every other knob lives. Both are now dashboard-editable, together
  with the risk rails they interact with: `MAX_TRADE_RISK_PCT`,
  `MAX_GROSS_EXPOSURE`, `REQUIRE_STOP`, `AUTO_SCALE_DEFAULT_R`,
  `AUTO_SCALE_DEFAULT_FRAC`. New "Trading rails & futures" card; the margin
  mirror sits in the existing tastytrade card next to the credentials it uses.
- **Those rails now apply without a restart.** They were bound as module
  constants at import, so a Settings change would have written to
  `settings.json` and `os.environ` and then been ignored until the container
  cycled — the UI would have looked like it worked while nothing changed. The
  live code paths read them at call time instead. Verified by tightening
  `MAX_TRADE_RISK_PCT` from 2.0 to 1.0 against an already-imported broker and
  watching the same order flip from accepted to rejected.

## [6.27.0] — 2026-08-01

### Added — mirror the owner's real tastytrade margin terms (opt-in, read-only)
New `daytrader/live/tastytrade_margin.py` pulls the account's actual broker
terms and applies them to the paper accounts. **Off by default**
(`USE_TASTYTRADE_MARGIN=1` to enable) so the running competition's terms never
change silently.

What it reads (verified against tastytrade SDK 13.2 in this container):
- `Account.get_balances()` → `equity_buying_power`, `day_trading_buying_power`,
  `futures_intraday_margin_requirement` / `..._overnight_...`, `margin_equity`
- `Account.get_margin_requirements()` → `margin_calculation_type` (Reg-T vs
  portfolio margin)
- `Future.get()` → authoritative `notional_multiplier` and `tick_size`, which
  override the static exchange table when available

**Leverage is mirrored as a RATIO, never a dollar amount.** The owner's real
account and a $25k paper account differ in size, so copying absolute buying
power would be meaningless — copying "your broker gives you 4x intraday" is the
part that transfers. Clamped to a maximum of 4x (Reg-T day-trading).

Accounting consequences, all verified:
- **Equity longs may be financed on margin.** A margin buy is a LOAN: cash still
  pays the full notional and may go negative (a debit balance), exactly as at a
  real broker. Only the LIMIT changes, from "cash on hand" to "a multiple of
  equity". Keeping the cash mechanics symmetric is what stops the close from
  crediting proceeds that were never paid — a round trip is provably flat.
- **The gross line no longer undercuts the mirrored multiple.** A 4x
  day-trading line means nothing behind a hardcoded 2x cap, so the effective
  gross cap is `max(MAX_GROSS_EXPOSURE, buying_power_multiple)`.
- **Futures are governed by MARGIN, not notional, when mirroring is on** —
  which is how tastytrade actually works. A notional cap mis-governs them badly:
  one MES is $37.6k of notional against ~$100 of stop risk. On a $25k account
  this moves capacity from 1 MES to 5, with the **2% per-trade risk cap** then
  binding at exactly $500 — the rail that should bind.

Desks see the terms in force via `get_contract_specs().margin_terms` and, when
mirroring is on, `account.margin_terms` in the snapshot.

**Read-only, verified at AST level** rather than by string match: the module
imports only `tastytrade.account.Account` and `tastytrade.instruments.Future`,
and calls only `get`, `get_balances`, `get_margin_requirements`. No `order`
module import; no `place_order`/`cancel`; `get_order_buying_power_effect` is
deliberately avoided because it requires constructing an `Order`. Any failure
degrades to the static exchange-minimum table.

## [6.26.0] — 2026-08-01

### Added — real futures support (contract-spec layer)
Futures are now tradeable properly rather than blocked. New
`daytrader/core/contracts.py` holds specs for 17 liquid contracts (ES/MES,
NQ/MNQ, RTY/M2K, YM/MYM, CL/MCL, NG, GC/MGC, SI/SIL, ZB, ZN): multiplier, tick
size, tick value, initial/maintenance margin and per-contract commission. Every
margin is an exchange minimum that moves in practice, so all are env-overridable
(`ES_INITIAL_MARGIN=18000`).

Threaded through both accounting paths — live broker **and** backtest engine,
since the engine feeds the research loop which now feeds live deployment:

- **Notional, P&L, MFE/MAE and slippage** are `price x qty x multiplier`. A
  10-point MES move is $50, not $10.
- **Margin, not cash.** A futures position pledges margin rather than spending
  notional: cash moves only by commission, `buying_power = cash - margin_held`
  gates new entries, and margin is released on close. `margin_held` is derived
  from the open book, so a restart cannot desynchronize it.
- **Equity contribution is unrealized P&L**, not market value — adding notional
  would double-count the whole contract value into equity.
- **Risk rails now see true dollars.** The 2% per-trade cap and the 2x gross
  exposure limit both apply the multiplier. One full-size ES on a $25k account
  ($376k notional, 15x equity) is now correctly refused; it previously passed
  because the guard saw $7.5k.
- **Commission is per contract** (~$1.25/side) instead of per share.
- Equities and ETFs are untouched: multiplier 1.0 is the existing share model,
  verified by regression (cash, unrealized P&L and the causality suite unchanged).

Desk-facing: new `get_contract_specs` tool (multiplier, tick value, margin, and
how many contracts current equity can margin), micro futures added to the scanned
universe so desks can see indicators on what they may trade (`FUTURES_SYMBOLS`,
default `MES=F,MNQ=F`, set empty to disable), and Trader-prompt guidance that
futures size in contracts with risk `(entry-stop) x contracts x multiplier`.

Unlisted futures are still refused — an unknown multiplier is the same
silent-wrong-numbers trap wearing a different ticker. Indices, FX pairs and
crypto remain blocked for their own reasons.

## [6.25.0] — 2026-08-01

### Added — research reaches the book
- **Deploy path: an accepted hypothesis can now trade.** Previously a validated
  rule was a green row in a log and nothing more — the desk still had to eyeball
  its own strategy each cycle and decide, which is exactly the discretionary
  judgement the leaderboard showed the models have no edge at. `deploy_strategy`
  promotes one of a desk's **own accepted** hypotheses into live service; its
  signals then appear in every snapshot under `deployed_signals`, already
  specified (symbol, side, stop, target) and carrying their out-of-sample record,
  generated by the same code that validated them. `undeploy_strategy` retires
  one; `list_deployed_strategies` shows the current set.
  - Deployment is **per-desk and own-research-only**: a rejected or pending
    hypothesis cannot be deployed, and neither can another desk's. That keeps the
    seven books independent instead of converging on identical trades, and makes
    a live slot something a desk earns.
  - The Trader prompt now ranks `deployed_signals` first, and requires a
    *stateable* reason to skip one — "I don't like the look of it" is precisely
    the hunch the validation replaced.

### Fixed — futures could silently corrupt the books
- **`place_trade` / `stage_order` / `broker.open` now reject untradeable
  instruments.** The broker prices every position as `price * qty`, a share
  model. Nothing errored on a futures symbol: Yahoo returns clean bars and quotes
  for `ES=F`, orders filled, and every resulting dollar figure was wrong by the
  contract multiplier. One ES contract is $50 × index ≈ **$376k notional — 15× a
  $25k account** — while the exposure guard saw $7.5k (0.3×) and allowed it, and
  P&L came out **50× understated** (1000× for `CL=F`). Blocked with an actionable
  message pointing at the ETF proxy (SPY for /ES, QQQ for /NQ, USO for /CL).
  Also blocked: indices (`^GSPC`), FX pairs (`=X`), and crypto (`-USD`, which
  trades 24/7 so the EOD flatten and session risk model don't apply).
  Enforced at the broker as well as the tool layer, since staged orders and
  deployed strategies reach `open()` without passing through tools.
  - Real futures support needs a contract-spec layer (multiplier, tick value,
    margin-based buying power, roll handling) — a deliberate piece of work, not a
    config flag. Until then, failing loudly beats plausible-looking nonsense.

## [6.24.0] — 2026-08-01

### Added — automated strategy research loop
The desks now run continuous strategy research between trading: they propose
hypotheses, and **code does the judging**. Models are useful for generating and
mechanizing ideas; they have no edge at discretionary calls, so every
accept/reject here is pure compute (zero tokens).

New package `daytrader/research/`, built around the one danger that dominates at
scale — testing many strategies manufactures false positives:

- **Pre-registration** (`registry.py`) — a hypothesis is registered with its
  pass/fail criteria *before* any result exists. `register()` refuses a spec
  carrying outcome fields; `record_result()` is write-once, so a disappointing
  result can never be re-run and re-recorded until it passes.
- **Failure log** — every proposal is keyed by a canonical content hash over the
  *normalized* rule, so a rejected idea cannot come back renamed, re-ordered, or
  spelled with a feature alias (`ema_9` vs `ema9`). Permanently closed.
- **Hard out-of-sample** (`evaluate.py`) — N contiguous, non-overlapping periods,
  each scored independently on a frozen rule. Two details that decide whether the
  numbers mean anything: each period runs with an indicator **warmup prefix**
  (else the first ~50 bars are silently signal-less), and each period **restarts
  from the same equity** (else period 1's P&L changes period 5's position sizes,
  coupling trials the gate treats as independent).
- **Multiple-comparison correction** (`gate.py`) — Bonferroni over the running
  family: `required_alpha = base_alpha / n_tests_to_date`, counted **globally
  across all seven desks**. Per-desk families would give each desk its own 5%
  budget and leave the true family-wise error rate near 30%.
- **Silence is the expected output** — the loop notifies only on a survivor.

Surfaces: `propose_hypothesis` + `research_log` tools (reviewer role), a
**Research** dashboard tab showing the full tested/rejected record and the live
significance bar, and a once-daily drain wired into `review_all`.

### Notes on two design decisions
- **Interval defaults to `1h`.** Five genuine 30-day periods need ~150 days of
  history; the Yahoo loader caps 5m at ~60 days. 1h serves 730 days. Lower
  fidelity per bar, far more statistical power — 5m remains available for
  short-horizon confirmation.
- **Period-consistency is a hard criterion, not part of the p-value.** Folding a
  binomial over N periods into the significance test looked more conservative but
  was wrong: it has a hard floor of `0.5**n` (0.031 at 5 periods), which sits
  above the corrected bar from the *second* test onward. That would have made the
  gate not merely strict but arithmetically unpassable, rejecting real edges for
  a counting reason. The bootstrap over trade P&L (continuous, no floor) is the
  significance test; `min_periods_profitable` remains a hard pre-registered bar;
  `p_periods` is still reported as evidence. `gate.feasible()` now flags when the
  corrected bar drops below the bootstrap's resolution, so an exhausted family is
  reported rather than silently rejecting everything forever.

## [6.23.0] — 2026-07-24

### Added — dev requests
- **`rollover_short_trigger` snapshot block** (analogous to `macd_trigger`),
  mechanizing the saved `trend_day_ema9_rollover_short` co-primary setup. Each
  cycle it flags names meeting ALL of: EMA-down (`ema9<ema21`), ADX>=25 AND
  rising, MACD hist re-expanding down (`hist < hist_prev < 0`), RSI>35,
  `vs_vwap_pct` in [-1.5, 0], and SPY aligned down with rising ADX — plus an
  `in_window` flag for 10:00–14:00 ET. Returns `count` and per-name `symbol`,
  `adx14`, `adx_slope`, `macd_hist`, `macd_hist_prev`, `rsi14`, `vs_vwap_pct`,
  `dist_from_ema9_atr`, sorted weakest-RS first. Also reports `spy_aligned` and
  `near_miss_count` so a blocked cycle says *why* (the SPY gate) instead of just
  printing zero. No more hand-checking hist vs hist_prev on every EMA-down name
  and arriving after the 14:00 gate.
- **`adx_decay_exit` is now a live `place_trade` parameter**, using the exact
  contract already in `backtest_strategy` / `backtest_custom_strategy` (e.g.
  `{"adx_drop_from_peak": 5.0, "negative_slope_bars": 3}`), so a config
  validated in a backtest behaves identically live. The server force-closes the
  position (`exit_reason: auto_adx_decay`) once its ADX falls that far from its
  post-entry peak or its slope has been negative that many consecutive cycles.
  Enforced on every trade cycle **and** on the ~2-min stop poll; the poll only
  pays for ADX bars when a held position actually opted in. Peak / negative-bar
  state is persisted, so a restart mid-trade can't silently disarm the exit.

### Fixed — dev requests
- **SPY VWAP null in the 09:32 snapshot.** Root cause: near the open the
  session's only bar is the still-forming 09:30 bar, whose volume the feed has
  not published yet — cumulative session volume is 0, so a volume-*weighted*
  average is genuinely undefined. (The v6.22.1 zero-fill fixed a null volume
  *mid*-session, where earlier bars still supplied weight; it could not fix a
  session with no volume at all yet.) Added a right-edge fallback: the
  unweighted mean of today's typical prices, which with a single bar *is* the
  VWAP and over a few bars is a close proxy. Always flagged — `vwap_status`
  reports `fallback_typical_mean_<n>bar` and `data_quality` carries a matching
  entry — and it yields to the true session VWAP the moment real volume prints.
- **`with_trend` was inverted for inverse ETFs.** A SQQQ/SOXS/SPXU *long* is
  mechanically aligned with a *down* tape, but the tag was computed from the raw
  order side, so the desk's most with-trend trade of the session landed in the
  counter_trend bucket — reversing the very lesson the breakdown exists to
  teach. `with_trend` is now computed on the trade's EFFECTIVE market direction
  (`side_sign × inverse_multiplier`) vs SPY at entry, applied at trade-recording
  time and inherited by `get_performance_breakdown`. Ships with an inverse-ETF
  map (broad index, sector, and long-vol; leveraged-but-not-inverse names like
  TQQQ/SOXL/SPXL correctly keep +1) and a **one-time historical re-tag** of
  affected trades and open positions, flag-guarded in the same transaction so a
  crash or restart can never double-flip it back.

### Changed
- **Trader prompt now treats pre-staging as an action, not a note.** `stage_order`
  and the full trading toolset were verified already available to the trader role
  (26 tools; `stage_order`, `place_trade`, `take_partial`, `move_stop_to_breakeven`,
  `modify_stops` all present in both schemas and handlers) — so the 15-session gap
  was behavioral, not a missing tool. The role prompt now states that "I want X at
  the open" *is* a `stage_order` call this cycle, names the journal pattern as the
  most-repeated failure, and points at out-of-window trigger blocks as the things
  to stage.

## [6.22.2] — 2026-07-24

### Changed
- **Claude desk upgraded to `claude-opus-5`** (from `claude-opus-4-8`). Opus 5
  is priced identically to Opus 4.8 (5/25 per 1M) and takes the same adaptive
  thinking, so no provider changes were needed. Override via `CLAUDE_MODEL`.

## [6.22.1] — 2026-07-21

### Fixed — dev request (recurring trading blocker)
- **Intraday VWAP now populates for liquid watchlist names** (SPY, NVDA, AMD,
  INTC, MU, AMAT, LRCX, …). Root cause: Yahoo's intraday chart API routinely
  reports `volume: null` for the most recent, still-forming bar even on the most
  liquid symbols. Since a cumulative sum leaves NaN in place at a NaN position,
  that single null last-bar volume made the *last* session-VWAP value NaN — the
  exact bar the live snapshot reads — so VWAP showed `vwap_unavailable` on names
  that clearly have volume. `vwap_session` (and `session_vwap_bands`) now
  zero-fill missing bar volume before the cumulative sum: a forming/volume-less
  bar contributes nothing (VWAP carries the prior value) instead of nulling the
  readout. VWAP is defined whenever any real volume has traded in the session.
  This unblocks the 10:00–12:00 VWAP-trend LONG lane that depends on it.
- **Clearer VWAP fallback status.** Each symbol now carries a `vwap_status`
  field (`ok` / `no_volume` / `undefined`), and the `data_quality` flag on an
  unavailable VWAP is now `vwap_unavailable_no_volume` vs `…_undefined` so the
  desk can tell a genuine session-wide data gap from a not-yet-meaningful VWAP,
  rather than a single opaque `vwap_unavailable`.

### Changed
- **Claude desk moved back to `claude-opus-4-8`** (from `claude-fable-5`, which
  was running ~$5/day). Pricing readout updated to Opus 4.8 rates (5/25 per 1M).
  Override via `CLAUDE_MODEL`.

## [6.22.0] — 2026-07-21

### Added
- **"Clear" button on the Health tab's "Recent errors & refusals" panel.**
  Dismisses the currently-shown errors for every desk by advancing a per-team
  acknowledgment watermark (`errors_ack_ts`) to now. Non-destructive: the
  underlying `agent_log` rows are untouched (history preserved) — the health
  view simply stops surfacing error/refusal rows at or before the watermark,
  and any *new* error after clearing appears normally. Backed by a new
  `POST /api/errors/clear` endpoint (CSRF/token-gated like the others).

## [6.21.1] — 2026-07-21

### Fixed — live issues
- **OpenAI desk erroring every cycle.** The `openai` pin `gpt-5.6-sol` rejects
  function tools on Chat Completions (`400: Function tools with reasoning_effort
  are not supported … in /v1/chat/completions` — that model needs the Responses
  API). Reverted the default pin back to **`gpt-5.5`**, which runs the desk's
  tool loop on Chat Completions as before. Override still available via
  `OPENAI_MODEL`.
- **"API $/day" showing $0 for every team despite active trading.** Some
  providers/proxies return a response with no `usage` object, so the recorded
  token count (and therefore cost) was zero even on successful cycles. Both
  providers now fall back to a character-based token estimate (~4 chars/token,
  accumulated over the full re-sent context per iteration) **only when the API
  reports no real usage**, so cost is never a misleading $0 when the model
  actually ran. Real reported usage is always preferred; failed/rejected
  requests are still billed at $0 (the estimate is only applied after a
  successful response).

## [6.21.0] — 2026-07-20

### Added — dev request
- **`stage_order` now accepts general feature `conditions`** using the same
  grammar as `backtest_custom_strategy` (each `{left, op, right}`; features incl.
  `macd_hist`, `macd_hist_prev`, `adx`, `ema9`, `rs_stable`, `adx_rising_nbars`,
  etc.). All conditions are re-checked on the ~2-min stop-poll before firing, so
  a desk can pre-stage its exact validated trigger (e.g. a downward MACD-hist
  re-expansion) and stop losing bar-resolved signals to 20-min cycle timing or
  the 14:00 gate. Conditions are validated at stage time and evaluated against
  the latest bar at fire time (SPY auto-loaded when an rs_* feature is used).
- Refactored the custom-strategy DSL: the feature matrix (`build_features`) and
  condition evaluation (`normalize_conditions` / `eval_condition` /
  `check_conditions`) are now shared module functions used by both the backtest
  and the staged-order fire check — no duplicated logic.

## [6.20.0] — 2026-07-20

### Added — dev requests
- **`macd_trigger` snapshot field** — mechanizes the desk's one proven A+ setup:
  each cycle it scans the watchlist for a FRESH MACD sign-flip in the direction
  of the name's EMA trend, with ADX>=25 & rising, price within 1.5xATR of EMA9,
  SPY-aligned, and flags the 10:00-14:00 window. Returns per hit: symbol, side,
  macd_hist now/prev, adx+slope, dist_from_ema9_atr, vs_vwap_pct, rs_rank — no
  more per-cycle manual reconstruction of the only edge that pays.
- **Snapshot data-quality guard** — each name now carries `data_quality` flags
  (`quote_vs_bar_X pct` when the live quote deviates >1.5% from the last bar
  close; `vwap_unavailable`) plus `tradeable_mark`/`indicator_source` so the desk
  knows the fill mark (live quote) vs the indicator basis (bar close). Also fixes
  a real bug: VWAP/`vs_vwap_pct` were emitting NaN (NaN is truthy) instead of
  null when session VWAP wasn't yet available.
- **RS + multi-bar-ADX features in `backtest_custom_strategy`** — new features
  `rs_vs_spy_pct`, `rs_slope_20m`, `rs_persistence`, `rs_stable` (SPY injected
  into the backtest), plus `adx_rising_nbars` / `adx_decaying_nbars`. Unlocks
  RS-continuation / RS-reversal strategy families and ADX-streak filters the desk
  couldn't previously test.

### Still not testable in-backtest (deferred, need universe-in-engine plumbing)
- Cross-sectional `rs_rank` and sector-cluster features inside
  backtest_custom_strategy, and a breadth-threshold backtest param (#6 / gap_fade
  breadth hybrid). These require feeding the whole universe (not one symbol) into
  the backtest engine — a larger change. Use `rs_vs_spy_pct`/`rs_stable` as a
  proxy for now.

## [6.19.1] — 2026-07-20

### Fixed — CRITICAL: all non-web_fetch external tools were erroring
- `http_json`/`http_text` in the restored feeds package called `opener.open()`
  where the fallback opener is the `urllib.request` *module* — which has
  `urlopen()`, not `open()` — so every non-SSRF-guarded call raised
  `AttributeError("module 'urllib.request' has no attribute 'open'")`. That broke
  ALL uw_* / poly_* / finviz_* / bullflow_* / web_search / youtube_* tools
  (web_fetch used the guarded opener, which has `.open()`, so it alone worked).
  Introduced in the v6.17.0 reconstruction; now uses `urlopen` for the plain path
  and the OpenerDirector's `.open` for the SSRF-guarded path. SSRF guard intact.

## [6.19.0] — 2026-07-20

### Changed — model refresh (verified vs. providers' own docs)
- Updated the stale default pins to current GA: **OpenAI gpt-5.5 → `gpt-5.6-sol`**,
  **xAI grok-4.3 → `grok-4.5`**, **Moonshot kimi-k2.6 → `kimi-k3`** (K2.6 was
  being sunset), and **Anthropic claude-opus-4-8 → `claude-fable-5`** (new top
  tier). Qwen (`qwen3.7-max`), DeepSeek (`deepseek-v4-pro`), and GLM (`glm-5.2`)
  were already current. All overridable via `*_MODEL` env / Settings.
- **Fable 5 compatibility:** the Anthropic provider now omits the `thinking`
  param for Fable/Mythos models (thinking is always-on there and the param is
  rejected); Opus/Sonnet still use adaptive thinking.
- Updated cost-telemetry rates: Claude → $10/$50 (Fable 5, up from $5/$25). GPT
  stays $5/$30; Grok/Kimi rates are approximate for the new revs (override with
  `<TEAM>_PRICE_IN/_OUT` for exact figures).

### Notes
- Swapping a live desk's model resets its leaderboard comparability (a "new
  season" for that desk).
- Fable 5 requires 30-day data retention — a zero-data-retention Anthropic org
  will get 400s; keep Claude on `claude-opus-4-8` via CLAUDE_MODEL if that
  applies to you.

## [6.18.0] — 2026-07-13

### Added — the four deferred dev requests
- **`min_trend_duration_bars`** on backtest_strategy / backtest_custom_strategy:
  only enter after the symbol's ADX has been >= adx_threshold AND strictly rising
  for N consecutive bars — filters short-lived regime spikes so a desk can test
  whether an edge is concentrated in sustained trends.
- **`adx_decay_exit`** on both backtests (engine-level): force-close a held
  position when its ADX drops >= `adx_drop_from_peak` from its post-entry peak OR
  slopes negative for >= `negative_slope_bars` bars — models intra-trade
  deceleration (the SQQQ-style loss the fixed duration filter couldn't catch).
- **Sector-cluster indicators** in `market_summary.sector_clusters`: per-sector
  (semis, mega-tech, EV, financials, energy, china, crypto-proxy, index) counts
  of RSI>70/>80/<30/<20, avg ADX + rising/falling, and an overbought/oversold
  cluster flag — so an exhaustion cluster (e.g. 9 semis all RSI>85) is visible on
  cycle 1 instead of requiring a manual scan.
- **Pre-staged auto-fire orders** for the 9:30-10:00 window: `stage_order`
  (symbol/side/qty/stop/target + `fire_after` ET time + optional `max_ema9_dist_atr`
  / `min_adx` gates), `list_staged_orders`, `cancel_staged_order`. The system
  auto-fires a staged order within ~2 min of its target time (on the stop-poll)
  IF the entry conditions still hold, else skips it — removing the calculation
  step from the time-critical window. Available to the Strategist (pre-open) and
  Trader. New `staged_orders` table.

## [6.17.0] — 2026-07-13

### Fixed — INCIDENT: research/web tools were never committed
- **The entire `daytrader/data/feeds/` package was silently gitignored** (a bare
  `data/` rule in .gitignore matched the *source* dir) and had **never been
  committed to `main`** — so the deployed app has been running WITHOUT the
  web_search / web_fetch / YouTube / Polygon / Unusual Whales / Quiver / Finviz /
  BullFlow tools the whole time (`tools.py` imported them, the import failed, and
  it degraded to "data feeds unavailable"). Fixed the .gitignore (now `/data/`,
  root-only) and restored + committed the package. base.py, web.py, and
  unusual_whales.py are exact; polygon/quiver/finviz/bullflow are faithful
  RECONSTRUCTIONS (verify their endpoints if you set those keys). Web/YouTube
  tools (no key) work immediately; the SSRF/byte-cap guards are included.

### Fixed — bug
- **Journal lessons now reliably carry across sessions** (reported as
  "journal_write returns ok but entries vanish"). Writes were never actually
  dropped (verified) — but the Reviewer is handed a snapshot built *before* its
  write (so it can't see its own entry), and EOD lessons got buried past the
  20-entry recency window before the next planner read them. Fixes: snapshot now
  carries a dedicated `recent_lessons` (topic-filtered: lesson/plan/risk/review,
  its own window) that always reaches the next planner; `journal_write` returns a
  `persisted` confirmation; the journal window grew to 40.

### Added — dev requests
- **RS persistence / leadership-stability** in the snapshot: per-symbol
  `rs_persistence`, `rs_slope_20m/60m`, `rs_rank_change_20m`, `rs_stable` — so
  desks can gate RS-continuation/vwap-trend entries on *durable* leadership
  instead of chasing one-bar leaders that mean-revert.
- **with_trend recorded at entry** from SPY's direction (not inferred from the
  label) → the breakdown's `with_trend` dimension is now meaningful.
- **Custom strategy names survive** the breakdown: new `strategy_raw` group_by
  preserves exact labels so custom setups don't collapse into "other".
- **Server-enforced +1R auto-scale** (default on): at +1R the system banks
  `auto_scale_frac` (default 0.5) and moves the stop to breakeven — no manual
  tool call needed. Override per trade (`auto_scale_frac: 0` disables) or
  globally via `AUTO_SCALE_DEFAULT_*`.
- **Planned-vs-realized risk audit**: trades record planned risk + a
  `risk_overrun` flag (realized loss >25% over planned = stop-through); surfaced
  as `risk_audit` in the snapshot.
- **Stop-execution transparency**: snapshot `stop_execution` states stops are
  *cycle-polled* (now also on a faster between-cycle poll, default 120s, not
  tick-by-tick) so desks size for gap risk. The between-cycle poll tightens stop
  enforcement from ~15 min to ~2 min, reducing stop-through severity.

### Deferred (next pass)
- adx_decay_exit + min_trend_duration_bars (backtest-engine entry/exit filters),
  sector-cluster indicators, and pre-staged auto-fire open-window orders — these
  need dedicated engine/scheduler work and will get their own release.

## [6.16.0] — 2026-07-06

### Added — dev requests
- **Canonical strategy labels in the performance breakdown** (dev #7). Free-text
  strategy names are now normalized server-side to the 8 built-in buckets (+
  "other") before aggregation, so "MACD" / "macd_with_trend_short" / "MACD trend
  continuation" collapse into one `macd` row instead of ~40 one-off rows with
  n=1. `get_performance_breakdown` also gains `direction` (long/short, from the
  trade side) and `with_trend` (with_trend/counter_trend, inferred) as group-by
  dimensions — so you can slice setup × direction × time.
- **Order-management controls** (dev #3): `take_partial` (close a fraction, e.g.
  0.5, banking a partial at +1R and leaving a runner — recorded as a trade with
  prorated costs), `move_stop_to_breakeven` (lock a no-loss runner), and
  `modify_stops` (adjust stop/target on an open position). Given to the Trader;
  the classic "take 50-60% at +1R, move to breakeven, trail the rest" is now one
  tool sequence instead of an all-or-nothing manual exit.
- **Pre-staged EMA scan for the open** (dev #1). The snapshot now carries an
  `ema_scan` field that classifies every watchlist name as a long/short
  EMA-pullback candidate with distance-from-EMA9 (in ATR), ADX + slope, VWAP
  position, and gap — ranked by ADX strength and shallow pullback — so the desk
  can act in the 9:30-10:00 window instead of analyzing 10+ minutes in after ADX
  has decayed. Mission now steers desks to only take candidates with ADX rising.

## [6.15.2] — 2026-07-02

### Added
- **Desk leaders can describe their tools in chat.** The chat channel is
  tool-less by design (it's Q&A, not a trading session), so a leader couldn't
  accurately answer "how do I execute trades?". The chat context now includes a
  concise, auto-generated summary of the desk's real tools (place_trade with
  stop/target/horizon/trailing, backtests, data feeds, etc.) so the leader can
  speak accurately about its capabilities. No effect on live trading.

## [6.15.1] — 2026-07-02

### Fixed
- **Connection test no longer false-FAILs reasoning models.** GLM-5.2 and
  Kimi K2.6 returned "(empty reply)" because the health ping's `max_tokens=20`
  was consumed by the model's reasoning before any text was produced. The ping
  now uses 512 tokens, and a clean round-trip with no error/refusal counts as
  connected even if the reply text is empty (it still proves the key + endpoint
  + model resolve). Real trading was unaffected — it uses full token budgets.

## [6.15.0] — 2026-07-02

### Added
- **Three open-weight competitors → the field is now 7 desks.** DeepSeek V4 Pro
  (`deepseek-v4-pro` @ api.deepseek.com), GLM-5.2 (`glm-5.2` @ api.z.ai), and
  Kimi K2.6 (`kimi-k2.6` @ api.moonshot.ai) join via the OpenAI-compatible
  provider path. Each activates only when its key is set (`DEEPSEEK_API_KEY`,
  `ZAI_API_KEY`, `MOONSHOT_API_KEY`), so the competition stays 4-way until you
  add them. Model/base-URL overrides (`DEEPSEEK_MODEL`, `GLM_MODEL`, `KIMI_MODEL`,
  `*_BASE_URL`) are on the Settings page; the base-URL allowlist already permits
  these hosts and private/localhost for self-hosting on a Spark/GX10-class box.
  New dashboard tabs, leaderboard rows, and settings fields for all three.
- **Per-cycle token + cost telemetry.** Every agent call's token usage is now
  captured (`AgentResult.usage`) and persisted to a `token_usage` table with an
  estimated USD cost (`daytrader/live/pricing.py`, per-team rates, overridable
  via `<TEAM>_PRICE_IN`/`_OUT`). The dashboard shows **API $/day** per team on
  the leaderboard and an **API $ today** stat on each team tab, so competition
  spend is visible instead of guessed.
- **Prompt caching.** The Anthropic path now marks the static system prompt +
  tool schemas as cacheable (`cache_control`), cutting the repeated-prefix input
  cost ~90%. The OpenAI-compatible providers (OpenAI/Grok/Qwen/DeepSeek/GLM/Kimi)
  cache automatically server-side; their reported cached tokens are recorded and
  billed at the discounted rate in the cost estimate.

---

## [6.14.0] — 2026-07-02

Large hardening release from a full multi-agent code review. Money-correctness,
security, and scheduler robustness, plus a new dev-request feature.

### Security
- **Dashboard CSRF + optional auth.** All POST endpoints now reject cross-origin
  requests (Origin check), so a malicious web page can no longer drive-by write
  API keys, spend tokens, or mutate state. Set `DASHBOARD_TOKEN` to require a
  token on every `/api/*` call (the page prompts once and stores it). `/api/check`
  (paid provider pings) moved to POST so an `<img>`/GET can't trigger it.
  `DASHBOARD_BIND` lets you bind to `127.0.0.1`; default stays `0.0.0.0`.
- **Base-URL allowlist.** `*_BASE_URL` settings are validated against known
  provider hosts (or a private/localhost address for self-hosting), closing the
  "point the API at an attacker and exfiltrate the key" vector.
- **SSRF guard on `web_fetch`.** Agent-supplied URLs are blocked from resolving
  to private / loopback / link-local / metadata addresses, redirects are
  re-validated, and every response is byte-capped (also prevents OOM).
- Dashboard sanitizes agent-supplied dev-request links (only `http(s)://`),
  caps POST body size, and stops iOS text auto-scaling.

### Fixed — money correctness
- **Risk rails in the broker.** `place_trade`/`open()` now reject: orders with no
  stop, a stop/target on the wrong side of entry, per-trade risk over
  `MAX_TRADE_RISK_PCT` (default 2%) of equity, and any order breaching a gross
  exposure cap of `MAX_GROSS_EXPOSURE`× equity (default 2×). This closes the
  unlimited-short / unlimited-leverage hole where one LLM order could take an
  account to hundreds of × leverage.
- **`side` parsing.** `buy`/`sell` (and long/short) are mapped explicitly;
  unknown values are rejected instead of silently becoming a SHORT.
- **Halted teams still get bracket enforcement.** A tripped circuit breaker no
  longer disables server-side stops for surviving swing/long positions.
- **Trailing stops keep working off-watchlist.** Held symbols dropped from the
  day's scan now get quote+ATR data so their trailing stops keep ratcheting.
- **`gap_pct` look-ahead removed** from the custom-strategy DSL (it leaked the
  current day's close into earlier bars — a fake-edge generator). Added a
  causality regression test suite (`tests/test_causality.py`).
- **Backtest engine** no longer holds an entry filled on a day's last bar
  overnight (a strategy could otherwise harvest gaps live trading can't realize).

### Fixed — scheduler & restart robustness
- **Deadline-based EOD.** Flatten + review now trigger on `time ≥ 15:50` even if
  a cycle overran 16:00; a failed close is retried and the Reviewer runs exactly
  once per day. No new (long) trade cycle starts after 15:30.
- **Persisted risk/schedule state** (`day_start_equity`, `halted`, plan/review
  done) keyed by ET date, so a mid-day restart can't reset the loss-limit
  baseline, un-halt a team, or double-run the plan/review.
- **Drawdown peak** recovered as the historical max on restart (was resetting).
- **Market-holiday calendar** — no more full API-spend "trading" days on closed
  sessions (static NYSE table).

### Fixed — correctness / cleanup
- **`profit_factor`** is `null` (rendered `∞`) when there are no losing trades,
  everywhere, instead of "PF = gross-profit-in-dollars" — so the Reviewer stops
  over-weighting an all-wins fluke.
- **strategy-lab param validation** — unknown `strategy_params` now error instead
  of silently backtesting the default config; `HH:MM` strings are coerced;
  applied params are echoed. Custom configs reject `max_entries_per_day ≤ 0` and
  bad/inverted time windows.
- Tool results are byte-capped before re-entering context; max-iteration
  exhaustion is flagged as an error. `WATCHLIST_SIZE` env var now works. SQLite
  `busy_timeout` set. Removed dead `runner.py` + `status` CLI; fixed stale `$10k`
  docs; Dockerfile header corrected to port 3737 / $25k; added a `HEALTHCHECK`.

### Added
- **`recent_exits` + `session_realized_pnl` in the snapshot** (dev request #6).
  The on-cycle Trader now sees when a server-side stop/target fired since its
  last cycle (symbol, exit_reason, pnl, time) and the day's true realized P&L —
  fixing the leak where a stopped-out loss looked identical to a banked winner
  and the trader ran on a stale mental model of the book.

---

## [6.13.0] — 2026-06-30

### Added
- **Regime/strategy/time-of-day performance breakdown** (dev request #2). New
  `get_performance_breakdown` tool returns realized n_trades, win_rate,
  profit_factor, total_pnl, avg_win, avg_loss grouped by `strategy` and/or
  `tod_bucket` — so a desk can see with hard numbers which setups and which
  session windows carry positive expectancy and concentrate risk there (or
  disable what bleeds), instead of eyeballing the trade log. Time-of-day buckets
  are ET: open (9:30–10:00), morning (10:00–12:00), midday (12:00–14:00), late
  (14:00–16:00). Given to the Strategist, Trader, and Reviewer; the Reviewer is
  now instructed to run a strategy×time breakdown at EOD and let it drive the
  plan. New module `daytrader/live/analytics.py`.
  - Correctly converts trade timestamps (recorded in the container's local /
    UTC time) to ET before bucketing, so the session windows are accurate
    regardless of the container timezone.

---

## [6.12.0] — 2026-06-30

### Added
- **Trailing stops + server-side bracket execution** (dev request #4 — "let
  winners run"). The live broker now *enforces* stops and targets automatically
  each trade cycle instead of relying on the agent to close manually, and a
  trade can carry a **trailing stop** that ratchets in its favor as price moves:
  - `place_trade(..., trail_atr_mult=2.0)` trails 2×ATR behind price, or
    `trail_pct=1.5` trails 1.5% behind. The stop only ever tightens toward
    price (never loosens) and auto-closes when hit — so a clean trend trade can
    run well past a fixed target while the open gain stays protected.
  - New `PaperBroker.manage_positions(quotes, atr_map)` runs at the top of every
    trade cycle (before the agent), ratcheting trails and auto-executing
    stops/targets. Trades close with reason `auto_stop` / `auto_target`.
  - `trail_atr_mult` / `trail_pct` persist on the position (new DB columns) and
    survive restarts. The dashboard marks a trailing stop with a ⤴ glyph.
  - Honest limitation: management runs at trade-cycle granularity (not
    intrabar), so a level breached between cycles fills at the next cycle's mark
    — real between-cycle gap risk remains. *Scale-out / partial exits (sell ½ at
    +1R, move to breakeven) are deferred to a follow-up — this v1 is the
    trailing-stop + auto-bracket half of the request.*

### Fixed
- **`trend_day` flag no longer fires on a single mover** (dev request #5). It was
  flipping TRUE whenever any one name ran with ADX≥30, so a lone laggard made the
  whole tape look like a trend day even with SPY ranging. `trend_day` now
  reflects the INDEX itself trending — SPY's own ADX14 ≥ ~22 **and** its EMA
  trend agreeing with its direction. Big movers and breadth are reported as
  separate signals (a lone mover can't fake it). Also exposes `spy_adx_slope` /
  `spy_adx_rising` (and per-symbol `adx_slope`) so desks can tell an emerging
  trend from a decaying one — the morning-window edge they flagged.

---

## [6.11.0] — 2026-06-17

### Changed
- **Desks can now swing-trade and hold longer-term, not just day-trade.** The
  mandate is now "prefer day trading, but hold when warranted," aimed at
  aggressive-but-steady growth / income generation. Each trade carries a
  **horizon**: `day` (the default — flattened automatically at the close),
  `swing` (held for days), or `long` (held weeks+). Swing/long positions survive
  the EOD flatten and the daily-loss circuit breaker, riding their own stops;
  only `day` positions are force-closed at 15:55 ET. Desks don't have to specify
  anything to keep day-trading — `day` is the default; they opt into longer holds
  explicitly via `place_trade(..., horizon="swing"|"long")`.
  - `horizon` flows through `place_trade` → `PaperBroker.open` → the
    `open_positions` table (new column, migrated in place) and is restored on
    restart, so multi-day holds survive container restarts.
  - `flatten_all(reason, horizons={...})` closes only the requested horizons; the
    runner uses `{"day"}` at the close and on the circuit breaker.
  - Open-positions table on the dashboard shows a **Hold** column (day/swing/long).
  - Mission goal reworded to "aggressive but steady growth (or income
    generation)"; PF 2:1+ target kept, max-drawdown guidance ~10–15%.

---

## [6.10.1] — 2026-06-17

### Fixed
- **Mobile layout actually works now.** The v6.6.2 attempt had a bug: it set the
  tables to `width:max-content`, which made the wide ones (the trades "Reason"
  column, the 10-col leaderboard) grow *past* their card and force the whole
  page wider than the screen — so everything got squeezed into a thin left
  column with text bleeding off to the right. Fixed properly:
  - `html,body` now hard-guard against any horizontal overflow
    (`max-width:100%; overflow-x:hidden`), so a wide child can never blow out
    the page again.
  - On phones the **card** is the horizontal scroll container; wide tables
    scroll inside their card instead of stretching the page.
  - The long free-text **Reason** column is hidden on phones (it's already in
    the Thinking & Activity feed), so the trades table's essentials —
    symbol/side/entry→exit/qty/P&L — fit on screen without scrolling.
  - Added `-webkit-text-size-adjust:100%` to stop iOS from rescaling text.

---

## [6.10.0] — 2026-06-15

### Added
- **Custom, agent-authored strategies (the rule DSL).** The desks can now invent
  brand-new setups from rules — no developer needed — and backtest them through
  the same engine/cost-model/metrics as the built-ins. A strategy is a small
  config: `{side, entry:[{left, op, right}…], stop_atr_mult, rr,
  max_entries_per_day, no_entry_before/after}`. Conditions are AND-ed; `left` is
  a feature, `op` ∈ `< <= > >= == != cross_above cross_below`, `right` is a
  number or another feature, and a `_prev` suffix reads the prior bar (for
  crossovers). ~30 causal features are exposed (price, ema9/21/50, sma20, rsi,
  rsi2, atr, atr_pct, adx, vwap, vs_vwap_pct, macd/signal/hist, bollinger
  bands+%, day_change_pct, gap_pct, ret1, ret3). Exits (ATR stop, rr target,
  EOD-flat) are engine-handled, so custom results are directly comparable to the
  built-ins. The config is a fixed feature/operator vocabulary — no arbitrary
  code is ever executed. New module `daytrader/strategies/custom.py`.
- **Three new tools** (Strategist, Trader, Reviewer): `backtest_custom_strategy`
  (inline config or saved name), `save_custom_strategy` (validates + persists to
  a per-team library), and `list_custom_strategies`. Backed by a new
  `custom_strategies` DB table + `LiveDB.save/get/list_custom_strategy`.
- **Mission updated** to push the desks to invent and validate their own setups
  aggressively — iterate the rules until PF≥2 on a real sample, save the winner,
  then trade it by applying its conditions live.

### Notes
- This is the v1 the v6.9.0 changelog flagged as a follow-up. Live
  auto-execution of a saved custom strategy (wiring it into `fresh_signals`) is
  the next possible step; for now a desk trades a validated custom setup by
  applying its rules itself when the snapshot shows the conditions.

---

## [6.9.0] — 2026-06-15

### Added
- **Self-serve strategy backtesting (`backtest_strategy` tool)** — Team Claude's
  dev request #3, the "single biggest leverage point." A desk can now test a
  hypothesis on recent intraday data in seconds instead of burning live
  sessions. It wraps the project's validated engine + cost model + metrics, so a
  result means the same thing it does in the offline backtests. Inputs: a
  strategy name / profile (trend, momentum, all) / list, symbols, lookback,
  interval, regime pin, ADX threshold, market filter, pessimistic costs, and
  per-strategy parameter overrides. Returns win rate, profit factor, avg
  win/loss, max DD, expectancy, return, alpha vs SPY, an equity curve, sample
  trades, and an honest verdict that flags small (non-conclusive) samples.
  Available to the Strategist, Trader, and Reviewer. New module
  `daytrader/live/strategy_lab.py`. (v1 tests the 8 built-in setups with tunable
  params — a custom entry/exit-rule DSL is a future step.)
- **Trend-day detection in the snapshot (`market_summary`)** — Team Qwen's dev
  request #2's core need. Every snapshot now carries a top-level read of the
  tape: a `trend_day` flag, SPY direction/ADX, market breadth (advancers vs
  decliners), the day's big movers (>=2% with ADX>=30), and RS leaders/laggers
  — computed from values already in the snapshot. The mission now tells desks to
  lean into leaders early on a flagged trend day, before ADX decays. Pairs with
  the per-symbol `rs_rank`/`rs_vs_spy_pct` (v6.8.0) and `get_opening_range`
  (v6.7.0) to form the morning pipeline the desks asked for.

### Already shipped (clarifying the other two open requests)
- **Relative-strength ranking vs SPY** (a dev request) shipped in **v6.8.0** —
  `rs_vs_spy_pct` + `rs_rank` are in every snapshot; `market_summary` now adds
  the leader/lagger view on top.
- **Unusual options flow + dark pool** (a dev request) shipped in **v6.5.0** —
  the `uw_flow_alerts` / `uw_ticker_flow` / `uw_dark_pool` / `uw_market_overview`
  tools appear in each desk's inventory automatically once
  `UNUSUAL_WHALES_API_KEY` is set in Settings.

---

## [6.8.1] — 2026-06-15

### Added
- **Dev requests now show when they were filed.** Each request on the team tab
  and the Health tab displays its timestamp plus a relative age ("2d ago",
  "3h ago"), so it's obvious at a glance whether an item is new or stale. The
  data was already stored (`ts`); this just surfaces it.

---

## [6.8.0] — 2026-06-15

### Fixed
- **Dev requests now persist without a `GITHUB_TOKEN` — and say so.** Filing a
  request always wrote to the local DB (and thus the dashboard), but
  `file_dev_request` returned `ok: False` when no token was set, so the desks
  reasonably concluded their request had vanished. It now returns a truthful
  `recorded` flag, and the `request_dev_help` tool replies with a clear note:
  "Saved to the dev-requests page … GitHub mirror skipped (no token) but your
  request IS persisted." **No token is required** for the dev-request workflow;
  a token only adds optional GitHub-issue mirroring.

### Added
- **Dev requests can be CLOSED now** (the missing half of the workflow). New
  `resolve_dev_request(id, status, resolution)` agent tool — added to the
  Reviewer, who is now instructed at EOD to close any open request whose
  tool/data/fix has actually shipped, with a one-line verification note.
  Backed by a new `LiveDB.update_dev_request` + `get_dev_request` and a
  forward-only migration that adds `resolution` / `resolved_ts` columns.
- **"Mark done" buttons on the dashboard** — both the per-team Dev requests
  card and the Health tab's open-requests list now show the request id and a
  one-click close (POST `/api/devrequest/close`), so the owner can clean up the
  page directly too.
- **Relative strength vs SPY baked into every snapshot** (Team Claude's dev
  request #3). Each symbol's indicator block now carries `rs_vs_spy_pct`
  (symbol % change − SPY % change over the last ~30 min) and `rs_rank`
  (1 = strongest), computed from bars already loaded that cycle — no extra
  fetches. SPY is loaded as the benchmark even when it isn't on the watchlist.

---

## [6.7.0] — 2026-06-15

### Fixed
- **Feed-vs-broker price gap closed** (Claude's #1 escalation — was flipping
  winners into losers). The market snapshot and the paper broker now draw from
  ONE shared quote source (`daytrader/data/quotes.py`) backed by Yahoo's
  chart-meta `regularMarketPrice` (the official last trade, fresher than the
  last 1-minute bar close). The competition loop pins each cycle's quote map
  onto the broker for the duration of that cycle, so the price the agent
  reasoned over **is** the price the broker fills at — zero drift. Also fixes
  the BA-style "price-feed discrepancy" pattern on names whose 1m bar lagged
  the live tape.
- **Indicators for held positions outside the day's scan.** When a team holds
  a symbol that isn't on the day's scanned watchlist, `with_account` now
  fetches its bars + live quote and adds a full indicator block to the
  snapshot, so the trader is never flying blind on what it already owns.

### Added (agent capabilities)
- **`get_recent_trades`** — detailed round-trip trade blotter (entry/exit time
  + price, qty, commission, slippage, pnl, exit reason, rationale). Asked for
  by Team OpenAI for post-trade review. Also added to the Reviewer's allowed
  tools.
- **`get_opening_range(symbol, minutes=15)`** — today's first N minutes for
  trend-day detection: O/H/L/C, volume, range %, gap from prior close. Asked
  for by Team Qwen.
- **`get_relative_strength_vs_spy(symbols, lookback_minutes=30)`** — ranks a
  list of symbols by intraday RS vs SPY (sym% − SPY%). Asked for by Team Qwen.
- **Mission: fractional shares are explicit + risk floor stated.** The mission
  text now tells every desk that `qty` accepts fractional values (e.g. 0.05)
  and to size trades to ~0.2–0.5% of equity (~$50–$125 on $25k). This unblocks
  Team Grok, which was sitting on cash because its risk math couldn't justify
  a whole share of expensive names. The `place_trade` schema description was
  also updated to advertise fractional support.

### Notes
- Unusual Whales tools (`uw_flow_alerts`, `uw_ticker_flow`, `uw_dark_pool`,
  `uw_market_overview`) have been available since v6.5.0 — they appear in each
  desk's tool inventory automatically when `UNUSUAL_WHALES_API_KEY` is set.
  Team Qwen's dev request for "real-time unusual options flow + dark pool"
  should now be visible in its inventory.
- The recurring Anthropic `500 / Internal Server Error` was a transient
  upstream API failure (not a code bug); the existing trade loop tolerates it
  and retries on the next cycle.

---

## [6.6.2] — 2026-06-15

### Fixed
- **Dashboard is now mobile-friendly.** Added a responsive layout for phones /
  narrow screens (≤640px): the version badge stacks above the title instead of
  overlapping it, the tab bar scrolls horizontally rather than wrapping into a
  pile, padding/fonts tighten up, and — the big one — the wide data tables
  (especially the 10-column leaderboard) now scroll sideways *inside their card*
  instead of forcing the whole page to overflow. Inputs use 16px text so iOS
  Safari no longer zooms in when you tap the chat box. Pure CSS — no behavior
  change on desktop.

---

## [6.6.1] — 2026-06-14

### Fixed
- **Dashboard header is now dynamic.** The subtitle shows the real starting cash
  (so it reads **$25,000**, not a hardcoded $10k) and a **version badge** (e.g.
  `v6.6.1`) sits in the top-right so you can glance up and confirm you're on the
  latest build. Both are rendered server-side from the VERSION file + START_CASH,
  so they never go stale. `VERSION` is now copied into the container image.

---

## [6.6.0] — 2026-06-14

### Added
- **Web + YouTube research tools** (always on, no key) — `web_search`,
  `web_fetch`, `youtube_search`, `youtube_transcript`. The desks can browse the
  open web and read video transcripts to discover and learn ANY strategy,
  including ones traders/influencers teach. (YouTube transcript fetch is blocked
  from datacenter IPs but works from a residential IP like a home server.)
- **Explicit tool inventory** injected into each desk's prompt, so every team
  knows exactly which tools/data sources it has at its disposal (varies by which
  keys are set).
- **`python -m daytrader.agent reset`** — wipe per-team DBs for a clean restart.

### Changed
- **Starting cash per team: $10k → $25k** (more buffer for strategies). Run
  `reset` (or clear `team_*.db`) once so existing desks restart at $25k.
- Mission now grants full strategy freedom (invent/adopt any strategy, not just
  the built-ins) and explicitly invites the desks to file dev requests for any
  data/tool/strategy they think would give them an edge.

---

## [6.5.0] — 2026-06-13

### Added
- **External research-data feeds** the desks can query on demand to hunt for an
  edge — pluggable read-only adapters under `daytrader/data/feeds/`, each behind
  its own API key (Settings → Research data providers), merged into the desks'
  toolset only when configured:
  - **Polygon.io** — `polygon_quote`, `polygon_news`, `polygon_aggregates`, `polygon_movers`.
  - **Unusual Whales** — `uw_flow_alerts`, `uw_ticker_flow`, `uw_dark_pool`, `uw_market_overview`.
  - **BullFlow** — `bullflow_alerts`, `bullflow_ticker` (SSE-snapshot reader).
  - **Quiver Quant** — `quiver_congress`, `quiver_insiders`, `quiver_wsb`, `quiver_gov_contracts`.
  - **Finviz Elite** — `finviz_screener`, `finviz_news` (authenticated CSV export).
  Strategist + Trader can call them; the mission prompt nudges using flow/news/
  screeners for confluence. All adapters are stdlib-only, defensive (never raise,
  short-TTL cached), and READ-ONLY.

### Notes
- BullFlow field names and a couple of endpoints are inferred from limited public
  docs and may need a small tweak once tested with a live key.

---

## [6.4.2] — 2026-06-13

### Fixed
- **OpenAI GPT-5-family models** (e.g. `gpt-5.1`) reject `max_tokens` and require
  `max_completion_tokens`. The OpenAI-compatible provider now detects this and
  switches automatically (caching the choice), so OpenAI works while Grok/Qwen
  keep using `max_tokens`.

---

## [6.4.1] — 2026-06-13

### Fixed
- **OpenAI-compatible provider** no longer sends `tool_choice` when there are no
  tools — xAI Grok (and others) reject that, which made the Grok connectivity
  test and the chat-with-leader feature fail with a 400. Tools/`tool_choice` are
  now only sent when tools are present.

---

## [6.4.0] — 2026-06-13

### Changed
- **tastytrade auth switched to OAuth** so 2FA-protected accounts work headless
  (no rolling/one-time code to enter). Settings now takes
  `TASTYTRADE_CLIENT_SECRET` + `TASTYTRADE_REFRESH_TOKEN` (generate once on
  tastytrade.com → API → OAuth Applications → Create Grant; the refresh token
  never expires) instead of username/password. Unpinned to `tastytrade>=12`
  (latest SDK is OAuth-only) and migrated the option-chain call to the 12.x
  `get_option_chain` API. Still strictly READ-ONLY — no order code path.

---

## [6.3.0] — 2026-06-13

### Added
- **Health tab** in the dashboard — at-a-glance monitoring: market/data-feed
  status, per-team status (key configured, equity, errors today, halted,
  open positions, last activity), a recent-errors/refusals feed, and the agents'
  open dev requests. Auto-refreshes (DB-only, no API cost).
- **Live API connectivity test** — `GET /api/check`, a "Test APIs now" button on
  the Health tab (and Settings), and a CLI `python -m daytrader.agent check`.
  Pings each team's model with its current key and reports ✓/✗ + latency +
  error detail (surfaces dead keys *and* wrong model IDs).
- **Discord breakage alerts** — when a team's cycle errors, a daily-loss circuit
  breaker trips, or the competition starts, an alert is pushed to
  `DISCORD_WEBHOOK_URL` (throttled). New module `daytrader/live/healthcheck.py`.

### How failures surface
Agent errors/refusals are logged per team (visible in the Health tab and team
thinking feed); the agents file GitHub issues via `request_dev_help` for things
needing a developer; and with a Discord webhook set, breakages are pushed to you.

---

## [6.2.1] — 2026-06-13

### Fixed
- **Dashboard default port reverted to 3737** to match the legacy container.
  v6.x had changed it to 8787, which broke existing Unraid port mappings
  (host:8787 → container:3737). The default is 3737 again so existing mappings
  work unchanged; override with `DASHBOARD_PORT` if desired.

---

## [6.2.0] — 2026-06-13

### Added
- **tastytrade live data feed (READ-ONLY)** (`daytrader/live/tastytrade_data.py`)
  — real-time stock + option quotes and Greeks (delta/gamma/theta/vega/rho/iv)
  via DXLink, plus near-the-money option chains. Enriches the teams' market
  snapshot when tastytrade credentials are set; degrades to the Yahoo feed
  otherwise. **Strictly data/read endpoints — there is no code path that can
  place, modify, or cancel an order on the tastytrade account.** All execution
  stays in the internal paper books.
- tastytrade username/password fields on the dashboard Settings page.

### Notes
- Pinned `tastytrade<10` because the latest SDK (12.x) is OAuth-only; 9.13 keeps
  simple username/password login. (OAuth can be added later if preferred.)

---

## [6.1.0] — 2026-06-13

### Added
- **Settings page** in the dashboard — enter API keys (Claude/OpenAI/Grok/Qwen,
  plus Alpaca) and model/endpoint overrides from the browser. Stored in a
  gitignored `settings.json` in the data volume (chmod 600), masked in the UI,
  never logged. New keys **activate their team within the next cycle, no restart**
  (`Competition._sync_teams`). New module `daytrader/live/settings.py`.

### Fixed
- **Dashboard port.** The new service listens on 8787 (the old crypto dashboard
  used 3737). Added `DASHBOARD_PORT` env support so an existing container/port
  mapping keeps working — set `DASHBOARD_PORT=3737` to reuse the old mapping.

---

## [6.0.0] — 2026-06-13

**Crypto removed. Multi-model competition + web dashboard. $10k per team.**
A breaking, ground-clearing release: the legacy crypto bot and its data are gone;
the project is now purely an equity day-trading backtester plus a live competition
between AI trading desks.

### Removed
- The entire legacy crypto bot (`engine/`), its old databases (`data.db*`,
  `sqlite.db*`), and the crypto Dockerfile. Not coming back.

### Added
- **Model competition** (`daytrader/live/competition.py`) — four desks (Claude,
  OpenAI, Grok, Qwen), each a full multi-agent team running entirely on its own
  model, each with an identical **$10,000** paper account, same tools, same data.
  Per-team daily-loss circuit breaker; teams without an API key are skipped.
- **Provider abstraction** (`daytrader/live/providers.py`) — `AnthropicProvider`
  + `OpenAICompatibleProvider` (covers OpenAI, xAI Grok, and Qwen, including
  local OpenAI-compatible servers via env-overridable base URL).
- **Broadened universe** (`daytrader/data/universe.py`) — 148 liquid US stocks +
  ETFs with a daily liquidity/volatility/momentum scanner that picks each day's
  watchlist (replaces the fixed SPY+Mag7 list).
- **Web dashboard** (`daytrader/live/dashboard.py`) — overview leaderboard +
  equity-curve comparison chart, per-team tabs (positions, trades, full thinking
  feed, dev requests), and chat-with-team-leader. Stdlib-only, offline-capable.
  `python -m daytrader.agent serve` runs the dashboard + competition together.
- **Brokerage recommendation** (PROJECT_NOTES) — Alpaca (#1), tastytrade,
  IBKR for an options-capable automated bot; note that the PDT $25k rule was
  eliminated 2026-06-04.

### Changed
- Starting equity default 100k → **10k**. Agent is now a team of members on a
  per-team model. The top-level `Dockerfile` builds the competition+dashboard
  service (port 8787); CLI is `python -m daytrader.agent {serve,compete,leaderboard,status}`.

---

## [5.1.0] — 2026-06-13

**Autonomous, Claude-powered agent desk for paper trading.** A team of agents
that day-trades SPY + Mag7 during market hours, self-directs, and asks the
developer for help via GitHub issues when blocked. All paper mode.

### Added
- **Agent team** (`daytrader/live/agents.py`) — Strategist (sets the day's plan),
  Trader (runs each intraday cycle and places trades), Reviewer (journals
  lessons, files dev requests). All share one persistent journal as memory.
- **LLM client** (`daytrader/live/llm_client.py`) — official Anthropic SDK,
  manual tool-use loop, adaptive thinking, refusal handling. Default model
  `claude-opus-4-8` (configurable via `AGENT_MODEL`).
- **Audited tool surface** (`daytrader/live/tools.py`) — the only ways an agent
  can act: place_trade, close_position, flatten_all, get_positions,
  get_performance, journal_write, request_dev_help.
- **Paper broker + SQLite persistence** (`daytrader/live/paper_broker.py`,
  `db.py`) — simulated market fills at live prices with realistic slippage,
  long/short accounting, restart-safe state (positions, cash, journal, equity).
- **Market-state snapshot** (`daytrader/live/market_state.py`) — live prices,
  indicators, regime, fresh signals from the validated book, account state.
- **Dev-request channel** (`daytrader/live/dev_requests.py`) — files GitHub
  issues (`GITHUB_TOKEN`/`GITHUB_REPO`), with a DB fallback.
- **Market-hours runner** (`daytrader/live/runner.py`) — open→plan,
  interval→trade, close→flatten+review; hard daily-loss circuit breaker and
  forced EOD flat enforced in code. CLI: `python -m daytrader.agent {run,once,plan,review,status}`.
- **`Dockerfile.agent`** — container for the agent service (separate from the
  legacy crypto image). Requires `ANTHROPIC_API_KEY` at runtime.

### Notes
- `status` runs with no API key (shows what the agents see). The trading
  commands require `ANTHROPIC_API_KEY` and degrade gracefully without it.

---

## [5.0.0] — 2026-06-13

**Ground-up rewrite: SPY / Mag7 intraday day-trading system.** A new, independent
engine that day-trades SPY and the Mag7 (AAPL, MSFT, GOOGL, AMZN, NVDA, META,
TSLA) with a backtester built to be honest rather than flattering. The legacy
crypto bot is untouched and still lives under `engine/`; the new system lives
entirely under `daytrader/`. Major version bump because this is a new product
surface, not an iteration on the crypto bot.

### Added
- **Realistic backtest engine** (`daytrader/backtest/engine.py`) — next-bar
  execution (no look-ahead), slippage + half-spread, gap-through-stop fills,
  forced end-of-day flat, daily loss limit, optional breakeven/trailing stops.
- **Nine intraday strategies** (`daytrader/strategies/`) — Opening Range
  Breakout, VWAP reversion, VWAP-trend pullback, Connors RSI(2), Bollinger fade,
  EMA pullback, MACD continuation, pivot reversal, gap-and-go. All causal and
  lookahead-verified.
- **Regime-gated ensemble + SPY market-direction filter** (`daytrader/portfolio/`)
  — strategies fire only in their suited ADX regime and only with SPY's trend.
- **Validation** (`daytrader/backtest/validate.py`) — walk-forward in-sample /
  out-of-sample split, Monte-Carlo drawdown distribution, strategy correlation.
- **Risk-based position sizing, full metric suite, HTML report** with an inline
  equity-vs-SPY chart and a reality score, plus a CLI (`python -m daytrader …`).
- **Free data loader** (Yahoo Finance) with on-disk caching: 5m/15m (~60d),
  1h (~2y), daily (full history).

### Results (honest)
- Validated `trend` book (5m, market filter): out-of-sample profit factor 1.60,
  max drawdown ~1.7% (Monte-Carlo p95 1.5%), beat SPY out-of-sample by +4.5 pts.
- The 2:1 profit-factor target was **not** robustly met; the sub-10% drawdown
  target was met by a wide margin. Full scorecard and reasoning in
  `daytrader/RESULTS.md`.

### Notes
- The new day trader is CLI-only for now; the Docker image (`docker.yml`) still
  builds and runs the legacy crypto `engine/`.

---

## [4.3.0] — 2026-05-01

Joint Codex + Claude code review pass. Closes four real money-affecting bugs,
adds a deterministic risk-sizing layer, and gives Claude (the PM) more
expressive trade tags.

### Fixed (Track A — stop the bleeding)
- **`database.py:get_period_pnl`** — was computing realized P&L as
  `sells - buys` over the time window, which double-counted gross transaction
  values and badly misled the weekly digest, monthly stats, and Claude's own
  performance feedback. Now sums FIFO sell P&L from `get_trades_with_pnl`.
- **`ai_strategy.py` agent loop** — typo `kraken.get_ohlc(...)` should have
  been `get_ohlcv(...)`. Every Haiku agent cycle was silently AttributeError-ing
  on BTC candle data between PM sessions.
- **Drawdown circuit breaker survives restarts** — `_peak_equity` no longer
  resets to starting capital on init; it loads `MAX(equity)` from the
  performance snapshot table. Previously a restart silently disabled the
  breaker until a new peak was hit.
- **Paper trader applies slippage** — docstring claimed slippage but no
  slippage was applied; default is now 0.05% per side (configurable via
  `PAPER_SLIPPAGE_PCT`). Paper P&L now resembles what live execution would
  deliver.
- **`bot.py` startup status** — `for/else` clause always logged "No open
  position" because the loop never broke. Restructured.

### Added (Track B — risk hardening)
- **`risk_manager.py`** — central deterministic sizing layer. Every BUY,
  SCALE-IN, and LIMIT_BUY runs through `clamp_buy_size()` which enforces:
  - `max_position_pct` (single position vs equity, default 25%)
  - `max_per_coin_pct` (combined exposure to one coin, default 35%)
  - `max_risk_per_trade_pct` (stop-distance dollars, default 1.5%)
  - `max_total_exposure_pct` (total holdings, default 80% — leaves dry powder)
  - drawdown breaker multiplier (halves size when drawdown active)
  - cash cap (always last; never overspend)
  Each clamp records a reason; the operator sees what bound the size.
- **Daily loss cooldown** — tracks day-start equity at UTC midnight. If
  `daily_loss_limit_pct` (default 4%) is breached, all new buys are blocked
  until midnight. Protective exits still execute.
- **Pending-buy cash reservation** — open buy limit orders subtract from
  "available cash" before sizing. Claude can no longer overcommit by
  stacking GTCs.
- **Pending-buy fills merge into existing positions** — previously a filled
  pending buy always inserted a new `Position` row, so a coin with both a
  market and a limit fill produced split records that broke `get_open_position`,
  scale-in math, and stop placement. Now merges via weighted average.

### Added (Track C — profit upside)
- **USD / risk-dollar trade sizing** — system prompt now teaches Claude to
  express size as `usd=N` (notional dollars) or `risk_usd=N` (stop-distance
  dollars) instead of coin units. Code converts to qty after risk clamps.
  Legacy `qty=` still accepted.
- **Multi-trade per PM session** — Claude can now place up to 3 trade tags
  per response (configurable via `MAX_TRADES_PER_PM_SESSION`). Risk clamps
  apply per-trade and the per-coin / total-exposure caps naturally
  distribute the budget. Previously only the first tag was acted on.
- **Multi-symbol order book depth** — instead of fetching only BTC depth,
  the scanner now grabs concurrent depth for BTC + every open position +
  the top 3 candidates by composite score. Claude sees real spread/wall
  data on thinly-traded alts (POL, DOT, DOGE) before trading them.

### Changed
- `StrategyConfig` defaults tightened: `risk_per_trade_pct` 2.0 → 1.5,
  `max_position_pct` 30 → 25; new `max_per_coin_pct`, `max_total_exposure_pct`,
  `daily_loss_limit_pct`, `max_trades_per_pm_session` fields.
- System prompt expanded with explicit hard sizing caps and daily-loss-limit
  description so Claude reasons inside the same rules the code enforces.

---

## [1.1.0] — 2026-04-15

### Added
- **Kraken integration** — 24/7 spot crypto trading via `python-kraken-sdk`
  - `kraken_session_manager.py` — REST client with ticker, OHLC, balance, and order placement
  - `kraken_order_executor.py` — limit/market order execution with dry-run support
  - Kraken `platform` type in Accounts UI — add your API key/secret from the web dashboard
- **Dual-broker crypto strategies** — `crypto_momentum` and `crypto_mean_reversion` now
  automatically route to Kraken when `platform=kraken`, Tastytrade when `platform=tasty_crypto`
- **`KRAKEN_API_KEY` / `KRAKEN_API_SECRET`** added to `.env.example` and `config.py`
- **Semantic versioning** — `VERSION` file + `CHANGELOG.md` added to repo root
- Engine gracefully skips a broker if its credentials are missing (warns in logs instead of crashing)

### Changed
- `strategies/base.py` — `BaseStrategy.__init__` now accepts optional `kraken` kwarg
- `engine.py` — routes strategies to Kraken or Tastytrade based on `platform` field;
  each broker connects independently on startup
- `requirements.txt` — added `python-kraken-sdk>=3.0.0`, removed `apscheduler` (unused)

---

## [1.0.0] — 2026-04-15

### Added
- Initial release — full AlgoTrader system
- Node.js/React web dashboard (Express + Vite + shadcn/ui + Drizzle/SQLite)
- Python strategy engine sidecar with 6 strategies:
  - **Short Put** — delta/DTE/POP filtering, DXLinkStreamer Greeks
  - **Credit Spread** — put or call spreads, configurable width
  - **Iron Condor** — simultaneous put + call spread
  - **Covered Call** — OTM call against existing long position
  - **Crypto Momentum** — EMA breakout buy with stop/target exit
  - **Crypto Mean Reversion** — EMA dip buy, exits at EMA recovery
- Tastytrade SDK integration (Session auth, Account, DXLinkStreamer)
- REST API sync — strategies, trades, positions, logs all flow through Node.js API
- `DRY_RUN=true` default — no live orders without explicit opt-in
- `run.sh` launch script with auto-dependency install and mode banner

## [1.2.0] - 2026-04-15

### Added
- **Bullflow Options Flow Scanner** (`options_flow_scanner` strategy type)
  - Real-time SSE stream from `api.bullflow.io/v1/streaming/alerts`
  - OCC symbol parser (extracts ticker, expiry, strike, option type, DTE)
  - Composite scoring model (premium size + Repeater pattern weight)
  - Configurable filters: minPremium, minScore, callsOnly, excludeEtfs, minDTE, maxDTE
  - Auto-executes calls or stock via Tastytrade on score threshold
  - All params tunable in the web UI
  - Auto-reconnects on stream drop
  - Daily trade limit + midnight reset
- `BULLFLOW_API_KEY` added to `config.py` and `.env.example`
- Scanner strategy type visible in Strategies page with default params pre-filled
- Account field optional for scanner type (only needed for live execution)

## [1.2.2] - 2026-04-15

### Fixed
- GitHub Actions: add `setup-buildx-action` so GHA cache backend works correctly
- Repo visibility set to public so `ghcr.io/emdoc12/algotrader:latest` is pullable without auth

## [1.2.3] - 2026-04-15

### Fixed
- Dockerfile: run `npm ci` with scripts enabled so `better-sqlite3` native addon compiles correctly
- supervisord: increase engine `startsecs` to 15s so web server is fully up before Python engine connects

## [1.2.4] - 2026-04-15

### Fixed
- supervisord + entrypoint: API_BASE_URL was pointing to port 3000 but Express listens on 5000 — corrected to http://localhost:5000
- Dockerfile: EXPOSE updated to 5000

## [1.2.5] - 2026-04-15

### Fixed
- server/db.ts: auto-create all tables on first boot using CREATE TABLE IF NOT EXISTS — no drizzle-kit push needed in Docker
- Fixes "no such table: bot_logs / strategies" errors on fresh container start
