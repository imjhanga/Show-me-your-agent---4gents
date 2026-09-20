"""Tests for the risk rule table."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from recall_agent import risk
from recall_agent.risk import RiskPolicyError, RuleTable

MINIMAL = """
schema_version = "1.0"
default_outcome = "STAFF_APPROVAL"
default_reason = "Nothing authorised this."

[[rule]]
id = "deceased"
outcome = "EXCLUDE"
reason = "Patient is deceased."
[rule.when]
patient_status = "deceased"

[[rule]]
id = "allow"
outcome = "AUTO_SEND"
reason = "Routine."
[rule.when]
may_send_reminder = true
days_overdue__lte = 730
"""


def write_table(body: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".toml", delete=False, encoding="utf-8"
    )
    handle.write(body)
    handle.close()
    return Path(handle.name)


def facts(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "message_purpose": risk.CARE_CONTINUITY,
        "patient_status": "active",
        "whatsapp_consent": True,
        "marketing_consent": False,
        "opted_out": False,
        "opted_out_in_log": False,
        "is_flagged_duplicate": False,
        "has_valid_phone": True,
        "requirement_kind": "RECALL",
        "recall_type": "routine_hygiene",
        "recall_status": "due",
        "treatment_type": None,
        "treatment_status": None,
        "clinical_urgency": 0,
        "base_urgency": 1,
        "days_overdue": 40,
        "notes": "",
        "is_overdue": True,
        "may_send_reminder": True,
        "contact_attempts": 0,
        "no_show_count": 0,
    }
    base.update(overrides)
    return base


class RuleTableLoadingTests(unittest.TestCase):
    """The table fails closed rather than skipping what it cannot parse."""

    def _expect_error(self, body: str, fragment: str) -> None:
        path = write_table(body)
        try:
            with self.assertRaises(RiskPolicyError) as caught:
                RuleTable.load(path)
            self.assertIn(fragment, str(caught.exception))
        finally:
            path.unlink()

    def test_rejects_unknown_outcome(self) -> None:
        self._expect_error(
            '[[rule]]\nid = "x"\noutcome = "MAYBE"\nreason = "r"\n[rule.when]\nopted_out = true\n',
            "unknown outcome",
        )

    def test_rejects_unknown_fact(self) -> None:
        # A typo'd fact name would otherwise be a condition that never matches,
        # silently disabling a safety rule.
        self._expect_error(
            '[[rule]]\nid = "x"\noutcome = "EXCLUDE"\nreason = "r"\n[rule.when]\npatient_statuss = "deceased"\n',
            "unknown fact",
        )

    def test_rejects_unknown_operator(self) -> None:
        self._expect_error(
            '[[rule]]\nid = "x"\noutcome = "EXCLUDE"\nreason = "r"\n[rule.when]\ndays_overdue__roughly = 5\n',
            "unknown operator",
        )

    def test_rejects_duplicate_rule_id(self) -> None:
        self._expect_error(
            '[[rule]]\nid = "x"\noutcome = "EXCLUDE"\nreason = "r"\n[rule.when]\nopted_out = true\n'
            '[[rule]]\nid = "x"\noutcome = "EXCLUDE"\nreason = "r"\n[rule.when]\nopted_out = false\n',
            "duplicate rule id",
        )

    def test_rejects_rule_without_conditions(self) -> None:
        self._expect_error(
            '[[rule]]\nid = "x"\noutcome = "AUTO_SEND"\nreason = "r"\n[rule.when]\n',
            "at least one condition",
        )

    def test_rejects_rule_without_a_reason(self) -> None:
        self._expect_error(
            '[[rule]]\nid = "x"\noutcome = "EXCLUDE"\n[rule.when]\nopted_out = true\n',
            "human-readable reason",
        )

    def test_rejects_empty_table(self) -> None:
        self._expect_error('schema_version = "1.0"\n', "empty")

    def test_rejects_membership_operator_without_a_list(self) -> None:
        self._expect_error(
            '[[rule]]\nid = "x"\noutcome = "EXCLUDE"\nreason = "r"\n[rule.when]\nrecall_type__in = "routine_hygiene"\n',
            "needs a list",
        )


class MinimalTableTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.path = write_table(MINIMAL)
        cls.table = RuleTable.load(cls.path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.path.unlink()

    def test_first_match_wins(self) -> None:
        decision = self.table.decide(facts(patient_status="deceased"))
        self.assertEqual(decision.rule_id, "deceased")
        self.assertEqual(decision.outcome, risk.EXCLUDE)

    def test_falls_through_to_the_allowlist(self) -> None:
        self.assertEqual(self.table.decide(facts()).outcome, risk.AUTO_SEND)

    def test_unmatched_case_denies_by_default(self) -> None:
        decision = self.table.decide(facts(may_send_reminder=False))
        self.assertEqual(decision.rule_id, "default")
        self.assertEqual(decision.outcome, risk.STAFF_APPROVAL)

    def test_ordered_comparison_against_a_missing_fact_does_not_crash(self) -> None:
        decision = self.table.decide(facts(days_overdue=None))
        self.assertEqual(decision.rule_id, "default")


class ShippedRuleTableTests(unittest.TestCase):
    """Behaviour of the real config/risk_rules.toml."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.table = RuleTable.load()

    def test_table_loads(self) -> None:
        self.assertGreater(len(self.table.rules), 10)
        self.assertEqual(self.table.default_outcome, risk.STAFF_APPROVAL)

    def test_nothing_auto_sends_unless_a_rule_says_so(self) -> None:
        # Deny by default is the whole point: the only AUTO_SEND rules are the
        # explicit allowlist at the bottom of the table.
        auto = [rule for rule in self.table.rules if rule.outcome == risk.AUTO_SEND]
        self.assertTrue(auto)
        for rule in auto:
            with self.subTest(rule=rule.id):
                self.assertIn("may_send_reminder", [c[0] for c in rule.conditions])

    def test_deceased_patient_is_excluded_for_being_deceased(self) -> None:
        # The reason the operator sees must be the true one. If patient status
        # were folded into consent this would read "no consent" instead.
        decision = self.table.decide(facts(patient_status="deceased"))
        self.assertEqual(decision.outcome, risk.EXCLUDE)
        self.assertEqual(decision.rule_id, "patient_deceased")
        self.assertIn("deceased", decision.reason.lower())

    def test_deceased_beats_every_other_condition(self) -> None:
        decision = self.table.decide(
            facts(patient_status="deceased", whatsapp_consent=True, days_overdue=5)
        )
        self.assertEqual(decision.rule_id, "patient_deceased")

    def test_care_and_marketing_are_separate_permissions(self) -> None:
        consenting = facts(whatsapp_consent=True, marketing_consent=False)
        care = self.table.decide({**consenting, "message_purpose": "care_continuity"})
        marketing = self.table.decide({**consenting, "message_purpose": "marketing"})
        self.assertEqual(care.outcome, risk.AUTO_SEND)
        self.assertEqual(marketing.outcome, risk.EXCLUDE)
        self.assertEqual(marketing.rule_id, "marketing_without_marketing_consent")

    def test_marketing_is_permitted_only_with_marketing_consent(self) -> None:
        decision = self.table.decide(
            facts(message_purpose="marketing", marketing_consent=True)
        )
        # It still is not auto-sent; it simply stops being an outright refusal.
        self.assertNotEqual(decision.rule_id, "marketing_without_marketing_consent")

    def test_solicitation_is_always_refused(self) -> None:
        decision = self.table.decide(
            facts(message_purpose="solicitation", marketing_consent=True)
        )
        self.assertEqual(decision.outcome, risk.EXCLUDE)
        self.assertEqual(decision.rule_id, "solicitation_is_never_permitted")

    def test_exclusions_that_must_never_auto_send(self) -> None:
        cases = {
            "patient_deceased": facts(patient_status="deceased"),
            "opted_out_of_all_contact": facts(opted_out=True),
            "opted_out_during_a_previous_conversation": facts(opted_out_in_log=True),
            "no_messaging_consent": facts(whatsapp_consent=False),
            "duplicate_patient_record": facts(is_flagged_duplicate=True),
            "no_reachable_whatsapp_number": facts(has_valid_phone=False),
            "patient_relocated_overseas": facts(patient_status="overseas"),
        }
        for rule_id, fact_set in cases.items():
            with self.subTest(rule=rule_id):
                decision = self.table.decide(fact_set)
                self.assertEqual(decision.outcome, risk.EXCLUDE)
                self.assertEqual(decision.rule_id, rule_id)

    def test_cases_that_require_a_human(self) -> None:
        cases = {
            "patient_marked_inactive": facts(patient_status="inactive"),
            "abandoned_treatment": facts(
                requirement_kind="TREATMENT",
                recall_type=None,
                treatment_status="abandoned",
            ),
            "post_treatment_review": facts(recall_type="post_treatment_review"),
            "high_clinical_urgency": facts(
                requirement_kind="TREATMENT", recall_type=None, clinical_urgency=5
            ),
            "overdue_more_than_two_years": facts(days_overdue=900),
            "record_carries_a_contact_constraint": facts(notes="Contact via spouse"),
        }
        for rule_id, fact_set in cases.items():
            with self.subTest(rule=rule_id):
                decision = self.table.decide(fact_set)
                self.assertEqual(decision.outcome, risk.STAFF_APPROVAL)
                self.assertEqual(decision.rule_id, rule_id)

    def test_treatment_follow_ups_never_auto_send(self) -> None:
        # The allowlist covers routine recalls only, so a healthy in-progress
        # treatment still reaches a human via deny-by-default.
        decision = self.table.decide(
            facts(
                requirement_kind="TREATMENT",
                recall_type=None,
                treatment_status="in_progress",
                clinical_urgency=3,
            )
        )
        self.assertEqual(decision.outcome, risk.STAFF_APPROVAL)
        self.assertEqual(decision.rule_id, "default")

    def test_blocked_eligibility_is_not_sent(self) -> None:
        decision = self.table.decide(facts(may_send_reminder=False))
        self.assertEqual(decision.outcome, risk.EXCLUDE)
        self.assertEqual(decision.rule_id, "eligibility_engine_blocked_contact")

    def test_every_rule_carries_a_reason_a_human_can_read(self) -> None:
        for rule in self.table.rules:
            with self.subTest(rule=rule.id):
                self.assertGreater(len(rule.reason.split()), 4)
                self.assertTrue(rule.citation)


class FactExtractionTests(unittest.TestCase):
    def test_facts_are_read_from_the_bridged_case(self) -> None:
        case = {
            "task": {"contact_attempts": 2},
            "clinic_context": {
                "patient_status": "overseas",
                "whatsapp_consent": True,
                "marketing_consent": False,
                "opted_out": False,
                "is_flagged_duplicate": False,
                "phone_normalised": "+6591234567",
                "requirement_kind": "RECALL",
                "recall_type": "routine_hygiene",
                "recall_status": "due",
                "base_urgency": 1,
                "days_overdue": 12,
                "notes": "",
                "appointment_history": {"no_show": 3},
                "contact_history": {"opted_out_in_log": True},
            },
        }
        result = {"is_overdue": True, "may_send_reminder": True}
        extracted = risk.facts_for(case, result)

        self.assertEqual(extracted["patient_status"], "overseas")
        self.assertTrue(extracted["has_valid_phone"])
        self.assertTrue(extracted["opted_out_in_log"])
        self.assertEqual(extracted["no_show_count"], 3)
        self.assertEqual(extracted["contact_attempts"], 2)
        self.assertEqual(extracted["message_purpose"], risk.CARE_CONTINUITY)

    def test_every_extracted_fact_is_declared(self) -> None:
        # Guards against facts_for growing a key the rule table cannot test.
        case = {
            "task": {"contact_attempts": 0},
            "clinic_context": {
                "patient_status": "active",
                "whatsapp_consent": True,
                "marketing_consent": True,
                "opted_out": False,
                "is_flagged_duplicate": False,
                "phone_normalised": None,
                "requirement_kind": "RECALL",
                "days_overdue": 0,
                "notes": "",
            },
        }
        extracted = risk.facts_for(case, {"is_overdue": False, "may_send_reminder": False})
        self.assertEqual(set(extracted) - risk.KNOWN_FACTS, set())


if __name__ == "__main__":
    unittest.main()
