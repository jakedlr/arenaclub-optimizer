# arenaclub-optimizer

A command-line tool that ranks Arena Club Slab Packs by the expected value of a Rip, using public Arena Club data and public market prices, and flags packs whose EV hides high risk with a High-variance Note. `arena rank` prints the Top Results for your criteria (Category, price range and more), and `arena watch` runs Watch Mode, sending Alerts through ntfy. See `GLOSSARY.md` for the vocabulary and `docs/plan.md` for the design.

## Install

Requires [uv](https://docs.astral.sh/uv/) and Python 3.14.

```sh
uv sync
```

## Run

```sh
uv run arena --help
uv run arena rank --category pokemon --price-min 50 --price-max 150 --min-ev 5
uv run arena rank --wide
uv run arena rank --json
uv run arena review --category pokemon --price-max 100
```

`arena rank` options override the config file (`docs/config.example.toml`). On the command line, `--min-ev` and `--min-p-profit` take percents (`5` means 5%), while the config file writes the same settings as fractions (`0.05`). The table fits 80 columns, with each Slab Pack's Notes on their own lines under it; `--wide` adds Market EV %, Slab Safe EV $ and Coverage. Requests are spaced 1 s apart and card details are cached, so the first run for a Category takes several minutes and later runs are much faster. A price range also cuts requests: Price Levels outside it are skipped before any Slab Pack is fetched. If Arena Club blocks a request (HTTP 403 or 429), the command stops at once and exits with code 2.

## Values

Each card's Market Value comes from the first of these that has one (ADR 0002):

1. your Manual Overrides (`docs/overrides.example.toml`);
2. PokéPrice Tracker, when `POKEPRICE_API_KEY` is set in the environment or in a `.env` file in the working directory (free key, 100 credits a day);
3. CardVolume (free, no key).

Values are cached in `~/.cache/arenaclub-optimizer/values.sqlite` (or under `$XDG_CACHE_HOME`) and refreshed after a week. Each run values only the cards of the Slab Packs matching your Category and price filters, Grail and Chase cards first, so a limited daily credit budget goes where it matters most. The summary line ends with `values: N from cache, M fetched, K need review`. A Slab Pack with low-confidence values (cards graded by Arena Club, thin sales, a stale cached value) gets a note, and every CardVolume figure behind the Top Results is cited in a Sources footer, as CardVolume's terms require.

`arena review` lists the cards no source could value (the Needs-review List) for the same Category and price filters as `rank`, each with why, and prints an `overrides.toml` snippet for them. Paste it into your overrides file and fill in each `market_value`; an entry left without one stops `rank` with an error rather than counting the card as $0. `arena review --json` prints the list as JSON.

## JSON output

`arena rank --json` prints one JSON document; `version` changes when a key is renamed or removed, or its meaning changes.

- **Version 2** (values): each result has a `cards` list, one entry per Checklist card with `card_id`, `cert_number`, `label`, `tier`, `tier_type`, `hit_rate`, `market_value` and a `value` object (`source`, `as_of`, `sales_count`, `confidence`, `citation`), or `null` when the card has no value. `summary.values` counts the cards valued by each origin (`override`, `cache`, `stale_cache`, `fetched`) and those that `needs_review`. `unavailable_sources` maps each value source that stopped answering during the run to its reason. Market Values now come from the value sources as well as Manual Overrides.
- **Version 1**: the first release.

`arena review --json` prints its own document (version 1) with `needs_review` entries of `card_id`, `cert_number`, `label`, `reason` and `slab_packs`.

## Test

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```

## Responsible use

Ripping Slab Packs is gambling-like: every Rip is a random draw, and a pack with positive EV can still lose money on most Rips. This tool is a decision aid, not advice. It reports variance and P(profit) alongside EV so the risk is visible, and its EV figures are estimates that rest on assumptions (such as k). It only reads public data with plain, rate-limited requests and never logs in or bypasses bot protection.
