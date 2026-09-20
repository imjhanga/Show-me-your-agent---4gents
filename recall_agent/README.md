# recall_agent

Ranking, risk policy, drafting, reply handling and the demo surface. Sits
alongside `followup_agent` and never modifies it.

`followup_agent` answers *"may we contact this person for this requirement?"*.
Everything here answers *"of everyone we may contact, who matters most, what do
we say, and what do we do with the reply?"*.

Fictional data only. No real patient records, no live messaging provider.

## Running it

```bash
python -m recall_agent.server --offline
```

Then open <http://127.0.0.1:8000/> and follow [DEMO.md](../DEMO.md).

Other entry points:

```bash
python -m recall_agent.prewarm --top 25  # fill the response cache (real API calls)
python -m recall_agent.scan              # one scheduled scan, prints a summary
python -m recall_agent.bridge --stats    # dataset -> eligibility JSON only
```

Useful flags: `--offline` replays cached model responses and never opens a
socket; `--no-llm` uses approved templates and the rule-based classifier only;
`--clinic-now` moves the evaluation instant.

## Dependencies

None, with one platform caveat.

**On Windows you must `pip install tzdata`.** This is not a new dependency —
`followup_agent.eligibility` calls `ZoneInfo("Asia/Singapore")`, and Windows
ships no IANA timezone database, so without it the *existing* test suite fails
17 of 18 tests with `unknown clinic timezone: Asia/Singapore`. `tzdata` is a
pure-data package and is the solution CPython documents. Worth a line in the
top-level README, since the demo laptop is a Windows machine.

## Modules

| Module | Does |
|---|---|
| `clock` | Pins evaluation to the dataset's reference date so runs are reproducible |
| `bridge` | Converts `dental_dataset` into the document `followup_agent` parses |
| `risk` | Loads and applies `config/risk_rules.toml` |
| `ranking` | Scores and orders everything contactable |
| `scan` | The scheduled trigger: bridge, evaluate, gate, rank, write |
| `drafting` | Writes a message, then checks it before it can be sent |
| `replies` | Interprets a patient reply and decides the next action |
| `conversation` | Workflow state per requirement |
| `llm` | Anthropic client over `urllib`, with a replay cache |
| `prewarm` | Fills that cache from the scripted demo inputs |
| `audit` | Append-only log of decisions, never of content |
| `server` | Local dashboard and mock WhatsApp panel |

## How it joins onto the eligibility engine

`bridge` emits one JSON document. `followup_agent.domain.parse_cases` pulls the
keys it knows and ignores the rest, so the same document carries a
`clinic_context` block for the modules here. Nothing in `followup_agent` is
imported into, subclassed from, or patched.

`scan` calls `evaluate_case()` in process rather than shelling out to the CLI:
it is a pure function, and a subprocess per case across 5,029 cases would be
needlessly slow.

## Two decisions worth knowing about

**The risk table is a separate gate, not an extension of the reason codes.**
The eligibility engine models a patient as consent plus contact validity. It has
no field for deceased, overseas, inactive, opted-out, duplicate, or the
care/marketing boundary. Folding those into `consent.granted` would make the
engine report `MESSAGING_CONSENT_NOT_CURRENT` for a deceased patient, which is
false — and false on the one screen where refusals are the selling point. So
they live in `config/risk_rules.toml`, and a case is contacted only when the
engine permits it *and* the table authorises it.

Patient `R000015` is the regression test: the engine returns
`OVERDUE_AND_CONTACT_ELIGIBLE` for a deceased patient, and only the risk table
stops the message.

**Treatment follow-ups are due `start_date + 30 days` by administrative
convention.** The dataset has no due date for an incomplete treatment. Thirty
days is a convention about chasing paperwork, not a clinical recall interval —
the agent sets no clinical intervals. It is surfaced per case as
`clinic_context.due_date_basis` rather than buried, and the team should confirm
it.

## Demo-only choices

Blueprint §18 leaves language, storage, LLM provider and deployment open. For
the demo: Python standard library only; `clinic.db` read read-only and run state
written as JSON under `runs/`; Claude via `urllib` with a disk cache; stdlib
`http.server`; no job runner, the scheduled scan is a single invocation. The
server binds to localhost, serves fictional data and has no authentication, so
it must not be exposed to a network.

`fixtures/demo/llm_cache/` is committed on purpose. It holds model responses for
the scripted demo inputs so `--offline` works from any clone with no key and no
network. Refresh it with `python -m recall_agent.prewarm`. A cache miss falls
back to an approved template silently, so if the draft panel reads `template:`
rather than `llm_cache`, the cache is cold for that patient.

## Tests

```bash
python -m unittest discover -s tests -v
```

`tests/test_answer_key.py` is the one that matters most: thirty patients picked
from the dataset by hand with the outcome *and the reason* a human reviewer says
is correct, written independently of the code.
