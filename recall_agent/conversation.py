"""Runtime workflow state: one conversation per follow-up requirement.

The scan decides who to contact. This module tracks what then happens to them —
drafted, awaiting approval, sent, replied, deferred, escalated, closed — and
applies the reply understanding to move the case on.

Workflow state is kept strictly separate from clinical state. Nothing here
asserts that treatment is complete; closing a conversation closes an
administrative task and says nothing about the patient's care.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping

from .audit import AuditLog, content_digest
from .drafting import Draft, draft_message
from .llm import LLMClient
from .replies import (
    ESCALATE_TO_STAFF,
    RECORD_OPT_OUT,
    SCHEDULE_RETRY,
    ReplyUnderstanding,
    understand_reply,
)
from .risk import AUTO_SEND, STAFF_APPROVAL

DEFAULT_STATE = Path("runs/state.json")

# Workflow states, from technical-blueprint.md §6.
MONITORING = "MONITORING"
PENDING_APPROVAL = "PENDING_APPROVAL"
AWAITING_RESPONSE = "AWAITING_RESPONSE"
RETRY_SCHEDULED = "RETRY_SCHEDULED"
ESCALATED = "ESCALATED"
DECLINED = "DECLINED"
CLOSED = "CLOSED"
BLOCKED = "BLOCKED"


class KillSwitchEngaged(RuntimeError):
    """Raised when patient-facing actions are disabled."""


@dataclass
class Message:
    direction: str
    text: str
    at: str

    def to_dict(self) -> dict[str, Any]:
        return {"direction": self.direction, "text": self.text, "at": self.at}


@dataclass
class Conversation:
    case_id: str
    patient_id: str
    patient_name: str
    requirement_label: str
    phone: str | None
    state: str = MONITORING
    risk_outcome: str = STAFF_APPROVAL
    risk_reason: str = ""
    why_ranked: str = ""
    rank: int = 0
    messages: list[Message] = field(default_factory=list)
    draft: dict[str, Any] | None = None
    understanding: dict[str, Any] | None = None
    escalation_reason: str | None = None
    defer_until: str | None = None
    clarification_attempts: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "patient_id": self.patient_id,
            "patient_name": self.patient_name,
            "requirement_label": self.requirement_label,
            "phone": self.phone,
            "state": self.state,
            "risk_outcome": self.risk_outcome,
            "risk_reason": self.risk_reason,
            "why_ranked": self.why_ranked,
            "rank": self.rank,
            "messages": [m.to_dict() for m in self.messages],
            "draft": self.draft,
            "understanding": self.understanding,
            "escalation_reason": self.escalation_reason,
            "defer_until": self.defer_until,
            "clarification_attempts": self.clarification_attempts,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Conversation:
        conversation = cls(
            case_id=data["case_id"],
            patient_id=data["patient_id"],
            patient_name=data["patient_name"],
            requirement_label=data["requirement_label"],
            phone=data.get("phone"),
            state=data.get("state", MONITORING),
            risk_outcome=data.get("risk_outcome", STAFF_APPROVAL),
            risk_reason=data.get("risk_reason", ""),
            why_ranked=data.get("why_ranked", ""),
            rank=data.get("rank", 0),
            draft=data.get("draft"),
            understanding=data.get("understanding"),
            escalation_reason=data.get("escalation_reason"),
            defer_until=data.get("defer_until"),
            clarification_attempts=data.get("clarification_attempts", 0),
        )
        conversation.messages = [Message(**m) for m in data.get("messages", [])]
        return conversation


class ConversationStore:
    """Holds every live conversation and applies the agent's decisions."""

    def __init__(
        self,
        clinic_now: datetime,
        audit: AuditLog | None = None,
        path: Path = DEFAULT_STATE,
        client: LLMClient | None = None,
        use_llm: bool = True,
    ) -> None:
        self.clinic_now = clinic_now
        self.audit = audit or AuditLog()
        self.path = path
        self.client = client
        self.use_llm = use_llm
        self.conversations: dict[str, Conversation] = {}
        self.kill_switch = False

    # -- lifecycle ---------------------------------------------------------

    def adopt(self, entry: Mapping[str, Any], case: Mapping[str, Any]) -> Conversation:
        """Create a conversation for a ranked worklist entry, if absent."""

        case_id = entry["case_id"]
        if case_id in self.conversations:
            return self.conversations[case_id]
        conversation = Conversation(
            case_id=case_id,
            patient_id=entry["patient_id"],
            patient_name=entry["patient_name"],
            requirement_label=entry["requirement_label"],
            phone=case["clinic_context"].get("phone_normalised"),
            risk_outcome=entry["risk_outcome"],
            risk_reason=entry["risk_reason"],
            why_ranked=entry["why"],
            rank=entry["rank"],
        )
        self.conversations[case_id] = conversation
        return conversation

    def get(self, case_id: str) -> Conversation:
        if case_id not in self.conversations:
            raise KeyError(case_id)
        return self.conversations[case_id]

    # -- actions -----------------------------------------------------------

    def prepare(self, case_id: str, case: Mapping[str, Any]) -> Conversation:
        """Draft a message and route it: auto-send, or queue for approval."""

        conversation = self.get(case_id)
        draft: Draft = draft_message(case, client=self.client, use_llm=self.use_llm)
        conversation.draft = draft.to_dict()

        self.audit.append(
            "draft.created",
            case_id=case_id,
            patient_id=conversation.patient_id,
            source=draft.source,
            template_id=draft.template_id,
            check=draft.check.to_dict(),
            content=content_digest(draft.text),
        )

        if not draft.check.passed:
            conversation.state = ESCALATED
            conversation.escalation_reason = (
                "The drafted message failed the pre-send check: "
                + " ".join(draft.check.failures)
            )
            self.audit.append(
                "draft.rejected", case_id=case_id, failures=list(draft.check.failures)
            )
            return conversation

        if conversation.risk_outcome == AUTO_SEND:
            self.send(case_id)
        else:
            conversation.state = PENDING_APPROVAL
            self.audit.append(
                "approval.requested",
                case_id=case_id,
                patient_id=conversation.patient_id,
                rule_id=conversation.risk_outcome,
                reason=conversation.risk_reason,
            )
        return conversation

    def send(self, case_id: str) -> Conversation:
        """Send the approved draft. The only patient-facing action."""

        if self.kill_switch:
            raise KillSwitchEngaged(
                "Patient-facing actions are disabled. Read-only access is unaffected."
            )
        conversation = self.get(case_id)
        if not conversation.draft:
            raise ValueError(f"{case_id} has no draft to send")
        if not conversation.draft["check"]["passed"]:
            raise ValueError(f"{case_id} has a draft that failed the pre-send check")

        text = conversation.draft["text"]
        conversation.messages.append(
            Message("outbound", text, self.clinic_now.isoformat())
        )
        conversation.state = AWAITING_RESPONSE
        self.audit.append(
            "reminder.sent",
            case_id=case_id,
            patient_id=conversation.patient_id,
            channel="whatsapp",
            content=content_digest(text),
        )
        return conversation

    def approve(self, case_id: str) -> Conversation:
        conversation = self.get(case_id)
        if conversation.state != PENDING_APPROVAL:
            raise ValueError(f"{case_id} is not awaiting approval")
        self.audit.append("approval.granted", case_id=case_id, actor="staff")
        return self.send(case_id)

    def reject(self, case_id: str, reason: str = "Declined by staff") -> Conversation:
        conversation = self.get(case_id)
        if conversation.state != PENDING_APPROVAL:
            raise ValueError(f"{case_id} is not awaiting approval")
        conversation.state = BLOCKED
        conversation.escalation_reason = reason
        self.audit.append("approval.declined", case_id=case_id, actor="staff", reason=reason)
        return conversation

    def receive_reply(self, case_id: str, text: str) -> Conversation:
        """Take a patient reply, interpret it, and act on it."""

        conversation = self.get(case_id)
        conversation.messages.append(
            Message("inbound", text, self.clinic_now.isoformat())
        )
        self.audit.append(
            "reply.received",
            case_id=case_id,
            patient_id=conversation.patient_id,
            content=content_digest(text),
        )

        understanding: ReplyUnderstanding = understand_reply(
            text,
            self.clinic_now.date(),
            client=self.client,
            use_llm=self.use_llm,
            attempt=conversation.clarification_attempts + 1,
        )
        conversation.understanding = understanding.to_dict()
        self.audit.append(
            "reply.classified",
            case_id=case_id,
            intent=understanding.intent,
            confidence=round(understanding.confidence, 2),
            action=understanding.action,
            source=understanding.source,
        )
        self._apply(conversation, understanding)
        return conversation

    def _apply(
        self, conversation: Conversation, understanding: ReplyUnderstanding
    ) -> None:
        action = understanding.action

        if action == RECORD_OPT_OUT:
            conversation.state = DECLINED
            conversation.escalation_reason = None
            self.audit.append(
                "contact.opted_out",
                case_id=conversation.case_id,
                patient_id=conversation.patient_id,
            )
            return

        if action == SCHEDULE_RETRY and understanding.defer_until:
            conversation.state = RETRY_SCHEDULED
            conversation.defer_until = understanding.defer_until.isoformat()
            self.audit.append(
                "followup.deferred",
                case_id=conversation.case_id,
                patient_id=conversation.patient_id,
                retry_after=conversation.defer_until,
            )
            return

        if action == ESCALATE_TO_STAFF:
            conversation.state = ESCALATED
            conversation.escalation_reason = understanding.escalation_reason
            self.audit.append(
                "escalation.created",
                case_id=conversation.case_id,
                patient_id=conversation.patient_id,
                reason=understanding.escalation_reason,
                intent=understanding.intent,
            )
            return

        # ASK_AGAIN: bounded, and replies.py escalates once the cap is hit.
        conversation.clarification_attempts += 1
        conversation.state = AWAITING_RESPONSE

    # -- persistence -------------------------------------------------------

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "clinic_now": self.clinic_now.isoformat(),
            "kill_switch": self.kill_switch,
            "conversations": [c.to_dict() for c in self.conversations.values()],
        }
        with self.path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    def load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            payload = json.load(handle)
        self.kill_switch = payload.get("kill_switch", False)
        self.conversations = {
            item["case_id"]: Conversation.from_dict(item)
            for item in payload.get("conversations", [])
        }

    def reset(self) -> None:
        """Return to a known state so the demo runs identically twice."""

        self.conversations.clear()
        self.kill_switch = False
        if self.path.exists():
            self.path.unlink()
        self.audit.clear()

    # -- views -------------------------------------------------------------

    def by_state(self, *states: str) -> list[Conversation]:
        return [c for c in self.conversations.values() if c.state in states]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for conversation in self.conversations.values():
            counts[conversation.state] = counts.get(conversation.state, 0) + 1
        return counts
