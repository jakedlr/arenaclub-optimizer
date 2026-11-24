# Rank on Buyback EV estimated from Market Value × k, using free value sources

The real exit from a Rip is Arena Club's 90% Buyback of its own AC Value, but AC Value isn't available before a Rip through any channel we accept. The pack and card API endpoints don't carry it, logged in or out. The marketplace exposes it only through an Algolia key that has to be captured from the Cloudflare-protected site. So we estimate AC Value as `k × Market Value`, where k is set by the user (default 1.0). Buyback EV is always labelled an estimate, and Market EV is shown next to it.

Market Value comes from free sources only, in this order: the user's manual overrides, then PokéPrice Tracker (a free personal-use key), then CardVolume. PriceCharting and SportsCardsPro ($49/month each) are optional and turn on only when the user adds a key. Cards graded by Arena Club itself fall back to their raw Market Value, marked low-confidence, because no provider prices Arena Club grades.

## Consequences

- In practice v1 ranks only Pokémon Slab Packs automatically. Other Categories need overrides or a paid key to reach the Value Coverage threshold (95% of Hit Rate probability).
- Our figures won't match shouldirip's, and they move with k.
- Free tiers are rate-limited (about 50 graded lookups a day), so values are cached and refreshed by staleness, not on every Watch Mode cycle.
- Research: `docs/research/arenaclub-api.md` and `docs/research/price-sources.md`.
