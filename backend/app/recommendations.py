from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
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
    "same_event_friction_multiplier": 1.5,
}

RECOMMENDATION_RULES: dict[str, int | float] = {
    "positive_history_cap": 3,
    "friction_history_cap": 3,
    "scheduled_no_show_multiplier": 1.5,
    "self_paced_no_show_multiplier": 0.5,
    "long_event_hours": 12,
    "long_event_dropped_multiplier": 1.5,
    "strong_match_min_score": 12,
    "good_match_min_score": 5,
    "onboarding_max_tenure_months": 3,
    "annual_compliance_days": 365,
}

RECURRING_EVENT_ID = "EV_036"

RejectionCode = Literal[
    "MANDATORY",
    "AUDIENCE",
    "ALREADY_COMPLETED",
    "IN_PROGRESS",
    "PREREQUISITES",
    "NO_RELEVANT_GAP",
    "MAX_LEVEL_REACHED",
    "NOT_AVAILABLE",
]


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
    status: Literal["overdue", "in_progress", "due"]
    due_date: date | None


class RejectedEvent(BaseModel):
    event_id: str
    reason_code: RejectionCode
    detail: str


class BlockingReason(BaseModel):
    event_id: str
    reason_code: RejectionCode


class BlockedGap(BaseModel):
    skill_id: str
    name: str
    effective_level: int
    target_level: int
    critical: bool
    blocking_reasons: list[BlockingReason]
    suggestion: str


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
    summary: str
    recommendations: list[Recommendation] = Field(max_length=3)
    required: list[RequiredEvent]
    unclosed_gaps: list[UnclosedGap]
    blocked_gaps: list[BlockedGap]
    rejected: list[RejectedEvent] | None = None


@dataclass(frozen=True)
class _FrictionSummary:
    same_event: dict[str, int]
    similar_events: dict[str, int]
    contribution: float

    @property
    def same_event_total(self) -> int:
        return sum(self.same_event.values())

    @property
    def total(self) -> int:
        return self.same_event_total + sum(self.similar_events.values())


@dataclass(frozen=True)
class _Candidate:
    event: Event
    next_session: date | None
    expected_effect: tuple[ExpectedSkillEffect, ...]
    factors: tuple[RecommendationFactor, ...]
    base_score: float
    positive_history_count: int
    friction: _FrictionSummary

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
        blocked_gaps = self._blocked_gaps(progress, candidates, rejected)
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
        response_fields: dict[str, object] = {
            "employee_id": employee_id,
            "as_of_date": self.as_of_date,
            "target": progress.target,
            "status": status,
            "summary": self._summary(employee, status, blocked_gaps),
            "recommendations": recommendations,
            "required": self._required_events(employee, history),
            "unclosed_gaps": unclosed_gaps,
            "blocked_gaps": blocked_gaps,
        }
        if debug:
            response_fields["rejected"] = rejected
        return EmployeeRecommendations(**response_fields)

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
                f"Audience roles: {', '.join(event.target_roles)}; audience grades: "
                f"{', '.join(grade.value for grade in event.target_grades)}. Employee: "
                f"{employee.role}/{employee.grade.value}; target: "
                f"{progress.target.role}/{progress.target.grade.value}.",
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
            detail = "; ".join(
                f"{skill_id} is {levels['effective']}, requires {levels['required']}"
                for skill_id, levels in sorted(unmet.items())
            )
            return self._reject(event, "PREREQUISITES", f"Unmet prerequisites: {detail}"), None

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
            levels = "; ".join(
                f"{development.skill_id} is {effective_levels.get(development.skill_id, 0)}, "
                f"event maximum is {development.max_level}"
                for development in relevant_developments
            )
            return self._reject(
                event,
                "MAX_LEVEL_REACHED",
                f"Event cannot improve its relevant gap skills: {levels}",
            ), None

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
        positive_history_count = self._positive_history_count(event, history)
        friction = self._friction(event, history)
        factors = self._score_factors(
            event,
            employee,
            progress,
            effects,
            positive_history_count,
            friction,
        )
        return None, _Candidate(
            event=event,
            next_session=next_session,
            expected_effect=tuple(effects),
            factors=tuple(factors),
            base_score=sum(item.contribution for item in factors),
            positive_history_count=positive_history_count,
            friction=friction,
        )

    @staticmethod
    def _reject(event: Event, reason_code: RejectionCode, detail: str) -> RejectedEvent:
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
        effects: list[ExpectedSkillEffect],
        positive_history_count: int,
        friction: _FrictionSummary,
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

        if positive_history_count:
            capped = min(
                positive_history_count,
                int(RECOMMENDATION_RULES["positive_history_cap"]),
            )
            factors.append(
                RecommendationFactor(
                    code="positive_history",
                    contribution=capped * SCORING_WEIGHTS["positive_history"],
                    detail=(
                        f"{positive_history_count} successful similar completion(s) "
                        "support this learning format or skill area"
                    ),
                )
            )

        if friction.total:
            same = friction.same_event
            similar = friction.similar_events
            factors.append(
                RecommendationFactor(
                    code="friction",
                    contribution=friction.contribution,
                    detail=(
                        f"Same activity: {self._localized_history_counts(same, 'en') or 'none'}; "
                        "similar activities: "
                        f"{self._localized_history_counts(similar, 'en') or 'none'}"
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
    ) -> _FrictionSummary:
        candidate_skills = {item.skill_id for item in event.develops_skills}
        same_event = {"no_show": 0, "dropped": 0, "declined": 0}
        similar_events = {"no_show": 0, "dropped": 0, "declined": 0}
        for record in history:
            if record.status.value not in same_event:
                continue
            if record.event_id == event.event_id:
                same_event[record.status.value] += 1
                continue
            past_event = self.dataset.indexes.events_by_id[record.event_id]
            past_skills = {item.skill_id for item in past_event.develops_skills}
            similar = bool(candidate_skills & past_skills) or (
                past_event.type == event.type and past_event.format == event.format
            )
            if similar:
                similar_events[record.status.value] += 1

        status_multipliers = {
            "no_show": (
                float(RECOMMENDATION_RULES["scheduled_no_show_multiplier"])
                if event.format != "self_paced"
                else float(RECOMMENDATION_RULES["self_paced_no_show_multiplier"])
            ),
            "dropped": (
                float(RECOMMENDATION_RULES["long_event_dropped_multiplier"])
                if event.duration_hours >= RECOMMENDATION_RULES["long_event_hours"]
                else 1.0
            ),
            "declined": 1.0,
        }
        history_cap = int(RECOMMENDATION_RULES["friction_history_cap"])
        contribution = 0.0
        for status in same_event:
            weight = SCORING_WEIGHTS[f"friction_{status}"]
            contribution += (
                min(same_event[status], history_cap)
                * weight
                * status_multipliers[status]
                * SCORING_WEIGHTS["same_event_friction_multiplier"]
            )
            contribution += (
                min(similar_events[status], history_cap)
                * weight
                * status_multipliers[status]
            )
        return _FrictionSummary(
            same_event=same_event,
            similar_events=similar_events,
            contribution=contribution,
        )

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
                        detail=f"Adds coverage for new gap skills: {', '.join(sorted(new_gaps))}",
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
                        candidate=chosen,
                    ),
                )
            )
            covered.update(chosen.covered_gap_ids)
        return selected

    @staticmethod
    def _match_label(score: float) -> Literal["Strong match", "Good match", "Exploratory"]:
        if score >= RECOMMENDATION_RULES["strong_match_min_score"]:
            return "Strong match"
        if score >= RECOMMENDATION_RULES["good_match_min_score"]:
            return "Good match"
        return "Exploratory"

    @classmethod
    def _explanation(
        cls,
        *,
        employee: Employee,
        target: CareerTarget,
        candidate: _Candidate,
    ) -> str:
        event = candidate.event
        primary = candidate.expected_effect[0]
        same = candidate.friction.same_event
        similar = candidate.friction.similar_events
        grade_names = {
            "en": {"Junior": "Junior", "Middle": "Middle", "Senior": "Senior", "Lead": "Lead"},
            "ru": {"Junior": "Junior", "Middle": "Middle", "Senior": "Senior", "Lead": "Lead"},
            "kk": {"Junior": "Junior", "Middle": "Middle", "Senior": "Senior", "Lead": "Lead"},
        }
        format_names = {
            "en": {"online": "online", "offline": "in person", "self_paced": "self-paced"},
            "ru": {"online": "онлайн", "offline": "очно", "self_paced": "самостоятельно"},
            "kk": {"online": "онлайн", "offline": "офлайн", "self_paced": "өз қарқынымен"},
        }
        language = employee.preferred_language
        current_grade = grade_names[language][employee.grade.value]
        target_grade = grade_names[language][target.grade.value]
        event_format = format_names[language][event.format]

        if candidate.friction.same_event_total:
            history_counts = cls._localized_history_counts(same, language)
            if language == "ru":
                history_text = (
                    f"Ранее вы {candidate.friction.same_event_total} раз не смогли завершить или посетить "
                    f"«{event.title}» ({history_counts}); поэтому активность ниже в рейтинге, "
                    "а альтернативы показаны раньше."
                )
            elif language == "kk":
                history_text = (
                    f"Бұрын «{event.title}» іс-шарасына қатысты {candidate.friction.same_event_total} рет "
                    f"қиындық болды ({history_counts}); сондықтан оның рейтингі төмендетіліп, "
                    "баламалар алдымен көрсетіледі."
                )
            else:
                history_text = (
                    f"You missed, dropped, or declined {event.title} "
                    f"{candidate.friction.same_event_total} time(s) ({history_counts}), so it is ranked "
                    "lower and alternatives come first."
                )
        elif sum(similar.values()):
            history_counts = cls._localized_history_counts(similar, language)
            if language == "ru":
                history_text = f"В похожих активностях были сложности: {history_counts}; это немного снижает рейтинг."
            elif language == "kk":
                history_text = f"Ұқсас іс-шараларда кедергілер болды: {history_counts}; бұл рейтингті аздап төмендетеді."
            else:
                history_text = f"Similar activities had some friction ({history_counts}), which lowers the rank slightly."
        elif candidate.positive_history_count:
            if language == "ru":
                history_text = f"У вас есть успешный опыт похожих активностей: {candidate.positive_history_count}."
            elif language == "kk":
                history_text = f"Ұқсас іс-шаралар бойынша сәтті тәжірибеңіз бар: {candidate.positive_history_count}."
            else:
                unit = "activity" if candidate.positive_history_count == 1 else "activities"
                history_text = (
                    f"You completed {candidate.positive_history_count} similar {unit} successfully."
                )
        elif language == "ru":
            history_text = "Похожего опыта участия пока нет."
        elif language == "kk":
            history_text = "Ұқсас қатысу тәжірибесі әзірге жоқ."
        else:
            history_text = "There is no similar participation history yet."

        if event.format == "self_paced":
            session_text = {
                "en": "It is available at any time.",
                "ru": "Начать можно в любое время.",
                "kk": "Кез келген уақытта бастауға болады.",
            }[language]
        else:
            session = candidate.next_session.isoformat() if candidate.next_session else ""
            session_text = {
                "en": f"The next session is {session}.",
                "ru": f"Ближайшая сессия — {session}.",
                "kk": f"Келесі сессия — {session}.",
            }[language]

        if language == "ru":
            critical = " Это критический навык." if primary.critical else ""
            return (
                f"Сейчас ваш грейд — {current_grade}; цель — {target.role}, грейд {target_grade}. "
                f"Требование следующего уровня по навыку {primary.name} — {primary.target_level}, "
                f"а ваш текущий уровень — {primary.from_level}.{critical} «{event.title}» может изменить "
                f"расчётную оценку с {primary.from_level} до {primary.to_level}; это оценка прогресса, "
                f"а не сертификация. {history_text} Формат — {event_format}, нагрузка — "
                f"{event.duration_hours:g} ч. {session_text}"
            )
        if language == "kk":
            critical = " Бұл сыни дағды." if primary.critical else ""
            return (
                f"Қазіргі грейдіңіз — {current_grade}; мақсатыңыз — {target.role}, {target_grade} грейді. "
                f"Келесі деңгейде {primary.name} дағдысына қойылатын талап — {primary.target_level}, "
                f"ал қазіргі бағалау — {primary.from_level}.{critical} «{event.title}» есептік бағалауды "
                f"{primary.from_level}-ден {primary.to_level}-ге өзгерте алады; бұл сертификаттау емес, "
                f"прогресс бағасы. {history_text} Форматы — {event_format}, ұзақтығы — "
                f"{event.duration_hours:g} сағат. {session_text}"
            )
        critical = " This is a critical skill." if primary.critical else ""
        return (
            f"Your current grade is {current_grade}; your target is {target.role} {target_grade}. "
            f"The next-level requirement for {primary.name} is {primary.target_level}, while your current "
            f"estimate is {primary.from_level}.{critical} {event.title} can move the estimate from "
            f"{primary.from_level} to {primary.to_level}; this is a progress estimate, not a certification. "
            f"{history_text} The format is {event_format}, the effort is {event.duration_hours:g} hours. "
            f"{session_text}"
        )

    @staticmethod
    def _localized_history_counts(counts: dict[str, int], language: str) -> str:
        def russian_form(number: int, forms: tuple[str, str, str]) -> str:
            if number % 10 == 1 and number % 100 != 11:
                return forms[0]
            if number % 10 in {2, 3, 4} and number % 100 not in {12, 13, 14}:
                return forms[1]
            return forms[2]

        parts: list[str] = []
        for status, count in counts.items():
            if not count:
                continue
            if language == "ru":
                forms = {
                    "no_show": ("неявка", "неявки", "неявок"),
                    "dropped": ("прерывание", "прерывания", "прерываний"),
                    "declined": ("отказ", "отказа", "отказов"),
                }[status]
                label = russian_form(count, forms)
            elif language == "kk":
                label = {
                    "no_show": "келмеу",
                    "dropped": "тоқтату",
                    "declined": "бас тарту",
                }[status]
            else:
                singular, plural = {
                    "no_show": ("no-show", "no-shows"),
                    "dropped": ("drop", "drops"),
                    "declined": ("decline", "declines"),
                }[status]
                label = singular if count == 1 else plural
            parts.append(f"{count} {label}")
        return ", ".join(parts)

    def _blocked_gaps(
        self,
        progress: EmployeeProgress,
        candidates: list[_Candidate],
        rejected: list[RejectedEvent],
    ) -> list[BlockedGap]:
        candidates_by_id = {candidate.event.event_id: candidate for candidate in candidates}
        rejected_by_id = {item.event_id: item for item in rejected}
        blocked: list[BlockedGap] = []
        for gap in progress.gaps:
            if any(gap.skill_id in candidate.covered_gap_ids for candidate in candidates):
                continue
            reasons: list[BlockingReason] = []
            for event in sorted(self.dataset.events.events, key=lambda item: item.event_id):
                developments = [
                    item for item in event.develops_skills if item.skill_id == gap.skill_id
                ]
                if not developments:
                    continue
                rejection = rejected_by_id.get(event.event_id)
                if rejection is not None:
                    reasons.append(
                        BlockingReason(
                            event_id=event.event_id,
                            reason_code=rejection.reason_code,
                        )
                    )
                    continue
                candidate = candidates_by_id.get(event.event_id)
                if candidate is not None and all(
                    development.max_level <= gap.effective_level
                    for development in developments
                ):
                    reasons.append(
                        BlockingReason(
                            event_id=event.event_id,
                            reason_code="MAX_LEVEL_REACHED",
                        )
                    )
            blocked.append(
                BlockedGap(
                    skill_id=gap.skill_id,
                    name=gap.name,
                    effective_level=gap.effective_level,
                    target_level=gap.target_level,
                    critical=gap.critical,
                    blocking_reasons=reasons,
                    suggestion=self._blocked_gap_suggestion(
                        progress.employee_id,
                        gap,
                    ),
                )
            )
        blocked.sort(key=lambda item: (-int(item.critical), item.name))
        return blocked

    def _blocked_gap_suggestion(self, employee_id: str, gap: SkillProgress) -> str:
        language = self.dataset.indexes.employees_by_id[employee_id].preferred_language
        if language == "ru":
            return (
                f"В каталоге нет активности, которая может повысить {gap.name} с "
                f"{gap.effective_level} до {gap.target_level}. Обсудите практику на рабочем месте "
                "или наставничество с руководителем; для HR это пробел каталога."
            )
        if language == "kk":
            return (
                f"Каталогта {gap.name} дағдысын {gap.effective_level}-ден {gap.target_level}-ге "
                "көтере алатын іс-шара жоқ. Жұмыс орнындағы практиканы немесе тәлімгерлікті "
                "басшымен талқылаңыз; HR үшін бұл каталогтағы мүмкіндік олқылығы."
            )
        return (
            f"No catalog activity can move {gap.name} from {gap.effective_level} to "
            f"{gap.target_level}. Discuss on-the-job practice or mentoring with your manager; "
            "HR sees this as a catalog gap."
        )

    @staticmethod
    def _summary(
        employee: Employee,
        status: Literal["OK", "NO_SUITABLE_ACTION"],
        blocked_gaps: list[BlockedGap],
    ) -> str:
        critical = [gap for gap in blocked_gaps if gap.critical]
        language = employee.preferred_language
        if critical:
            names = ", ".join(gap.name for gap in critical)
            if language == "ru":
                return f"Критический пробел заблокирован: {names}. Рекомендации ниже развивают другие навыки."
            if language == "kk":
                return f"Сыни дағды олқылығы бұғатталған: {names}. Төмендегі ұсыныстар басқа дағдыларды дамытады."
            return f"A critical gap is blocked: {names}. The recommendations below develop other skills."
        if status == "NO_SUITABLE_ACTION":
            if language == "ru":
                return "Сейчас в каталоге нет подходящей активности для незакрытых пробелов."
            if language == "kk":
                return "Қазір каталогта жабылмаған дағды олқылықтарына сай іс-шара жоқ."
            return "There is no suitable catalog activity for the unclosed gaps right now."
        if blocked_gaps:
            if language == "ru":
                return "Некоторые некритические пробелы пока не поддержаны каталогом."
            if language == "kk":
                return "Кейбір сыни емес дағды олқылықтары каталогта әзірге қолдау таппайды."
            return "Some non-critical gaps do not yet have an eligible catalog activity."
        if language == "ru":
            return "Рекомендации охватывают пробелы, для которых сейчас есть подходящие активности."
        if language == "kk":
            return "Ұсыныстар қазір қолжетімді іс-шаралары бар дағды олқылықтарын қамтиды."
        return "Recommendations cover the gaps that currently have eligible catalog activities."

    def _required_events(
        self, employee: Employee, history: tuple[HistoryRecord, ...]
    ) -> list[RequiredEvent]:
        required: list[RequiredEvent] = []
        annual_cutoff = self.as_of_date - timedelta(
            days=int(RECOMMENDATION_RULES["annual_compliance_days"])
        )
        for event in self.dataset.events.events:
            if not event.mandatory:
                continue
            if employee.role not in event.target_roles or employee.grade not in event.target_grades:
                continue
            event_history = [record for record in history if record.event_id == event.event_id]
            if event.type == "onboarding":
                if employee.tenure_months > RECOMMENDATION_RULES["onboarding_max_tenure_months"]:
                    continue
                if any(record.status == HistoryStatus.COMPLETED for record in event_history):
                    continue
            elif any(
                record.status == HistoryStatus.COMPLETED
                and annual_cutoff <= record.date <= self.as_of_date
                for record in event_history
            ):
                continue
            if any(record.status == HistoryStatus.OVERDUE for record in event_history):
                status: Literal["overdue", "in_progress", "due"] = "overdue"
            elif any(record.status == HistoryStatus.IN_PROGRESS for record in event_history):
                status = "in_progress"
            else:
                status = "due"
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
