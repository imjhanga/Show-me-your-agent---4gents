"""Tests for rule-based prioritisation."""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from recall_agent import ranking
from recall_agent.ranking import RankingConfigError, RankingWeights, rank, score_case
from recall_agent.risk import AUTO_SEND, EXCLUDE, STAFF_APPROVAL, RiskDecision

BASE_CASE: dict = {
    "case_id": "R000001",
    "patient": {"patient_id": "P00001"},
    "task": {"contact_attempts": 0},
    "clinic_context": {
        "full_name": "Test Patient",
        "requirement_kind": "RECALL",
        "recall_type": "routine_hygiene",
        "base_urgency": 1,
        "days_overdue": 10,
        "appointment_history": {"completed": 3, "no_show": 0, "cancelled": 0},
    },
}

ALLOWED = RiskDecision(AUTO_SEND, "routine_recall_reminder", "Routine.", "cite")
NEEDS_HUMAN = RiskDecision(STAFF_APPROVAL, "abandoned_treatment", "Human.", "cite")
REFUSED = RiskDecision(EXCLUDE, "patient_deceased", "Deceased.", "cite")


def case_with(**context: object) -> dict:
    data = copy.deepcopy(BASE_CASE)
    data["clinic_context"].update(context)
    return data


def score_of(case: dict, weights: RankingWeights) -> float:
    return score_case(case, weights)[0]


def write_weights(body: str) -> Path:
    handle = tempfile.NamedTemporaryFile(
        "w", suffix=".toml", delete=False, encoding="utf-8"
    )
    handle.write(body)
    handle.close()
    return Path(handle.name)


class WeightsLoadingTests(unittest.TestCase):
    def test_shipped_weights_load(self) -> None:
        weights = RankingWeights.load()
        self.assertGreater(weights.urgency_points, 0)
        self.assertGreater(len(weights.overdue_bands), 1)

    def test_final_band_must_be_unbounded(self) -> None:
        path = write_weights("[[overdue_band]]\nup_to_days = 30\npoints = 1.0\n")
        try:
            with self.assertRaises(RankingConfigError) as caught:
                RankingWeights.load(path)
            self.assertIn("unbounded", str(caught.exception))
        finally:
            path.unlink()

    def test_bands_must_ascend(self) -> None:
        path = write_weights(
            "[[overdue_band]]\nup_to_days = 90\npoints = 1.0\n"
            "[[overdue_band]]\nup_to_days = 30\npoints = 2.0\n"
            "[[overdue_band]]\npoints = 3.0\n"
        )
        try:
            with self.assertRaises(RankingConfigError) as caught:
                RankingWeights.load(path)
            self.assertIn("ascend", str(caught.exception))
        finally:
            path.unlink()

    def test_band_lookup_picks_the_first_matching_band(self) -> None:
        weights = RankingWeights.load()
        cases = [(0, 30), (30, 30), (31, 90), (365, 365), (100000, None)]
        for days, expected_bound in cases:
            with self.subTest(days=days):
                self.assertEqual(weights.band_for(days).up_to_days, expected_bound)


class ScoringTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.weights = RankingWeights.load()

    def test_abandoned_treatment_outranks_routine_hygiene(self) -> None:
        # The headline dental rule: a stalled root canal matters more than a
        # cleaning that is equally overdue.
        hygiene = case_with(recall_type="routine_hygiene", base_urgency=1, days_overdue=200)
        abandoned = case_with(
            requirement_kind="TREATMENT",
            recall_type=None,
            treatment_type="root_canal",
            treatment_status="abandoned",
            clinical_urgency=5,
            treatment_age_days=1500,
            days_overdue=200,
        )
        self.assertGreater(score_of(abandoned, self.weights), score_of(hygiene, self.weights))

    def test_longer_overdue_outranks_recently_due(self) -> None:
        recent = case_with(days_overdue=10)
        stale = case_with(days_overdue=800)
        self.assertGreater(score_of(stale, self.weights), score_of(recent, self.weights))

    def test_higher_urgency_outranks_lower(self) -> None:
        low = case_with(base_urgency=1)
        high = case_with(base_urgency=5)
        self.assertGreater(score_of(high, self.weights), score_of(low, self.weights))

    def test_a_longer_stalled_treatment_outranks_a_fresher_one(self) -> None:
        def stalled(days: int) -> dict:
            return case_with(
                requirement_kind="TREATMENT",
                recall_type=None,
                treatment_type="crown_fitting",
                treatment_status="abandoned",
                clinical_urgency=4,
                treatment_age_days=days,
                days_overdue=400,
            )

        self.assertGreater(
            score_of(stalled(3000), self.weights), score_of(stalled(200), self.weights)
        )

    def test_staleness_is_capped(self) -> None:
        def stalled(days: int) -> dict:
            return case_with(
                requirement_kind="TREATMENT",
                recall_type=None,
                treatment_type="crown_fitting",
                treatment_status="abandoned",
                clinical_urgency=4,
                treatment_age_days=days,
            )

        self.assertEqual(
            score_of(stalled(20 * 365), self.weights),
            score_of(stalled(60 * 365), self.weights),
        )

    def test_no_shows_raise_priority_but_are_capped(self) -> None:
        none = case_with(appointment_history={"completed": 3, "no_show": 0})
        some = case_with(appointment_history={"completed": 3, "no_show": 2})
        many = case_with(appointment_history={"completed": 3, "no_show": 9})
        capped = case_with(appointment_history={"completed": 3, "no_show": 40})
        self.assertGreater(score_of(some, self.weights), score_of(none, self.weights))
        self.assertEqual(score_of(many, self.weights), score_of(capped, self.weights))

    def test_unanswered_messages_lower_priority(self) -> None:
        # Repeated silence means lower yield, and hammering is not respectful of
        # it. The hard stop is the engine's attempt limit, not this.
        answered = copy.deepcopy(BASE_CASE)
        ignored = copy.deepcopy(BASE_CASE)
        ignored["task"]["contact_attempts"] = 2
        self.assertLess(score_of(ignored, self.weights), score_of(answered, self.weights))

    def test_unanswered_penalty_has_a_floor(self) -> None:
        a = copy.deepcopy(BASE_CASE)
        a["task"]["contact_attempts"] = 3
        b = copy.deepcopy(BASE_CASE)
        b["task"]["contact_attempts"] = 50
        self.assertEqual(score_of(a, self.weights), score_of(b, self.weights))

    def test_no_completed_visit_raises_priority(self) -> None:
        seen = case_with(appointment_history={"completed": 2, "no_show": 0})
        unseen = case_with(appointment_history={"completed": 0, "no_show": 0})
        self.assertGreater(score_of(unseen, self.weights), score_of(seen, self.weights))

    def test_every_scoring_component_explains_itself(self) -> None:
        case = case_with(
            requirement_kind="TREATMENT",
            recall_type=None,
            treatment_type="implant_placement",
            treatment_status="abandoned",
            clinical_urgency=5,
            treatment_age_days=2000,
            days_overdue=900,
            appointment_history={"completed": 0, "no_show": 2},
        )
        _, components = score_case(case, self.weights)
        self.assertGreater(len(components), 3)
        for component in components:
            with self.subTest(reason=component.reason):
                self.assertGreater(len(component.reason.split()), 2)


class RankOrderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.weights = RankingWeights.load()

    def _rank(self, cases: list[dict], decisions: dict) -> list:
        results = {c["case_id"]: {"reason_codes": []} for c in cases}
        return rank(cases, results, decisions, self.weights)

    def test_excluded_cases_are_dropped_from_the_worklist(self) -> None:
        keep = case_with()
        keep["case_id"] = "R000001"
        drop = case_with()
        drop["case_id"] = "R000002"
        entries = self._rank(
            [keep, drop], {"R000001": ALLOWED, "R000002": REFUSED}
        )
        self.assertEqual([e.case_id for e in entries], ["R000001"])

    def test_excluded_cases_can_be_included_for_reporting(self) -> None:
        keep = case_with()
        keep["case_id"] = "R000001"
        drop = case_with()
        drop["case_id"] = "R000002"
        results = {"R000001": {"reason_codes": []}, "R000002": {"reason_codes": []}}
        entries = rank(
            [keep, drop],
            results,
            {"R000001": ALLOWED, "R000002": REFUSED},
            self.weights,
            include_excluded=True,
        )
        self.assertEqual(len(entries), 2)

    def test_ranks_are_dense_and_start_at_one(self) -> None:
        cases = []
        decisions = {}
        for index in range(5):
            case = case_with(days_overdue=index * 100)
            case["case_id"] = f"R00000{index}"
            cases.append(case)
            decisions[case["case_id"]] = ALLOWED
        entries = self._rank(cases, decisions)
        self.assertEqual([e.rank for e in entries], [1, 2, 3, 4, 5])

    def test_highest_score_ranks_first(self) -> None:
        low = case_with(days_overdue=5, base_urgency=1)
        low["case_id"] = "R000001"
        high = case_with(days_overdue=900, base_urgency=5)
        high["case_id"] = "R000002"
        entries = self._rank([low, high], {"R000001": ALLOWED, "R000002": ALLOWED})
        self.assertEqual(entries[0].case_id, "R000002")
        self.assertEqual(entries[0].rank, 1)

    def test_ties_break_deterministically(self) -> None:
        # The demo has to run identically twice.
        cases = []
        decisions = {}
        for case_id in ("R000003", "R000001", "R000002"):
            case = case_with()
            case["case_id"] = case_id
            cases.append(case)
            decisions[case_id] = ALLOWED
        first = [e.case_id for e in self._rank(cases, decisions)]
        second = [e.case_id for e in self._rank(list(reversed(cases)), decisions)]
        self.assertEqual(first, second)
        self.assertEqual(first, ["R000001", "R000002", "R000003"])

    def test_entry_carries_the_risk_verdict_and_reason(self) -> None:
        case = case_with()
        entries = self._rank([case], {"R000001": NEEDS_HUMAN})
        entry = entries[0]
        self.assertEqual(entry.risk_outcome, STAFF_APPROVAL)
        self.assertEqual(entry.risk_rule_id, "abandoned_treatment")
        self.assertEqual(entry.risk_reason, "Human.")

    def test_why_reads_as_a_sentence(self) -> None:
        case = case_with(base_urgency=4, days_overdue=400)
        entry = self._rank([case], {"R000001": ALLOWED})[0]
        self.assertTrue(entry.why.endswith("."))
        self.assertIn("urgency", entry.why)

    def test_worklist_serialises_and_can_be_limited(self) -> None:
        cases = []
        decisions = {}
        for index in range(4):
            case = case_with(days_overdue=index * 50)
            case["case_id"] = f"R00000{index}"
            cases.append(case)
            decisions[case["case_id"]] = ALLOWED
        entries = self._rank(cases, decisions)
        rows = ranking.worklist(entries, limit=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["rank"], 1)
        self.assertIn("why", rows[0])
        self.assertIn("risk_outcome", rows[0])


if __name__ == "__main__":
    unittest.main()
