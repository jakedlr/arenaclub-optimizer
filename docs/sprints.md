# Sprint log

## Sprint 0: setup (2026-11-24)
**Done:** planning docs (plan, glossary, ADRs 0001–0002, research); scaffold, CLI stub and CI (#1, PR #2); branch protection on `main`.

**Retro**
- Three spikes changed the plan before any code was written: the API is public, so no browser is needed; AC Value isn't available before a Rip; free graded-price sources are thin. Spiking early paid off.
- setup-uv publishes no floating major tags, so action versions are pinned exactly.
- `uv init` copies the git author's name and email into `pyproject.toml`. Strip it in a public repo.
- Carry-over (non-blocking review nits from PR #2): make the `--version` test assert exact output, and assert on `result.stderr` for the stubs. Fold these into the first Sprint 1 story that touches the CLI.
