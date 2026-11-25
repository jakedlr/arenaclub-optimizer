# Sprint log

## Sprint 0: setup (2026-11-24)
**Done:** planning docs (plan, glossary, ADRs 0001–0002, research); scaffold, CLI stub and CI (#1, PR #2); branch protection on `main`.

**Retro**
- Three spikes changed the plan before any code was written: the API is public, so no browser is needed; AC Value isn't available before a Rip; free graded-price sources are thin. Spiking early paid off.
- setup-uv publishes no floating major tags, so action versions are pinned exactly.
- `uv init` copies the git author's name and email into `pyproject.toml`. Strip it in a public repo.
- Carry-over (non-blocking review nits from PR #2): make the `--version` test assert exact output, and assert on `result.stderr` for the stubs. Fold these into the first Sprint 1 story that touches the CLI.

## Sprint 1: offline calculator (2026-11-24)
**Done:** pack model and Hit Rate normalisation (#4, PR #9); Cost model (#5, PR #8); EV engine (#6, PR #10); risk metrics and the High-variance Note (#7, PR #11); a refactor that unifies these through a shared Pull helper (#12, PR #13). 441 tests.

**Retro**
- **Worked:** test cases computed by hand, with the arithmetic in comments, plus reviewers who recompute the expected values with exact `Fraction` arithmetic and run mutation probes. The reviews caught real issues: a Top-heaviness tie-break bug, a divide-by-zero when Cost rounded to 0, and NaN Market Values getting through.
- **Worked:** building #6 and #7 in parallel, each confined to its own module, gave no merge conflicts. The cost was drift between the two modules (exception types, validation, constants), which needed a refactor story (#12). Next time, put shared building blocks in the first story.
- **Float tolerance:** boundaries such as 0.5%/5% drift, 95% Value Coverage, P(profit) and the median all needed a single `FLOAT_TOLERANCE` (1e-9). Every boundary test checks that the tolerance is actually needed.
- **Process:** non-blocking review findings that are cheap to fix are fixed on the same PR before merge, so they don't pile up as debt. `main` moves fast, so PR branches need `update-branch` (strict protection) before they can merge.
- The Sprint 0 carry-over (stricter CLI tests from the PR #2 review) was done in #4.
- **Carry-over to Sprint 2:** map `isInsuranceAvailable` explicitly; pass each pack's `offerPercentage`/`offerInsuredPercentage` through; skip and log packs whose Hit Rates are rejected. All of these are in #15 and #17.

## Sprint 2: live ranking (2026-11-24)
**Done:**
- Arena Club API client (#14, PR #19)
- Mapping from JSON to the domain model (#15, PR #21)
- Config and Manual Overrides files (#16, PR #20)
- Ranking and the `arena rank` command (#17, PR #22)
- Follow-ups: an 80-column layout, a price pre-filter and help defaults (#23, PR #24)

823 tests. One live run of `arena rank --category pokemon` found 8 Pokémon Slab Packs and made 477 card requests with no blocks. Result: 0 ranked and 8 unrankable, as expected with no Market Values yet.

**Retro**
- **Fixtures went stale.** The pack and card fixtures were captured at different times, and Arena Club had replaced 39 of the 62 cards in between. The first fix was a test helper that faked the alignment. We replaced it with one consistent snapshot captured together (`tests/fixtures/arena/snapshot/`). Capture related fixtures in a single pass.
- **Public repo hygiene.** Sanitising fixtures has to cover every user-linked field, not only `userId`: `collectionId` got through once. That fixture landed in early commits on a PR branch, so we squashed the branch before merging. Old SHAs may stay reachable on GitHub until garbage collection.
- **Spec slips cost review cycles.** #16 said "reject negative k" where it meant k > 0, and a review caught it as blocking. Cross-check new validation rules against the existing domain validation (`_pulls`) when writing an issue.
- **Reviews kept finding real bugs.** Examples: rich markup injection from pack names, a huge integer raising `OverflowError`, an ntfy URL with no host, and `Retry-After` accepting `nan`. Each was cheap to fix on the same PR.
- **Interruptions:** an account usage limit stopped two agents mid-task. Both resumed from their saved state after the reset with no lost work. GitHub had brief 500s during an incident, and pushes succeeded on retry.
- **Carry-over to Sprint 4:** Watch Mode must inspect `FetchResult.skipped`. Losing the network mid-run currently exits 0.
- **Blocked for Sprint 3:** the user needs a PokéPrice Tracker API key (free personal tier) for live value checks.

## Sprint 3: values (2026-11-24 to 2026-11-25)
**Done:**
- Value pipeline core: identity, quotes, cache, budget and resolver (#26, PR #30)
- CardVolume provider (#27, PR #31)
- PokéPrice Tracker provider (#28, PR #32)
- Values wired into `rank`, plus `arena review` (#29, PR #33)
- Spending the budget pack-at-a-time, cheapest pack first (#34, PR #35). The user chose this approach.

1332 tests. Live run with no PokéPrice key: CardVolume valued 2 of the 183 in-scope cards and 181 need review, so no packs are rankable yet. Expected time to the first ranked Gold Pokémon pack with a free PokéPrice key: about 5 days.

**Retro**
- **Building the foundation first paid off.** The value core (#26) went in before the providers. #27 and #28 were then built in parallel against a single contract, with no drift between them, which avoids the Sprint 1 problem.
- **Wrong-match bugs are the dangerous kind.** Two blocking review findings came from fuzzy matching that returned a *wrong* card at high confidence: a CardVolume Promo wildcard matching the wrong year, and PokéPrice matching on number alone (Charizard #4 → Palkia #4). We now fail safe: an ambiguous or partial match returns Unmatched or LOW confidence. Reviewers reproduced both bugs offline with mock data before the fixes.
- **Undisclosed scope needs review too.** The stale-cache fallback in #26 was never asked for, and it introduced two blocking bugs: a retracted quote could be served, and stale quotes looked fresh. Reviewers should treat any undisclosed scope as high-risk.
- **Key hygiene.** Reviewers actively tried to leak the API key. They found it exposed through captured traceback locals and through `__context__` request headers. Moving it into an `httpx.Auth` class fixed both.
- **Budget economics drove the design.** The free PokéPrice tier (100 credits a day, 7–8 per first lookup) combined with the 95% Value Coverage rule made "how to spend credits" a real product decision. It went to the user, who chose pack-at-a-time.
- **Interruptions.** We hit an account usage limit overnight. Agents resumed cleanly after the reset.
- **Still blocked:** the user needs a PokéPrice Tracker key to get real rankings. Until then, Manual Overrides via `arena review` are the only route.
- **Carry-over to Sprint 4:** check `FetchResult.skipped` (degraded runs), rebuild providers every cycle, and honour Retry-After. These are in #38.
