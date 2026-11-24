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
```

## Test

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```

## Responsible use

Ripping Slab Packs is gambling-like: every Rip is a random draw, and a pack with positive EV can still lose money on most Rips. This tool is a decision aid, not advice. It reports variance and P(profit) alongside EV so the risk is visible, and its EV figures are estimates that rest on assumptions (such as k). It only reads public data with plain, rate-limited requests and never logs in or bypasses bot protection.
