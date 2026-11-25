# Plan

Decisions from the planning session on 2026-11-24. Vocabulary is defined in `GLOSSARY.md`, the reasons behind key choices are in `docs/adr/`, and the API and pricing facts are in `docs/research/`. HANDOFF.md is the original research brief; where it conflicts with this file, this file wins.

## Product
- A Python CLI. `rank` prints the Top Results as a `rich` table, or as JSON with `--json`. `watch` runs Watch Mode.
- **Data:** plain HTTP GETs to the public `api.arenaclub.com` (ADR 0001). The user-agent is an honest `arenaclub-optimizer/<version> (+repo url)`. Requests are spaced ≥1s apart, card details are cached by card id, and the tool never logs in, stores cookies, opens a browser, or bypasses bot protection. If a request gets a 403 or 429, the tool backs off and reports it; it never tries a workaround.
- **Scope:** only Slab Packs whose Product Type is card. A Slab Pack whose `status` isn't `active` counts as sold out and is always excluded.
- **Values** (ADR 0002): Market Value comes from Manual Overrides, then PokéPrice Tracker (free key), then CardVolume; PriceCharting/SportsCardsPro are used only if the user adds a key. Values are cached with their date and number of sales and refreshed by staleness within a daily credit budget. Cards graded by Arena Club use their raw Market Value, marked low-confidence. Unmatched cards go on the Needs-review List. The budget is spent a Slab Pack at a time (issue #34): packs cheapest first, ties by pack id; within a pack Grail, then Chase, then Lineup, higher Hit Rate first. A pack already rankable from overrides and fresh cache goes last, packs with rejected Hit Rates are skipped, and a shared card is valued once. At about 13 new cards a day on PokéPrice's free tier, the first Gold Pokémon pack (about 60 of its 62 cards to reach 95% Value Coverage) is ranked on about day 5, then one more pack about every 5 days; spreading the budget across all packs by tier delayed the first by more than a week.
- **AC Value** = k × Market Value, where k is set in config (default 1.0).
- **Cost** = list price + payment-method fee (wallet by default, which is free; ACH is 0.8% capped at $5; card is 2.9% + $0.30) + a configurable tax rate (default 0). Promos are out of scope.
- **Metrics per Slab Pack:**
  - Market EV and Buyback EV (estimated), each in $ and as EV %
  - Slab Safe EV as an extra column: `Σ p·max(0.8·P, 0.9·A) − 1.1·P`, adjusted for Cost
  - P(profit), standard deviation (as a multiple of Cost), median Pull value ÷ Cost
  - Top-heaviness: the share of expected Buyback proceeds from the rarest 1% of probability mass
- **Ranking:** by Buyback EV % descending.
- **Hit Rates:** `formattedHitRate` is a per-card percentage. Scale the rates so they sum to exactly 100%; warn if the raw sum is more than 0.5% off, and skip the Slab Pack with a logged error if it's more than 5% off.
- **Value Coverage:** a card with no value counts as $0, which makes EV a lower bound. A Slab Pack is ranked only if Value Coverage is ≥95%. Show a "lower bound" note when any Grail or Chase card has no value.
- **High-variance Note:** added when P(profit) < 25% or Top-heaviness > 50%. Both thresholds are configurable.
- **Filters:** Category (multiple allowed), price min/max, minimum EV %, minimum P(profit), `--exclude-high-variance`, and `--top N` (default 10).
- **Watch Mode:**
  - runs locally; default interval 10 min, minimum 5, with jitter
  - sends an Alert only when a Slab Pack newly meets the criteria or its EV % moves past a threshold, deduped using local state
  - "meets the criteria" means passing the filters, before the top-N cut, so a pack moving across the cut isn't alerted again; `watch` has no `--top` (issue #38)
  - a failed cycle backs off: from the interval, doubling to 2 hours, or Retry-After if longer; reset by a successful cycle
  - ntfy: a random 24-character Topic generated on first run and saved in the local state directory (`$XDG_STATE_HOME/arenaclub-optimizer/topic`, never the user's config file; `watch.topic` in config overrides it); `arena topic` prints `https://ntfy.sh/<topic>` and `--test` sends a test notification; the server is configurable
  - sends one Alert when requests start failing, and one when they recover
- **Stack:** Python 3.14, uv, pytest, ruff, httpx, typer, rich.
- Secrets and local state (`.env`, config, cache) are gitignored and never committed. The repo is public.

## Process
- Work is organised in sprints. Every story is a GitHub issue with acceptance criteria, grouped under a milestone for its sprint.
- Each story is done by an implementer subagent on `feat/<issue#>-<slug>`, test-first (red-green-refactor). The implementer opens the PR with `Closes #N`.
- A separate reviewer subagent reviews the PR against the issue, the plan, and the tests, and posts its review as a PR comment. (All agents run as the same GitHub account, so formal approvals aren't possible.)
- The orchestrator squash-merges once the review passes and CI is green. Branch protection on `main` requires CI to pass.
- Each sprint ends with a short review/retro, appended to `docs/sprints.md`.

## Backlog
- **Sprint 0: setup.** Initial docs commit. Scaffold (uv, pytest, ruff, package layout, CLI stub). CI on GitHub Actions. Branch protection.
- **Sprint 1: offline calculator.** Pack model and Hit Rate normalisation; Cost model; Market EV, Buyback EV and Slab Safe EV; risk metrics and the High-variance Note; Value Coverage and the lower-bound note. Tests against a hand-computed 3-card pack.
- **Sprint 2: live ranking.** Arena Club client, filters, `rank` output and `--json`, Manual Overrides file.
- **Sprint 3: values.** Value-provider interface, PokéPrice and CardVolume providers, cache and credit budget, card matching (`insert` + `setNumber` + name), Needs-review List.
- **Sprint 4: Watch Mode.** Topic, Alerts with dedupe, back-off, interval with jitter.
