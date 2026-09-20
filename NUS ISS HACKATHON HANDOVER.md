# Handover: Dental Patient Follow-Up Agent — demo build

Read this whole file before planning. Then read the existing repo (below) before
writing any code. Several design decisions here are already settled with the team —
do not relitigate them, but do flag it if the existing code contradicts them.

---

## 1. Context

**Event:** NUS ISS "Show Me Your Agent" hackathon. Team of 2. Roughly two weeks
remaining, part-time alongside full-time jobs.

**Fixed problem statement (cannot be changed):**

> Dental clinics maintain regular follow-up schedules for routine examinations,
> preventive care, and ongoing treatments. Staff manually review patient records to
> identify overdue appointments before contacting patients individually. As the
> patient database grows, maintaining consistent follow-up becomes increasingly
> difficult, resulting in missed appointments and delayed treatments.

**What we are judged on:** a working agent with sound guardrails, demonstrated live.
Not market novelty. The demo is the deliverable.

**Our differentiator (drives priority order):** existing dental recall software
broadcasts — it fires a reminder sequence and stops at "sent". Our agent holds a
conversation: it reads the patient's reply, interprets it, and either resolves the
case or escalates. The one-line pitch is *"existing tools send messages; ours
finishes the job."*

---

## 2. What already exists

### 2a. Teammate's repo — the eligibility engine

https://github.com/Zhengbin99368/Show-me-your-agent---4gents

**Read `README.md`, `technical-blueprint.md`, and all of `followup_agent/` before
planning.** The blueprint is long (~500 lines) and unusually rigorous — treat it as
the authoritative safety spec.

Summary of what it does today (Milestone 1, read-only):
- `followup_agent/domain.py` — strict parsing of fictional input, domain types
- `followup_agent/eligibility.py` — deterministic, side-effect-free eligibility rules
- `followup_agent/cli.py` — read-only JSON CLI
- Reads `fixtures/fictional/follow_up_cases.json`, prints per-requirement JSON:
  `is_overdue`, `may_send_reminder`, `reason_codes`, `source_versions`
- Python 3.11+, standard library only, no third-party packages
- Sends nothing, ranks nothing, uses no LLM

Key rules it encodes (preserve these):
- `is_overdue` is strict: `approved_due_date < clinic_today`. Due today is NOT overdue.
- Eligibility is per *follow-up requirement*, not per patient. One patient can have several.
- A confirmed future appointment suppresses outreach only for *explicitly linked* requirements.
- Policy engine authorises execution, never the LLM.
- Silence is not consent, refusal, or delivery evidence.

**Constraint: do not rewrite his modules.** Build alongside them. If something in his
code genuinely blocks progress, raise it in the plan rather than editing around it.

### 2b. Synthetic dataset

`dental_dataset/` — 3,255 fictional patients, ~1,188 currently overdue (36%).
Reference date **8 September 2026**. Both CSV and SQLite (`clinic.db`), plus
`generate.py` (seeded, reproducible) and a README documenting the schema.

Tables: `patients`, `appointments`, `treatments`, `recall_schedule`, `contact_log`.

**Deliberate data quality problems — handling these correctly is part of the demo:**
- 201 patients with no phone number
- inconsistent phone formats (missing `+65`, two numbers in one field)
- ~18% missing email
- 371 patients without WhatsApp consent
- ~3.5% opted out of all contact
- 55 duplicate patient records (same person, name casing/spacing differs)
- patients flagged `overseas`, `inactive`, `deceased`
- ~5% with no visit history
- 455 abandoned treatments (started, never completed)
- notes containing contact constraints ("do not call during work hours",
  "contact via spouse", "interpreter required")

`marketing_consent` is deliberately separate from `whatsapp_consent` — see §6.

---

## 3. Architecture decisions already made

These were argued through with the team. Build to them.

1. **Trigger is a scheduled scan, not user input.** The agent wakes on a schedule and
   scans the patient database. A human prompting it would rebuild the manual process
   we are replacing. Inbound patient replies are a *second* entry point, not the first.

2. **Risk classification is a deterministic rule table, not an LLM judgement.** The
   clinic owns the rules. The LLM drafts and interprets; rules decide who approves.

3. **Verification happens BEFORE the send, not after.** Checking that a message sent
   is an API status code. The valuable check is on the draft — right patient, right
   treatment context, no clinical claims.

4. **Retries are capped at 2 attempts, then escalate to staff** with the failure
   reason. No unbounded loops.

5. **The agent never makes clinical judgements.** It manages the administrative
   workflow around clinical care. No diagnosis, no treatment recommendation, no
   setting of recall intervals.

6. **Ranking is a separate module from eligibility.** Eligibility answers "may we
   contact this person?" per requirement. Ranking answers "of everyone eligible, who
   matters most?" across the set. They meet at the eligibility engine's JSON output —
   that is the integration contract. This keeps the two workstreams from colliding.

### Agent loop

```
Scheduled scan
  → detect and rank (dental-specific prioritisation)
  → draft message
  → pre-send check
  → risk rules → low: auto-send | high: staff approval queue
  → send (mocked WhatsApp)
  → patient replies
  → agent interprets → update record or escalate (2 attempts, then staff)
  → unresolved cases return to the next scan
```

---

## 4. Build list

Ordered. Items 1–3 unblock everything else.

| # | Component | What it must do | Est. |
|---|---|---|---|
| 1 | CSV → JSON bridge | Convert `dental_dataset` into the shape `followup_agent` consumes, so the eligibility engine runs against 3,255 patients instead of hand-written fixtures. Read his `domain.py` for the exact expected schema. | 0.5d |
| 2 | Ranking module | Take everything eligible and order it. Dental-specific: abandoned treatments and post-surgical review outrank routine hygiene; longer overdue outranks recently due; prior no-shows and ignored contacts adjust score. Output an ordered worklist with a written reason per patient. Rule-based scoring, NO LLM — fast, auditable, testable. | 1d |
| 3 | Answer key + tests | 30 hand-picked patients from the dataset with the correct expected outcome (top priority / contact normally / exclude entirely / escalate). Ranking must be tested against it. Without this, correctness is unfalsifiable. | 0.5d |
| 4 | Risk rule table | An explicit, readable config file: conditions → outcome (auto-send / staff approval / exclude). Must cover: no consent, opted out, deceased/overseas/inactive, no valid phone, duplicate record, abandoned treatment, post-surgical review, >24 months overdue, marketing-vs-care boundary. This file IS the safety story — a judge should be able to read it. | 0.5d |
| 5 | Message drafting | LLM writes a personalised message per patient using real treatment context. Must never make clinical claims or give advice. Falls back to an approved template if the LLM call fails. | 1d |
| 6 | Mock WhatsApp panel | Single browser page, split view. Left: clinic dashboard. Right: WhatsApp-style chat (green bubbles, patient name header). Operator can type as the patient. Replaces live Twilio for the demo — see §5. | 1d |
| 7 | Reply understanding | **Highest-value component. Do not cut.** Agent interprets free-text replies into intents: reschedule request, defer with date ("I'm overseas till March"), defer indefinite, opt-out, question, complaint, unparseable. Acts accordingly — update record, schedule retry, or escalate. Anything not confidently classified escalates to a human. | 1.5d |
| 8 | Dashboard | Ranked worklist, agent decisions with reasons, pending staff approvals, resolved count, escalation queue. This is what the judge actually looks at. | 1.5d |
| 9 | Demo script | Written five-minute run-through: which patient, what the operator types, what appears on screen when. Plus a seeded reset so it runs identically twice. | 0.5d |

**Deliberately cut (mention on a slide, do not build):** live Twilio/WhatsApp Business
API integration, ROI metrics computation (estimate on a slide instead), new-patient
intake, nurse scheduling, real PMS integration, clinic onboarding / column mapping.

---

## 5. Demo constraints

The demo runs offline on one laptop. No dependency on venue wifi for the core flow.

**The 40-second moment the whole pitch rests on:**
1. Agent picks the top-ranked patient from 3,255
2. Draft appears, showing the reasoning that selected them
3. Message appears in the chat panel as an outgoing bubble
4. Operator types as the patient: *"I'm in Australia until March"*
5. Agent interprets it, updates the record, schedules a retry for March
6. That row leaves the urgent list

Also worth showing: a case the agent *refuses* to contact (deceased / no consent /
duplicate), and a case it escalates to staff. The refusals are as impressive as the sends.

If live LLM calls are a risk on the day, include a recorded/cached-response mode that
replays known-good outputs for the scripted demo patients.

---

## 6. Regulatory context (Singapore) — shapes the rules, not just the slides

- Under PDPC/MOH healthcare advisory guidelines, appointment and recall reminders are
  generally **not** "specified messages" and fall outside Do Not Call provisions. Any
  promotional content flips them into regulated marketing.
- The Singapore Dental Council ethical code prohibits soliciting patients. Frame
  everything as continuity of care for existing patients. Never prospecting.
- This is why `marketing_consent` and `whatsapp_consent` are separate fields — the
  care/marketing boundary must be enforced in the risk rules, and visible in the demo.
- A third-party vendor processing clinic data is a PDPA "data intermediary"; the
  clinic stays accountable. Design implication: log every action, minimise data held.

---

## 7. Working style

- **Python 3.11+**, matching the existing repo. Keep third-party dependencies minimal
  and justify any you add. His modules use the standard library only.
- Work on a **feature branch**, not `main`. Teammate is actively committing.
- **Tests alongside code**, using the existing `tests/` structure and `unittest`.
- Fictional data only. No secrets in the repo, no `.env` committed.
- Small, reviewable commits with clear messages.

**Please flag rather than silently resolve:** any conflict between this brief and the
teammate's blueprint; any need to modify his existing modules; any component that
looks like it will overrun its estimate.

---

## 8. Known open questions

Raise these in the plan; don't guess:

1. His blueprint has 4 milestones with the working demo at Milestone 3. We have ~2
   weeks. How much of Milestones 2–3 is reachable, and what is the minimum that still
   demos?
2. His eligibility engine reads hand-written fixtures. Does the bridge convert the
   dataset to his format, or should the ranking module call his eligibility check
   per patient directly? Prefer whichever requires fewer changes to his code.
3. Blueprint §18 leaves language, storage, LLM provider and deployment undecided. For
   demo purposes propose the simplest thing that works and note it as a demo-only choice.
4. Prioritisation does not currently exist anywhere in his design — eligibility is a
   yes/no gate. Confirm ranking is genuinely unbuilt before building it.

---

## 9. First action

**Start in plan mode.** Read the repo and the dataset README, then produce a plan
covering: what you'll build, in what order, what you found in the existing code that
changes the approach, and answers to §8. Do not write code until the plan is approved.
