from __future__ import annotations

import ulid
import uuid


def new_intent_id() -> str:
    return f"int_{ulid.new().str}"


def new_correlation_id() -> str:
    return f"cor_{ulid.new().str}"


def new_trace_id() -> str:
    return str(uuid.uuid4())
