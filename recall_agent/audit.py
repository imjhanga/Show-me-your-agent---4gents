"""Append-only audit log.

Records what the agent decided and why, without recording what it said. Message
bodies are logged as a hash and a length, never as text: the blueprint requires
that logs carry references and decisions rather than patient content or private
reasoning (§13).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_LOG = Path("runs/audit.jsonl")

# Anything matching these names is replaced with a redaction marker, wherever it
# appears in an event payload.
REDACTED_FIELDS = frozenset(
    {
        "phone",
        "phone_raw",
        "phone_normalised",
        "email",
        "full_name",
        "patient_name",
        "message",
        "body",
        "text",
        "reply",
        "prompt",
        "draft",
    }
)
REDACTION = "[redacted]"


def content_digest(text: str) -> dict[str, Any]:
    """Describe a message without storing it."""

    return {
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
        "length": len(text),
    }


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: (REDACTION if key in REDACTED_FIELDS else _redact(inner))
            for key, inner in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


@dataclass(frozen=True)
class AuditEvent:
    recorded_at: str
    event_type: str
    payload: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "recorded_at": self.recorded_at,
            "event_type": self.event_type,
            **self.payload,
        }


class AuditLog:
    def __init__(self, path: Path = DEFAULT_LOG) -> None:
        self.path = path

    def append(self, event_type: str, **payload: Any) -> AuditEvent:
        event = AuditEvent(
            recorded_at=datetime.now(timezone.utc).isoformat(),
            event_type=event_type,
            payload=_redact(payload),
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
        return event

    def read(self, limit: int | None = None) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        events = [json.loads(line) for line in self._lines()]
        return events[-limit:] if limit else events

    def _lines(self) -> Iterator[str]:
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield line

    def clear(self) -> None:
        if self.path.exists():
            self.path.unlink()
