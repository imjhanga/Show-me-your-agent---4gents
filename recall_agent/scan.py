"""The scheduled scan: the agent's primary trigger.

The agent wakes on a schedule and scans the database itself. A human prompting
it per patient would rebuild the manual process this replaces, so patient input
is a second entry point, never the first.

One scan is: bridge the dataset, evaluate eligibility per requirement, apply the
risk rule table, rank whatever survives, and write the result. It sends nothing
— that is the sending layer's job, working from this output.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from followup_agent.domain import parse_cases
from followup_agent.eligibility import evaluate_case

from . import bridge
from .audit import DEFAULT_LOG, AuditLog
from .clock import DEFAULT_CLINIC_NOW, parse_clinic_now
from .ranking import RankingWeights, rank
from .risk import EXCLUDE, RuleTable, facts_for

DEFAULT_SCAN = Path("runs/scan.json")


@dataclass(frozen=True)
class ScanResult:
    clinic_now: str
    policy_versions: dict[str, str]
    totals: dict[str, int]
    worklist: list[dict[str, Any]]
    refusals: list[dict[str, Any]]
    refusal_summary: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "clinic_now": self.clinic_now,
            "policy_versions": self.policy_versions,
            "totals": self.totals,
            "worklist": self.worklist,
            "refusals": self.refusals,
            "refusal_summary": self.refusal_summary,
        }


def run_scan(
    db_path: Path = bridge.DEFAULT_DB,
    clinic_now: datetime | None = None,
    rules: RuleTable | None = None,
    weights: RankingWeights | None = None,
    audit: AuditLog | None = None,
) -> ScanResult:
    clinic_now = clinic_now or DEFAULT_CLINIC_NOW
    rules = rules or RuleTable.load()
    weights = weights or RankingWeights.load()

    document = bridge.build_document(db_path, clinic_now)
    cases = document["cases"]
    by_id = {case["case_id"]: case for case in cases}

    results = {
        result.case_id: result.to_dict()
        for result in (evaluate_case(case) for case in parse_cases(document))
    }
    decisions = {
        case_id: rules.decide(facts_for(case, results[case_id]))
        for case_id, case in by_id.items()
    }

    entries = rank(cases, results, decisions, weights)

    refusals: list[dict[str, Any]] = []
    for case_id, decision in decisions.items():
        if decision.outcome != EXCLUDE:
            continue
        context = by_id[case_id]["clinic_context"]
        refusals.append(
            {
                "case_id": case_id,
                "patient_id": by_id[case_id]["patient"]["patient_id"],
                "patient_name": context.get("full_name", ""),
                "requirement_kind": context["requirement_kind"],
                "rule_id": decision.rule_id,
                "reason": decision.reason,
                "citation": decision.citation,
                "eligibility_reason_codes": results[case_id]["reason_codes"],
            }
        )

    refusal_counts = Counter(row["rule_id"] for row in refusals)
    refusal_summary = [
        {
            "rule_id": rule_id,
            "count": count,
            "reason": next(r["reason"] for r in refusals if r["rule_id"] == rule_id),
        }
        for rule_id, count in refusal_counts.most_common()
    ]

    outcomes = Counter(decision.outcome for decision in decisions.values())
    totals = {
        "requirements_scanned": len(cases),
        "patients_scanned": len({c["patient"]["patient_id"] for c in cases}),
        "overdue": sum(1 for r in results.values() if r["is_overdue"]),
        "eligible": sum(1 for r in results.values() if r["may_send_reminder"]),
        "worklist": len(entries),
        "auto_send": outcomes.get("AUTO_SEND", 0),
        "staff_approval": outcomes.get("STAFF_APPROVAL", 0),
        "excluded": outcomes.get(EXCLUDE, 0),
    }

    result = ScanResult(
        clinic_now=clinic_now.isoformat(),
        policy_versions={
            "risk_rules": rules.version,
            "ranking_weights": "1.0",
            "eligibility_engine": "followup_agent milestone 1",
        },
        totals=totals,
        worklist=[entry.to_dict() for entry in entries],
        refusals=refusals,
        refusal_summary=refusal_summary,
    )

    if audit is not None:
        audit.append(
            "scan.completed",
            clinic_now=result.clinic_now,
            policy_versions=result.policy_versions,
            totals=totals,
        )
    return result


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run one scheduled follow-up scan. Sends nothing."
    )
    parser.add_argument("--db", type=Path, default=bridge.DEFAULT_DB)
    parser.add_argument("--out", type=Path, default=DEFAULT_SCAN)
    parser.add_argument("--clinic-now", default=None)
    parser.add_argument("--rules", type=Path, default=None)
    parser.add_argument("--weights", type=Path, default=None)
    parser.add_argument("--audit", type=Path, default=DEFAULT_LOG)
    parser.add_argument(
        "--top", type=int, default=10, help="How many worklist rows to print"
    )
    args = parser.parse_args(argv)

    result = run_scan(
        db_path=args.db,
        clinic_now=parse_clinic_now(args.clinic_now),
        rules=RuleTable.load(args.rules) if args.rules else None,
        weights=RankingWeights.load(args.weights) if args.weights else None,
        audit=AuditLog(args.audit),
    )

    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        json.dump(result.to_dict(), handle, indent=2)

    totals = result.totals
    print(
        f"Scanned {totals['requirements_scanned']} follow-up requirements across "
        f"{totals['patients_scanned']} patients as at {result.clinic_now}."
    )
    print(
        f"  {totals['overdue']} overdue, {totals['eligible']} passed eligibility, "
        f"{totals['worklist']} on the worklist."
    )
    print(
        f"  {totals['auto_send']} may send automatically, "
        f"{totals['staff_approval']} need a human, {totals['excluded']} refused."
    )
    if result.refusal_summary:
        print("\nWhy we are not contacting people:")
        for row in result.refusal_summary:
            print(f"  {row['count']:>5}  {row['rule_id']}")
    if args.top:
        print(f"\nTop {args.top} of the worklist:")
        for entry in result.worklist[: args.top]:
            print(
                f"  #{entry['rank']:<3} {entry['score']:>6}  "
                f"{entry['patient_name'][:22]:22} {entry['requirement_label'][:30]:30} "
                f"{entry['risk_outcome']}"
            )
            print(f"        {entry['why']}")
    print(f"\nWrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
