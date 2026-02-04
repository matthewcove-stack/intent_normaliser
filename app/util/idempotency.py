from __future__ import annotations

from typing import Any

from app.util.canonical import canonical_json
from app.util.hashing import sha256_hex


def compute_idempotency_key(payload: Any) -> str:
    canonical = canonical_json(payload)
    return sha256_hex(canonical)
