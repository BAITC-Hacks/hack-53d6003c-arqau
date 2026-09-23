from __future__ import annotations

from collections import defaultdict
from typing import Literal

from pydantic import BaseModel

from .data_loader import DatasetBundle
from .models import Employee, Grade, HistoryStatus, RoleProfile


GRADE_ORDER = (Grade.JUNIOR, Grade.MIDDLE, Grade.SENIOR, Grade.LEAD)


class CareerTarget(BaseModel):
    role: str
    grade: Grade
    source: Literal["career_goal", "next_grade", "current_grade_ceiling"]
    is_lateral: bool


class SkillProgress(BaseModel):
    skill_id: str
    name: str
    category: str
    assessed_level: int
    post_review_development: int
    effective_level: int
    target_level: int
    gap: int
    critical: bool
    evidence_event_ids: list[str]


class CareerReadiness(BaseModel):
    status: Literal["ready", "developing", "critical_gaps", "grade_ceiling"]
    requirements_met: int
    requirements_total: int
    critical_requirements_met: int
    critical_requirements_total: int


class EmployeeProgress(BaseModel):
    employee_id: str
    assessed_at: str
    target: CareerTarget
    readiness: CareerReadiness
    skills: list[SkillProgress]
    gaps: list[SkillProgress]
    critical_gaps: list[SkillProgress]
    completed_after_review: list[str]


class CareerPathService:
    def __init__(self, dataset: DatasetBundle) -> None:
        self.dataset = dataset

    def resolve_target(self, employee: Employee) -> tuple[CareerTarget, RoleProfile]:
        if employee.career_goal is not None:
            role = employee.career_goal.target_role
            grade = employee.career_goal.target_grade
            source: Literal["career_goal", "next_grade", "current_grade_ceiling"] = "career_goal"
        else:
            current_index = GRADE_ORDER.index(employee.grade)
            if current_index < len(GRADE_ORDER) - 1:
                role = employee.role
                grade = GRADE_ORDER[current_index + 1]
                source = "next_grade"
            else:
                role = employee.role
                grade = employee.grade
                source = "current_grade_ceiling"

        profile = self.dataset.indexes.role_profiles_by_key[(role, grade.value)]
        return (
            CareerTarget(
                role=role,
                grade=grade,
                source=source,
                is_lateral=role != employee.role,
            ),
            profile,
        )


class SkillProgressService:
    def __init__(self, dataset: DatasetBundle) -> None:
        self.dataset = dataset
        self.career_paths = CareerPathService(dataset)

    def calculate(self, employee_id: str) -> EmployeeProgress:
        employee = self.dataset.indexes.employees_by_id[employee_id]
        target, target_profile = self.career_paths.resolve_target(employee)
        effective = dict(employee.skills)
        evidence: dict[str, list[str]] = defaultdict(list)
        completed_after_review: list[str] = []

        history = sorted(
            self.dataset.indexes.history_by_employee.get(employee_id, ()),
            key=lambda item: (item.date, item.record_id),
        )
        for record in history:
            if record.status != HistoryStatus.COMPLETED or record.date <= employee.last_review_date:
                continue
            completed_after_review.append(record.event_id)
            event = self.dataset.indexes.events_by_id[record.event_id]
            for development in event.develops_skills:
                previous = effective.get(development.skill_id, 0)
                updated = min(previous + development.gain, development.max_level)
                effective[development.skill_id] = updated
                if updated > previous:
                    evidence[development.skill_id].append(record.event_id)

        skill_ids = set(employee.skills) | set(target_profile.required_skills) | set(effective)
        critical_ids = set(target_profile.critical_skills)
        progress: list[SkillProgress] = []
        for skill_id in skill_ids:
            assessed = employee.skills.get(skill_id, 0)
            current = effective.get(skill_id, assessed)
            target_level = target_profile.required_skills.get(skill_id, 0)
            skill = self.dataset.indexes.skills_by_id[skill_id]
            progress.append(
                SkillProgress(
                    skill_id=skill_id,
                    name=skill.name,
                    category=skill.category,
                    assessed_level=assessed,
                    post_review_development=current - assessed,
                    effective_level=current,
                    target_level=target_level,
                    gap=max(target_level - current, 0),
                    critical=skill_id in critical_ids,
                    evidence_event_ids=evidence.get(skill_id, []),
                )
            )

        progress.sort(key=lambda item: (-int(item.critical), -item.gap, item.name))
        target_skills = [item for item in progress if item.target_level > 0]
        gaps = [item for item in target_skills if item.gap > 0]
        critical_gaps = [item for item in gaps if item.critical]
        critical_skills = [item for item in target_skills if item.critical]

        if target.source == "current_grade_ceiling":
            status: Literal["ready", "developing", "critical_gaps", "grade_ceiling"] = "grade_ceiling"
        elif critical_gaps:
            status = "critical_gaps"
        elif gaps:
            status = "developing"
        else:
            status = "ready"

        return EmployeeProgress(
            employee_id=employee.employee_id,
            assessed_at=employee.last_review_date.isoformat(),
            target=target,
            readiness=CareerReadiness(
                status=status,
                requirements_met=len(target_skills) - len(gaps),
                requirements_total=len(target_skills),
                critical_requirements_met=len(critical_skills) - len(critical_gaps),
                critical_requirements_total=len(critical_skills),
            ),
            skills=progress,
            gaps=gaps,
            critical_gaps=critical_gaps,
            completed_after_review=completed_after_review,
        )

