from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


SkillLevel = Annotated[int, Field(ge=0, le=5)]
PositiveLevel = Annotated[int, Field(ge=1, le=5)]


class Grade(StrEnum):
    JUNIOR = "Junior"
    MIDDLE = "Middle"
    SENIOR = "Senior"
    LEAD = "Lead"


class HistoryStatus(StrEnum):
    COMPLETED = "completed"
    IN_PROGRESS = "in_progress"
    DROPPED = "dropped"
    NO_SHOW = "no_show"
    DECLINED = "declined"
    OVERDUE = "overdue"


class DatasetMeta(BaseModel):
    dataset: str
    version: str
    as_of_date: date


class Skill(BaseModel):
    skill_id: str
    name: str
    type: Literal["hard", "soft"]
    category: str
    description: str


class RoleProfile(BaseModel):
    role: str
    grade: Grade
    required_skills: dict[str, SkillLevel]
    critical_skills: list[str]


class SkillsDataset(BaseModel):
    meta: DatasetMeta
    proficiency_scale: dict[str, str]
    skills: list[Skill]
    role_profiles: list[RoleProfile]


class CareerGoal(BaseModel):
    target_role: str
    target_grade: Grade


class Employee(BaseModel):
    employee_id: str
    full_name: str
    department: str
    role: str
    grade: Grade
    manager_id: str | None
    hire_date: date
    tenure_months: Annotated[int, Field(ge=0)]
    work_format: Literal["office", "hybrid", "remote"]
    preferred_language: Literal["kk", "ru", "en"]
    career_goal: CareerGoal | None
    skills: dict[str, SkillLevel]
    last_review_date: date


class EmployeesDataset(BaseModel):
    meta: DatasetMeta
    employees: list[Employee]


class SkillDevelopment(BaseModel):
    skill_id: str
    gain: PositiveLevel
    max_level: PositiveLevel


class Event(BaseModel):
    event_id: str
    title: str
    description: str
    type: Literal[
        "compliance",
        "onboarding",
        "course",
        "workshop",
        "mentoring",
        "certification",
        "meetup",
    ]
    format: Literal["online", "offline", "self_paced"]
    duration_hours: Annotated[float, Field(gt=0)]
    mandatory: bool
    target_roles: list[str]
    target_grades: list[Grade]
    develops_skills: list[SkillDevelopment]
    prerequisites: dict[str, SkillLevel]
    upcoming_sessions: list[date]


class EventsDataset(BaseModel):
    meta: DatasetMeta
    events: list[Event]


class HistoryRecord(BaseModel):
    record_id: str
    employee_id: str
    event_id: str
    date: date
    due_date: date | None = None
    status: HistoryStatus
    completion_pct: Annotated[int, Field(ge=0, le=100)]
    score: Annotated[int, Field(ge=0, le=100)] | None = None
    feedback_rating: Annotated[int, Field(ge=1, le=5)] | None = None
    assigned_by: Literal["self", "manager", "hr"]

    @model_validator(mode="after")
    def validate_status_progress(self) -> "HistoryRecord":
        if self.status == HistoryStatus.COMPLETED and self.completion_pct != 100:
            raise ValueError("completed records must have completion_pct=100")
        if self.status in {HistoryStatus.NO_SHOW, HistoryStatus.DECLINED} and self.completion_pct != 0:
            raise ValueError(f"{self.status} records must have completion_pct=0")
        return self

    @field_validator("due_date", "score", "feedback_rating", mode="before")
    @classmethod
    def empty_string_is_none(cls, value: object) -> object:
        return None if value == "" else value

