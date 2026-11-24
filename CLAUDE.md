This project has the goal of creating a script much like shouldirip.com.
It will pull live data from arenaclub and return the top results fitting the specified criteria.
Criteria may include category (e.g. "Pokemon") or price range (e.g. $50-$150), and any other filtering that seems reasonable.
Top results are defined as the results with the highest EV.
Variance should also be returned in some form (measures of variance are not to my expertise, so I'll take suggestions from you on how to implement this).
Notes should also be included for packs with high variance (once again, I'll take suggestions from you on what we will consider "high variance")
Functionality should also be provided to enable an automatic mode that will run every 5 minutes (or longer period if this is too short).
This auto mode should send notifications through ntfy and should output the channel that the user needs to subscribe to (generated at random)

This project should be done in sprints, following agile/scrum principles. It should also apply principles of test driven development such as red-green-refactor loops while working toward the end goal.
Work should be delegated to subagents when applicable. Use opus 5.5 with the effort level set based on your discretion (with higher effort level used for problems with more edge cases).
You should act as the orchestrator for these subagents.
Work should be done using feature branches on github, with pull requests being submitted by each agent and reviewed by reviewer subagents.
I will not manually review any pull requests.

I've created a repo for this project at: https://github.com/jakedlr/arenaclub-optimizer.git

## Browser automation (Playwright)
Run Playwright headless by default. Use a visible (headed) browser only when the user must see or interact with the screen, e.g. logging in manually.
- MCP: use the project's `playwright-headless` server (`.mcp.json`, runs `@playwright/mcp --headless`) by default. The `playwright` plugin server is headed; use it only when the user needs to see the screen, and say so before opening it.
- Project code: any Playwright launch defaults to `headless=True`; only commands that need the user's eyes or hands (e.g. `login`) run headed.
- `.playwright-mcp/` is MCP scratch output and is gitignored.

## Where things are
- `docs/plan.md`: the agreed decisions and the sprint backlog. Read it before starting any work.
- `GLOSSARY.md`: domain vocabulary. Use these terms in code, issues and PRs.
- `docs/adr/`: architecture decisions. `docs/research/`: Arena Club API and price-source findings.

## Tooling notes
- `gh issue view`, `gh pr view` and `gh pr edit` error on a deprecated "Projects (classic)" field. Use `--json ...`, or `gh api` for edits.
- uv is at `~/.local/bin/uv`.
- `main` is protected: CI `test` must pass, admins included. Every change goes through a PR.
