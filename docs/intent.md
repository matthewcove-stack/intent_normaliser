# intent_normaliser — Intent

This service ingests intent packets, normalises them into deterministic action plans, and persists an append-only audit trail in Postgres.
It may optionally execute plans against the Notion OS kernel (notion_gateway) when explicitly enabled.

See the repository-level `INTENT.md` for the canonical spec; this doc exists to align the repo with the Brain OS “gold standard” documentation set.
