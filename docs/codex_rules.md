# Codex Rules — intent_normaliser

- Prefer small, surgical changes; do not refactor endpoints unless required for Phase scope.
- Do not change wire contracts without updating notion_assistant_contracts and bumping contract version.
- Keep `INTENT.md` authoritative for requirements; keep `docs/current_state.md` authoritative for “what works today”.
- Add tests for any new execution behaviour and keep them Docker-runnable.
