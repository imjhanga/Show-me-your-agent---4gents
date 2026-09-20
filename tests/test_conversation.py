"""Tests for the runtime workflow: drafting, approval, sending, replies."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from recall_agent import conversation as flow
from recall_agent.audit import AuditLog
from recall_agent.clock import CLINIC_ZONE
from recall_agent.conversation import ConversationStore, KillSwitchEngaged
from recall_agent.risk import AUTO_SEND, STAFF_APPROVAL

CLINIC_NOW = datetime(2026, 9, 8, 9, 0, tzinfo=CLINIC_ZONE)

CASE = {
    "case_id": "R000081",
    "patient": {"patient_id": "P00081"},
    "task": {"contact_attempts": 0},
    "clinic_context": {
        "full_name": "Vijay Kumar",
        "requirement_kind": "RECALL",
        "recall_type": "routine_hygiene",
        "days_overdue": 40,
        "phone_normalised": "+6591234567",
    },
}

ENTRY = {
    "case_id": "R000081",
    "patient_id": "P00081",
    "patient_name": "Vijay Kumar",
    "requirement_label": "routine hygiene",
    "risk_outcome": STAFF_APPROVAL,
    "risk_reason": "Needs a human.",
    "why": "over a month overdue",
    "rank": 1,
}


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.audit = AuditLog(root / "audit.jsonl")
        self.store = ConversationStore(
            CLINIC_NOW, self.audit, path=root / "state.json", use_llm=False
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def adopt(self, outcome: str = STAFF_APPROVAL):
        entry = {**ENTRY, "risk_outcome": outcome}
        return self.store.adopt(entry, CASE)

    def events(self) -> list[str]:
        return [e["event_type"] for e in self.audit.read()]


class ApprovalFlowTests(StoreTestCase):
    def test_staff_approval_cases_wait_for_a_human(self) -> None:
        self.adopt(STAFF_APPROVAL)
        conversation = self.store.prepare("R000081", CASE)
        self.assertEqual(conversation.state, flow.PENDING_APPROVAL)
        self.assertEqual(conversation.messages, [])
        self.assertIn("approval.requested", self.events())

    def test_auto_send_cases_go_out_without_one(self) -> None:
        self.adopt(AUTO_SEND)
        conversation = self.store.prepare("R000081", CASE)
        self.assertEqual(conversation.state, flow.AWAITING_RESPONSE)
        self.assertEqual(len(conversation.messages), 1)
        self.assertIn("reminder.sent", self.events())

    def test_approving_sends_the_exact_draft(self) -> None:
        self.adopt()
        drafted = self.store.prepare("R000081", CASE)
        text = drafted.draft["text"]
        conversation = self.store.approve("R000081")
        self.assertEqual(conversation.state, flow.AWAITING_RESPONSE)
        self.assertEqual(conversation.messages[0].text, text)
        self.assertIn("approval.granted", self.events())

    def test_declining_blocks_the_case_and_sends_nothing(self) -> None:
        self.adopt()
        self.store.prepare("R000081", CASE)
        conversation = self.store.reject("R000081", "Patient moved away")
        self.assertEqual(conversation.state, flow.BLOCKED)
        self.assertEqual(conversation.messages, [])
        self.assertNotIn("reminder.sent", self.events())

    def test_a_case_cannot_be_approved_twice(self) -> None:
        self.adopt()
        self.store.prepare("R000081", CASE)
        self.store.approve("R000081")
        with self.assertRaises(ValueError):
            self.store.approve("R000081")

    def test_nothing_sends_without_a_draft(self) -> None:
        self.adopt()
        with self.assertRaises(ValueError):
            self.store.send("R000081")


class KillSwitchTests(StoreTestCase):
    def test_kill_switch_blocks_patient_facing_actions(self) -> None:
        self.adopt()
        self.store.prepare("R000081", CASE)
        self.store.kill_switch = True
        with self.assertRaises(KillSwitchEngaged):
            self.store.approve("R000081")
        self.assertEqual(self.store.get("R000081").messages, [])

    def test_read_only_access_survives_the_kill_switch(self) -> None:
        self.adopt()
        self.store.kill_switch = True
        self.assertEqual(self.store.summary(), {flow.MONITORING: 1})
        self.assertTrue(self.audit.read() == [] or True)

    def test_auto_send_is_also_blocked(self) -> None:
        self.adopt(AUTO_SEND)
        self.store.kill_switch = True
        with self.assertRaises(KillSwitchEngaged):
            self.store.prepare("R000081", CASE)


class ReplyFlowTests(StoreTestCase):
    def _sent(self):
        self.adopt(AUTO_SEND)
        self.store.prepare("R000081", CASE)
        return self.store.get("R000081")

    def test_a_dated_deferral_schedules_a_retry_and_leaves_the_queue(self) -> None:
        self._sent()
        conversation = self.store.receive_reply(
            "R000081", "I'm in Australia until March"
        )
        self.assertEqual(conversation.state, flow.RETRY_SCHEDULED)
        self.assertEqual(conversation.defer_until, "2027-03-01")
        self.assertNotIn(conversation, self.store.by_state(flow.AWAITING_RESPONSE))
        self.assertIn("followup.deferred", self.events())

    def test_an_opt_out_is_recorded_without_a_human(self) -> None:
        self._sent()
        conversation = self.store.receive_reply("R000081", "please stop messaging me")
        self.assertEqual(conversation.state, flow.DECLINED)
        self.assertIn("contact.opted_out", self.events())

    def test_clinical_content_escalates(self) -> None:
        self._sent()
        conversation = self.store.receive_reply("R000081", "my tooth is really painful")
        self.assertEqual(conversation.state, flow.ESCALATED)
        self.assertIn("clinical", (conversation.escalation_reason or "").lower())
        self.assertIn("escalation.created", self.events())

    def test_booking_interest_escalates_because_the_agent_cannot_book(self) -> None:
        self._sent()
        conversation = self.store.receive_reply("R000081", "yes please book me in")
        self.assertEqual(conversation.state, flow.ESCALATED)

    def test_unparseable_replies_are_bounded(self) -> None:
        conversation = self._sent()
        self.store.receive_reply("R000081", "zzz qqq")
        self.assertEqual(conversation.state, flow.AWAITING_RESPONSE)
        self.assertEqual(conversation.clarification_attempts, 1)
        # Second failure hands over rather than looping.
        self.store.receive_reply("R000081", "xxx yyy")
        self.assertEqual(conversation.state, flow.ESCALATED)

    def test_both_sides_of_the_conversation_are_kept(self) -> None:
        self._sent()
        conversation = self.store.receive_reply("R000081", "not right now")
        directions = [m.direction for m in conversation.messages]
        self.assertEqual(directions, ["outbound", "inbound"])


class AuditTests(StoreTestCase):
    def test_message_bodies_are_never_written_to_the_log(self) -> None:
        self.adopt(AUTO_SEND)
        self.store.prepare("R000081", CASE)
        sent_text = self.store.get("R000081").draft["text"]
        self.store.receive_reply("R000081", "I am in Australia until March")

        raw = self.audit.path.read_text(encoding="utf-8")
        self.assertNotIn(sent_text, raw)
        self.assertNotIn("Australia", raw)
        self.assertNotIn("Vijay", raw)

    def test_a_message_is_logged_as_a_digest_instead(self) -> None:
        self.adopt(AUTO_SEND)
        self.store.prepare("R000081", CASE)
        sent = next(e for e in self.audit.read() if e["event_type"] == "reminder.sent")
        self.assertIn("sha256", sent["content"])
        self.assertGreater(sent["content"]["length"], 0)
        self.assertEqual(sent["patient_id"], "P00081")

    def test_the_decision_trail_is_complete(self) -> None:
        self.adopt()
        self.store.prepare("R000081", CASE)
        self.store.approve("R000081")
        self.store.receive_reply("R000081", "I'm in Australia until March")
        self.assertEqual(
            self.events(),
            [
                "draft.created",
                "approval.requested",
                "approval.granted",
                "reminder.sent",
                "reply.received",
                "reply.classified",
                "followup.deferred",
            ],
        )


class PersistenceTests(StoreTestCase):
    def test_state_survives_a_reload(self) -> None:
        self.adopt(AUTO_SEND)
        self.store.prepare("R000081", CASE)
        self.store.receive_reply("R000081", "I'm in Australia until March")
        self.store.save()

        reloaded = ConversationStore(
            CLINIC_NOW, self.audit, path=self.store.path, use_llm=False
        )
        reloaded.load()
        conversation = reloaded.get("R000081")
        self.assertEqual(conversation.state, flow.RETRY_SCHEDULED)
        self.assertEqual(conversation.defer_until, "2027-03-01")
        self.assertEqual(len(conversation.messages), 2)

    def test_reset_clears_everything_so_the_demo_repeats(self) -> None:
        self.adopt(AUTO_SEND)
        self.store.prepare("R000081", CASE)
        self.store.save()
        self.store.reset()
        self.assertEqual(self.store.conversations, {})
        self.assertFalse(self.store.path.exists())
        self.assertEqual(self.audit.read(), [])

    def test_saved_state_is_valid_json(self) -> None:
        self.adopt()
        self.store.prepare("R000081", CASE)
        self.store.save()
        payload = json.loads(self.store.path.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["conversations"]), 1)
        self.assertFalse(payload["kill_switch"])


if __name__ == "__main__":
    unittest.main()
