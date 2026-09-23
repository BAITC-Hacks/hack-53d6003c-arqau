from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, TypeVar

from pydantic import BaseModel, ValidationError

from .models import EmployeesDataset, EventsDataset, HistoryRecord, SkillsDataset


class DatasetValidationError(ValueError):
    """Raised when Career Quest source files are malformed or inconsistent."""


@dataclass(frozen=True)
class DatasetIndexes:
    employees_by_id: dict[str, Any]
    events_by_id: dict[str, Any]
    skills_by_id: dict[str, Any]
    role_profiles_by_key: dict[tuple[str, str], Any]
    history_by_employee: dict[str, tuple[HistoryRecord, ...]]
    history_by_event: dict[str, tuple[HistoryRecord, ...]]


@dataclass(frozen=True)
class DatasetBundle:
    data_dir: Path
    skills: SkillsDataset
    employees: EmployeesDataset
    events: EventsDataset
    history: tuple[HistoryRecord, ...]
    indexes: DatasetIndexes

    @property
    def counts(self) -> dict[str, int]:
        return {
            "skills": len(self.skills.skills),
            "role_profiles": len(self.skills.role_profiles),
            "employees": len(self.employees.employees),
            "events": len(self.events.events),
            "history_records": len(self.history),
        }


ModelT = TypeVar("ModelT", bound=BaseModel)


class DatasetLoader:
    REQUIRED_FILES = (
        "skills.json",
        "employees.json",
        "events.json",
        "activity_history.csv",
    )

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir).expanduser().resolve()

    def load(self) -> DatasetBundle:
        self._check_required_files()
        skills = self._load_json("skills.json", SkillsDataset)
        employees = self._load_json("employees.json", EmployeesDataset)
        events = self._load_json("events.json", EventsDataset)
        history = self._load_history()
        self._validate_relations(skills, employees, events, history)
        indexes = self._build_indexes(skills, employees, events, history)
        return DatasetBundle(
            data_dir=self.data_dir,
            skills=skills,
            employees=employees,
            events=events,
            history=history,
            indexes=indexes,
        )

    def _check_required_files(self) -> None:
        missing = [name for name in self.REQUIRED_FILES if not (self.data_dir / name).is_file()]
        if missing:
            raise DatasetValidationError(
                f"Dataset directory {self.data_dir} is missing required files: {', '.join(missing)}"
            )

    def _load_json(self, filename: str, model: type[ModelT]) -> ModelT:
        path = self.data_dir / filename
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise DatasetValidationError(
                f"{filename} contains invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
        try:
            return model.model_validate(raw)
        except ValidationError as exc:
            raise DatasetValidationError(f"{filename} failed schema validation:\n{exc}") from exc

    def _load_history(self) -> tuple[HistoryRecord, ...]:
        path = self.data_dir / "activity_history.csv"
        records: list[HistoryRecord] = []
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            expected = set(HistoryRecord.model_fields)
            actual = set(reader.fieldnames or [])
            if actual != expected:
                missing = sorted(expected - actual)
                extra = sorted(actual - expected)
                raise DatasetValidationError(
                    "activity_history.csv has invalid columns: "
                    f"missing={missing or 'none'}, extra={extra or 'none'}"
                )
            for line_number, row in enumerate(reader, start=2):
                try:
                    records.append(HistoryRecord.model_validate(row))
                except ValidationError as exc:
                    raise DatasetValidationError(
                        f"activity_history.csv row {line_number} failed validation:\n{exc}"
                    ) from exc
        return tuple(records)

    @staticmethod
    def _duplicates(values: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        duplicates: set[str] = set()
        for value in values:
            if value in seen:
                duplicates.add(value)
            seen.add(value)
        return sorted(duplicates)

    def _validate_relations(
        self,
        skills: SkillsDataset,
        employees: EmployeesDataset,
        events: EventsDataset,
        history: tuple[HistoryRecord, ...],
    ) -> None:
        errors: list[str] = []
        skill_ids = {item.skill_id for item in skills.skills}
        employee_by_id = {item.employee_id: item for item in employees.employees}
        event_ids = {item.event_id for item in events.events}
        role_profile_keys = {(item.role, item.grade.value) for item in skills.role_profiles}

        for label, values in (
            ("skill_id", (item.skill_id for item in skills.skills)),
            ("employee_id", (item.employee_id for item in employees.employees)),
            ("event_id", (item.event_id for item in events.events)),
            ("record_id", (item.record_id for item in history)),
        ):
            duplicates = self._duplicates(values)
            if duplicates:
                errors.append(f"duplicate {label} values: {', '.join(duplicates[:10])}")

        profile_duplicates = self._duplicates(
            f"{item.role}/{item.grade.value}" for item in skills.role_profiles
        )
        if profile_duplicates:
            errors.append(f"duplicate role/grade profiles: {', '.join(profile_duplicates)}")

        for profile in skills.role_profiles:
            unknown_required = sorted(set(profile.required_skills) - skill_ids)
            unknown_critical = sorted(set(profile.critical_skills) - skill_ids)
            non_required_critical = sorted(set(profile.critical_skills) - set(profile.required_skills))
            if unknown_required:
                errors.append(
                    f"role profile {profile.role}/{profile.grade.value} references unknown required skills: "
                    f"{', '.join(unknown_required)}"
                )
            if unknown_critical:
                errors.append(
                    f"role profile {profile.role}/{profile.grade.value} references unknown critical skills: "
                    f"{', '.join(unknown_critical)}"
                )
            if non_required_critical:
                errors.append(
                    f"role profile {profile.role}/{profile.grade.value} has critical skills absent from "
                    f"required_skills: {', '.join(non_required_critical)}"
                )

        for employee in employees.employees:
            key = (employee.role, employee.grade.value)
            if key not in role_profile_keys:
                errors.append(
                    f"employee {employee.employee_id} references unknown role/grade: {key[0]}/{key[1]}"
                )
            unknown_skills = sorted(set(employee.skills) - skill_ids)
            if unknown_skills:
                errors.append(
                    f"employee {employee.employee_id} references unknown skills: {', '.join(unknown_skills)}"
                )
            if employee.manager_id is not None:
                manager = employee_by_id.get(employee.manager_id)
                if manager is None:
                    errors.append(
                        f"employee {employee.employee_id} references unknown manager_id {employee.manager_id}"
                    )
                elif manager.department != employee.department:
                    errors.append(
                        f"employee {employee.employee_id} manager {manager.employee_id} is in a different department"
                    )
                elif manager.grade.value != "Lead":
                    errors.append(
                        f"employee {employee.employee_id} manager {manager.employee_id} is not a Lead"
                    )
            if employee.career_goal is not None:
                goal_key = (
                    employee.career_goal.target_role,
                    employee.career_goal.target_grade.value,
                )
                if goal_key not in role_profile_keys:
                    errors.append(
                        f"employee {employee.employee_id} references unknown career goal: "
                        f"{goal_key[0]}/{goal_key[1]}"
                    )

        known_roles = {role for role, _ in role_profile_keys}
        known_grades = {grade for _, grade in role_profile_keys}
        for event in events.events:
            event_skill_ids = {item.skill_id for item in event.develops_skills}
            unknown_skills = sorted((event_skill_ids | set(event.prerequisites)) - skill_ids)
            if unknown_skills:
                errors.append(
                    f"event {event.event_id} references unknown skills: {', '.join(unknown_skills)}"
                )
            unknown_roles = sorted(set(event.target_roles) - known_roles)
            unknown_grades = sorted({grade.value for grade in event.target_grades} - known_grades)
            if unknown_roles:
                errors.append(
                    f"event {event.event_id} references unknown roles: {', '.join(unknown_roles)}"
                )
            if unknown_grades:
                errors.append(
                    f"event {event.event_id} references unknown grades: {', '.join(unknown_grades)}"
                )

        for record in history:
            if record.employee_id not in employee_by_id:
                errors.append(
                    f"history record {record.record_id} references unknown employee_id {record.employee_id}"
                )
            if record.event_id not in event_ids:
                errors.append(
                    f"history record {record.record_id} references unknown event_id {record.event_id}"
                )

        if errors:
            preview = "\n- ".join(errors[:30])
            suffix = f"\n... and {len(errors) - 30} more error(s)" if len(errors) > 30 else ""
            raise DatasetValidationError(f"Cross-file dataset validation failed:\n- {preview}{suffix}")

    @staticmethod
    def _build_indexes(
        skills: SkillsDataset,
        employees: EmployeesDataset,
        events: EventsDataset,
        history: tuple[HistoryRecord, ...],
    ) -> DatasetIndexes:
        history_by_employee: dict[str, list[HistoryRecord]] = {}
        history_by_event: dict[str, list[HistoryRecord]] = {}
        for record in history:
            history_by_employee.setdefault(record.employee_id, []).append(record)
            history_by_event.setdefault(record.event_id, []).append(record)
        return DatasetIndexes(
            employees_by_id={item.employee_id: item for item in employees.employees},
            events_by_id={item.event_id: item for item in events.events},
            skills_by_id={item.skill_id: item for item in skills.skills},
            role_profiles_by_key={
                (item.role, item.grade.value): item for item in skills.role_profiles
            },
            history_by_employee={key: tuple(value) for key, value in history_by_employee.items()},
            history_by_event={key: tuple(value) for key, value in history_by_event.items()},
        )

