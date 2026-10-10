# Claude Code instructions

@AGENTS.md

Project-specific reminders on top of `AGENTS.md`:

- Run `./scripts/check.sh` before every commit; do not commit if it fails.
- Do not stage `.env`, `data/`, `planning/`, `resources/`, evaluation run
  outputs or anything containing credentials or raw query rows. Stage
  explicit paths rather than `git add -A`.
- Security, privacy, scope, budget and deletion rules live in application
  code. Never move one into a prompt or satisfy a test by weakening a guard.
- When a change affects behaviour described in `docs/`, update the document
  in the same commit and keep its status labels truthful.
- Live tests need `.env` credentials and are skipped otherwise; say so when
  reporting results instead of calling a skipped test a pass.
