# arenaclub-optimizer

A command-line tool that tells you which Arena Club Slab Packs are worth ripping, and can notify your phone when a good one appears.

For each Slab Pack it works out the **expected value** (EV) of one Rip: what you'd get back on average if you sold the card straight back to Arena Club. It ranks the packs from best to worst and warns you when a pack's good average depends on a small chance of a huge hit, so that most Rips actually lose money.

> **Responsible use.** Ripping Slab Packs is gambling-like. Every Rip is a random draw, and a pack with a positive EV can still lose money on most Rips. This tool is a decision aid, not advice. Its numbers are estimates built on public prices and on the assumption described under `k` below. It only reads public data, spaces its requests out, and never logs in to Arena Club or gets around its bot protection.

---

## Contents

1. [Key terms](#key-terms)
2. [Setup (one time)](#setup-one-time)
3. [Quick start](#quick-start)
4. [Commands](#commands)
   - [`arena rank`](#arena-rank): rank the packs
   - [`arena review`](#arena-review): see which cards have no price
   - [`arena topic`](#arena-topic): set up phone notifications
   - [`arena watch`](#arena-watch): keep checking and notify you
5. [Reading the `rank` table](#reading-the-rank-table)
6. [Where prices come from, and why ranking takes days](#where-prices-come-from-and-why-ranking-takes-days)
7. [Manual Overrides: setting prices yourself](#manual-overrides-setting-prices-yourself)
8. [The config file](#the-config-file)
9. [Running Watch Mode in the background](#running-watch-mode-in-the-background)
10. [Files the tool creates](#files-the-tool-creates)
11. [Exit codes](#exit-codes)
12. [JSON output](#json-output)
13. [Troubleshooting](#troubleshooting)
14. [For developers](#for-developers)

---

## Key terms

| Term | Meaning |
|---|---|
| **Slab Pack** | An Arena Club product that gives you exactly one graded card when opened, e.g. "Gold Pokémon". |
| **Rip** | Buying and opening one Slab Pack. The card you get is your **Pull**. |
| **Category** | What the cards are: Pokémon, Football, One Piece, and so on. |
| **Price Level** | Arena Club's price tiers ("Gold" is $100, for example). One Price Level holds packs in several Categories. |
| **Checklist** | Every card a pack can currently give you, each with its **Hit Rate**: the published chance of pulling it. It changes as cards are pulled. |
| **Tier** | Cards grouped by rarity: Grail (rarest), Chase, and Lineup (most common). |
| **Buyback** | Arena Club's instant offer to buy your Pull back for 90% of its own estimated value (the **AC Value**). |
| **Market Value** | What the same card, at the same grade from the same grading company, sells for on public markets. |
| **k** | Arena Club doesn't publish AC Values before you rip, so the tool assumes AC Value = k × Market Value. The default k is 1. |
| **Cost** | What you actually pay for one Rip: list price plus any payment fee and tax. |
| **Buyback EV** | The average Buyback you'd get from one Rip, minus the Cost. **Buyback EV %** is that divided by the Cost. It is what the ranking uses. |
| **P(profit)** | The chance that a single Rip gives you back at least what you paid. |
| **Top-heaviness** | How much of a pack's EV comes from its rarest 1% of pulls. High means the EV is mostly a lottery ticket. |
| **High-variance Note** | A warning added when P(profit) is under 25% or Top-heaviness is over 50%. |
| **Value Coverage** | The share of a pack's pull chances whose cards have a price. A pack is only ranked at **95% or more**, because an EV built on missing prices would be misleading. |
| **Manual Override** | A price you set yourself for a specific card. It always wins over every price source. |
| **Watch Mode** | `arena watch`: re-ranking on a timer and sending **Alerts** to your phone. |
| **Topic** | Your private notification channel name on ntfy, the free notification service the tool uses. |

---

## Setup (one time)

You need a terminal on Linux, macOS or Windows (WSL). Every command below goes into that terminal.

**1. Install uv.** uv runs the tool and installs Python for you.

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close and reopen the terminal afterwards so the `uv` command is found.

**2. Download the tool and install its dependencies.**

```sh
git clone https://github.com/jakedlr/arenaclub-optimizer.git
cd arenaclub-optimizer
uv sync
```

`uv sync` installs the right Python version (3.14) and everything else the tool needs. **Run every `uv run arena …` command from inside this `arenaclub-optimizer` folder.**

**3. Get a free price key (strongly recommended).** Without it, the tool can only price a few dozen famous cards, and almost no pack will reach the 95% Value Coverage it needs to be ranked.

1. Sign up for a free account at [pokemonpricetracker.com](https://www.pokemonpricetracker.com) and copy your API key. The free tier is for personal use, which this is.
2. In the `arenaclub-optimizer` folder, create a file named `.env` containing one line:

   ```
   POKEPRICE_API_KEY=paste-your-key-here
   ```

   The `.env` file is never uploaded to GitHub. Don't share your key or paste it anywhere public.

**4. Check it works.**

```sh
uv run arena --help
```

This should list the commands `rank`, `review`, `topic` and `watch`. To print the installed version, run `uv run arena --version`.

---

## Quick start

```sh
# Rank Pokémon packs between $50 and $150:
uv run arena rank --category pokemon --price-min 50 --price-max 150

# See which cards still have no price, and why:
uv run arena review --category pokemon --price-max 150

# Set up phone notifications, then send yourself a test:
uv run arena topic
uv run arena topic --test

# Keep checking every 10 minutes and notify your phone:
uv run arena watch --category pokemon --price-max 150
```

**On your first day, expect "0 ranked".** The tool needs prices for nearly every card in a pack before it ranks that pack. The free price key covers about 13–17 new cards a day, so the first pack is usually ranked after about 4–5 days. Each run carries on where the last one stopped. [Why it works this way](#where-prices-come-from-and-why-ranking-takes-days), and [how to speed it up](#manual-overrides-setting-prices-yourself).

**The first run for a Category takes several minutes.** The tool waits 1 second between requests to Arena Club so it never overloads the site. It also saves card details on your computer, so later runs are much faster.

---

## Commands

All commands start with `uv run arena`. Add `--help` to any command to see its options in the terminal, e.g. `uv run arena rank --help`.

You never *have* to give an option. Anything you leave out comes from [the config file](#the-config-file) if you have one, and otherwise from the built-in default listed below.

### `arena rank`

Prints the best Slab Packs for your criteria, best first. "Best" means the highest Buyback EV %.

```sh
uv run arena rank [OPTIONS]
```

| Option | What it does | Default |
|---|---|---|
| `--category NAME` | Only include this Category, e.g. `pokemon`, `football`, `"one piece"`. Capital letters and accents don't matter. Repeat the option for several: `--category pokemon --category football`. | All Categories |
| `--price-min DOLLARS` | Only include packs whose list price is at least this, e.g. `--price-min 50`. | No minimum |
| `--price-max DOLLARS` | Only include packs whose list price is at most this, e.g. `--price-max 150`. A price range also makes the run faster, because price tiers outside it are never downloaded. | No maximum |
| `--min-ev PERCENT` | Only show packs whose Buyback EV % is at least this. Write it as a plain percentage: `--min-ev 5` means 5%. It can be negative: `--min-ev -10` keeps packs losing at most 10% on average. | No minimum |
| `--min-p-profit PERCENT` | Only show packs where the chance of a single Rip paying for itself is at least this, from 0 to 100: `--min-p-profit 20` means 20%. | No minimum |
| `--exclude-high-variance` | Hide packs that carry a High-variance Note (see [Key terms](#key-terms)). | Shown |
| `--include-high-variance` | Show them. This only matters if your config file excludes them by default. | |
| `--top N` | How many packs to show (1 or more). | 10 |
| `--k RATIO` | The assumed AC Value ÷ Market Value ratio, e.g. `--k 1.2` if you believe Arena Club values cards 20% above market. Must be more than 0. | 1 |
| `--wide` | Show three extra columns: Market EV %, Slab Safe EV $ and Coverage. The default table fits an 80-character terminal; the wide one needs about 112. | Off |
| `--json` | Print the results as JSON instead of a table, for scripts and spreadsheets. See [JSON output](#json-output). | Off |
| `--help` | Show the options and exit. | |

**Examples**

```sh
uv run arena rank --category pokemon
uv run arena rank --category pokemon --price-max 100 --min-p-profit 25 --exclude-high-variance
uv run arena rank --category football --category basketball --top 5 --wide
uv run arena rank --k 1.2 --json > results.json
```

What the output means is explained in [Reading the `rank` table](#reading-the-rank-table).

> `rank` looks up missing prices as it goes, so every run can spend some of the day's free price credits. That's expected: it's how packs fill in over the days.

### `arena review`

Lists the cards in your chosen packs that have **no price yet** (the Needs-review List), explains why for each one, and prints a ready-to-paste snippet so you can price them yourself. Use it to get a pack ranked sooner, or to unblock a pack that will never reach 95% on its own.

```sh
uv run arena review [OPTIONS]
```

| Option | What it does | Default |
|---|---|---|
| `--category NAME` | As in `rank`. Repeatable. | All Categories |
| `--price-min DOLLARS` | As in `rank`. | No minimum |
| `--price-max DOLLARS` | As in `rank`. | No maximum |
| `--json` | Print the list as JSON. | Off |
| `--help` | Show the options and exit. | |

`review` has no EV, P(profit) or `--top` options, because packs with missing prices don't have an EV yet. Like `rank`, it may spend price credits looking cards up.

**The output has two parts:**

- **Needs review.** No source could price these cards, e.g. "unmatched by all providers" or "no price at grade". Price them yourself with the snippet below the table; see [Manual Overrides](#manual-overrides-setting-prices-yourself).
- **Waiting.** These cards simply weren't looked up yet, because today's free credits ran out or a source was briefly unavailable. They'll be tried again on a later run, so they're left out of the snippet. You don't need to do anything.

### `arena topic`

Shows your **Topic**, the private ntfy channel your phone listens to for Alerts, and how to subscribe to it.

```sh
uv run arena topic [--test]
```

| Option | What it does |
|---|---|
| `--test` | Also send one test notification, to check your phone receives it. |
| `--help` | Show the options and exit. |

**Setting up notifications:**

1. Install the free **ntfy** app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy), [iPhone](https://apps.apple.com/app/ntfy/id1625396347)), or use a web browser.
2. Run `uv run arena topic`. It prints your Topic and a Subscribe URL.
3. In the ntfy app, tap **+** and enter the Topic. Leave the server as `ntfy.sh`. Or open the Subscribe URL in a browser.
4. Run `uv run arena topic --test`. A test notification should arrive within a few seconds.

The Topic is a random 24-character name created the first time you run this, and it stays the same after that. **Anyone who knows your Topic can read your Alerts, so keep it private.** To use a Topic of your own choosing, or your own ntfy server, set `watch.topic` or `watch.ntfy_server` in [the config file](#the-config-file).

### `arena watch`

**Watch Mode.** It re-runs the ranking on a timer, every 10 minutes by default, and sends an Alert to your phone when something worth knowing happens. Leave it running and check your phone instead of the terminal.

```sh
uv run arena watch [OPTIONS]
```

It takes the same filters as `rank`, plus three of its own:

| Option | What it does | Default |
|---|---|---|
| `--category NAME` | As in `rank`. Repeatable. | All Categories |
| `--price-min DOLLARS` | As in `rank`. | No minimum |
| `--price-max DOLLARS` | As in `rank`. | No maximum |
| `--min-ev PERCENT` | As in `rank`. Only packs at or above this trigger Alerts. | No minimum |
| `--min-p-profit PERCENT` | As in `rank`. | No minimum |
| `--exclude-high-variance` / `--include-high-variance` | As in `rank`. | Shown |
| `--k RATIO` | As in `rank`. | 1 |
| `--interval MINUTES` | Minutes between checks, at least 5. Each wait varies randomly by up to 10%, so requests don't land at exactly the same time every cycle. | 10 |
| `--once` | Run a single check and stop. Useful for testing your filters. | Off |
| `--quiet` | Only print a line when a check fails. Useful when it runs in the background. | Off |
| `--help` | Show the options and exit. | |

There is no `--top`: every pack that meets your criteria can trigger an Alert, not just the top 10.

**Example**

```sh
uv run arena watch --category pokemon --price-max 150 --min-ev 0
```

At start it prints your Topic and Subscribe URL. After each check it prints one line, for example:

```
[2026-11-26 10:00 UTC] 2 ranked, 5 unrankable, 0 skipped; 1 alert sent
```

"2 ranked" means two packs met your criteria. Press **Ctrl+C** to stop. It saves its state and exits cleanly.

**When you get an Alert:**

- **New Top Result:** a pack newly meets your criteria. The Alert shows its Buyback EV %, the dollar amount, P(profit) and any High-variance Note.
- **Improved:** a pack you were already alerted about has climbed at least 5 percentage points of Buyback EV % since that Alert.
- **Problem / Recovered:** checks started failing (Arena Club blocking requests, network trouble, or the price cache broken), and later, when they work again. You get one Alert when the problem starts and one when it ends, not one per check.

**When you don't get an Alert:** a pack that stays in your results doesn't re-alert every 10 minutes. A pack that briefly drops out isn't announced again when it comes back, as long as it returns within 24 hours. That covers dropping out because Arena Club swapped in a card that has no price yet, or because a check couldn't download it. A pack that drops out because it really fell below your criteria (e.g. its EV dropped under `--min-ev`) *is* announced again if it returns.

**When checks fail**, Watch Mode waits longer before trying again: one interval, then twice as long each time, up to 2 hours, or longer if Arena Club asks for more. It goes back to the normal interval after a successful check.

Watch Mode reads the config file and Manual Overrides only when it starts. **Restart it after you change them.** To keep it running after you close the terminal, see [Running Watch Mode in the background](#running-watch-mode-in-the-background).

**Things to know:**

- If ntfy itself is down, the Alert is retried on the next check, so very occasionally one arrives twice.
- If ntfy is down for a whole outage, you won't get the Problem or Recovered Alert for it.

---

## Reading the `rank` table

Example (`uv run arena rank --category pokemon`):

```
                             Buyback   Buyback                              Top-
Slab Pack            Price      EV %      EV $   P(profit)   median×   heaviness
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Silver Pokémon      $50.00     +3.1%     $1.55       34.0%     0.71×          8%
Gold Pokémon       $100.00     -6.2%    -$6.20       22.0%     0.58×         41%
        Buyback EV is an estimate: AC Value = k × Market Value, k = 1.
Notes:
- Gold Pokémon: Only 22% of pulls return at least your cost; a typical pull
returns 0.58× your cost.
2 ranked, 1 unrankable (low Value Coverage), 0 skipped; values: 120 from cache, 14 fetched, 5 need review
```

(The numbers are illustrative.)

| Column | Meaning |
|---|---|
| **Slab Pack** | Price Level and Category. |
| **Price** | Arena Club's list price. |
| **Buyback EV %** | The headline number. On average, how much you gain (+) or lose (−) per Rip as a share of what you paid, if you sell every Pull back to Arena Club. +3.1% means you get back $1.031 per $1 spent *on average*, not on every Rip. |
| **Buyback EV $** | The same in dollars per Rip. |
| **P(profit)** | The chance that a single Rip pays for itself. 34% means roughly one Rip in three. |
| **median×** | What a typical Rip returns, as a multiple of your cost. 0.71× means a middle-of-the-road Rip gets back 71% of what you paid. When this is well below 1 but the EV is positive, a few rare big hits are carrying the average. |
| **Top-heaviness** | How much of the EV comes from the rarest 1% of pulls. 41% means nearly half the value is a long shot. |

**With `--wide`** you also get:

| Column | Meaning |
|---|---|
| **Market EV %** | EV if you could sell each Pull at full market price instead of taking the 90% Buyback. This is what most EV websites quote. |
| **Slab Safe EV $** | EV per Rip if you buy Slab Safe, Arena Club's insurance that costs 10% extra and guarantees a Buyback of at least 80% of the price. Shown as "—" when a pack doesn't offer it. |
| **Coverage** | The pack's Value Coverage. Ranked packs are always 95% or more. |

**Below the table:**

- **The k line** reminds you that Buyback EV rests on the k assumption.
- **Notes** cover each pack:
  - The **High-variance Note**: low P(profit) or high Top-heaviness.
  - A **lower-bound** note: a Grail or Chase card has no price, so the real EV is probably higher.
  - **"N cards valued at low confidence"**: prices from few sales, cards graded by Arena Club itself, or old prices waiting to be refreshed.
  - A note when Arena Club's published Hit Rates didn't add up to 100% and were rescaled.
- **Skipped** lines name packs that couldn't be evaluated this run, with the reason. Buyback might be disabled for that pack, or its Hit Rates might be inconsistent.
- **Sources** lists the CardVolume pages behind any prices shown, as CardVolume's terms require.
- **The summary line** counts:
  - **ranked:** shown, or met your criteria
  - **unrankable:** below 95% Value Coverage, so not ranked yet
  - **skipped:** couldn't be evaluated
  - **values:** where the card prices came from. "from cache" means saved from earlier runs, "fetched" means looked up now, and "need review" means still missing.

---

## Where prices come from, and why ranking takes days

Each card's Market Value comes from the first of these that has one:

1. **Your Manual Overrides**: prices you set. These always win.
2. **PokéPrice Tracker**: Pokémon cards, using your free key. It covers PSA, CGC, BGS, SGC, TAG and ACE grades.
3. **CardVolume**: free and needs no key, but it only lists a couple of hundred well-known cards.

Prices are saved on your computer and reused for 7 days before they're looked up again.

**Why packs take days to rank.** The free PokéPrice key gives 100 credits a day, and it resets at midnight UTC. Looking a card up the first time costs about 6–8 credits, and a card the tool has already identified costs 1–2 to refresh. That works out to roughly 13–17 new cards a day. A typical pack has about 60 cards. Common Lineup cards make up about 98% of the pull chances, so a pack can't reach 95% Value Coverage until nearly all of them are priced.

To get packs ranked as soon as possible, the tool spends credits **one pack at a time**:

- **Cheapest pack first.** Each pack is finished before the next one starts.
- **Rarest cards first within a pack:** Grail, then Chase, then Lineup.
- **Already-rankable packs go to the back of the queue**, along with packs whose Hit Rates are broken. A card shared by several packs is looked up once.

With the free key, expect your first pack to be ranked after about **4–5 days**, then about one more every 4–5 days. To speed up a particular pack, narrow `--category` and the price range to it, or price its cards yourself with Manual Overrides.

**Cards no source can price.** Some cards PokéPrice can't find, such as some older Japanese sets, and the tool never prices cards graded by Arena Club itself directly. A card graded by Arena Club gets its ungraded price, marked low confidence. If a pack still can't reach 95% because of unpriced cards, it keeps getting first call on credits each month when the "not found" answers expire. Run `arena review` and price those cards yourself to unblock it.

---

## Manual Overrides: setting prices yourself

A Manual Override sets a card's Market Value to a number you choose, for example from recent eBay sold listings for that exact card, grader and grade. It wins over every other source.

**1. Find the cards that need prices:**

```sh
uv run arena review --category pokemon --price-max 100
```

**2. Create the overrides file** if you don't have one yet:

```sh
mkdir -p ~/.config/arenaclub-optimizer
cp docs/overrides.example.toml ~/.config/arenaclub-optimizer/overrides.toml
```

Delete the example entries in it.

**3. Paste the snippet that `review` printed** into that file, and fill in a `market_value` (in dollars) for each card. **Delete any entry you don't want to price.** An entry left without a `market_value` stops the tool with an error, so a card never silently counts as $0. An entry looks like this:

```toml
["159274382"]              # the card's cert number (or its "card_…" id)
market_value = 42.50       # dollars, 0 or more
note = "avg of 5 eBay sold, PSA 10"   # optional, for your own reference
```

Cards are identified by cert number, or by Arena Club card id (starting `card_`). Copy them exactly as `review` prints them: they are case-sensitive. If both match a card, the cert number wins.

**4. Run `rank` again.** Your prices apply immediately. Restart `watch` if it's running.

---

## The config file

The config file sets your defaults, so you don't have to type the same options every time. It's optional: with no file, everything uses the defaults shown in the command tables above. Command-line options always override it.

```sh
mkdir -p ~/.config/arenaclub-optimizer
cp docs/config.example.toml ~/.config/arenaclub-optimizer/config.toml
```

Then edit `~/.config/arenaclub-optimizer/config.toml` in any text editor. Every setting, with its default:

| Setting | Meaning | Default |
|---|---|---|
| `k` | AC Value ÷ Market Value (see [Key terms](#key-terms)). More than 0. | `1.0` |
| `payment_method` | How you pay for Rips: `"wallet"` (no fee), `"ach"` (0.8%, max $5) or `"card"` (2.9% + $0.30). It is added to your Cost. | `"wallet"` |
| `tax_rate` | Sales tax on a Rip, as a fraction: `0.0775` means 7.75%. | `0.0` |
| `coverage_threshold` | Value Coverage a pack needs before it's ranked, from 0 to 1. Lowering it ranks packs sooner, but on less reliable numbers. | `0.95` |
| `p_profit_threshold` | A High-variance Note is added when P(profit) is below this. | `0.25` |
| `top_heaviness_threshold` | …or when Top-heaviness is above this. | `0.5` |
| `[filters]` `categories` | Default Categories, e.g. `["Pokemon", "Football"]`. Empty means all. | `[]` |
| `[filters]` `price_min`, `price_max` | Default price range in dollars. Leave them out for no limit. | none |
| `[filters]` `min_ev_pct` | Default minimum Buyback EV %, **as a fraction**: `0.05` means 5%. | none |
| `[filters]` `min_p_profit` | Default minimum P(profit), **as a fraction**: `0.2` means 20%. | none |
| `[filters]` `exclude_high_variance` | `true` to hide High-variance packs by default. | `false` |
| `[filters]` `top` | Default number of Top Results. | `10` |
| `[watch]` `interval_minutes` | Default minutes between Watch Mode checks, at least 5. | `10` |
| `[watch]` `ntfy_server` | Where Alerts are sent. | `"https://ntfy.sh"` |
| `[watch]` `topic` | Your own Topic name instead of the generated one: 1–64 letters, digits, `_` or `-`. | generated |

> **Percent vs fraction.** On the command line you type percentages (`--min-ev 5` means 5%). In the config file the same settings are fractions (`min_ev_pct = 0.05`).

A mistake in the file, such as a misspelled setting or a value out of range, stops the tool with a message naming the setting. Nothing is silently ignored.

---

## Running Watch Mode in the background

`arena watch` stops when you close its terminal. To keep it running, use one of the two methods below.

**Keep its output private.** Watch Mode's output starts with your Topic, and anyone who can read it can read your Alerts. Both methods below keep the output readable only by you.

### Option A: `nohup` (simplest)

From the `arenaclub-optimizer` folder, paste this whole block. Change the filters on the `nohup` line to what you want watched.

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

- **See what it's doing:** `tail -f ~/.local/state/arenaclub-optimizer/watch.log` (Ctrl+C stops viewing, not Watch Mode)
- **Stop it:** `pkill -f 'bin/arena watch'`. Use this exact pattern; a looser one like `"arena watch"` can kill other programs that mention it.

### Option B: systemd user service (starts automatically)

Follow the steps at the top of [`docs/arena-watch.service`](docs/arena-watch.service). In short: copy it to `~/.config/systemd/user/`, set the folder and filters in it, then run `systemctl --user enable --now arena-watch`.

- **See what it's doing:** `journalctl --user -u arena-watch -f`
- **Stop it:** `systemctl --user stop arena-watch`

The system journal keeps the startup lines that show your Topic. Your user, root, and members of the `systemd-journal` and `adm` groups can read it.

### On Windows (WSL)

Windows may shut WSL down when no WSL terminal window is open, and that stops Watch Mode whichever method you use. **Keep a WSL window open** while it runs. Option B also needs systemd turned on in WSL; the unit file explains how.

---

## Files the tool creates

| File | What it holds | Safe to delete? |
|---|---|---|
| `.env` (in the project folder) | Your PokéPrice key. You create this. | It stops PokéPrice lookups |
| `~/.config/arenaclub-optimizer/config.toml` | Your settings. You create this. | Defaults are used |
| `~/.config/arenaclub-optimizer/overrides.toml` | Your Manual Overrides. You create this. | Your prices are lost |
| `~/.cache/arenaclub-optimizer/cards/` | Saved Arena Club card details | Yes; they're re-downloaded slowly |
| `~/.cache/arenaclub-optimizer/values.sqlite` | Saved prices, plus today's credit count | Yes, but every price is looked up again, using credits |
| `~/.local/state/arenaclub-optimizer/topic` | Your generated Topic | Deleting it gives you a new Topic, and you must re-subscribe |
| `~/.local/state/arenaclub-optimizer/alerts.json` | What you've already been alerted about | Yes; current packs alert again once |

If you've set `XDG_CONFIG_HOME`, `XDG_CACHE_HOME` or `XDG_STATE_HOME`, those folders are used instead of `~/.config`, `~/.cache` and `~/.local/state`.

---

## Exit codes

Each command sets an exit code when it finishes, for use in scripts.

| Code | `rank` / `review` | `topic` | `watch --once` |
|---|---|---|---|
| `0` | Success | Success | The check went fine, or you stopped it |
| `1` | Arena Club couldn't be reached or answered with an error | The Topic file couldn't be saved, or `--test` failed | The check failed for another reason |
| `2` | Arena Club blocked a request (HTTP 403/429), or an option or the config/overrides file is invalid | The config file is invalid | Arena Club blocked a request |

`watch` without `--once` keeps running through failures (backing off as described above) and exits 0 when you stop it.

---

## JSON output

`arena rank --json` prints one JSON document. It has the same packs as the table, plus every card in each ranked pack, with its price, source, sales count, confidence and citation. It also records the settings used. A `version` number at the top changes whenever a field is renamed or removed or its meaning changes.

- **Version 2** (current): each result has a `cards` list. Each card has `card_id`, `cert_number`, `label`, `tier`, `tier_type`, `hit_rate` and `market_value`, plus a `value` object (`source`, `as_of`, `sales_count`, `confidence`, `citation`), which is `null` if the card has no price.
  - `hit_rate` is the raw per-card rate Arena Club publishes, as a fraction, before rescaling.
  - `summary.values` counts prices by origin: `override`, `cache`, `stale_cache`, `fetched` and `needs_review`.
  - `unavailable_sources` lists any price source that stopped answering, with the reason.
- **Version 1**: the first release.

`arena review --json` prints its own document (version 1): `needs_review` and `waiting` lists of `card_id`, `cert_number`, `label`, `reason` and `slab_packs`, with counts under `summary`.

---

## Troubleshooting

| Symptom | What it means / what to do |
|---|---|
| `0 ranked, N unrankable` | Normal for the first few days: those packs don't have 95% of their cards priced yet. Keep running `rank` or `watch` daily, or add [Manual Overrides](#manual-overrides-setting-prices-yourself). |
| `Value source unavailable: pokeprice (PokéPrice key not set)` | The `.env` file is missing, misspelled, or not in the folder you ran the command from. See [Setup step 3](#setup-one-time). |
| `budget exhausted, retry after … 00:00 UTC` | Today's 100 free credits are used up. Run again after midnight UTC. |
| Arena Club blocked a request (exit code 2) | Arena Club is refusing requests for now. Wait a while and try again. The tool deliberately doesn't try to get around this. |
| `market_value is required` | An entry in `overrides.toml` has no price. Fill it in or delete the entry. |
| Error naming a config setting | That setting in `config.toml` is misspelled or out of range; the message says which. |
| No test notification arrives | Check that the Topic in the ntfy app exactly matches `uv run arena topic`, and that the app allows notifications. |
| `uv: command not found` | Reopen the terminal after installing uv, or run `~/.local/bin/uv` instead. |
| The first run is very slow | Expected. Card details download at 1 per second, then they're saved for next time. A price range (`--price-max`) cuts the download. |

---

## For developers

The design is in `docs/plan.md`, the vocabulary in `GLOSSARY.md`, decisions in `docs/adr/`, and sprint history in `docs/sprints.md`.

```sh
uv run pytest              # tests
uv run ruff check          # lint
uv run ruff format --check # formatting
```
