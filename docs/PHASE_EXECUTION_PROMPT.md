# Phase Execution Prompt — intent_normaliser

You are implementing a single requested phase for intent_normaliser.

Follow:
- Repo `AGENTS.md` + this docs set
- Truth hierarchy: docs/current_state.md > INTENT.md > docs/phases.md > README.md > code

Phase 1 focus (only):
- Implement execution for the normalised plan (Notion task create/update) behind `EXECUTE_ACTIONS=true`.
- Add idempotency for duplicate request_ids.
- Persist execution artifacts and return an outcome payload.
- Add/extend tests and run: `docker compose run --rm api pytest`.

Stop and ask if you would change contracts, data integrity, or auth.
