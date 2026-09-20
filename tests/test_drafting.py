"""Tests for message drafting and the pre-send check.

Verification happens before the send. These tests are about what the check
refuses to let out, not about whether the wording is nice.
"""

from __future__ import annotations

import unittest

from recall_agent import drafting
from recall_agent.drafting import check_draft, draft_message, render_template
from recall_agent.llm import LLMClient, LLMResponse, LLMUnavailable

RECALL_CASE = {
    "case_id": "R000081",
    "patient": {"patient_id": "P00081"},
    "clinic_context": {
        "full_name": "Vijay Kumar",
        "requirement_kind": "RECALL",
        "recall_type": "routine_hygiene",
        "days_overdue": 40,
    },
}

TREATMENT_CASE = {
    "case_id": "T000002",
    "patient": {"patient_id": "P00003"},
    "clinic_context": {
        "full_name": "Jun Jie Tay",
        "requirement_kind": "TREATMENT",
        "treatment_type": "periodontal_therapy",
        "treatment_status": "abandoned",
        "days_overdue": 2591,
    },
}

CONTEXT = {**RECALL_CASE["clinic_context"], "patient_id": "P00081"}


class StubClient(LLMClient):
    def __init__(self, text: str | None = None, fail: bool = False) -> None:
        self.text = text
        self.fail = fail

    def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 512,
        normalise_key: bool = False,
    ) -> LLMResponse:
        if self.fail:
            raise LLMUnavailable("stub is offline")
        return LLMResponse(self.text or "", from_cache=False, model="stub")


class NameTests(unittest.TestCase):
    def test_first_name_handles_the_datasets_messy_names(self) -> None:
        cases = {
            "Vijay Kumar": "Vijay",
            "DANIEL FERNANDEZ": "Daniel",
            "Ravi  Rajan": "Ravi",
            "": "there",
        }
        for full, expected in cases.items():
            with self.subTest(full=full):
                self.assertEqual(drafting.first_name(full), expected)


class TemplateTests(unittest.TestCase):
    def test_templates_pass_their_own_check(self) -> None:
        # The fallback must always be safe, or the fallback is not a fallback.
        for case in (RECALL_CASE, TREATMENT_CASE):
            with self.subTest(case_id=case["case_id"]):
                text, template_id = render_template(case["clinic_context"])
                context = {
                    **case["clinic_context"],
                    "patient_id": case["patient"]["patient_id"],
                }
                self.assertTrue(check_draft(text, context).passed)
                self.assertTrue(template_id)

    def test_recall_template_offers_a_way_to_opt_out(self) -> None:
        text, _ = render_template(RECALL_CASE["clinic_context"])
        self.assertIn("stop", text.lower())

    def test_treatment_template_does_not_diagnose(self) -> None:
        text, _ = render_template(TREATMENT_CASE["clinic_context"])
        self.assertIn("not completed", text)
        for term in ("urgent", "risk", "serious", "infection"):
            self.assertNotIn(term, text.lower())


class PreSendCheckTests(unittest.TestCase):
    def test_a_good_draft_passes(self) -> None:
        text = (
            "Hi Vijay, this is Bright Smile Dental. You are due for your routine "
            "cleaning. Please reply and we will arrange a time."
        )
        self.assertTrue(check_draft(text, CONTEXT).passed)

    def test_clinical_claims_are_refused(self) -> None:
        cases = [
            "Hi Vijay, your gum disease is worsening. Bright Smile Dental",
            "Hi Vijay, you may have an infection. Bright Smile Dental",
            "Hi Vijay, this is urgent. Bright Smile Dental",
            "Hi Vijay, you could lose the tooth. Bright Smile Dental",
        ]
        for text in cases:
            with self.subTest(text=text):
                check = check_draft(text, CONTEXT)
                self.assertFalse(check.passed)
                self.assertTrue(any("clinical" in f for f in check.failures))

    def test_promotional_content_is_refused(self) -> None:
        # The care/marketing boundary, enforced on the wording as well as in the
        # risk table.
        cases = [
            "Hi Vijay, 20% discount this month at Bright Smile Dental",
            "Hi Vijay, special price on whitening. Bright Smile Dental",
            "Hi Vijay, free consultation this week. Bright Smile Dental",
        ]
        for text in cases:
            with self.subTest(text=text):
                check = check_draft(text, CONTEXT)
                self.assertFalse(check.passed)
                self.assertTrue(any("promotional" in f for f in check.failures))

    def test_claiming_an_appointment_exists_is_refused(self) -> None:
        text = "Hi Vijay, we have booked you for 3pm Tuesday. Bright Smile Dental"
        check = check_draft(text, CONTEXT)
        self.assertFalse(check.passed)
        self.assertTrue(any("appointment exists" in f for f in check.failures))

    def test_links_are_refused(self) -> None:
        text = "Hi Vijay, book at https://example.com - Bright Smile Dental"
        self.assertFalse(check_draft(text, CONTEXT).passed)

    def test_a_draft_for_another_patient_is_refused(self) -> None:
        # Guards against a cross-patient mix-up reaching a real phone.
        text = "Hi Vijay, this is Bright Smile Dental about record P09999."
        check = check_draft(text, CONTEXT)
        self.assertFalse(check.passed)
        self.assertTrue(any("not this patient" in f for f in check.failures))

    def test_an_unaddressed_or_unsigned_draft_is_refused(self) -> None:
        check = check_draft("Hello, you are due for a check-up.", CONTEXT)
        self.assertFalse(check.passed)
        self.assertEqual(len(check.failures), 2)

    def test_over_length_drafts_are_refused(self) -> None:
        text = "Hi Vijay, this is Bright Smile Dental. " + ("x" * drafting.MAX_MESSAGE_CHARS)
        self.assertFalse(check_draft(text, CONTEXT).passed)

    def test_an_empty_draft_is_refused(self) -> None:
        self.assertFalse(check_draft("   ", CONTEXT).passed)


class DraftingTests(unittest.TestCase):
    def test_a_passing_model_draft_is_used(self) -> None:
        client = StubClient(
            "Hi Vijay, this is Bright Smile Dental. You are due for your routine "
            "cleaning. Please reply and we will find a time."
        )
        draft = draft_message(RECALL_CASE, client=client)
        self.assertEqual(draft.source, "llm")
        self.assertTrue(draft.check.passed)
        self.assertIsNone(draft.template_id)

    def test_a_failing_model_draft_is_discarded_not_repaired(self) -> None:
        # The approved template is already known to be safe, so there is no
        # reason to negotiate with a draft that broke the rules.
        client = StubClient(
            "Hi Vijay, your infection is serious. 20% off. Bright Smile Dental"
        )
        draft = draft_message(RECALL_CASE, client=client)
        self.assertEqual(draft.source, "template:llm_draft_failed_check")
        self.assertTrue(draft.check.passed)
        self.assertEqual(draft.template_id, "RECALL_REMINDER_V1")

    def test_an_unavailable_model_falls_back_to_the_template(self) -> None:
        draft = draft_message(RECALL_CASE, client=StubClient(fail=True))
        self.assertEqual(draft.source, "template:llm_unavailable")
        self.assertTrue(draft.check.passed)

    def test_drafting_can_run_without_a_model_at_all(self) -> None:
        draft = draft_message(TREATMENT_CASE, use_llm=False)
        self.assertEqual(draft.source, "template:llm_disabled")
        self.assertTrue(draft.check.passed)
        self.assertIn("Jun", draft.text)

    def test_every_draft_carries_its_check_result(self) -> None:
        payload = draft_message(RECALL_CASE, use_llm=False).to_dict()
        self.assertIn("check", payload)
        self.assertTrue(payload["check"]["passed"])
        self.assertEqual(payload["patient_id"], "P00081")

    def test_the_system_prompt_forbids_clinical_content(self) -> None:
        lowered = drafting.SYSTEM_PROMPT.lower()
        for phrase in ("never", "clinical advice", "discount"):
            self.assertIn(phrase, lowered)


if __name__ == "__main__":
    unittest.main()
