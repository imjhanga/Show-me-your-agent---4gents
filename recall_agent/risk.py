"""Deterministic risk policy: EXCLUDE, STAFF_APPROVAL or AUTO_SEND.

The clinic owns these rules, not the model. ``config/risk_rules.toml`` is read
top to bottom, first match wins, and nothing is sent automatically unless a rule
explicitly authorises it.

This gate exists because the eligibility engine's schema has no field for
patient status, opt-out, duplicate records or the care/marketing boundary. Those
facts decide whether we may contact someone at all, so they need somewhere
honest to live. The two verdicts travel together: a case is only sent when the
engine permits it *and* this table authorises it.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

DEFAULT_RULES = Path("config/risk_rules.toml")

EXCLUDE = "EXCLUDE"
STAFF_APPROVAL = "STAFF_APPROVAL"
AUTO_SEND = "AUTO_SEND"
OUTCOMES = frozenset({EXCLUDE, STAFF_APPROVAL, AUTO_SEND})

OPERATORS = frozenset({"gt", "gte", "lt", "lte", "in", "not_in", "contains"})

# Declared so that a typo in the rule table is a load-time error instead of a
# condition that silently never matches.
KNOWN_FACTS = frozenset(
    {
        "message_purpose",
        "patient_status",
        "whatsapp_consent",
        "marketing_consent",
        "opted_out",
        "opted_out_in_log",
        "is_flagged_duplicate",
        "has_valid_phone",
        "requirement_kind",
        "recall_type",
        "recall_status",
        "treatment_type",
        "treatment_status",
        "clinical_urgency",
        "base_urgency",
        "days_overdue",
        "notes",
        "is_overdue",
        "may_send_reminder",
        "contact_attempts",
        "no_show_count",
    }
)

CARE_CONTINUITY = "care_continuity"


class RiskPolicyError(ValueError):
    """Raised when the rule table cannot be loaded safely."""


@dataclass(frozen=True)
class RiskDecision:
    outcome: str
    rule_id: str
    reason: str
    citation: str

    @property
    def is_excluded(self) -> bool:
        return self.outcome == EXCLUDE

    @property
    def needs_approval(self) -> bool:
        return self.outcome == STAFF_APPROVAL

    def to_dict(self) -> dict[str, str]:
        return {
            "outcome": self.outcome,
            "rule_id": self.rule_id,
            "reason": self.reason,
            "citation": self.citation,
        }


@dataclass(frozen=True)
class Rule:
    id: str
    outcome: str
    reason: str
    citation: str
    conditions: tuple[tuple[str, str, Any], ...]

    def matches(self, facts: Mapping[str, Any]) -> bool:
        return all(
            _compare(facts.get(fact), operator, expected)
            for fact, operator, expected in self.conditions
        )


def _compare(actual: Any, operator: str, expected: Any) -> bool:
    if operator == "eq":
        return actual == expected
    if operator == "in":
        return actual in expected
    if operator == "not_in":
        return actual not in expected
    if operator == "contains":
        return isinstance(actual, str) and expected in actual
    # Ordered comparisons against a missing fact are False rather than a crash.
    if actual is None or isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    if operator == "gt":
        return actual > expected
    if operator == "gte":
        return actual >= expected
    if operator == "lt":
        return actual < expected
    return actual <= expected


def _parse_condition(key: str, value: Any, rule_id: str) -> tuple[str, str, Any]:
    fact, _, suffix = key.partition("__")
    operator = suffix or "eq"
    if fact not in KNOWN_FACTS:
        raise RiskPolicyError(f"rule {rule_id!r} tests unknown fact {fact!r}")
    if operator != "eq" and operator not in OPERATORS:
        raise RiskPolicyError(f"rule {rule_id!r} uses unknown operator {suffix!r}")
    if operator in ("in", "not_in") and not isinstance(value, list):
        raise RiskPolicyError(f"rule {rule_id!r} condition {key!r} needs a list")
    return fact, operator, value


class RuleTable:
    """An ordered, first-match-wins policy table."""

    def __init__(
        self, rules: list[Rule], default_outcome: str, default_reason: str, version: str
    ) -> None:
        self.rules = rules
        self.default_outcome = default_outcome
        self.default_reason = default_reason
        self.version = version

    @classmethod
    def load(cls, path: Path = DEFAULT_RULES) -> RuleTable:
        try:
            with path.open("rb") as handle:
                document = tomllib.load(handle)
        except OSError as error:
            raise RiskPolicyError(f"cannot read risk rules: {error}") from error
        except tomllib.TOMLDecodeError as error:
            raise RiskPolicyError(f"risk rules are not valid TOML: {error}") from error

        default_outcome = document.get("default_outcome", STAFF_APPROVAL)
        if default_outcome not in OUTCOMES:
            raise RiskPolicyError(f"unknown default_outcome {default_outcome!r}")

        rules: list[Rule] = []
        seen: set[str] = set()
        for index, entry in enumerate(document.get("rule", [])):
            rule_id = entry.get("id")
            if not isinstance(rule_id, str) or not rule_id:
                raise RiskPolicyError(f"rule at position {index} has no id")
            if rule_id in seen:
                raise RiskPolicyError(f"duplicate rule id {rule_id!r}")
            seen.add(rule_id)

            outcome = entry.get("outcome")
            if outcome not in OUTCOMES:
                raise RiskPolicyError(f"rule {rule_id!r} has unknown outcome {outcome!r}")
            reason = entry.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                raise RiskPolicyError(f"rule {rule_id!r} needs a human-readable reason")

            when = entry.get("when")
            if not isinstance(when, dict) or not when:
                raise RiskPolicyError(f"rule {rule_id!r} needs at least one condition")
            conditions = tuple(
                _parse_condition(key, value, rule_id) for key, value in when.items()
            )
            rules.append(
                Rule(
                    id=rule_id,
                    outcome=outcome,
                    reason=reason,
                    citation=entry.get("citation", ""),
                    conditions=conditions,
                )
            )

        if not rules:
            raise RiskPolicyError("risk rule table is empty")
        return cls(
            rules,
            default_outcome,
            document.get(
                "default_reason", "No rule authorised automatic contact."
            ),
            str(document.get("schema_version", "unknown")),
        )

    def decide(self, facts: Mapping[str, Any]) -> RiskDecision:
        for rule in self.rules:
            if rule.matches(facts):
                return RiskDecision(rule.outcome, rule.id, rule.reason, rule.citation)
        return RiskDecision(
            self.default_outcome, "default", self.default_reason, "deny by default"
        )


def facts_for(
    case: Mapping[str, Any],
    result: Mapping[str, Any],
    message_purpose: str = CARE_CONTINUITY,
) -> dict[str, Any]:
    """Flatten one bridged case plus its eligibility result into rule facts.

    ``case`` is a bridge case dict, ``result`` is ``EligibilityResult.to_dict()``.
    """

    context = case["clinic_context"]
    appointments = context.get("appointment_history", {})
    return {
        "message_purpose": message_purpose,
        "patient_status": context["patient_status"],
        "whatsapp_consent": context["whatsapp_consent"],
        "marketing_consent": context["marketing_consent"],
        "opted_out": context["opted_out"],
        "opted_out_in_log": context.get("contact_history", {}).get(
            "opted_out_in_log", False
        ),
        "is_flagged_duplicate": context["is_flagged_duplicate"],
        "has_valid_phone": context["phone_normalised"] is not None,
        "requirement_kind": context["requirement_kind"],
        "recall_type": context.get("recall_type"),
        "recall_status": context.get("recall_status"),
        "treatment_type": context.get("treatment_type"),
        "treatment_status": context.get("treatment_status"),
        "clinical_urgency": context.get("clinical_urgency", 0),
        "base_urgency": context.get("base_urgency", 0),
        "days_overdue": context["days_overdue"],
        "notes": context["notes"],
        "is_overdue": result["is_overdue"],
        "may_send_reminder": result["may_send_reminder"],
        "contact_attempts": case["task"]["contact_attempts"],
        "no_show_count": appointments.get("no_show", 0),
    }
