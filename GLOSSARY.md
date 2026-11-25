# Arena Club Pack Optimizer

Ranks Arena Club Slab Packs by expected value of a rip and flags the ones whose EV hides high risk.

## Packs

**Slab Pack**:
An Arena Club product, identified by one Price Level and one Category (e.g. Gold Pokémon), that yields exactly one graded card drawn from its Checklist when ripped. Counts as sold out when Arena Club no longer marks it active.
_Avoid_: Pack (unqualified), box, break, series, repack

**Price Level**:
A named price point (e.g. Gold, $100) shared by Slab Packs across Categories.
_Avoid_: Repack, level, tier (Tier means something else)

**Category**:
The subject of a Slab Pack's cards, such as Pokémon, Football or One Piece.
_Avoid_: Series, sport

**Product Type**:
What a Slab Pack yields: card, watch, coin or wine. Only card packs are in scope.
_Avoid_: Category (Arena Club's own API uses that word for this)

**Checklist**:
The current set of cards a Slab Pack can yield, each with its Hit Rate. It changes as cards are pulled, so every figure derived from it is point-in-time.
_Avoid_: Pool, odds sheet

**Hit Rate**:
The published probability that one rip yields a specific card on the Checklist.
_Avoid_: Odds, weight, pull rate

**Tier**:
A named group of Checklist cards sharing a per-card Hit Rate (e.g. Grail, Chase, Lineup).
_Avoid_: Rarity

**Rip**:
Buying and opening one Slab Pack.
_Avoid_: Pull (a Pull is the card a Rip yields), open

**Pull**:
The single card a Rip yields.

## Value

**AC Value**:
Arena Club's own estimated value for a card, and the basis for Buyback. It isn't visible before a Rip, so it's estimated as k × Market Value.
_Avoid_: Market value, price, comp

**Market Value**:
What a specific graded card (same card, grading company and grade) sells for on public markets, based on comps.
_Avoid_: Price, AC Value

**Value Quote**:
A Market Value as one source states it: the figure, the date it applies to, how many sales it rests on, how far it can be trusted, and the citation the source asks for, if any. A Manual Override counts as a Value Quote from the user.
_Avoid_: Price, comp, valuation

**k**:
The assumed ratio of AC Value to Market Value, set by the user (default 1.0).
_Avoid_: Multiplier, markup

**Manual Override**:
A Market Value the user enters for a specific card. It takes precedence over every value source.

**Needs-review List**:
The cards whose Market Value couldn't be determined automatically, shown to the user to resolve with Manual Overrides.

**Value Coverage**:
The share of a Checklist's Hit Rate probability whose cards have a Market Value. A Slab Pack below the threshold is not ranked.

**Buyback**:
Arena Club's instant offer to buy a Pull back for 90% of its AC Value.

**Cost**:
What the user actually pays for one Rip.
_Avoid_: Price (Price is the list price; Cost may differ)

**Buyback EV**:
The expected Buyback proceeds of one Rip, minus Cost. It's always an estimate, because it rests on k.
_Avoid_: EV (unqualified)

**Market EV**:
The expected Market Value of one Rip, minus Cost. This is the figure shouldirip-style sites report.

**EV %**:
Buyback EV divided by Cost. The metric that orders Top Results.
_Avoid_: ROI, return

**Top Results**:
The Slab Packs matching the user's criteria, ordered by EV % descending.

## Risk

**P(profit)**:
The probability that a single Rip's Buyback proceeds are at least its Cost.
_Avoid_: Win rate, hit chance

**Top-heaviness**:
The share of a Slab Pack's expected Buyback proceeds contributed by its rarest 1% of probability mass.
_Avoid_: Lottery-ness

**High-variance Note**:
A warning attached to a Slab Pack whose P(profit) or Top-heaviness crosses the configured threshold.

## Watch mode

**Watch Mode**:
Running the ranking repeatedly on an interval and sending Alerts.
_Avoid_: Auto mode, daemon

**Alert**:
A notification sent when a Slab Pack newly meets the user's criteria or its EV % moves past the threshold.

**Topic**:
The randomly generated ntfy channel name the user subscribes to in order to receive Alerts.
_Avoid_: Channel
