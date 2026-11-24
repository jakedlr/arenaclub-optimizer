# Read Arena Club's public API directly, with no browser and no login

We get Slab Packs, Checklists and Hit Rates from plain, unauthenticated HTTP GETs to `api.arenaclub.com`, the same endpoints the website's frontend calls. Requests are spaced out and send an honest user-agent. We originally chose to drive the user's logged-in browser with Playwright, but a spike (2026-11-24) showed that the API needs no session and that logging in exposes no extra data. It also showed that headless Chrome is blocked by Cloudflare on `arenaclub.com`, which would force a visible browser for every Watch Mode cycle. See `docs/research/arenaclub-api.md`.

## Considered Options

- **Manual CSV input.** This doesn't meet the goal of live data.
- **Playwright with a logged-in profile.** It's heavier and more fragile, and it would need a headed browser to get past Cloudflare. Nothing it would gain is worth that.

## Consequences

- The tool stores no credentials or cookies.
- The endpoints are unofficial and can change or start requiring auth without notice. If that happens, the tool fails loudly (and sends an Alert in Watch Mode) rather than falling back to bypass techniques.
