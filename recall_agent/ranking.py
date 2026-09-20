"""Rule-based prioritisation of everyone the clinic may contact.

Eligibility is a per-requirement yes/no gate. Ranking is the ordering across the
whole set, which does not exist anywhere in the eligibility engine and is a
separate concern by design: the engine never reads treatment type or urgency.

Scoring is deterministic and additive so the worklist is reproducible and every
position can be explained in a sentence. No LLM is involved.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .risk import EXCLUDE, RiskDecision

DEFAULT_WEIGHTS = Path("config/ranking_weights.toml")


class RankingConfigError(ValueError):
    """Raised when the weights file cannot be loaded safely."""


@dataclass(frozen=True)
class ScoreComponent:
    points: float
    reason: str


@dataclass(frozen=True)
class RankedEntry:
    rank: int
    case_id: str
    patient_id: str
    patient_name: str
    requirement_kind: str
    requirement_label: str
    days_overdue: int
    score: float
    components: tuple[ScoreComponent, ...]
    risk_outcome: str
    risk_rule_id: str
    risk_reason: str
    eligibility_reason_codes: tuple[str, ...] = field(default=())

    @property
    def why(self) -> str:
        """One sentence explaining this position, for the dashboard."""

        parts = [c.reason for c in self.components if c.points]
        if not parts:
            return "No prioritising factors; ordered by due date."
        return "; ".join(parts) + "."

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "case_id": self.case_id,
            "patient_id": self.patient_id,
            "patient_name": self.patient_name,
            "requirement_kind": self.requirement_kind,
            "requirement_label": self.requirement_label,
            "days_overdue": self.days_overdue,
            "score": round(self.score, 2),
            "why": self.why,
            "components": [
                {"points": round(c.points, 2), "reason": c.reason}
                for c in self.components
            ],
            "risk_outcome": self.risk_outcome,
            "risk_rule_id": self.risk_rule_id,
            "risk_reason": self.risk_reason,
            "eligibility_reason_codes": list(self.eligibility_reason_codes),
        }


@dataclass(frozen=True)
class OverdueBand:
    points: float
    label: str
    up_to_days: int | None


class RankingWeights:
    def __init__(self, document: Mapping[str, Any]) -> None:
        self.urgency_points = float(
            document.get("urgency", {}).get("points_per_level", 0.0)
        )
        self.treatment_state = {
            str(k): float(v) for k, v in document.get("treatment_state", {}).items()
        }
        staleness = document.get("treatment_staleness", {})
        self.staleness_per_year = float(staleness.get("points_per_year", 0.0))
        self.staleness_max = float(staleness.get("max_points", 0.0))

        bands: list[OverdueBand] = []
        for entry in document.get("overdue_band", []):
            bands.append(
                OverdueBand(
                    points=float(entry.get("points", 0.0)),
                    label=str(entry.get("label", "")),
                    up_to_days=entry.get("up_to_days"),
                )
            )
        if not bands:
            raise RankingConfigError("at least one overdue_band is required")
        if bands[-1].up_to_days is not None:
            raise RankingConfigError(
                "the final overdue_band must be unbounded (omit up_to_days)"
            )
        bounded = [b.up_to_days for b in bands[:-1]]
        if any(value is None for value in bounded):
            raise RankingConfigError("only the final overdue_band may omit up_to_days")
        if bounded != sorted(bounded):
            raise RankingConfigError("overdue_band entries must ascend by up_to_days")
        self.overdue_bands = tuple(bands)

        history = document.get("history", {})
        self.no_show_each = float(history.get("no_show_points_each", 0.0))
        self.no_show_max = float(history.get("no_show_max_points", 0.0))
        self.unanswered_each = float(history.get("unanswered_points_each", 0.0))
        self.unanswered_min = float(history.get("unanswered_min_points", 0.0))
        self.never_attended = float(history.get("never_attended_points", 0.0))

    @classmethod
    def load(cls, path: Path = DEFAULT_WEIGHTS) -> RankingWeights:
        try:
            with path.open("rb") as handle:
                return cls(tomllib.load(handle))
        except OSError as error:
            raise RankingConfigError(f"cannot read ranking weights: {error}") from error
        except tomllib.TOMLDecodeError as error:
            raise RankingConfigError(f"ranking weights are not valid TOML: {error}") from error

    def band_for(self, days_overdue: int) -> OverdueBand:
        for band in self.overdue_bands:
            if band.up_to_days is not None and days_overdue <= band.up_to_days:
                return band
        return self.overdue_bands[-1]


def _requirement_label(context: Mapping[str, Any]) -> str:
    if context["requirement_kind"] == "TREATMENT":
        treatment = str(context.get("treatment_type", "treatment")).replace("_", " ")
        status = str(context.get("treatment_status", "")).replace("_", " ")
        return f"{treatment} ({status})"
    return str(context.get("recall_type", "recall")).replace("_", " ")


def score_case(
    case: Mapping[str, Any], weights: RankingWeights
) -> tuple[float, tuple[ScoreComponent, ...]]:
    """Score one bridged case, returning the total and its explained parts."""

    context = case["clinic_context"]
    components: list[ScoreComponent] = []

    urgency = int(
        context.get("clinical_urgency") or context.get("base_urgency") or 0
    )
    if urgency and weights.urgency_points:
        components.append(
            ScoreComponent(
                urgency * weights.urgency_points,
                f"clinical urgency {urgency} of 5",
            )
        )

    if context["requirement_kind"] == "TREATMENT":
        status = str(context.get("treatment_status", ""))
        state_points = weights.treatment_state.get(status, 0.0)
        if state_points:
            treatment = str(context.get("treatment_type", "treatment")).replace("_", " ")
            phrase = (
                f"{treatment} was started and never completed"
                if status == "abandoned"
                else f"{treatment} still in progress"
            )
            components.append(ScoreComponent(state_points, phrase))

        age_years = int(context.get("treatment_age_days", 0)) / 365.25
        staleness = min(age_years * weights.staleness_per_year, weights.staleness_max)
        if staleness >= 1:
            components.append(
                ScoreComponent(staleness, f"stalled for {age_years:.0f} years")
            )

    days_overdue = int(context.get("days_overdue", 0))
    band = weights.band_for(days_overdue)
    if band.points:
        components.append(ScoreComponent(band.points, band.label))

    appointments = context.get("appointment_history", {})
    no_shows = int(appointments.get("no_show", 0))
    if no_shows and weights.no_show_each:
        points = min(no_shows * weights.no_show_each, weights.no_show_max)
        components.append(
            ScoreComponent(
                points,
                f"{no_shows} missed appointment{'s' if no_shows > 1 else ''}",
            )
        )

    unanswered = int(case["task"].get("contact_attempts", 0))
    if unanswered and weights.unanswered_each:
        points = max(unanswered * weights.unanswered_each, weights.unanswered_min)
        components.append(
            ScoreComponent(
                points,
                f"{unanswered} earlier message{'s' if unanswered > 1 else ''} went unanswered",
            )
        )

    if not appointments.get("completed") and weights.never_attended:
        # Phrased as a record fact: the dataset does not link treatments to
        # appointments, so someone with an abandoned treatment can still have no
        # completed appointment row.
        components.append(
            ScoreComponent(weights.never_attended, "no completed visit on record")
        )

    total = sum(component.points for component in components)
    return total, tuple(components)


def rank(
    cases: Iterable[Mapping[str, Any]],
    results: Mapping[str, Mapping[str, Any]],
    decisions: Mapping[str, RiskDecision],
    weights: RankingWeights | None = None,
    include_excluded: bool = False,
) -> list[RankedEntry]:
    """Order every contactable case, highest priority first.

    Excluded cases are dropped: ranking answers "who first?" among people we are
    allowed to contact, and ordering people we have refused to contact would be
    meaningless. Pass ``include_excluded`` to score them anyway for reporting.
    """

    weights = weights or RankingWeights.load()
    scored: list[tuple[float, int, str, RankedEntry]] = []

    for case in cases:
        case_id = case["case_id"]
        decision = decisions[case_id]
        if decision.outcome == EXCLUDE and not include_excluded:
            continue
        context = case["clinic_context"]
        result = results.get(case_id, {})
        total, components = score_case(case, weights)
        days_overdue = int(context.get("days_overdue", 0))
        entry = RankedEntry(
            rank=0,
            case_id=case_id,
            patient_id=case["patient"]["patient_id"],
            patient_name=str(context.get("full_name", "")),
            requirement_kind=context["requirement_kind"],
            requirement_label=_requirement_label(context),
            days_overdue=days_overdue,
            score=total,
            components=components,
            risk_outcome=decision.outcome,
            risk_rule_id=decision.rule_id,
            risk_reason=decision.reason,
            eligibility_reason_codes=tuple(result.get("reason_codes", ())),
        )
        # Negated so that a plain ascending sort puts the highest score first,
        # while case_id still breaks ties alphabetically.
        scored.append((-total, -days_overdue, case_id, entry))

    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    ordered: list[RankedEntry] = []
    for position, (_, _, _, entry) in enumerate(scored, start=1):
        ordered.append(
            RankedEntry(
                rank=position,
                case_id=entry.case_id,
                patient_id=entry.patient_id,
                patient_name=entry.patient_name,
                requirement_kind=entry.requirement_kind,
                requirement_label=entry.requirement_label,
                days_overdue=entry.days_overdue,
                score=entry.score,
                components=entry.components,
                risk_outcome=entry.risk_outcome,
                risk_rule_id=entry.risk_rule_id,
                risk_reason=entry.risk_reason,
                eligibility_reason_codes=entry.eligibility_reason_codes,
            )
        )
    return ordered


def worklist(entries: Sequence[RankedEntry], limit: int | None = None) -> list[dict[str, Any]]:
    selected = entries[:limit] if limit else entries
    return [entry.to_dict() for entry in selected]
