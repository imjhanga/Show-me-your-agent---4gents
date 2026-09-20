"""The pipeline is checked against a hand-written answer key.

Every other test in this package checks that the code does what the code says.
This one checks that the code does what a human reviewer says is *right*, using
thirty patients picked out of the dataset by hand with their expected outcome
written down independently of the implementation.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from recall_agent.clock import parse_clinic_now
from recall_agent.risk import AUTO_SEND, EXCLUDE, STAFF_APPROVAL
from recall_agent.scan import run_scan

ANSWER_KEY = Path("fixtures/demo/answer_key.json")
DATASET_DB = Path("dental_dataset/clinic.db")


class AnswerKeyTests(unittest.TestCase):
    key: dict
    verdicts: dict

    @classmethod
    def setUpClass(cls) -> None:
        if not DATASET_DB.exists():  # pragma: no cover - dataset is committed
            raise unittest.SkipTest(f"{DATASET_DB} not found; run from repo root")
        cls.key = json.loads(ANSWER_KEY.read_text(encoding="utf-8"))
        result = run_scan(clinic_now=parse_clinic_now(cls.key["clinic_now"]))
        cls.result = result
        cls.verdicts = {
            row["case_id"]: (row["risk_outcome"], row["risk_rule_id"], row["rank"])
            for row in result.worklist
        }
        for row in result.refusals:
            cls.verdicts[row["case_id"]] = (EXCLUDE, row["rule_id"], None)

    def test_answer_key_is_large_enough_to_be_meaningful(self) -> None:
        self.assertGreaterEqual(len(self.key["entries"]), 30)

    def test_every_entry_states_an_outcome_a_reason_and_a_rationale(self) -> None:
        for entry in self.key["entries"]:
            with self.subTest(case_id=entry["case_id"]):
                self.assertIn(
                    entry["expected_outcome"], {EXCLUDE, STAFF_APPROVAL, AUTO_SEND}
                )
                self.assertTrue(entry["expected_rule_id"])
                self.assertTrue(entry["dataset_fact"])
                self.assertGreater(len(entry["rationale"].split()), 10)

    def test_pipeline_reaches_the_expected_outcome(self) -> None:
        for entry in self.key["entries"]:
            case_id = entry["case_id"]
            with self.subTest(case_id=case_id, patient=entry["patient"]):
                self.assertIn(case_id, self.verdicts, f"{case_id} was not scanned")
                outcome, _, _ = self.verdicts[case_id]
                self.assertEqual(
                    outcome,
                    entry["expected_outcome"],
                    f"{entry['patient']}: {entry['rationale']}",
                )

    def test_pipeline_gives_the_expected_reason(self) -> None:
        # A refusal for the wrong stated reason is still a wrong answer: the
        # operator acts on the reason, not just the verdict.
        for entry in self.key["entries"]:
            case_id = entry["case_id"]
            with self.subTest(case_id=case_id, patient=entry["patient"]):
                _, rule_id, _ = self.verdicts[case_id]
                self.assertEqual(
                    rule_id,
                    entry["expected_rule_id"],
                    f"{entry['patient']}: {entry['rationale']}",
                )

    def test_top_priority_patients_rank_near_the_top(self) -> None:
        expected = self.key["top_priority"]
        for case_id in expected["case_ids"]:
            with self.subTest(case_id=case_id):
                self.assertIn(case_id, self.verdicts)
                _, _, position = self.verdicts[case_id]
                self.assertIsNotNone(position, f"{case_id} is not on the worklist")
                self.assertLessEqual(position, expected["within_rank"])

    def test_abandoned_treatment_outranks_routine_hygiene_on_real_data(self) -> None:
        # The differentiator, asserted against the actual worklist rather than a
        # constructed pair.
        ranks = {row["case_id"]: row for row in self.result.worklist}
        abandoned = [
            row
            for row in self.result.worklist
            if "abandoned" in row["requirement_label"]
        ]
        hygiene = [
            row
            for row in self.result.worklist
            if row["requirement_label"] == "routine hygiene"
        ]
        self.assertTrue(abandoned and hygiene)
        self.assertLess(
            min(row["rank"] for row in abandoned),
            min(row["rank"] for row in hygiene),
        )
        self.assertIn(abandoned[0]["case_id"], ranks)

    def test_a_deceased_patient_is_eligible_but_still_refused(self) -> None:
        # The case the risk gate exists for. The eligibility engine permits
        # contact here because its schema cannot express "deceased"; only the
        # risk table stops the message.
        refusal = next(
            row for row in self.result.refusals if row["case_id"] == "R000015"
        )
        self.assertEqual(refusal["rule_id"], "patient_deceased")
        self.assertIn("OVERDUE_AND_CONTACT_ELIGIBLE", refusal["eligibility_reason_codes"])

    def test_no_excluded_case_ever_reaches_the_worklist(self) -> None:
        excluded = {row["case_id"] for row in self.result.refusals}
        worklisted = {row["case_id"] for row in self.result.worklist}
        self.assertEqual(excluded & worklisted, set())

    def test_every_auto_send_case_passed_the_eligibility_engine(self) -> None:
        for row in self.result.worklist:
            if row["risk_outcome"] != AUTO_SEND:
                continue
            with self.subTest(case_id=row["case_id"]):
                self.assertEqual(
                    row["eligibility_reason_codes"], ["OVERDUE_AND_CONTACT_ELIGIBLE"]
                )

    def test_scan_is_reproducible(self) -> None:
        again = run_scan(clinic_now=parse_clinic_now(self.key["clinic_now"]))
        self.assertEqual(again.totals, self.result.totals)
        self.assertEqual(
            [row["case_id"] for row in again.worklist[:50]],
            [row["case_id"] for row in self.result.worklist[:50]],
        )


if __name__ == "__main__":
    unittest.main()
