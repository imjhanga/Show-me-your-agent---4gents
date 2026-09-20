"""Tests for the dental_dataset -> followup_agent bridge."""

from __future__ import annotations

import unittest
from datetime import date, datetime
from pathlib import Path

from followup_agent.domain import parse_cases
from followup_agent.eligibility import evaluate_case
from recall_agent import bridge
from recall_agent.clock import CLINIC_ZONE

DATASET_DB = Path("dental_dataset/clinic.db")

# dental_dataset/README.md documents this figure. It is the bridge's oracle: if
# the mapping drifts, this number moves.
DOCUMENTED_OVERDUE_RECALLS = 1188
DOCUMENTED_FLAGGED_DUPLICATES = 55
RECALL_ROWS = 3255
INCOMPLETE_TREATMENTS = 1774


class PhoneNormalisationTests(unittest.TestCase):
    def test_normalises_real_dataset_formats(self) -> None:
        cases = [
            ("+6588078673", "+6588078673"),
            ("+6592197935", "+6592197935"),
            ("96312692", "+6596312692"),
            ("91873301", "+6591873301"),
            ("6591873301", "+6591873301"),
            ("+65 9187 3301", "+6591873301"),
        ]
        for raw, expected in cases:
            with self.subTest(raw=raw):
                self.assertEqual(bridge.normalise_sg_mobile(raw), expected)

    def test_rejects_unusable_numbers(self) -> None:
        # An ambiguous number is an invalid one: we cannot tell which of the two
        # people would receive the message.
        cases = ["", "   ", "91234567 / 98765432", "1234", "+6512345678", "notaphone"]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertIsNone(bridge.normalise_sg_mobile(raw))


class BridgeDocumentTests(unittest.TestCase):
    """Whole-dataset tests. The document is built once and shared."""

    document: dict
    contexts: dict
    results: list

    @classmethod
    def setUpClass(cls) -> None:
        if not DATASET_DB.exists():  # pragma: no cover - dataset is committed
            raise unittest.SkipTest(f"{DATASET_DB} not found; run from repo root")
        cls.document = bridge.build_document(DATASET_DB)
        cls.contexts = {
            case["case_id"]: case["clinic_context"] for case in cls.document["cases"]
        }
        cls.results = [evaluate_case(case) for case in parse_cases(cls.document)]

    def _of_kind(self, kind: str) -> list:
        return [r for r in self.results if self.contexts[r.case_id]["requirement_kind"] == kind]

    def test_every_case_parses_under_strict_validation(self) -> None:
        # parse_cases raises InputValidationError on any field it dislikes, so
        # reaching setUpClass at all proves the shape. This pins the counts.
        self.assertEqual(len(self.results), RECALL_ROWS + INCOMPLETE_TREATMENTS)
        self.assertEqual(len(self._of_kind("RECALL")), RECALL_ROWS)
        self.assertEqual(len(self._of_kind("TREATMENT")), INCOMPLETE_TREATMENTS)

    def test_overdue_recall_count_matches_dataset_readme(self) -> None:
        overdue = sum(
            result.is_overdue
            for result in self._of_kind("RECALL")
            if self.contexts[result.case_id]["recall_status"] != "booked"
        )
        self.assertEqual(overdue, DOCUMENTED_OVERDUE_RECALLS)

    def test_case_ids_are_unique(self) -> None:
        ids = [case["case_id"] for case in self.document["cases"]]
        self.assertEqual(len(ids), len(set(ids)))

    def test_one_patient_can_carry_several_requirements(self) -> None:
        # The engine evaluates per requirement, not per patient. If every patient
        # had exactly one requirement that design would go unexercised.
        seen: dict[str, int] = {}
        for result in self.results:
            seen[result.patient_id] = seen.get(result.patient_id, 0) + 1
        self.assertGreater(max(seen.values()), 1)

    def test_booked_recalls_are_suppressed_by_a_covering_appointment(self) -> None:
        booked = [
            result
            for result in self._of_kind("RECALL")
            if self.contexts[result.case_id]["recall_status"] == "booked"
        ]
        self.assertTrue(booked)
        for result in booked:
            with self.subTest(case_id=result.case_id):
                self.assertIn("COVERING_APPOINTMENT_EXISTS", [c.value for c in result.reason_codes])
                self.assertFalse(result.may_send_reminder)

    def test_covering_appointments_are_labelled_as_reconstructed(self) -> None:
        # The dataset has no appointment dated after the reference date, so these
        # are bridge-generated and must never read as source records.
        for case in self.document["cases"]:
            if case["appointments"]:
                with self.subTest(case_id=case["case_id"]):
                    self.assertTrue(case["clinic_context"]["synthetic_appointment"])
                    for appointment in case["appointments"]:
                        self.assertTrue(appointment["appointment_id"].startswith("A-SYNTH-"))

    def test_all_flagged_duplicates_are_marked_stale(self) -> None:
        flagged = [c for c in self.contexts.values() if c["is_flagged_duplicate"]]
        self.assertEqual(len(flagged), DOCUMENTED_FLAGGED_DUPLICATES)
        stale = [
            result
            for result in self.results
            if self.contexts[result.case_id]["is_flagged_duplicate"]
        ]
        for result in stale:
            with self.subTest(case_id=result.case_id):
                self.assertIn("SOURCE_RECORD_STALE", [c.value for c in result.reason_codes])
                self.assertFalse(result.may_send_reminder)

    def test_five_duplicates_cannot_be_linked_and_are_still_flagged(self) -> None:
        flagged = [c for c in self.contexts.values() if c["is_flagged_duplicate"]]
        unlinked = [c for c in flagged if c["duplicate_of"] is None]
        # Both records lack a phone number, so (date_of_birth, phone) cannot
        # resolve them. They must not silently drop out of the exclusion path.
        self.assertEqual(len(unlinked), 5)
        for context in unlinked:
            with self.subTest(name=context["full_name"]):
                self.assertEqual(context["phone_raw"], "")

    def test_patient_status_is_not_collapsed_into_consent(self) -> None:
        # Deceased/overseas/inactive have no field in the engine's schema. Forcing
        # them into consent.granted would make the engine report
        # MESSAGING_CONSENT_NOT_CURRENT, which is false. recall_agent.risk owns
        # these exclusions instead, so consent here reflects consent alone.
        deceased = [
            case
            for case in self.document["cases"]
            if case["clinic_context"]["patient_status"] == "deceased"
        ]
        self.assertTrue(deceased)
        consenting = [
            case for case in deceased if case["patient"]["consent"]["granted"]
        ]
        self.assertTrue(
            consenting,
            "expected at least one deceased patient whose consent flag is untouched",
        )
        for case in consenting:
            with self.subTest(case_id=case["case_id"]):
                self.assertTrue(case["clinic_context"]["whatsapp_consent"])

    def test_invalid_phones_block_contact(self) -> None:
        invalid = [
            case
            for case in self.document["cases"]
            if not case["patient"]["contact"]["is_valid"]
        ]
        self.assertTrue(invalid)
        for case in invalid:
            with self.subTest(case_id=case["case_id"]):
                self.assertIsNone(case["clinic_context"]["phone_normalised"])

    def test_policy_defaults_target_the_demo_channel(self) -> None:
        policy = self.document["policy_defaults"]
        self.assertEqual(policy["channel"], bridge.CHANNEL)
        self.assertEqual(policy["clinic_timezone"], "Asia/Singapore")
        self.assertLess(
            policy["sending_window_start_hour"], policy["sending_window_end_hour"]
        )

    def test_evaluation_is_pinned_to_the_dataset_reference_date(self) -> None:
        for result in self.results[:50]:
            with self.subTest(case_id=result.case_id):
                self.assertEqual(result.clinic_date, date(2026, 9, 8))


class ClinicNowTests(unittest.TestCase):
    def test_moving_the_clock_forward_increases_overdue_cases(self) -> None:
        later = datetime(2027, 3, 1, 9, 0, tzinfo=CLINIC_ZONE)
        baseline = bridge.build_document(DATASET_DB)
        shifted = bridge.build_document(DATASET_DB, later)

        def overdue(document: dict) -> int:
            return sum(evaluate_case(c).is_overdue for c in parse_cases(document))

        self.assertGreater(overdue(shifted), overdue(baseline))


if __name__ == "__main__":
    unittest.main()
