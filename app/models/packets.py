from __future__ import annotations

from typing import Any, Dict, Literal, Optional

from pydantic import BaseModel, ConfigDict


class IntentPacket(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Literal["intent"]
    intent_type: Optional[str] = None
    intent_id: Optional[str] = None
    correlation_id: Optional[str] = None


class ActionPacket(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Literal["action"]
    action: Optional[str] = None
    intent_id: Optional[str] = None
    correlation_id: Optional[str] = None


class IngestResponse(BaseModel):
    status: str
    error_code: Optional[str] = None
    message: Optional[str] = None
    intent_id: str
    correlation_id: str
    details: Optional[Dict[str, Any]] = None
