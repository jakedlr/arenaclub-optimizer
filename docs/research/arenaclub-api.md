# Arena Club API findings (spike, 2026-11-24)

Observed from the arenaclub.com frontend; unofficial and subject to change.

## Access
- `https://api.arenaclub.com` answers plain HTTP GETs (curl, no cookies) with 200 for the public endpoints below.
- `https://arenaclub.com` (the website) returns 403 to curl, and **headless Chrome gets a Cloudflare "Attention Required" block**. Only a headed browser loads it. Do not attempt to bypass this.
- Logging in adds nothing relevant: card responses carry no value fields logged in or out.

## Endpoints
- `GET /v4/repacks`: Price Levels. `items[]`: `id, name` ("Gold"), `numItems, category` (card/watch/coin/wine = Product Type), `priceCents, position, repackSeriesIds[]`. 44 levels ($25–$10,000), 136 Slab Packs total.
- `GET /v4/repacks/{id}`: one Slab Pack. `name` (Category, e.g. "Pokemon"), `status` ("active"), `isOfferEnabled, offerPercentage` (0.9), `offerInsuredPercentage` (0.8), `isInsuranceAvailable, isTokenLocked`, `category{id,name,numItems,priceCents}`, `tiers[]{name, type (grail/chase/line_up), position, formattedHitRate (per-card %), itemIds[]}`.
  Gold Pokémon example: Super Grail 0.0002%×1, Grail 0.02%×3, Chase 0.25%×8, Lineup 1.96%×50.
- `GET /v4/items/{card_id}`: card identity. `year, brand, playerName, setName, subsetName, setNumber, parallelName, insert, gradingCompany, overall` (grade), `slabKind, rarity, holo, team, cardCertNumber, status, priceBadge, clPriceBadge`. **No AC Value.**
- No sold-out or remaining-count field; only `status`.
- `POST /v3/repacks/{id}/purchase-details` requires `type: 'stripe'|'wallet'`. It's probably a checkout quote. Not called with a valid body, because it might start a purchase.

## AC Value availability (spike 2, 2026-11-24)
- `GET /v3/items/{card_id}` (the id keeps its `card_` prefix) also has **no value field**. It adds `isInMarketplace, isWatched, numFavoriteCount`. In Gold Pokémon, 8 of 62 checklist cards were `isInMarketplace: true` (Lineup cards already pulled and relisted by users).
- The marketplace's `estimatedValueCents` comes from Algolia. The search key has to be captured from the Cloudflare-protected marketplace page and appears to be short-lived, so it's **out of bounds** under our rules.
- The Hit Feed exists only on the protected site.
- The API returns 403 to Python urllib's default user-agent, but 200 to curl's and to an honest custom UA such as `arenaclub-optimizer/0.1 (+repo url)`.
- Conclusion: there is no ToS-clean source of AC Value before a rip.
