from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from .career_progress import CareerTarget, EmployeeProgress, SkillProgress, SkillProgressService
from .data_loader import DatasetBundle
from .models import Employee, Event, HistoryRecord, HistoryStatus


SCORING_WEIGHTS: dict[str, float] = {
    "critical_gap_closure": 12.0,
    "gap_closure": 4.0,
    "goal_relevance": 2.5,
    "positive_history": 1.5,
    "friction_no_show": -1.25,
    "friction_dropped": -1.0,
    "friction_declined": -0.75,
    "format_fit": -0.75,
    "effort_per_hour": -0.04,
    "diversity_new_gap": 1.0,
}

RECURRING_EVENT_ID = "EV_036"


class RecommendationFactor(BaseModel):
    code: str
    contribution: float
    detail: str


class ExpectedSkillEffect(BaseModel):
    skill_id: str
    name: str
    from_level: int
    to_level: int
    target_level: int
    critical: bool


class Recommendation(BaseModel):
    event_id: str
    title: str
    type: str
    format: str
    duration_hours: float
    next_session: date | None
    match_label: Literal["Strong match", "Good match", "Exploratory"]
    score: float
    expected_effect: list[ExpectedSkillEffect]
    factors: list[RecommendationFactor]
    explanation: str


class RequiredEvent(BaseModel):
    event_id: str
    title: str
    status: Literal["overdue", "in_progress", "not_started"]
    due_date: date | None


class RejectedEvent(BaseModel):
    event_id: str
    reason_code: Literal[
        "MANDATORY",
        "AUDIENCE",
        "ALREADY_COMPLETED",
        "IN_PROGRESS",
        "PREREQUISITES",
        "NO_RELEVANT_GAP",
        "MAX_LEVEL_REACHED",
        "NOT_AVAILABLE",
    ]
    detail: str


class UnclosedGap(BaseModel):
    skill_id: str
    name: str
    effective_level: int
    target_level: int
    gap: int
    critical: bool


class EmployeeRecommendations(BaseModel):
    employee_id: str
    as_of_date: date
    target: CareerTarget
    status: Literal["OK", "NO_SUITABLE_ACTION"]
    recommendations: list[Recommendation] = Field(max_length=3)
    required: list[RequiredEvent]
    unclosed_gaps: list[UnclosedGap]
    rejected: list[RejectedEvent] | None = None


@dataclass(frozen=True)
class _Candidate:
    event: Event
    next_session: date | None
    expected_effect: tuple[ExpectedSkillEffect, ...]
    factors: tuple[RecommendationFactor, ...]
    base_score: float

    @property
    def covered_gap_ids(self) -> set[str]:
        return {effect.skill_id for effect in self.expected_effect}


class RecommendationEngine:
    """Deterministic recommendation engine over one immutable dataset snapshot."""

    def __init__(self, dataset: DatasetBundle) -> None:
        self.dataset = dataset
        self.progress_service = SkillProgressService(dataset)
        self.as_of_date = dataset.skills.meta.as_of_date

    def recommend(self, employee_id: str, *, debug: bool = False) -> EmployeeRecommendations:
        employee = self.dataset.indexes.employees_by_id[employee_id]
        progress = self.progress_service.calculate(employee_id)
        history = self.dataset.indexes.history_by_employee.get(employee_id, ())
        effective_levels = {item.skill_id: item.effective_level for item in progress.skills}
        gaps = {item.skill_id: item for item in progress.gaps}
        rejected: list[RejectedEvent] = []
        candidates: list[_Candidate] = []

        for event in self.dataset.events.events:
            rejection, candidate = self._evaluate_event(
                employee=employee,
                progress=progress,
                history=history,
                event=event,
                effective_levels=effective_levels,
                gaps=gaps,
            )
            if rejection is not None:
                rejected.append(rejection)
            elif candidate is not None:
                candidates.append(candidate)

        recommendations = self._select_diverse(candidates, employee, progress)
        status: Literal["OK", "NO_SUITABLE_ACTION"] = (
            "OK" if recommendations else "NO_SUITABLE_ACTION"
        )
        unclosed_gaps = [
            UnclosedGap(
                skill_id=item.skill_id,
                name=item.name,
                effective_level=item.effective_level,
                target_level=item.target_level,
                gap=item.gap,
                critical=item.critical,
            )
            for item in progress.gaps
        ]
        return EmployeeRecommendations(
            employee_id=employee_id,
            as_of_date=self.as_of_date,
            target=progress.target,
            status=status,
            recommendations=recommendations,
            required=self._required_events(employee, history),
            unclosed_gaps=unclosed_gaps,
            rejected=rejected if debug else None,
        )

    def _evaluate_event(
        self,
        *,
        employee: Employee,
        progress: EmployeeProgress,
        history: tuple[HistoryRecord, ...],
        event: Event,
        effective_levels: dict[str, int],
        gaps: dict[str, SkillProgress],
    ) -> tuple[RejectedEvent | None, _Candidate | None]:
        event_history = tuple(record for record in history if record.event_id == event.event_id)

        if event.mandatory:
            return self._reject(event, "MANDATORY", "Mandatory events belong in required, not development recommendations"), None

        role_match = employee.role in event.target_roles or progress.target.role in event.target_roles
        grade_match = (
            employee.grade in event.target_grades or progress.target.grade in event.target_grades
        )
        if not (role_match and grade_match):
            return self._reject(
                event,
                "AUDIENCE",
                f"roles={event.target_roles}, grades={[grade.value for grade in event.target_grades]}; "
                f"employee={employee.role}/{employee.grade.value}, target={progress.target.role}/{progress.target.grade.value}",
            ), None

        if event.event_id != RECURRING_EVENT_ID and any(
            record.status == HistoryStatus.COMPLETED for record in event_history
        ):
            return self._reject(event, "ALREADY_COMPLETED", "A completed record already exists for this non-recurring event"), None

        if any(record.status == HistoryStatus.IN_PROGRESS for record in event_history):
            return self._reject(event, "IN_PROGRESS", "The employee already has this event in progress"), None

        unmet = {
            skill_id: {"effective": effective_levels.get(skill_id, 0), "required": required}
            for skill_id, required in event.prerequisites.items()
            if effective_levels.get(skill_id, 0) < required
        }
        if unmet:
            return self._reject(event, "PREREQUISITES", f"Unmet prerequisites: {unmet}"), None

        relevant_developments = [
            development for development in event.develops_skills if development.skill_id in gaps
        ]
        if not relevant_developments:
            return self._reject(event, "NO_RELEVANT_GAP", "The event develops none of the target's unclosed skill gaps"), None

        useful_developments = [
            development
            for development in relevant_developments
            if effective_levels.get(development.skill_id, 0) < development.max_level
        ]
        if not useful_developments:
            levels = {
                development.skill_id: {
                    "effective": effective_levels.get(development.skill_id, 0),
                    "max_level": development.max_level,
                }
                for development in relevant_developments
            }
            return self._reject(event, "MAX_LEVEL_REACHED", f"Event cannot improve its relevant gap skills: {levels}"), None

        next_session = None
        if event.format != "self_paced":
            future_sessions = sorted(
                session for session in event.upcoming_sessions if session >= self.as_of_date
            )
            if not future_sessions:
                return self._reject(
                    event,
                    "NOT_AVAILABLE",
                    f"No session on or after dataset as_of_date {self.as_of_date.isoformat()}",
                ), None
            next_session = future_sessions[0]

        effects = self._expected_effects(event, progress, effective_levels)
        factors = self._score_factors(event, employee, progress, history, effects)
        return None, _Candidate(
            event=event,
            next_session=next_session,
            expected_effect=tuple(effects),
            factors=tuple(factors),
            base_score=sum(item.contribution for item in factors),
        )

    @staticmethod
    def _reject(event: Event, reason_code: str, detail: str) -> RejectedEvent:
        return RejectedEvent(event_id=event.event_id, reason_code=reason_code, detail=detail)

    def _expected_effects(
        self,
        event: Event,
        progress: EmployeeProgress,
        effective_levels: dict[str, int],
    ) -> list[ExpectedSkillEffect]:
        gaps = {item.skill_id: item for item in progress.gaps}
        effects: list[ExpectedSkillEffect] = []
        for development in event.develops_skills:
            gap = gaps.get(development.skill_id)
            if gap is None:
                continue
            current = effective_levels.get(development.skill_id, 0)
            updated = min(current + development.gain, development.max_level)
            if updated <= current:
                continue
            effects.append(
                ExpectedSkillEffect(
                    skill_id=development.skill_id,
                    name=gap.name,
                    from_level=current,
                    to_level=updated,
                    target_level=gap.target_level,
                    critical=gap.critical,
                )
            )
        effects.sort(key=lambda item: (-int(item.critical), -(item.target_level - item.from_level), item.name))
        return effects

    def _score_factors(
        self,
        event: Event,
        employee: Employee,
        progress: EmployeeProgress,
        history: tuple[HistoryRecord, ...],
        effects: list[ExpectedSkillEffect],
    ) -> list[RecommendationFactor]:
        critical_gain = sum(
            max(min(item.to_level, item.target_level) - item.from_level, 0)
            for item in effects
            if item.critical
        )
        other_gain = sum(
            max(min(item.to_level, item.target_level) - item.from_level, 0)
            for item in effects
            if not item.critical
        )
        factors: list[RecommendationFactor] = []
        if critical_gain:
            factors.append(
                RecommendationFactor(
                    code="critical_gap_closure",
                    contribution=critical_gain * SCORING_WEIGHTS["critical_gap_closure"],
                    detail=f"Useful gain {critical_gain} across critical target skills",
                )
            )
        if other_gain:
            factors.append(
                RecommendationFactor(
                    code="gap_closure",
                    contribution=other_gain * SCORING_WEIGHTS["gap_closure"],
                    detail=f"Useful gain {other_gain} across other target skills",
                )
            )

        goal_match = (
            progress.target.role in event.target_roles
            and progress.target.grade in event.target_grades
        )
        factors.append(
            RecommendationFactor(
                code="goal_relevance",
                contribution=SCORING_WEIGHTS["goal_relevance"] if goal_match else 0.0,
                detail=(
                    f"Targets career goal {progress.target.role}/{progress.target.grade.value}"
                    if goal_match
                    else "Matches the current audience but not the full target role/grade pair"
                ),
            )
        )

        positive_count = self._positive_history_count(event, history)
        if positive_count:
            capped = min(positive_count, 3)
            factors.append(
                RecommendationFactor(
                    code="positive_history",
                    contribution=capped * SCORING_WEIGHTS["positive_history"],
                    detail=f"{positive_count} similar completion(s) had score >=80 or feedback >=4; score uses cap {capped}",
                )
            )

        friction_counts, friction_contribution = self._friction(event, history)
        if any(friction_counts.values()):
            factors.append(
                RecommendationFactor(
                    code="friction",
                    contribution=friction_contribution,
                    detail=(
                        "Similar-event history (excluding the same event): "
                        f"no_show={friction_counts['no_show']}, dropped={friction_counts['dropped']}, "
                        f"declined={friction_counts['declined']}"
                    ),
                )
            )

        if employee.work_format == "remote" and event.format == "offline":
            factors.append(
                RecommendationFactor(
                    code="format_fit",
                    contribution=SCORING_WEIGHTS["format_fit"],
                    detail="Remote work format receives a small penalty for an offline event",
                )
            )

        factors.append(
            RecommendationFactor(
                code="effort",
                contribution=event.duration_hours * SCORING_WEIGHTS["effort_per_hour"],
                detail=f"{event.duration_hours:g} hours × {SCORING_WEIGHTS['effort_per_hour']}",
            )
        )
        return factors

    def _positive_history_count(
        self, event: Event, history: tuple[HistoryRecord, ...]
    ) -> int:
        candidate_skills = {item.skill_id for item in event.develops_skills}
        count = 0
        for record in history:
            if record.status != HistoryStatus.COMPLETED:
                continue
            if not (
                (record.score is not None and record.score >= 80)
                or (record.feedback_rating is not None and record.feedback_rating >= 4)
            ):
                continue
            past_event = self.dataset.indexes.events_by_id[record.event_id]
            past_skills = {item.skill_id for item in past_event.develops_skills}
            if (
                past_event.type == event.type
                or past_event.format == event.format
                or bool(candidate_skills & past_skills)
            ):
                count += 1
        return count

    def _friction(
        self, event: Event, history: tuple[HistoryRecord, ...]
    ) -> tuple[dict[str, int], float]:
        candidate_skills = {item.skill_id for item in event.develops_skills}
        counts = {"no_show": 0, "dropped": 0, "declined": 0}
        for record in history:
            if record.event_id == event.event_id or record.status.value not in counts:
                continue
            past_event = self.dataset.indexes.events_by_id[record.event_id]
            past_skills = {item.skill_id for item in past_event.develops_skills}
            similar = bool(candidate_skills & past_skills) or (
                past_event.type == event.type and past_event.format == event.format
            )
            if similar:
                counts[record.status.value] += 1

        no_show_multiplier = 1.5 if event.format != "self_paced" else 0.5
        dropped_multiplier = 1.5 if event.duration_hours >= 12 else 1.0
        contribution = (
            min(counts["no_show"], 3) * SCORING_WEIGHTS["friction_no_show"] * no_show_multiplier
            + min(counts["dropped"], 3)
            * SCORING_WEIGHTS["friction_dropped"]
            * dropped_multiplier
            + min(counts["declined"], 3) * SCORING_WEIGHTS["friction_declined"]
        )
        return counts, contribution

    def _select_diverse(
        self,
        candidates: list[_Candidate],
        employee: Employee,
        progress: EmployeeProgress,
    ) -> list[Recommendation]:
        selected: list[Recommendation] = []
        covered: set[str] = set()
        pool = list(candidates)
        while pool and len(selected) < 3:
            ranked: list[tuple[float, float, str, _Candidate, set[str]]] = []
            for candidate in pool:
                new_gaps = candidate.covered_gap_ids - covered
                diversity = len(new_gaps) * SCORING_WEIGHTS["diversity_new_gap"] if selected else 0.0
                ranked.append(
                    (
                        candidate.base_score + diversity,
                        candidate.base_score,
                        candidate.event.event_id,
                        candidate,
                        new_gaps,
                    )
                )
            ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
            final_score, _, _, chosen, new_gaps = ranked[0]
            pool.remove(chosen)
            factors = list(chosen.factors)
            diversity = final_score - chosen.base_score
            if diversity:
                factors.append(
                    RecommendationFactor(
                        code="diversity",
                        contribution=diversity,
                        detail=f"Adds coverage for new gap skills: {sorted(new_gaps)}",
                    )
                )
            selected.append(
                Recommendation(
                    event_id=chosen.event.event_id,
                    title=chosen.event.title,
                    type=chosen.event.type,
                    format=chosen.event.format,
                    duration_hours=chosen.event.duration_hours,
                    next_session=chosen.next_session,
                    match_label=self._match_label(final_score),
                    score=round(final_score, 3),
                    expected_effect=list(chosen.expected_effect),
                    factors=factors,
                    explanation=self._explanation(
                        employee=employee,
                        target=progress.target,
                        event=chosen.event,
                        effects=chosen.expected_effect,
                        factors=factors,
                    ),
                )
            )
            covered.update(chosen.covered_gap_ids)
        return selected

    @staticmethod
    def _match_label(score: float) -> Literal["Strong match", "Good match", "Exploratory"]:
        if score >= 12:
            return "Strong match"
        if score >= 5:
            return "Good match"
        return "Exploratory"

    @staticmethod
    def _explanation(
        *,
        employee: Employee,
        target: CareerTarget,
        event: Event,
        effects: tuple[ExpectedSkillEffect, ...],
        factors: list[RecommendationFactor],
    ) -> str:
        primary = effects[0]
        critical_en = " (critical)" if primary.critical else ""
        critical_ru = " (критический навык)" if primary.critical else ""
        critical_kk = " (сыни дағды)" if primary.critical else ""
        positive = next((factor for factor in factors if factor.code == "positive_history"), None)
        friction = next((factor for factor in factors if factor.code == "friction"), None)

        if positive:
            history_en = positive.detail
            history_ru = "Есть положительный опыт похожих активностей: " + positive.detail
            history_kk = "Ұқсас іс-шаралар бойынша оң тәжірибе бар: " + positive.detail
        elif friction:
            history_en = "Past similar-event friction lowers the rank without excluding the option."
            history_ru = "Прошлые сложности с похожими активностями снижают рейтинг, но не исключают вариант."
            history_kk = "Ұқсас іс-шаралардағы бұрынғы қиындықтар нұсқаны алып тастамай, рейтингін төмендетеді."
        else:
            history_en = "No strong positive or friction signal was found in similar history."
            history_ru = "В истории похожих активностей нет выраженного положительного сигнала или трудностей."
            history_kk = "Ұқсас іс-шаралар тарихында айқын оң белгі немесе қиындық табылған жоқ."

        if employee.preferred_language == "ru":
            return (
                f"{primary.name}: уровень {primary.from_level} при требовании {primary.target_level} "
                f"для {target.grade.value}{critical_ru}. «{event.title}» может повысить уровень до "
                f"{primary.to_level}. Активность соответствует траектории {target.role}/{target.grade.value}, "
                f"формат — {event.format}, длительность — {event.duration_hours:g} ч. {history_ru}"
            )
        if employee.preferred_language == "kk":
            return (
                f"{primary.name}: {target.grade.value} үшін талап {primary.target_level}, қазіргі деңгей "
                f"{primary.from_level}{critical_kk}. «{event.title}» деңгейді {primary.to_level}-ге дейін көтере алады. "
                f"Іс-шара {target.role}/{target.grade.value} бағытына сай, форматы — {event.format}, "
                f"ұзақтығы — {event.duration_hours:g} сағат. {history_kk}"
            )
        return (
            f"{primary.name}: level {primary.from_level} versus {primary.target_level} required for "
            f"{target.grade.value}{critical_en}. {event.title} can raise it to {primary.to_level}. "
            f"It matches the {target.role}/{target.grade.value} path, uses {event.format} format, and takes "
            f"{event.duration_hours:g} hours. {history_en}"
        )

    def _required_events(
        self, employee: Employee, history: tuple[HistoryRecord, ...]
    ) -> list[RequiredEvent]:
        required: list[RequiredEvent] = []
        for event in self.dataset.events.events:
            if not event.mandatory:
                continue
            if employee.role not in event.target_roles or employee.grade not in event.target_grades:
                continue
            event_history = [record for record in history if record.event_id == event.event_id]
            if any(record.status == HistoryStatus.COMPLETED for record in event_history):
                continue
            if any(record.status == HistoryStatus.OVERDUE for record in event_history):
                status: Literal["overdue", "in_progress", "not_started"] = "overdue"
            elif any(record.status == HistoryStatus.IN_PROGRESS for record in event_history):
                status = "in_progress"
            else:
                status = "not_started"
            due_dates = sorted(record.due_date for record in event_history if record.due_date)
            required.append(
                RequiredEvent(
                    event_id=event.event_id,
                    title=event.title,
                    status=status,
                    due_date=due_dates[-1] if due_dates else None,
                )
            )
        return required
