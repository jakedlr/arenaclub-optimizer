# HANDOFF: Arena Club Slab Pack EV Tool

Planning handoff for a free, open-source alternative to shouldirip.com: an expected-value (EV) calculator for Arena Club Slab Packs.

Compiled 2026-11-24. The research below was current as of that date. Verify Arena Club's mechanics in the app before relying on them, because the platform changes often.

---

## 1. Goal

Given an Arena Club Slab Pack (price, checklist, per-card hit rates), answer two questions:

1. **Does this pack have positive EV?** In other words, is the average market value of a pull at least the price?
2. **What is my real cash-out EV?** That means the 90% buyback minus my actual cost, including promos and tax.

The tool should be accurate enough to support a single rip decision. It does not need to be a live dashboard.

### Non-goals (v1)
- No auto-ripping or buying.
- No high-frequency polling or bypassing bot protection (see §6).
- No multi-user hosting.

---

## 2. Domain facts (Arena Club)

| Fact | Detail |
|---|---|
| Pack structure | One graded slab per pack. Every card on the checklist has a published hit rate (rounded to 0.01%). |
| Tiers | From highest to lowest: Super Grail, Grail, Chase, Tier 1–3, and Lineup, depending on the pack. Tier hit rate = per-card rate × number of cards in the tier. |
| Pool dynamics | The checklist updates in real time as cards are pulled. A pulled card is replaced with one "of equal value" or the pack sells out. EV is a point-in-time figure. |
| Instant buyback | 90% of **Arena Club's own estimated value**, credited to the wallet instantly. You have about 24 hours to accept. |
| AC valuation vs market | AC's estimate often runs **above** eBay comps. One review saw an AC value of $90 for a card whose comps were $50–60. This gap is the main reason buyback EV can beat market EV. |
| Slab Safe / Collect Safe | Costs 10% of the pack price. It guarantees a buyback offer of at least 80% of the pack price, or 90% of AC value, whichever is higher. **It cannot be combined with the T-Mobile promo.** |
| Marketplace sale | 10% seller fee (waived for VIP tiers). |
| Withdrawal | Wallet → Stripe → bank or debit card, typically 1–3 business days. Requires KYC. "Arena Cash" promo credit **cannot** be withdrawn. |
| Shipping a slab home | $1 retrieval fee plus shipping and insurance. |
| Payment fees | Wallet: free. ACH: 0.8% (capped at $5). Card: 2.9% + $0.30. |

### The current trigger: T-Mobile Tuesdays offer
- 50% off one Slab Pack, **plus tax**. Redeem by **Tue Dec 1, 4:59am ET**.
- Cannot be combined with Slab Safe.
- Unknown: whether there's a dollar cap, and whether tax is charged on the list price or the discounted price. Check at checkout.

---

## 3. The math

Let `p_i` = hit rate of card *i*, `M_i` = market value (comps), `A_i` = Arena Club estimated value, `P` = list price, `C` = actual cost (after promo and tax).

```
Market EV      = Σ p_i · M_i                 # what shouldirip-style sites report
Market EV %    = Market EV / P − 1

Buyback EV     = Σ p_i · 0.9 · A_i           # what you actually cash out instantly
Net buyback    = Buyback EV − C

Sell-on-eBay   = Σ p_i · (M_i · (1 − ebay_fee) − ship_out_cost) − C
Best-exit EV   = Σ p_i · max(0.9·A_i, M_i·(1−fee) − costs) − C   # per-card optimal exit
```

Add a Slab Safe variant when it applies: `Σ p_i · max(0.8·P, 0.9·A_i) − 1.1·P`.

**Break-even rule of thumb with the T-Mobile promo.** Cost is about 0.54·P (50% + ~7.75% San Diego tax on the discounted price, *assumed*). Buyback-only break-even is `Σ p_i·A_i ≥ 0.60·P`. In shouldirip terms, any pack at **−40% EV or better** is +EV, *if* A ≈ M.

Also compute these useful outputs:
- P(pull ≥ 1× price), P(≥ 2×), P(≥ 10×)
- A histogram of value/price (shouldirip's "shape": 16 log-spaced buckets, red below 1×, green above)
- Variance / standard deviation, and how much of the EV comes from the top 1% of probability mass (the lottery-ness of the pack)
- Sensitivity: EV if Lineup values are ±20%

### Known reference: shouldirip.com methodology (from its /about page)
- `EV = Σ (card_value × hit_rate)`. Values are its own estimates from public-marketplace sold results, with rank-based interpolation where no comp exists.
- It doesn't say whether it uses the 90% buyback or what fees it subtracts. Their "+1.6%" figures are market-based, not buyback-based.
- It admits that 1/1 grails and thin cards have rough values.

---

## 4. Data inputs

### 4a. Pack checklist + hit rates (required)
Options, simplest first:
1. **Manual CSV** (v1 recommendation): `tier, card_label, grade, grader, cert, hit_rate`. Paste it from the app or transcribe it from screenshots. A Gold Pokémon pack has about 61 cards, so this is quick.
2. **Screenshot OCR** of the in-app checklist (v2).
3. **Arena Club endpoints.** Pack pages are at `arenaclub.com/slab-packs/<uuid>` (e.g. Gold Pokémon = `c43ab013-f7d9-4930-b2fd-f06eb8191de3`). They're JS-rendered and blocked to simple fetchers. The checklist/odds API endpoint is **not yet identified**: inspect the network tab in a normal logged-in browser session.

Example data: mycardrips.com/catalog/pokemon/arena-club/gold/ has a September 2026 snapshot of the Gold Pokémon checklist (61 cards with grades and certs, no values). Tier rates: Grail 0.02%/card, Chase 0.24%/card, Lineup 1.96%/card.

### 4b. Market value per slab (required)
- **eBay sold listings** for the same card, grade and grader. Comp-Sniper uses the last 21 days with outliers trimmed. The eBay Browse/Marketplace Insights API is the ToS-clean route, but sold-data access is restricted. Scraping is against eBay's ToS.
- **PriceCharting.** Has per-grade prices (Grade 7–10, PSA 10) based on eBay sales, and offers a paid API. The poke-value repo scrapes it with a spoofed user-agent, which violates their ToS, so don't copy that approach.
- **Fallback:** manual entry and cached values, with a `confidence` field (comp count, days since last sale).
- Sports cards need their own source. 130point, PSA APR, and Card Ladder are candidates; check each one's terms.

### 4c. Arena Club estimated value (needed for buyback EV)
- Marketplace listings expose **`estimatedValueCents`**, plus `askingPriceCents` and `unifiedPrice`, through Arena Club's Algolia search (found in Comp-Sniper's `sniper/arena_client.py`).
- Item detail: `https://api.arenaclub.com/v3/items/{objectID}`. Check whether pack checklist items carry the same field.
- If AC values aren't available before ripping, model `A_i = k · M_i` with `k` set by the user (default 1.0; reviews suggest 1.2–1.5 for some cards).

---

## 5. Prior art reviewed (GitHub, 2026-11-24)

No repo does Arena Club Slab Pack EV. What each one offers:

| Repo | License | Reuse |
|---|---|---|
| `mymoneymymind/ArenaClub-Comp-Sniper` | none | **Reference only.** It shows Algolia listing fields (`estimatedValueCents`, `card.{setName,gradingCompany,overall}`), the `api.arenaclub.com/v3/items/{id}` endpoint, and an eBay comp approach (21-day window, outlier trim). ⚠️ It bypasses Cloudflare/eBay protections, polls every 20s, and opens a Chrome debug port. Don't copy those patterns. |
| `kirkian95-eng/poke-value` | MIT | Its `graded_prices` schema (`database/schema.py:139`) is a reasonable model for per-grade prices. Its PriceCharting scraper violates their ToS. |
| `sternd/card-pack-ev-calculator` | MIT | Generic weighted-sheet EV + Monte Carlo (`backend/calculations.py` `pack_ev_dynamic` ~L387, `simulate_pack_opening_dynamic` ~L645). Note: it uses int weights and drops $0 cards from the mean, which skews EV. Don't repeat that. |
| `FutureGadgetCollections/collection-market-tracker-ev-simulator` | none | Sealed-pack, per-rarity only. Not useful. |
| `bingomanatee/arenaClub` | none | A UI mockup with a static marketplace snapshot. Not useful. |
| `milesdavidlee/rippies` | none | Pack-opening animation. Not useful. |

Without a license, a repo is all rights reserved. Read it for ideas, but don't copy its code.

---

## 6. Constraints & ethics

- **Respect Terms of Service.** No Cloudflare bypass, no spoofed user-agents, and no high-frequency polling of Arena Club or eBay. Prefer manual input, official APIs, paid data APIs, or low-rate requests from a normal logged-in browser session.
- Keep credentials and cookies local (`.env`, gitignored). Never commit them.
- This is a decision aid for gambling-like products. Show variance and P(loss) prominently alongside EV.

---

## 7. Suggested plan

**Phase 1: Core calculator (offline, manual data)**
- [ ] Data model: Pack (name, price, timestamp) → Cards (tier, label, grade, grader, cert, hit_rate, market_value, ac_value?, value_confidence)
- [ ] Load the pack from CSV and validate that hit rates sum to ~100% (warn on rounding drift and normalize)
- [ ] Compute Market EV, Buyback EV, Best-exit EV, and the Slab Safe variant
- [ ] Cost model: list price, promo %, promo cap, tax rate, tax base (list vs discounted), payment fee
- [ ] Outputs: EV table, P(≥1×/2×/10×), value/price histogram, top contributors to EV, sensitivity (±20% Lineup)
- [ ] Unit tests with a hand-computed 3-card pack

**Phase 2: Pricing**
- [ ] Pricing provider interface (manual → PriceCharting API → eBay API)
- [ ] Cache comps with a timestamp and comp count, and flag stale or thin values
- [ ] Map card labels (e.g. `2023 POKEMON PAL EN IONO SPECIAL ILLUSTRATION RARE PSA 10`) to search queries

**Phase 3: Convenience**
- [ ] Screenshot/OCR or clipboard import of the in-app checklist
- [ ] Investigate Arena Club endpoints for checklists and `estimatedValueCents` (manually, in a normal browser session)
- [ ] Saved packs, re-run when the pool changes, compare packs side by side
- [ ] Optional: Monte Carlo simulation of N rips to show the bankroll path

**Phase 4 (optional):** a simple UI (CLI first; then a PyQt5 or web front end)

---

## 8. Open questions

1. Does the T-Mobile 50% promo have a dollar cap? Is tax charged on the list price or the discounted price?
2. Which packs is the promo valid on? All Slab Packs, or only some?
3. Is AC's estimated value visible anywhere before ripping (checklist item detail, marketplace listing for the same cert)?
4. How large is the typical AC-value / market-value ratio by tier? Log your own rips to measure it.
5. Is a PriceCharting API subscription worth it, versus manual comps, for occasional use?

---

## 9. Sources

- Arena Club buyback, Slab Safe, fees: rotowire.com/sports-cards/arena-club; arenaclubsupport.zendesk.com (Slab Safe article 33940647225627; FanDuel article 56323183100571)
- Arena Cash and KYC: thelines.com/card-rips/arena-club/
- Gold Pokémon checklist snapshot: mycardrips.com/catalog/pokemon/arena-club/gold/
- shouldirip.com methodology: shouldirip.com/about
- Repo reviews: local clones read by subagents on 2026-11-24 (no code executed)
