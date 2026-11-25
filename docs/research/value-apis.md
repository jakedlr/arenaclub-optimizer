# Value provider APIs: PokéPrice Tracker and CardVolume (research, 2026-11-24)

Prep for Sprint 3 (value providers, ADR 0002). Everything below comes from the providers' own public docs, API specs and terms, fetched on 2026-11-24 with plain HTTP and an honest user-agent. No account, no API key, no authenticated calls. Lines marked **UNVERIFIED** are inferences that need a check with a real key or response.

Sources (cited by tag):

- [PPT-OAS] https://www.pokemonpricetracker.com/api/openapi (OpenAPI 3.1 spec, v2.0.0, behind the Scalar viewer at /docs)
- [PPT-REF] https://www.pokemonpricetracker.com/api-reference
- [PPT-PRICE] https://www.pokemonpricetracker.com/pricing
- [PPT-TOS] https://www.pokemonpricetracker.com/terms (last updated 2026-10-13)
- [PPT-LIC] https://www.pokemonpricetracker.com/licensing (last updated 2026-10-13)
- [PPT-COV] https://www.pokemonpricetracker.com/data-coverage
- [CV-DEV] https://cardvolume.com/developers/
- [CV-OAS] https://cardvolume.com/api/v1/openapi.json
- [CV-METH] https://cardvolume.com/methodology/
- [CV-TOS] https://cardvolume.com/terms/ (last updated 2026-09-26)
- [CV-API] live responses from https://cardvolume.com/api/v1/... (fetched 2026-11-24)

## 1. PokéPrice Tracker (PPT)

### Basics
- **Base URL:** `https://www.pokemonpricetracker.com/api/v2` [PPT-OAS `servers`]
- **Auth:** `Authorization: Bearer YOUR_API_KEY` on every request [PPT-OAS info, `bearerAuth`; PPT-REF examples]
- **Format:** JSON. The envelope is `{ "data": <Card | Card[]>, "metadata": {...} }`. `data` is a single object when you query by `tcgPlayerId`, otherwise an array [PPT-OAS `CardsResponse`]

### Endpoints we care about [PPT-OAS paths]
| Endpoint | Use | Cost |
|---|---|---|
| `GET /sets?search=&language=&series=&limit≤500` | Map a set name to ids | 1 credit per request, however many sets come back |
| `GET /cards?set=<name>&search=<words>&limit=N&includeEbay=true` | Find a card by set, name and number | `limit × (1 + includeHistory + includeEbay + …)` |
| `GET /cards?tcgPlayerId=<id>&includeEbay=true` | Look up one known card | 2 credits (1 basic + 1 eBay) |
| `GET /cards/{id}?includeEbay=true` | Same, by tcgPlayerId or 24-hex `id`. Returns **404** on a miss, where `/cards` returns 200 with empty `data` | same as `/cards` |
| `POST /parse-title` `{"title": "...", "options": {...}}` | Fuzzy-match an eBay-style title such as "2021 Pokemon Evolving Skies Umbreon VMAX #215 PSA 10" | 2 credits base, +1 for fuzzy, +1 for a title over 100 chars, +1 for maxSuggestions > 5 |
| `GET /population`, `GET /export` | Not usable: Business plan only, returns 403 | n/a |

`/cards` filter parameters [PPT-OAS]:
- `set`: set name, partial match.
- `setId`: any id from `/sets`, meaning the numeric TCGplayer GroupId (`tcgPlayerNumericId`), the slug (`tcgPlayerId`, e.g. `sv-prismatic-evolutions`) or the Mongo `id`.
- `search`: multi-word search across name, setName, cardNumber, rarity and cardType. It supports the `X/Y` number format and ignores grade words like "PSA 10".
- Others: `language=english|japanese` (default english), `printing`, `sortBy`, `fetchAllInSet`.

**Sets endpoint exists.** Set fields are `id`, `tcgPlayerId` (slug), `tcgPlayerNumericId`, `name`, `series`, `releaseDate` and `cardCount`. The doc example is `{"tcgPlayerId":"sv01-scarlet-and-violet-base-set","tcgPlayerNumericId":23228,"name":"Scarlet & Violet","series":"Scarlet & Violet","cardCount":258}` [PPT-OAS `/sets` example]. One `GET /sets?limit=500` call per language (2 credits total) should return the whole set list, which we can cache. **UNVERIFIED:** whether set `name` follows TCGplayer's group names (e.g. "SWSH07: Evolving Skies", "Crown Zenith: Galarian Gallery"). The docs only show query examples like `set=Black Bolt`, `set=celebrations` and `set=sv08-surging-sparks` [PPT-REF].

### Credits and rate limits [PPT-OAS info; PPT-REF; PPT-PRICE]
- Free plan: **100 credits/day and 60 calls/min**. The daily allowance resets at 00:00 UTC.
- Costs per card: basic 1 credit, `includeEbay` (graded) +1, `includeHistory` +1.
- **Billing uses the requested `limit` (default 50), not the number of results.** A bare `/cards?search=…` costs 50 credits, or 100 with eBay. Always pass `limit=1`, or a small limit.
- A graded lookup of one known card (`tcgPlayerId` + `includeEbay`) costs **2 credits**, so about 50 per day on Free. This matches ADR 0002.
- `fetchAllInSet=true` with `includeEbay` costs 2 × the set size, billed once. A 200-card set is about 400 credits, so it is out of reach on Free.
- Response caps: 200 cards (basic), 100 with history or eBay, 25 with both [PPT-OAS `/cards`]. The spec contradicts itself here: the `includeEbay` parameter text says "Limits response to 50 cards max".
- Per-response headers:
  - `X-API-Calls-Consumed`, `X-API-Calls-Breakdown` (e.g. `cards=50,history=50,ebay=50`)
  - `X-RateLimit-Daily-Remaining`, `X-RateLimit-Minute-Remaining`, `X-RateLimit-Cost`
  - The body also carries `metadata.apiCallsConsumed.{total,breakdown,costPerCard}` [PPT-REF; PPT-OAS]
- The docs advise bounded concurrency of 5–10 and stopping once 3–5 calls remain in the minute [PPT-OAS info].
- Plan facts disagree between pages: Free API keys are "1 key" on [PPT-REF] but "Up to 2" on [PPT-PRICE].

### Graded (PSA and other graders) data [PPT-OAS `EbayData`, `EbayGradeData`]
- Graded data only appears when `includeEbay=true`, under `card.ebay`.
- `ebay.salesByGrade` is keyed `{company}{grade}`: `psa10`, `psa9`, `cgc9_5`, `bgs10`. Only whole and half grades exist.
- **Supported graders: PSA, CGC, BGS, SGC, TAG and ACE.** Arena Club is not listed.
- Per-grade object fields:
  - Counts and prices: `count`, `totalValue`, `averagePrice`, `medianPrice`, `minPrice`, `maxPrice`
  - Rolling values: `marketPrice7Day`, `marketPriceMedian7Day`, `dailyVolume7Day`
  - Trend and dates: `marketTrend` (rising/falling/stable), `lastMarketUpdate`, `lastSaleDate` (may be null)
  - `smartMarketPrice {price, confidence: high|medium|low, method, daysUsed}`
  - `salesBySource {ebay, fanatics}`
  - `labels`: only on cgc10, bgs10, sgc10 and tag10, splitting by slab tier (pristine, blackLabel, …). **`bgs10` stats exclude Black Label.**
- Card-level fields: `ebay.smartPriceOutlierByGrade`, `salesVelocity`, `totalSales`, `gradesTracked`, `priceHistory[grade][date]`, `salesByPrinting`.
  - `smartPriceOutlierByGrade` is `{psa10:false, psa9:true}`. `true` means `smartMarketPrice` and `medianPrice` differ by more than 2×, usually because the pool mixes in a different card. The docs say to **prefer `medianPrice`** when it is true.
  - `salesByPrinting` holds sales of other printings ("Reverse Holofoil", or the other 1st Edition/Unlimited printing). These are **excluded** from the headline `salesByGrade`.
  - `soldListings` is Business only.
- Sales are filtered by the card's language, read from the listing title.
- Sources are eBay plus Fanatics Collect (since 2026-10) [PPT-OAS; PPT-COV].
- Doc example (`cardWithEbayData`, trimmed) [PPT-OAS `/cards` 200 examples]:
```json
{"data":{"tcgPlayerId":"233294","name":"Charizard","setName":"Base Set","prices":{"market":420},
 "ebay":{"salesByGrade":{"psa10":{"count":23,"averagePrice":15000,"medianPrice":14500,
   "marketPrice7Day":14800,"marketTrend":"stable",
   "smartMarketPrice":{"price":14650,"confidence":"medium","method":"rolling-median","daysUsed":30}}},
  "salesVelocity":{"dailyAverage":1.2,"weeklyAverage":8.5,"monthlyTotal":35},"totalSales":146}},
 "metadata":{"total":1,"count":1,"includes":{"priceHistory":false,"ebayData":true}}}
```
(This doc example leaves out `apiCallsConsumed`. The `includeBoth` example does include it: `{"total":3,"breakdown":{"cards":1,"history":1,"ebay":1},"costPerCard":3}`.)
- **Freshness:** PPT says prices refresh daily, with a "24hr update cycle" [PPT-COV; PPT-REF]. It gives no separate cadence for graded data. The earlier spike's note that "PSA prices update weekly" is **UNVERIFIED** and conflicts with these pages. Use `lastMarketUpdate` and `lastSaleDate` to judge staleness.
- The raw (ungraded) fallback for Arena Club grades is `card.prices.market`, the TCGplayer price. That price may not be Near Mint: check `variants[printing].conditionUsed` [PPT-REF export notes].

### Errors [PPT-OAS responses]
- Error body: `{"error": "...", "message": "...", "details": {...}}`.
- `400`: bad or missing parameters. `401`: invalid or missing key. `403`: Business-only endpoint. `404`: only from `/cards/{id}`. `500`: server error.
- **Out of credits returns `429`, not 402**, and so does a per-minute overrun. Every 429 has a `Retry-After` header (seconds). The body has `limitType: "daily" | "per_minute"`, `retryAfter`, `resetsAt` (ISO 8601) and `resolution`.
  - On `daily`, stop until 00:00 UTC and don't retry.
  - On `per_minute`, wait `Retry-After`.
  - **UNVERIFIED:** the exact 429 JSON. The spec documents the fields but gives no example body.

### Terms that matter [PPT-TOS §5–6; PPT-LIC]
- Free covers "personal collection tracking, valuation, and portfolio management" and free, non-revenue hobby tools. Any revenue, including ads or affiliate links, needs Business. The licensing table lists "internal business use … buying decisions" as Business, so this stays a personal tool.
- **Attribution is not required.** You may say factually that the product uses PPT data. You may not imply endorsement or partnership, or use PPT's logo.
- **Caching is allowed.** You may cache and store data in your own DB to serve your own app, and should keep it "reasonably fresh". You may not expose the stored copy to third parties. Data pulled while subscribed may be kept after cancelling.
- Never resell or redistribute the data.
- The ToS bans scraping the Service without written consent, so use the API, not the HTML pages.
- Don't exceed rate limits or circumvent limits. Abuse can lead to key revocation without notice.

## 2. CardVolume (CV)

### Basics [CV-DEV; CV-OAS]
- Free, read-only, **no key**. Base URL `https://cardvolume.com/api/v1`. `www.` returns a 301 redirect to the apex domain [CV-API headers].
- Endpoints:
  - `GET /cards.json`: index of every published card.
  - `GET /cards/{id}.json`: one card. Unknown ids return **404 as an HTML page**, not JSON [CV-API].
  - `GET /records.json`: the top sale per card, version and grade.
  - `GET /sets.json` and `/sets/{id}.json`: checklists. As of today these cover **Harry Potter sets only** (23 sets, no Pokémon) [CV-API].
  - CV also runs an MCP server at `https://cardvolume.com/mcp` (tools `search_cards`, `get_card`, …) and serves per-page Markdown at `…/index.md` [CV-DEV].
- **Single-card lookup:** the card `id` is the card's site path with `/` replaced by `--`, e.g. `pokemon--evolving-skies--umbreon-vmax-215-203` [CV-DEV]. The JSON API has no search endpoint. Fetch `cards.json` once (160 KB), match locally on `name`, `set` and `number`, then fetch `json_url`.
- Coverage on 2026-11-24: `cards.json` lists `count: 212` cards, 65 of them Pokémon [CV-API].

### Response shapes (real, trimmed) [CV-API]
`/cards.json` → `{"as_of":"2026-11-24","count":212,"methodology":"…","cards":[…]}`. Each entry looks like this:
```json
{"id":"pokemon/evolving-skies/umbreon-vmax-215-203","name":"Umbreon VMAX","category":"pokemon",
 "set":"Evolving Skies","number":"215/203","year":2021,"language":"English",
 "url":"https://cardvolume.com/pokemon/evolving-skies/umbreon-vmax-215-203/",
 "json_url":"https://cardvolume.com/api/v1/cards/pokemon--evolving-skies--umbreon-vmax-215-203.json",
 "verified_sales":69,"headline":{"label":"English Alternate Art Secret, 215/203 PSA 10","market_price_usd":3800,"sales_n":47},
 "record_sale_usd":…,"updated":"2026-11-22"}
```
`/cards/{id}.json` has top-level keys `id, url, name, title, set, number, year, language, rarity, versions[], grades[], verified_sales[], graded_market_value_usd, faq[], sources[], cite_as, as_of, updated`. One entry in `grades[]`:
```json
{"version":"English Alternate Art Secret, 215/203","version_key":"english-alternate-art-secret",
 "grader":"PSA","grade":"10","label":"PSA 10","market_price_usd":3800,"market_sales_n":47,
 "market_window":{"from":"2026-09-18","to":"2026-11-21","days":90},"verified_sales":47,
 "last_verified_sale":{"date":"2026-11-21","price_usd":3416.06,"venue":"eBay","buyer_premium_included":false,"source_url":"https://www.ebay.com/itm/206700087454"},
 "record_sale":{…},"population":{"count":21714,"as_of":"2026-09-26","source_url":"…"}}
```
- **Per-grade market price** is in `market_price_usd`, with the sales count in `market_sales_n` (90-day window) and lifetime qualifying sales in `verified_sales`.
- `market_price_usd` is **null** when there are fewer than 5 qualifying sales in 90 days. In that case the docs say to quote `last_verified_sale` with its date instead [CV-METH §3; CV-DEV].
- `grader` and `grade` are strings, and a grade can carry a tier: the Mega Charizard X ex 023 card has `"grader":"CGC","grade":"10 Pristine"` [CV-API].
- One card can have several `versions[]`, so match on `version_key` too. CV never mixes versions, languages, or raw and graded sales [CV-METH §1].
- Fixture hits: Umbreon VMAX 215 PSA 10 is $3,800 (n=47). Mega Charizard X ex 023 PSA 10 is $132.50 (n=23), listed as set "Mega Evolution Promo Cards", number "023/132", version "Ultra-Premium Collection foil promo".

### Attribution, rate limits, caching
- **Attribution is required:** "Please cite CardVolume with the card's URL whenever you use a figure." Quote the version, grade, figure and as-of date. Each card response includes a ready-made `cite_as` string, e.g. "Umbreon VMAX 215/203 (Evolving Skies). CardVolume, https://cardvolume.com/pokemon/evolving-skies/umbreon-vmax-215-203/ (accessed 2026-11-24)." [CV-DEV; CV-OAS; CV-API]. Store `url` and `as_of` with each value and show them in the UI.
- **Rate limits are not published. UNVERIFIED.** No rate-limit headers were seen [CV-API].
- **Caching rules are not published.** Responses carry `ETag` and `Last-Modified`, and a conditional GET of `cards.json` with `If-None-Match`/`If-Modified-Since` returns **304** (verified 2026-11-25 while capturing the #27 fixtures; the 200 sends a weak `W/"…"` ETag, the 304 a strong one). Data changes about daily (`as_of`, per-card `updated`) [CV-API].
- Terms: you may quote short excerpts with attribution. "Scrape the site at scale" is banned [CV-TOS]. The JSON API is the sanctioned route.
- `robots.txt` disallows `/api/cards/` but not `/api/v1/` (https://cardvolume.com/robots.txt).
- Suggested approach: refresh `cards.json` at most daily and fetch a card JSON only when it matches.

## 3. Matching Arena Club fields to provider queries

| Arena field | PPT | CardVolume |
|---|---|---|
| `insert` (fallback `subsetName`) | → `/sets` name → `setId`, or `set=` partial name | → `cards[].set` (free text) |
| `playerName` | `search=` words | `cards[].name` |
| `setNumber` | `search=` number, compared with `cardNumber` in results | prefix of `number` ("215" vs "215/203") |
| `year` | tie-breaker only | `cards[].year` |
| setName containing "JPN" | `language=japanese` (separate set DB) | `language: "Japanese"` |
| `gradingCompany` + `overall` | key `psa` + `10` → `salesByGrade.psa10`; 9.5 → `…9_5` | `grades[]` where `grader=="PSA"` and `grade=="10"` |
| `cardCertNumber` | not usable (no cert data) | not usable (`cert` is per sale and usually null) |

Recommended PPT flow:
1. Resolve each distinct card identity once with `limit≤5` and no eBay (≤5 credits).
2. Verify the result's `name` and `cardNumber`, then cache the `tcgPlayerId` permanently.
3. Price it with `/cards/{tcgPlayerId}?includeEbay=true` (2 credits).
4. Use `parse-title` (2–3 credits) only as a fallback when the structured search fails.

Pitfalls seen in `tests/fixtures/arena/snapshot/gold_pokemon_items.json`:
- **Generic `setName`:** it includes stray suffixes such as "2002 Pokemon " and "2019 Pokemon S&M " with trailing spaces. Use `insert`. `insert` is **null on 9 of 62** items, where `subsetName` is "Base" or "Promo":
  - When `subsetName` is "Base", the set must come from `setName` ("2000 Pokemon Neo Genesis", "2025 Pokemon Mega Evolution", "2020 Pokemon Futsal", "2001 Pokemon Neo 4 JPN").
- **Subset suffixes:** "Silver Tempest TG", "Brilliant Stars TG", "Lost Origin TG", "Crown Zenith GG" and "Celebrations CC" are separate subsets.
  - CV uses separate sets: `crown-zenith-galarian-gallery` with number "GG44/70", and `celebrations-classic-collection`.
  - PPT likely does too, since TCGplayer splits Trainer Gallery and Galarian Gallery into their own groups (**UNVERIFIED**). Strip the suffix and try both.
- **Promos:** `subsetName: "Promo"` with `parallelName` naming the product, e.g. "Hidden Fates Tins".
  - "SM211", "SM138" and "SWSH231" belong to the Black Star Promo sets. CV writes these as "SM210/248" and "SWSH144/307" [CV-API].
  - The Mega Evolution promo "023" has no prefix, so it needs the year and series to find the promo set.
  - "GG22" is Crown Zenith Galarian Gallery.
- **Number formats:** Arena uses leading zeros ("059", "023", "002"), letter prefixes ("TG29", "GG50", "SV6", "H30") and suffixes ("88a"). Providers add "/total" (CV "023/132", "215/203"). Compare normalised forms: uppercase, strip "/…", strip leading zeros.
- **Japanese:** 8 of 62 items have "JPN" in `setName`, e.g. Neo 4 JPN, Alter Genesis, Tag Team GX AS, Ultra Shiny GX, VMAX Climax, and SV JPN "Pokemon 151".
  - PPT needs `language=japanese`, and its Japanese set names likely differ ("Tag Team GX AS" means "Tag Team GX All Stars"; "Neo 4" is a numbering convention).
  - Japanese history starts in late October 2025 [PPT-COV]. Per-set Japanese coverage is **UNVERIFIED**.
  - Arena gives no language field beyond "JPN" in `setName`.
- **Name forms:** Arena uses "Greninja/Zoroark GX", "Mega Lopunny & Jigglypuff GX", "Gyarados EX", "M Gardevoir EX" and "Lt. Surge's Magneton". Normalise "/" to "&", treat "-GX"/" GX" and "-EX"/" EX" alike, and ignore case and punctuation. CV's own names are inconsistent too: "151", "Scarlet & Violet - 151" and "Scarlet & Violet-151" all appear [CV-API].
- **Printings:** Arena uses `holo: "reverse_holo"` (e.g. Legendary Collection Snorlax 64). In PPT, reverse-holo graded sales sit in `ebay.salesByPrinting["Reverse Holofoil"]`, not in `salesByGrade` [PPT-OAS].
  - WotC 1st Edition vs Unlimited has no Arena field we have seen, so Jungle, Fossil, Base Set and Neo cards are ambiguous. Default to the headline (primary printing) and mark it low-confidence.
- **Half grades:** PPT keys use underscores (`cgc9_5`). PSA does not issue 9.5. In the fixture, the only 9.5 (Darkrai GX 88a) is graded by `arena_club`. CV grade strings are free-form ("10 Pristine"), and the format of a CV 9.5 is **UNVERIFIED**.
- **Arena Club grades** (`arena_club`, cert `8AC…`, 6 of 62 in this fixture): neither provider prices them. PPT's grader list is PSA, CGC, BGS, SGC, TAG and ACE. Per ADR 0002, fall back to the raw value (PPT `prices.market`) and mark it low-confidence.
- **Thin or polluted pools:**
  - PPT: when `smartPriceOutlierByGrade[g]` is true, use `medianPrice`. Check `count` and `lastSaleDate` before trusting a value.
  - CV: `market_price_usd` null means "insufficient", not zero.
