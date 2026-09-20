"""Convert ``dental_dataset`` into the document ``followup_agent`` consumes.

The eligibility engine expects a small, strict shape: one case per follow-up
requirement, with consent and contact collapsed to booleans for a single
channel. The dataset is much richer and much messier. This module does the
mapping, and is deliberate about what it refuses to map.

What is mapped into the engine's own fields
-------------------------------------------
``consent.granted``          whatsapp_consent == 1, and nothing else
``contact.is_valid``         phone normalises to exactly one Singapore mobile
``requirement.state``        OPEN for every live recall and incomplete treatment
``approved_due_date``        recall.next_due_date, or treatment admin review date
``source_records_current``   false when the patient is a flagged duplicate

The duplicate mapping is the one judgement call. A known-duplicate patient
record is a stale, unreliable source, which is what SOURCE_RECORD_STALE means
in the blueprint, so the engine emits an accurate reason code for it.

What is deliberately NOT mapped
-------------------------------
Deceased, overseas and inactive patients, marketing-versus-care consent, and
note-based contact constraints have no home in the engine's schema. Collapsing
them into ``consent.granted`` would make the engine report
MESSAGING_CONSENT_NOT_CURRENT for a deceased patient, which is false and would
be false on the one screen where refusals are the point. Those live in the risk
rule table instead (``recall_agent.risk``), which gates every case before the
eligibility result is acted on.

Extra context for the downstream modules travels in a ``clinic_context`` block
per case. ``followup_agent.domain`` parses by pulling named keys and ignores
everything it does not recognise, so one document serves both sides without
either module knowing about the other.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

from .clock import (
    CLINIC_TIMEZONE,
    DATASET_REFERENCE_DATE,
    DEFAULT_CLINIC_NOW,
    at_clinic_hour,
    clinic_today,
    days_between,
    parse_clinic_now,
)

DEFAULT_DB = Path("dental_dataset/clinic.db")

# Demo channel. The mock panel is WhatsApp, so consent and contact validity are
# both evaluated for that channel.
CHANNEL = "whatsapp"

# Proposed prototype limits from technical-blueprint.md §7. Not approved clinic
# policy.
MAX_CONTACT_ATTEMPTS = 3
SENDING_WINDOW_START_HOUR = 9
SENDING_WINDOW_END_HOUR = 20

# How long after an outbound message we leave a patient alone.
COOLDOWN_DAYS = 14

# Window over which unanswered outbound messages count toward the attempt limit.
ATTEMPT_WINDOW_DAYS = 365

# Administrative convention, NOT a clinical interval: the paperwork follow-up on
# an incomplete treatment is considered due this long after the treatment began.
# The agent does not set clinical recall intervals (blueprint §1). Surfaced as
# clinic_context.due_date_basis so it is visible rather than buried.
ADMIN_REVIEW_DAYS = 30

BOOKED_RECALL_STATUS = "booked"
INCOMPLETE_TREATMENT_STATUSES = ("in_progress", "abandoned")

DUPLICATE_NOTE = "Possible duplicate record"
OPT_OUT_NOTE = "Opted out of all contact"


def normalise_sg_mobile(raw: str) -> str | None:
    """Return the number in +65XXXXXXXX form, or None if it is unusable.

    Returns None for a blank field and for the two-numbers-in-one-field rows:
    an ambiguous contact detail is an invalid one, because we cannot tell which
    person would receive the message.
    """

    if not raw:
        return None
    text = raw.strip().replace(" ", "").replace("-", "")
    if not text or "/" in text or "," in text:
        return None
    if text.startswith("+65"):
        text = text[3:]
    elif text.startswith("65") and len(text) == 10:
        text = text[2:]
    if len(text) != 8 or not text.isdigit():
        return None
    if text[0] not in "89":
        return None
    return f"+65{text}"


def _connect(db_path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _iso(value: Any) -> str | None:
    if value in (None, ""):
        return None
    return str(value)


def _as_date(value: Any) -> date | None:
    text = _iso(value)
    return date.fromisoformat(text) if text else None


def _duplicate_map(patients: list[sqlite3.Row]) -> dict[str, str]:
    """Map a flagged duplicate patient_id to the original it shadows.

    Names are not usable for this: hundreds of patients share a normalised name
    by chance while only 55 are real duplicates. The (date_of_birth, phone) pair
    resolves 50 of the 55; the remaining 5 have no phone on either record and
    cannot be linked. Those 5 are still flagged duplicates, so callers must key
    exclusion on ``is_flagged_duplicate`` and treat this link as extra detail.
    """

    by_identity: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in patients:
        if row["phone"]:
            by_identity[(row["date_of_birth"], row["phone"])].append(row)

    duplicate_of: dict[str, str] = {}
    for rows in by_identity.values():
        if len(rows) < 2:
            continue
        flagged = [r for r in rows if r["notes"] == DUPLICATE_NOTE]
        originals = [r for r in rows if r["notes"] != DUPLICATE_NOTE]
        if not flagged or not originals:
            continue
        original_id = sorted(r["patient_id"] for r in originals)[0]
        for row in flagged:
            duplicate_of[row["patient_id"]] = original_id
    return duplicate_of


def _appointment_history(connection: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    history: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "completed": 0,
            "no_show": 0,
            "cancelled": 0,
            "last_completed_visit": None,
        }
    )
    rows = connection.execute(
        "SELECT patient_id, appointment_date, status FROM appointments"
    )
    for row in rows:
        entry = history[row["patient_id"]]
        status = row["status"]
        if status in entry:
            entry[status] += 1
        if status == "completed":
            current = entry["last_completed_visit"]
            if current is None or row["appointment_date"] > current:
                entry["last_completed_visit"] = row["appointment_date"]
    return history


def _contact_history(
    connection: sqlite3.Connection, today: date
) -> dict[str, dict[str, Any]]:
    cutoff = (today - timedelta(days=ATTEMPT_WINDOW_DAYS)).isoformat()
    history: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "outbound_total": 0,
            "unanswered_recent": 0,
            "last_outbound_date": None,
            "last_inbound_date": None,
            "opted_out_in_log": False,
        }
    )
    rows = connection.execute(
        "SELECT patient_id, contact_date, direction, outcome FROM contact_log"
    )
    for row in rows:
        entry = history[row["patient_id"]]
        contact_date = row["contact_date"]
        if row["direction"] == "outbound":
            entry["outbound_total"] += 1
            current = entry["last_outbound_date"]
            if current is None or contact_date > current:
                entry["last_outbound_date"] = contact_date
            if row["outcome"] in ("no_response", "failed") and contact_date >= cutoff:
                entry["unanswered_recent"] += 1
            if row["outcome"] == "opted_out":
                entry["opted_out_in_log"] = True
        else:
            current = entry["last_inbound_date"]
            if current is None or contact_date > current:
                entry["last_inbound_date"] = contact_date
    return history


def _patient_block(row: sqlite3.Row, phone: str | None) -> dict[str, Any]:
    return {
        "patient_id": row["patient_id"],
        "consent": {
            "channel": CHANNEL,
            "granted": row["whatsapp_consent"] == 1,
            "expires_at": None,
        },
        "contact": {
            "channel": CHANNEL,
            "is_valid": phone is not None,
        },
    }


def _task_block(contact: dict[str, Any], today: date) -> dict[str, Any]:
    next_permitted = None
    last_outbound = _as_date(contact["last_outbound_date"])
    if last_outbound is not None and days_between(last_outbound, today) < COOLDOWN_DAYS:
        resume_on = last_outbound + timedelta(days=COOLDOWN_DAYS)
        next_permitted = at_clinic_hour(resume_on, SENDING_WINDOW_START_HOUR)
    return {
        "contact_attempts": contact["unanswered_recent"],
        "next_permitted_contact_at": (
            next_permitted.isoformat() if next_permitted is not None else None
        ),
        # Workflow state is owned by the scan loop, not by the source data.
        "has_pending_approval": False,
        "has_unresolved_escalation": False,
    }


def _shared_context(
    row: sqlite3.Row,
    phone: str | None,
    duplicate_of: str | None,
    appointments: dict[str, Any],
    contact: dict[str, Any],
) -> dict[str, Any]:
    notes = row["notes"] or ""
    return {
        "is_flagged_duplicate": notes == DUPLICATE_NOTE,
        "full_name": row["full_name"],
        "date_of_birth": row["date_of_birth"],
        "patient_status": row["patient_status"],
        "preferred_channel": row["preferred_channel"],
        "whatsapp_consent": row["whatsapp_consent"] == 1,
        "marketing_consent": row["marketing_consent"] == 1,
        "opted_out": notes == OPT_OUT_NOTE,
        "notes": notes,
        "phone_raw": row["phone"],
        "phone_normalised": phone,
        "email": row["email"] or None,
        "duplicate_of": duplicate_of,
        "appointment_history": dict(appointments),
        "contact_history": dict(contact),
    }


def _recall_case(
    row: sqlite3.Row,
    patient: sqlite3.Row,
    phone: str | None,
    duplicate_of: str | None,
    appointments: dict[str, Any],
    contact: dict[str, Any],
    clinic_now: datetime,
    today: date,
) -> dict[str, Any]:
    recall_id = row["recall_id"]
    episode_id = f"E-{recall_id}"
    due_date = _as_date(row["next_due_date"])
    assert due_date is not None  # next_due_date is never null in this dataset

    synthetic_appointments: list[dict[str, Any]] = []
    if row["recall_status"] == BOOKED_RECALL_STATUS:
        # No appointment row in the dataset is dated after the reference date,
        # so a confirmed future appointment has to be reconstructed from the
        # booked recall status. Labelled below so it is never mistaken for a
        # real source record.
        starts_on = max(due_date, today + timedelta(days=7))
        synthetic_appointments.append(
            {
                "appointment_id": f"A-SYNTH-{recall_id}",
                "starts_at": at_clinic_hour(starts_on, 10).isoformat(),
                "status": "CONFIRMED",
                "covered_follow_up_ids": [recall_id],
            }
        )

    context = _shared_context(patient, phone, duplicate_of, appointments, contact)
    context.update(
        {
            "requirement_kind": "RECALL",
            "recall_type": row["recall_type"],
            "interval_months": row["interval_months"],
            "base_urgency": row["base_urgency"],
            "recall_status": row["recall_status"],
            "last_visit_date": _iso(row["last_visit_date"]),
            "days_overdue": max(0, days_between(due_date, today)),
            "due_date_basis": "recall_schedule.next_due_date (clinician-set)",
            "synthetic_appointment": bool(synthetic_appointments),
        }
    )

    return {
        "case_id": recall_id,
        "evaluated_at": clinic_now.isoformat(),
        "patient": _patient_block(patient, phone),
        "episode": {
            "episode_id": episode_id,
            "clinical_status": f"RECALL_{row['recall_type'].upper()}",
            "source_record_version": 1,
        },
        "requirement": {
            "follow_up_id": recall_id,
            "episode_id": episode_id,
            "state": "OPEN",
            "approved_due_date": due_date.isoformat(),
            "source_record_version": 1,
            "source_records_current": not context["is_flagged_duplicate"],
            "instructions_consistent": True,
        },
        "appointments": synthetic_appointments,
        "task": _task_block(contact, today),
        "clinic_context": context,
    }


def _treatment_case(
    row: sqlite3.Row,
    patient: sqlite3.Row,
    phone: str | None,
    duplicate_of: str | None,
    appointments: dict[str, Any],
    contact: dict[str, Any],
    clinic_now: datetime,
    today: date,
) -> dict[str, Any]:
    treatment_id = row["treatment_id"]
    episode_id = f"E-{treatment_id}"
    start_date = _as_date(row["start_date"])
    assert start_date is not None
    due_date = start_date + timedelta(days=ADMIN_REVIEW_DAYS)

    context = _shared_context(patient, phone, duplicate_of, appointments, contact)
    context.update(
        {
            "requirement_kind": "TREATMENT",
            "treatment_type": row["treatment_type"],
            "treatment_status": row["treatment_status"],
            "clinical_urgency": row["clinical_urgency"],
            "dentist": row["dentist"],
            "start_date": start_date.isoformat(),
            "treatment_age_days": days_between(start_date, today),
            "days_overdue": max(0, days_between(due_date, today)),
            "due_date_basis": (
                f"treatment start_date + {ADMIN_REVIEW_DAYS}d administrative "
                "review convention (not a clinical interval)"
            ),
            "synthetic_appointment": False,
        }
    )

    return {
        "case_id": treatment_id,
        "evaluated_at": clinic_now.isoformat(),
        "patient": _patient_block(patient, phone),
        "episode": {
            "episode_id": episode_id,
            "clinical_status": f"TREATMENT_{row['treatment_status'].upper()}",
            "source_record_version": 1,
        },
        "requirement": {
            "follow_up_id": treatment_id,
            "episode_id": episode_id,
            "state": "OPEN",
            "approved_due_date": due_date.isoformat(),
            "source_record_version": 1,
            "source_records_current": not context["is_flagged_duplicate"],
            "instructions_consistent": True,
        },
        "appointments": [],
        "task": _task_block(contact, today),
        "clinic_context": context,
    }


def build_document(
    db_path: Path = DEFAULT_DB, clinic_now: datetime | None = None
) -> dict[str, Any]:
    """Build the full eligibility document for every follow-up requirement."""

    clinic_now = clinic_now or DEFAULT_CLINIC_NOW
    today = clinic_today(clinic_now)

    connection = _connect(db_path)
    try:
        patients = connection.execute("SELECT * FROM patients").fetchall()
        by_id = {row["patient_id"]: row for row in patients}
        duplicate_of = _duplicate_map(patients)
        appointment_history = _appointment_history(connection)
        contact_history = _contact_history(connection, today)
        phones = {
            row["patient_id"]: normalise_sg_mobile(row["phone"]) for row in patients
        }

        cases: list[dict[str, Any]] = []
        recalls = connection.execute(
            "SELECT * FROM recall_schedule ORDER BY recall_id"
        ).fetchall()
        for row in recalls:
            patient = by_id[row["patient_id"]]
            cases.append(
                _recall_case(
                    row,
                    patient,
                    phones[patient["patient_id"]],
                    duplicate_of.get(patient["patient_id"]),
                    appointment_history[patient["patient_id"]],
                    contact_history[patient["patient_id"]],
                    clinic_now,
                    today,
                )
            )

        treatments = connection.execute(
            "SELECT * FROM treatments WHERE treatment_status IN (?, ?) "
            "ORDER BY treatment_id",
            INCOMPLETE_TREATMENT_STATUSES,
        ).fetchall()
        for row in treatments:
            patient = by_id[row["patient_id"]]
            cases.append(
                _treatment_case(
                    row,
                    patient,
                    phones[patient["patient_id"]],
                    duplicate_of.get(patient["patient_id"]),
                    appointment_history[patient["patient_id"]],
                    contact_history[patient["patient_id"]],
                    clinic_now,
                    today,
                )
            )
    finally:
        connection.close()

    return {
        "generated_by": "recall_agent.bridge",
        "clinic_now": clinic_now.isoformat(),
        "dataset_reference_date": DATASET_REFERENCE_DATE.isoformat(),
        "bridge_notes": [
            "Fictional data only.",
            "consent.granted reflects whatsapp_consent alone. Deceased, overseas, "
            "inactive, opted-out, duplicate and marketing-consent handling lives "
            "in recall_agent.risk, not in these fields.",
            "Appointments on booked recalls are reconstructed from recall_status "
            "because the dataset contains no appointment dated after the "
            "reference date.",
            f"Treatment requirements are due start_date + {ADMIN_REVIEW_DAYS}d by "
            "administrative convention; the agent sets no clinical intervals.",
        ],
        "policy_defaults": {
            "clinic_timezone": CLINIC_TIMEZONE,
            "channel": CHANNEL,
            "max_contact_attempts": MAX_CONTACT_ATTEMPTS,
            "sending_window_start_hour": SENDING_WINDOW_START_HOUR,
            "sending_window_end_hour": SENDING_WINDOW_END_HOUR,
        },
        "cases": cases,
    }


def summarise(document: dict[str, Any]) -> dict[str, Any]:
    cases: Iterable[dict[str, Any]] = document["cases"]
    counts: dict[str, int] = defaultdict(int)
    for case in cases:
        context = case["clinic_context"]
        counts["total"] += 1
        counts[f"kind_{context['requirement_kind'].lower()}"] += 1
        if not case["patient"]["consent"]["granted"]:
            counts["no_whatsapp_consent"] += 1
        if not case["patient"]["contact"]["is_valid"]:
            counts["invalid_phone"] += 1
        if context["is_flagged_duplicate"]:
            counts["duplicate_flagged"] += 1
        if context["duplicate_of"]:
            counts["duplicate_linked_to_original"] += 1
        counts[f"status_{context['patient_status']}"] += 1
    return dict(sorted(counts.items()))


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert dental_dataset into followup_agent case JSON."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=Path("runs/cases.json"))
    parser.add_argument(
        "--clinic-now",
        default=None,
        help="ISO-8601 instant to evaluate against (default: dataset reference date)",
    )
    parser.add_argument("--compact", action="store_true")
    parser.add_argument(
        "--stats", action="store_true", help="Print a case summary to stderr"
    )
    args = parser.parse_args(argv)

    document = build_document(args.db, parse_clinic_now(args.clinic_now))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        json.dump(document, handle, indent=None if args.compact else 2)
    print(f"wrote {len(document['cases'])} cases to {args.out}")
    if args.stats:
        print(json.dumps(summarise(document), indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
