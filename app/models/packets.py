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
    natural_language: Optional[str] = None
    fields: Optional[Dict[str, Any]] = None
    confidence: Optional[float] = None
    source: Optional[str] = None
    timestamp: Optional[str] = None


class ActionPacket(BaseModel):
    model_config = ConfigDict(extra="allow")

    kind: Literal["action"]
    action: Optional[str] = None
    intent_id: Optional[str] = None
    correlation_id: Optional[str] = None
    idempotency_key: Optional[str] = None
    step_id: Optional[str] = None
    depends_on: Optional[List[str]] = None
    fields: Optional[Dict[str, Any]] = None
    payload: Optional[Dict[str, Any]] = None


class ClarificationCandidate(BaseModel):
    id: str
    label: str
    meta: Optional[Dict[str, Any]] = None


class Clarification(BaseModel):
    clarification_id: str
    intent_id: str
    question: str
    expected_answer_type: Literal["choice", "free_text", "date", "datetime"]
    candidates: List[ClarificationCandidate]
    status: Literal["open", "answered", "expired"]
    answer: Optional[Dict[str, Any]] = None
    answered_at: Optional[str] = None


class Plan(BaseModel):
    actions: List[ActionPacket]


class ClarificationAnswerRequest(BaseModel):
    choice_id: Optional[str] = None
    answer_text: Optional[str] = None


class IngestResponse(BaseModel):
    status: Literal["ready", "needs_clarification", "rejected", "accepted", "executed", "failed"]
    intent_id: str
    correlation_id: str
    plan: Optional[Plan] = None
    clarification: Optional[Clarification] = None
    error_code: Optional[str] = None
    message: Optional[str] = None
    details: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
