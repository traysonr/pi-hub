STATUS: REFERENCE
OWNER: trays
LAST UPDATED: 2026-04-19
SCOPE: Repo-local operating rules for agents working in the Pi Hub repository.
RELATED: README.md, docs/README.md, docs/INDEX.md
Security / Secrets Rule (ALWAYS)
NEVER paste, print, log, commit, or hardcode any secrets or credentials.
This includes (but is not limited to): API keys, tokens, passwords, private keys,
session cookies, OAuth tokens, Telegram bot tokens, chat IDs, webhook URLs with secrets.
All secrets MUST be loaded from environment variables or a local untracked secrets file
(e.g., .env) that is listed in .gitignore or OS credential store (Windows Credential Manager).
If code requires credentials, it MUST:
(1) validate required env vars at startup,
(2) fail fast with a clear, actionable error message,
(3) avoid outputting secret values (only mention the variable name).
When providing examples, ALWAYS use placeholder values like:
TELEGRAM_BOT_TOKEN="YOUR_TOKEN_HERE"
Never show realistic-looking keys (e.g., "sk-...", "xoxb-...").
Documentation Governance (ALWAYS)
Canonical structure
`/docs/README.md` is the repo-wide canonical docs portal. Do not replace it; update it.
`/docs/INDEX.md` is the repo-wide navigation map. Every repo-wide doc must be listed here.
Each major component must have its own `README.md` next to the code as its component portal.
Required taxonomy header
Every new markdown file MUST begin with this block (before any heading):
STATUS: CANONICAL | REFERENCE | HISTORICAL
OWNER: <team or person>
LAST UPDATED: YYYY-MM-DD
SCOPE: <one sentence>
RELATED: <comma-separated paths>
`CANONICAL` = authoritative, kept in sync with code.
`REFERENCE` = supporting material (checklists, templates); accurate but not the primary truth source.
`HISTORICAL` = frozen snapshot; do not edit.
When editing any existing doc, bump LAST UPDATED to today's date.
Placement and naming
Repo-wide policy docs: `/docs/policy/<name>.md`
Repo-wide templates: `/docs/templates/<name>.md`
Repo-wide history snapshots: `/docs/history/YYYY-MM-DD-<topic>.md`
Component-specific docs: `<component>/docs/<name>.md`
Component portal: `<component>/README.md`
Do NOT create markdown files in arbitrary directories outside these paths.
No orphan docs
Any new doc must be linked from the appropriate index in the same commit.
Repo-wide docs => link from `/docs/INDEX.md`.
Component docs => link from the component `README.md` or component docs index.
A doc not reachable from any index must not be committed.
Sync and rename hygiene
If behavior, commands, or setup steps change => update the nearest canonical README in the same commit.
If a file, directory, or path is renamed or moved => search all `.md` files for the old path and fix every link before committing.
Minimal churn
Prefer small, targeted edits. Do not restructure or reformat docs unless explicitly requested.
Add content; do not move content that does not need moving.
Ephemeral artifact rule
Do NOT commit planning notes, verification scratch pads, or agent reasoning transcripts unless the user explicitly requests it.
Summarize findings in the appropriate existing doc instead.
