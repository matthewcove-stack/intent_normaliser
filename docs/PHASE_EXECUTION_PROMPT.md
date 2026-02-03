# Phase Execution Prompt (Canonical)

You are implementing the requested phase ONLY.

Rules:
- Do not implement future phases.
- Do not refactor unrelated code.
- Follow docs/intent.md.
- Update docs/current_state.md after changes.
- Use the smallest safe assumptions; document them.
- If verification fails twice, stop and report.

## Mandatory enforcement (Drift Guard MCP)
Before claiming completion, call these MCP tools and ensure ok=true:
- repo_contract_validate()
- verify_run(profile="default")
- drift_check()

Include the returned JSON in your final phase report.
