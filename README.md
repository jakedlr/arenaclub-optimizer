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
```

`arena rank` options override the config file (`docs/config.example.toml`). On the command line, `--min-ev` and `--min-p-profit` take percents (`5` means 5%), while the config file writes the same settings as fractions (`0.05`). The table fits 80 columns, with each Slab Pack's Notes on their own lines under it; `--wide` adds Market EV %, Slab Safe EV $ and Coverage. Requests are spaced 1 s apart and card details are cached, so the first run for a Category takes several minutes and later runs are much faster. A price range also cuts requests: Price Levels outside it are skipped before any Slab Pack is fetched. If Arena Club blocks a request (HTTP 403 or 429), the command stops at once and exits with code 2.

Until value sources arrive, cards have no Market Value unless you add Manual Overrides (`docs/overrides.example.toml`), so most Slab Packs show as unrankable (low Value Coverage).

## Test

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```

## Responsible use

Ripping Slab Packs is gambling-like: every Rip is a random draw, and a pack with positive EV can still lose money on most Rips. This tool is a decision aid, not advice. It reports variance and P(profit) alongside EV so the risk is visible, and its EV figures are estimates that rest on assumptions (such as k). It only reads public data with plain, rate-limited requests and never logs in or bypasses bot protection.
