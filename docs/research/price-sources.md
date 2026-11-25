# Graded-card price sources (spike, 2026-11-24)

Test set: the 62-card Gold Pokémon checklist. Terms and prices come from each provider's own pages as of this date; recheck before relying on them.

| Provider | Free? | Terms | Coverage (of 62) | Sports | Notes |
|---|---|---|---|---|---|
| CardVolume | Yes, JSON API with no key | API use allowed and must cite the card URL; scraping the site at scale is banned | 2 | Yes | Only ~212 curated high-value cards; a market price needs ≥5 sales in 90 days |
| PokéPrice Tracker | Free key (signup) | Free tier is personal/non-commercial; Business plan is $99/month | not tested | No | 100 credits/day and 60 requests/min; a graded lookup costs +1 credit (~50 cards/day); "PSA prices update weekly" is **UNVERIFIED** and conflicts with PPT's own daily-refresh claim (see `value-apis.md`) |
| PriceCharting | Legendary plan, $49/month | Internal use only; cache must be purged when the subscription ends | not tested | Separate (SportsCardsPro, $49/month) | 1 request/s; below grade 10 its grades ignore the grader ("Graded 9"); grader-specific prices exist only for 10s |
| HobbyCardIndex | No (API from $49.99/month) | Scraping banned | not tested | Yes | |
| TCG Price Lookup | Graded prices from $19.99/month | Free and $19.99 tiers are non-commercial | not tested | No | |

## Findings
- No provider prices **Arena Club's own grades** (`gradingCompany: arena_club`, cert format `8AC…`). There were 6 of them in Gold Pokémon, all Lineup.
- Matching: `setName` is generic ("2021 Pokemon SWSH"). Use `insert` (e.g. "Evolving Skies") plus `setNumber` plus the card name.
- PSA's API only verifies certs; it has no prices.
