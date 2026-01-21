from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict


class IntentPacket(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Literal["intent"]
    intent_type: Optional[str] = None
    intent_id: Optional[str] = None
    correlation_id: Optional[str] = None
    conversation_id: Optional[str] = None
    message_id: Optional[str] = None


class ActionPacket(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Literal["action"]
    action: Optional[str] = None
    intent_id: Optional[str] = None
    correlation_id: Optional[str] = None


class ClarificationCandidate(BaseModel):
    id: str
    label: str
    meta: Optional[Dict[str, Any]] = None


class Clarification(BaseModel):
    clarification_id: str
    intent_id: str
    question: str
    expected_answer_type: str
    candidates: List[ClarificationCandidate]
    status: str


class Plan(BaseModel):
    actions: List[ActionPacket]


class ClarificationAnswerRequest(BaseModel):
    choice_id: Optional[str] = None
    answer_text: Optional[str] = None


class IngestResponse(BaseModel):
    status: Literal["ready", "needs_clarification", "rejected", "accepted"]
    intent_id: str
    correlation_id: str
    plan: Optional[Plan] = None
    clarification: Optional[Clarification] = None
    error_code: Optional[str] = None
    message: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
