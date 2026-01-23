# AGENTS.md

Codex guidance for this repo.

## What this repo is

FastAPI service for intent ingestion, normalization, clarification handling, and artifact persistence.

## Quick commands

- Run: `docker compose up --build`
- Tests: `docker compose run --rm api pytest`

## Required env vars

- `DATABASE_URL`
- `INTENT_SERVICE_TOKEN`

## Safe to edit

- `app/`
- `scripts/`
- `README.md`

## Avoid or be careful

- `migrations/` (if present; prefer generated tooling)
- `docker-compose.yml` unless needed for behavior changes

## Related repos

- `..\notion_gateway` for Notion execution endpoints
- `..\context_api` for search

## Contracts

- JSON schemas live in `..\notion_assistant_contracts\schemas\v1\`.
- Examples live in `..\notion_assistant_contracts\examples\`.
