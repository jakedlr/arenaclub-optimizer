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
- **Carry-over to Sprint 2:** map `isInsuranceAvailable` explicitly; pass each pack's `offerPercentage`/`offerInsuredPercentage` through; skip and log packs whose Hit Rates are rejected. All of these are in #15 and #17.
