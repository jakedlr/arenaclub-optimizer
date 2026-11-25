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
uv run arena topic --test
uv run arena watch --category pokemon --price-max 100
```

`arena rank` options override the config file (`docs/config.example.toml`). On the command line, `--min-ev` and `--min-p-profit` take percents (`5` means 5%), while the config file writes the same settings as fractions (`0.05`). The table fits 80 columns, with each Slab Pack's Notes on their own lines under it; `--wide` adds Market EV %, Slab Safe EV $ and Coverage. Requests are spaced 1 s apart and card details are cached, so the first run for a Category takes several minutes and later runs are much faster. A price range also cuts requests: Price Levels outside it are skipped before any Slab Pack is fetched. If Arena Club blocks a request (HTTP 403 or 429), the command stops at once and exits with code 2.

`arena topic` prints the Topic, the ntfy channel Watch Mode sends Alerts to, and its subscribe URL. Subscribe in the ntfy app or open the URL in a browser; `--test` sends one test notification so you can check it arrives. The Topic is generated at random on first use and saved in `$XDG_STATE_HOME/arenaclub-optimizer/topic` (else `~/.local/state/...`); anyone who knows it can read your Alerts, so keep it private. Set `watch.topic` or `watch.ntfy_server` in the config file to use your own.

## Watch Mode

`arena watch` runs the same ranking as `rank` every 10 minutes and sends Alerts to your Topic through ntfy. It takes `rank`'s filter options (`--category`, `--price-min`, `--price-max`, `--min-ev`, `--min-p-profit`, `--exclude-high-variance`, `--k`), plus:

- `--interval MINUTES`: time between cycles, at least 5 (default: `watch.interval_minutes` in the config file, else 10). Each wait varies by up to 10% either way and is measured from the start of the last cycle, so a cycle that runs long is followed at once. Cycles never overlap.
- `--once`: run one cycle and exit, with 0 if it went fine, 2 if Arena Club blocked a request (as `rank` does) and 1 for any other failure.
- `--quiet`: print a cycle's summary line only when the cycle fails.

At start it prints your Topic and its subscribe URL. Each cycle then prints one line, for example `[2026-11-26 10:00 UTC] 2 ranked, 5 unrankable, 0 skipped; 1 alert sent`, where "ranked" counts every Slab Pack that meets the criteria.

An Alert goes out when a Slab Pack newly meets your criteria, or when its Buyback EV % rises 5 percentage points above the value last alerted. A pack that stays in gets no repeat Alert. Every pack that meets the criteria counts, not only the top 10, so `watch` has no `--top`. A pack that briefly drops below 95% Value Coverage (say a replacement card has no value yet) or can't be fetched for a cycle keeps its place for up to 24 hours, so it isn't announced again when it comes back. One that falls below `--min-ev` or another criterion is a real change, and is announced again if it returns. Watch Mode also sends one Alert when its cycles start failing (see below) and one when they work again. What has been alerted is kept in `$XDG_STATE_HOME/arenaclub-optimizer/alerts.json`, so a restart doesn't repeat Alerts. A notification that can't be sent is logged and tried again on the next cycle, so now and then an Alert can arrive twice (for example if ntfy received it but the reply was lost). If ntfy is down for a whole outage, neither the failure Alert nor the recovery Alert arrives.

When a cycle fails, Watch Mode backs off instead of waiting the interval. Failure means Arena Club blocked a request (HTTP 403 or 429), another request failed, more than half the Slab Packs couldn't be fetched, or the value cache couldn't be used. The first failure waits one interval, and each further failure doubles the wait, up to 2 hours. If Arena Club's Retry-After asks for longer, Watch Mode waits that long instead. A successful cycle resets the back-off. Each cycle opens a new connection to Arena Club and new value sources, and closes them at its end. The caches are on disk, so they carry over between cycles.

Ctrl+C (or SIGTERM) abandons the cycle in progress, saves the Alert state and exits 0; a second Ctrl+C during that save is ignored. Log lines, like the summary lines, are stamped in UTC. Watch Mode reads the config and overrides files once at start, so restart it after you change them.

### Running it in the background (Linux and WSL)

Watch Mode's output starts with your Topic, so wherever it goes (a log file, the systemd journal) has to stay private: anyone who can read it can subscribe to your Alerts.

With `nohup`, from the repo directory. The subshell's `umask 077` makes the directory and log readable by you only, and the `chmod`s fix ones left by an earlier run:

```sh
(
  umask 077
  dir=~/.local/state/arenaclub-optimizer
  mkdir -p "$dir" && chmod 700 "$dir"
  touch "$dir/watch.log" && chmod 600 "$dir/watch.log"
  nohup uv run arena watch --quiet --category pokemon --price-max 100 \
    >> "$dir/watch.log" 2>&1 &
)
```

Stop it with `pkill -f 'bin/arena watch'`. That sends SIGTERM to the Watch Mode process itself, which saves its state and exits. The pattern is narrow on purpose: a looser one such as `"arena watch"` also matches any shell, editor or script whose command line mentions it.

With systemd, run it as a user service: [`docs/arena-watch.service`](docs/arena-watch.service) is an example unit, with instructions for installing it and turning systemd on in WSL. Follow its output with `journalctl --user -u arena-watch -f`. The journal keeps the startup lines with your Topic; your own user, root and members of the `systemd-journal` and `adm` groups can read it.

On WSL, Windows may stop the Linux distribution once no WSL terminal is open, and that stops Watch Mode either way, under `nohup` or systemd (even with linger turned on). Keep a WSL window open while it runs.

## Values

Each card's Market Value comes from the first of these that has one (ADR 0002):

1. your Manual Overrides (`docs/overrides.example.toml`);
2. PokéPrice Tracker, when `POKEPRICE_API_KEY` is set in the environment or in a `.env` file in the working directory (free key, 100 credits a day);
3. CardVolume (free, no key).

Values are cached in `~/.cache/arenaclub-optimizer/values.sqlite` (or under `$XDG_CACHE_HOME`) and refreshed after a week. Each run values only the cards of the Slab Packs matching your Category and price filters.

The daily credit budget is spent a Slab Pack at a time, so packs reach the 95% Value Coverage threshold, and get ranked, as early as possible. Packs go cheapest first (ties by pack id), and within a pack Grail cards come first, then Chase, then Lineup, the higher Hit Rate first. A pack already rankable from your Manual Overrides and fresh cached values costs nothing and goes last, packs whose Hit Rates would be rejected are skipped, and a card shared by several packs is looked up once. When the budget runs out the run stops looking cards up, and the next run carries on from the cache. On PokéPrice's free tier a first lookup costs 7–8 credits, about 13 new cards a day. A Gold Pokémon pack has about 62 cards, and Lineup cards hold about 98% of its Hit Rate, so reaching 95% takes about 60 of them: expect the first pack to be ranked on about the fifth day, and each further pack about five days later (sooner when overrides, CardVolume or shared cards cover some). To get a particular pack ranked first, narrow `--category` and the price range to it, or give its cards Manual Overrides. A pack that can't reach coverage because some of its cards no source prices stays first in line: each time those cards' cached "not found" answers expire (after 30 days) it gets first call on credits again, so give those cards Manual Overrides from `arena review` to unblock it.

The summary line ends with `values: N from cache, M fetched, K need review`. A Slab Pack with low-confidence values (cards graded by Arena Club, thin sales, a stale cached value) gets a note, and every CardVolume figure behind the Top Results is cited in a Sources footer, as CardVolume's terms require.

`arena review` lists the cards no source could value (the Needs-review List) for the same Category and price filters as `rank`, each with why, and prints an `overrides.toml` snippet for them. Paste it into your overrides file, then fill in each `market_value` or delete the entry; an entry left without one stops `rank` with an error rather than counting the card as $0. Cards that only weren't looked up this run, because a source's daily budget ran out or it was temporarily unavailable, are listed separately under "Waiting" and left out of the snippet: a later run will try them again. `arena review --json` prints the list as JSON.

## JSON output

`arena rank --json` prints one JSON document; `version` changes when a key is renamed or removed, or its meaning changes.

- **Version 2** (values): each result has a `cards` list, one entry per Checklist card with `card_id`, `cert_number`, `label`, `tier`, `tier_type`, `hit_rate` (the raw per-card rate Arena Club publishes, as a fraction, before EV rescales the Checklist to sum to 100%), `market_value` and a `value` object (`source`, `as_of`, `sales_count`, `confidence`, `citation`), or `null` when the card has no value. `summary.values` counts the cards valued by each origin (`override`, `cache`, `stale_cache`, `fetched`) and those that `needs_review`. `unavailable_sources` maps each value source that stopped answering during the run to its reason. Market Values now come from the value sources as well as Manual Overrides.
- **Version 1**: the first release.

`arena review --json` prints its own document (version 1) with `needs_review` and `waiting` lists of entries with `card_id`, `cert_number`, `label`, `reason` and `slab_packs`, and their counts under `summary`.

## Test

```sh
uv run pytest
uv run ruff check
uv run ruff format --check
```

## Responsible use

Ripping Slab Packs is gambling-like: every Rip is a random draw, and a pack with positive EV can still lose money on most Rips. This tool is a decision aid, not advice. It reports variance and P(profit) alongside EV so the risk is visible, and its EV figures are estimates that rest on assumptions (such as k). It only reads public data with plain, rate-limited requests and never logs in or bypasses bot protection.
