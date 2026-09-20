# Synthetic dental clinic dataset

Fictional data for testing the patient follow-up agent. No real patient data.
Reference date: **8 September 2026**. Seeded (42) — regenerating gives identical output.

Provided as both CSV files and `clinic.db` (SQLite, indexed).

## Scale

| Table | Rows |
|---|---|
| patients | 3,255 |
| appointments | 11,279 |
| treatments | 3,563 |
| recall_schedule | 3,255 |
| contact_log | 4,756 |

**1,188 patients are currently overdue (36%)** — in line with the 25–40% range typically
cited for an active dental patient base. Large enough that manual review is genuinely
impractical, which is the premise of the problem statement.

## Schema

**patients** — `patient_id`, `full_name`, `date_of_birth`, `phone`, `email`,
`registration_date`, `preferred_channel` (whatsapp/sms/email/phone_call),
`whatsapp_consent`, `marketing_consent`, `patient_status`
(active/inactive/overseas/deceased), `notes`

**appointments** — `appointment_id`, `patient_id`, `appointment_date`,
`appointment_type`, `status` (completed/no_show/cancelled), `dentist`, `duration_min`

**treatments** — `treatment_id`, `patient_id`, `treatment_type`, `start_date`,
`treatment_status` (completed/in_progress/abandoned), `clinical_urgency` (1–5), `dentist`

**recall_schedule** — `recall_id`, `patient_id`, `recall_type`, `interval_months`,
`last_visit_date`, `next_due_date`, `recall_status` (due/scheduled/booked), `base_urgency`

**contact_log** — `contact_id`, `patient_id`, `contact_date`, `channel`, `direction`
(inbound/outbound), `message_type`, `outcome`

Recall intervals follow dental practice: 6 or 12 month routine hygiene, 3–4 month
perio maintenance, 2 month ortho adjustment, 3 month post-treatment review, 6 month
implant review, 6 month paediatric.

## Deliberate data quality issues

These are in the data on purpose. Handling them is part of what makes the agent
credible — a demo on clean data proves nothing.

- **201 patients with no phone number** — cannot be contacted by WhatsApp or SMS
- **Inconsistent phone formats** — some missing `+65`, a few with two numbers in one field
- **~18% missing email**
- **371 patients without WhatsApp consent** — must not be messaged on that channel
- **~3.5% opted out of all contact** — must be excluded entirely
- **55 duplicate patient records** — same person, name spacing or casing differs,
  flagged in notes. Contacting both is a visible failure
- **Patients marked overseas, inactive or deceased** — must be filtered
- **~5% with no visit history** — registered but never attended
- **455 abandoned treatments** — started, never completed. Higher clinical priority
  than a routine hygiene recall
- **Inconsistent name casing** — some ALL CAPS
- **Notes containing contact constraints** — "do not call during work hours",
  "contact via spouse", "interpreter required"

## Suggested prioritisation signals

Available in the data if your ranking logic wants them: `clinical_urgency` on
treatments, `base_urgency` on recalls, days overdue (`next_due_date` vs today),
abandoned treatment status, no-show history in appointments, and prior contact
outcomes in `contact_log` (a patient who already ignored three messages is a
different case from one never contacted).

## PDPA note

`marketing_consent` is separate from `whatsapp_consent` on purpose. Recall reminders
are permitted as service messages; anything promotional is not. Keeping the two flags
distinct lets you show that separation in the demo.

## Regenerating

`generate.py` is included. Change `N` for a different patient count, or the seed for
different data.
