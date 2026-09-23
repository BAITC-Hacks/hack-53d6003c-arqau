from __future__ import annotations

import csv
import json
import re
from io import StringIO
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError

from .career_progress import SkillProgressService
from .data_loader import DatasetBundle, DatasetLoader, DatasetValidationError
from .models import (
    Employee,
    EmployeesDataset,
    Event,
    EventsDataset,
    HistoryRecord,
    HistoryStatus,
    RoleProfile,
    Skill,
    SkillsDataset,
)
from .recommendations import RECURRING_EVENT_ID, RecommendationEngine


class ImportIssue(BaseModel):
    file: str
    row: int | None = None
    id: str | None = None
    message: str


class ImportReport(BaseModel):
    added_employees: list[str] = Field(default_factory=list)
    added_history: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    errors: list[ImportIssue] = Field(default_factory=list)


class ActivityUpdate(BaseModel):
    event_id: str
    status: Literal["completed", "in_progress"]
    score: int | None = Field(default=None, ge=0, le=100)
    feedback_rating: int | None = Field(default=None, ge=1, le=5)
    source: Literal["self_report", "qr_verified", "hr_confirmed"]


class SkillChange(BaseModel):
    skill_id: str
    name: str
    from_level: int
    to_level: int


class ReadinessChange(BaseModel):
    from_pct: int = Field(serialization_alias="from")
    to_pct: int = Field(serialization_alias="to")


class ActivityUpdateResult(BaseModel):
    record_id: str
    employee_id: str
    event_id: str
    status: Literal["completed", "in_progress"]
    skills_changed: list[SkillChange]
    readiness: ReadinessChange
    recommendations_before: list[str]
    recommendations_after: list[str]
    progress_note: str


class DataStoreError(ValueError):
    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class DataStore:
    """Owns the base snapshot and an atomically replaceable runtime dataset."""

    def __init__(self, data_dir: str | Path, extra_data_dir: str | Path | None = None) -> None:
        self.loader = DatasetLoader(data_dir)
        self._original = self.loader.load()
        self._dataset = self._original
        self._lock = RLock()
        self._runtime_record_counter = 1
        self._refresh_services()
        if extra_data_dir is not None:
            payload = self.payload_from_directory(extra_data_dir)
            report = self.import_data(payload, mode="add", dry_run=False)
            if report.errors:
                messages = "; ".join(issue.message for issue in report.errors)
                raise DatasetValidationError(f"Startup extra-data import failed: {messages}")

    @property
    def dataset(self) -> DatasetBundle:
        return self._dataset

    @property
    def progress(self) -> SkillProgressService:
        return self._progress

    @property
    def recommendations(self) -> RecommendationEngine:
        return self._recommendations

    def _refresh_services(self) -> None:
        self._progress = SkillProgressService(self._dataset)
        self._recommendations = RecommendationEngine(self._dataset)

    @staticmethod
    def payload_from_directory(directory: str | Path) -> dict[str, object]:
        source = Path(directory).expanduser().resolve()
        if not source.is_dir():
            raise DatasetValidationError(f"Extra data directory does not exist: {source}")
        payload: dict[str, object] = {}
        for filename, key in (
            ("skills.json", "skills"),
            ("employees.json", "employees"),
            ("events.json", "events"),
        ):
            path = source / filename
            if path.is_file():
                try:
                    payload[key] = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise DatasetValidationError(
                        f"{filename} contains invalid JSON at line {exc.lineno}: {exc.msg}"
                    ) from exc
        history_path = source / "activity_history.csv"
        if history_path.is_file():
            payload["history"] = DataStore.parse_history_csv(
                history_path.read_text(encoding="utf-8-sig")
            )
        if not payload:
            raise DatasetValidationError(f"Extra data directory contains no supported files: {source}")
        return payload

    @staticmethod
    def parse_history_csv(content: str) -> list[dict[str, str]]:
        return list(csv.DictReader(StringIO(content)))

    def import_data(
        self,
        payload: dict[str, object],
        *,
        mode: Literal["add", "upsert"] = "add",
        dry_run: bool,
    ) -> ImportReport:
        with self._lock:
            report = ImportReport()
            parsed = self._parse_import_payload(payload, report)
            if report.errors:
                return report

            current = self._dataset
            skills_by_id = {item.skill_id: item for item in current.skills.skills}
            profiles_by_key = {
                (item.role, item.grade.value): item for item in current.skills.role_profiles
            }
            employees_by_id = {
                item.employee_id: item for item in current.employees.employees
            }
            events_by_id = {item.event_id: item for item in current.events.events}
            history_by_id = {item.record_id: item for item in current.history}

            self._merge_models(
                skills_by_id,
                parsed["skills"],
                key=lambda item: item.skill_id,
                file="skills.json",
                mode=mode,
                report=report,
            )
            self._merge_models(
                profiles_by_key,
                parsed["role_profiles"],
                key=lambda item: (item.role, item.grade.value),
                file="skills.json",
                mode=mode,
                report=report,
            )
            new_employee_ids = self._merge_models(
                employees_by_id,
                parsed["employees"],
                key=lambda item: item.employee_id,
                file="employees.json",
                mode=mode,
                report=report,
            )
            self._merge_models(
                events_by_id,
                parsed["events"],
                key=lambda item: item.event_id,
                file="events.json",
                mode=mode,
                report=report,
            )
            new_history_ids = self._merge_models(
                history_by_id,
                parsed["history"],
                key=lambda item: item.record_id,
                file="activity_history.csv",
                mode=mode,
                report=report,
            )
            if report.errors:
                return report

            proficiency_scale = parsed["proficiency_scale"] or current.skills.proficiency_scale
            candidate_skills = SkillsDataset(
                meta=current.skills.meta,
                proficiency_scale=proficiency_scale,
                skills=list(skills_by_id.values()),
                role_profiles=list(profiles_by_key.values()),
            )
            candidate_employees = EmployeesDataset(
                meta=current.employees.meta,
                employees=list(employees_by_id.values()),
            )
            candidate_events = EventsDataset(
                meta=current.events.meta,
                events=list(events_by_id.values()),
            )
            try:
                candidate = self.loader.build_bundle(
                    candidate_skills,
                    candidate_employees,
                    candidate_events,
                    tuple(history_by_id.values()),
                )
            except DatasetValidationError as exc:
                report.errors.extend(self._relation_issues(str(exc)))
                return report

            report.added_employees = [str(item) for item in new_employee_ids]
            report.added_history = [str(item) for item in new_history_ids]
            report.warnings.extend(candidate.warnings)
            if not dry_run:
                self._dataset = candidate
                self._refresh_services()
            return report

    def _parse_import_payload(
        self, payload: dict[str, object], report: ImportReport
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "skills": [],
            "role_profiles": [],
            "proficiency_scale": None,
            "employees": [],
            "events": [],
            "history": [],
        }
        skills_raw = payload.get("skills")
        if skills_raw is not None:
            if not isinstance(skills_raw, dict):
                report.errors.append(
                    ImportIssue(file="skills.json", message="Expected a JSON object")
                )
            else:
                result["skills"] = self._parse_models(
                    skills_raw.get("skills", []), Skill, "skills.json", "skill_id", report
                )
                result["role_profiles"] = self._parse_models(
                    skills_raw.get("role_profiles", []),
                    RoleProfile,
                    "skills.json",
                    "role",
                    report,
                    check_duplicates=False,
                )
                profile_keys: set[tuple[str, str]] = set()
                for profile in result["role_profiles"]:
                    profile_key = (profile.role, profile.grade.value)
                    if profile_key in profile_keys:
                        report.errors.append(
                            ImportIssue(
                                file="skills.json",
                                id=f"{profile.role}/{profile.grade.value}",
                                message="Duplicate role/grade profile in import",
                            )
                        )
                    profile_keys.add(profile_key)
                if "proficiency_scale" in skills_raw:
                    scale = skills_raw["proficiency_scale"]
                    if isinstance(scale, dict) and all(
                        isinstance(key, str) and isinstance(value, str)
                        for key, value in scale.items()
                    ):
                        result["proficiency_scale"] = scale
                    else:
                        report.errors.append(
                            ImportIssue(
                                file="skills.json",
                                message="proficiency_scale must be a string-to-string object",
                            )
                        )

        result["employees"] = self._parse_container(
            payload.get("employees"), "employees", Employee, "employees.json", "employee_id", report
        )
        result["events"] = self._parse_container(
            payload.get("events"), "events", Event, "events.json", "event_id", report
        )
        history_raw = payload.get("history")
        if history_raw is not None:
            result["history"] = self._parse_models(
                history_raw,
                HistoryRecord,
                "activity_history.csv",
                "record_id",
                report,
            )
        if not any(
            result[key] for key in ("skills", "role_profiles", "employees", "events", "history")
        ):
            report.errors.append(
                ImportIssue(file="request", message="No importable employees, history, events, or skills were supplied")
            )
        return result

    def _parse_container(
        self,
        raw: object,
        container: str,
        model: type[BaseModel],
        file: str,
        id_field: str,
        report: ImportReport,
        check_duplicates: bool = True,
    ) -> list[BaseModel]:
        if raw is None:
            return []
        if isinstance(raw, dict):
            raw = raw.get(container, [])
        return self._parse_models(
            raw,
            model,
            file,
            id_field,
            report,
            check_duplicates=check_duplicates,
        )

    @staticmethod
    def _parse_models(
        raw: object,
        model: type[BaseModel],
        file: str,
        id_field: str,
        report: ImportReport,
        check_duplicates: bool = True,
    ) -> list[BaseModel]:
        if not isinstance(raw, list):
            report.errors.append(ImportIssue(file=file, message="Expected a list of records"))
            return []
        parsed: list[BaseModel] = []
        seen: set[object] = set()
        for row_number, item in enumerate(raw, start=1):
            item_id = item.get(id_field) if isinstance(item, dict) else None
            try:
                value = model.model_validate(item)
            except ValidationError as exc:
                report.errors.append(
                    ImportIssue(
                        file=file,
                        row=row_number,
                        id=str(item_id) if item_id is not None else None,
                        message=str(exc),
                    )
                )
                continue
            key = getattr(value, id_field)
            if check_duplicates and key in seen:
                report.errors.append(
                    ImportIssue(
                        file=file,
                        row=row_number,
                        id=str(key),
                        message=f"Duplicate {id_field} in import",
                    )
                )
                continue
            if check_duplicates:
                seen.add(key)
            parsed.append(value)
        return parsed

    @staticmethod
    def _merge_models(
        existing: dict[Any, BaseModel],
        incoming: list[BaseModel],
        *,
        key: Any,
        file: str,
        mode: Literal["add", "upsert"],
        report: ImportReport,
    ) -> list[Any]:
        added: list[Any] = []
        for item in incoming:
            item_key = key(item)
            if item_key in existing and mode != "upsert":
                report.errors.append(
                    ImportIssue(
                        file=file,
                        id=str(item_key),
                        message=f"ID {item_key} already exists; use mode=upsert to replace it",
                    )
                )
                continue
            if item_key in existing:
                report.warnings.append(f"Replaced {file} record {item_key}")
            else:
                added.append(item_key)
            existing[item_key] = item
        return added

    @staticmethod
    def _relation_issues(message: str) -> list[ImportIssue]:
        issues: list[ImportIssue] = []
        for line in message.splitlines():
            detail = line.removeprefix("- ").strip()
            if not detail or detail.startswith("Cross-file dataset validation failed"):
                continue
            history_match = re.search(r"history record (\S+)", detail)
            employee_match = re.search(r"employee (\S+)", detail)
            event_match = re.search(r"event (\S+)", detail)
            if history_match:
                file, item_id = "activity_history.csv", history_match.group(1)
            elif employee_match:
                file, item_id = "employees.json", employee_match.group(1)
            elif event_match:
                file, item_id = "events.json", event_match.group(1)
            else:
                file, item_id = "merged dataset", None
            issues.append(ImportIssue(file=file, id=item_id, message=detail))
        return issues or [ImportIssue(file="merged dataset", message=message)]

    def record_activity(
        self, employee_id: str, update: ActivityUpdate
    ) -> ActivityUpdateResult:
        with self._lock:
            employee = self._dataset.indexes.employees_by_id.get(employee_id)
            if employee is None:
                raise DataStoreError(404, f"Employee {employee_id} not found")
            event = self._dataset.indexes.events_by_id.get(update.event_id)
            if event is None:
                raise DataStoreError(404, f"Event {update.event_id} not found")

            history = self._dataset.indexes.history_by_employee.get(employee_id, ())
            completed = any(
                item.event_id == update.event_id and item.status == HistoryStatus.COMPLETED
                for item in history
            )
            in_progress = any(
                item.event_id == update.event_id and item.status == HistoryStatus.IN_PROGRESS
                for item in history
            )
            if update.status == "completed" and completed and update.event_id != RECURRING_EVENT_ID:
                raise DataStoreError(409, "This non-recurring event is already completed")
            if update.status == "in_progress" and in_progress:
                raise DataStoreError(409, "This event is already in progress")
            if update.status == "in_progress" and completed and update.event_id != RECURRING_EVENT_ID:
                raise DataStoreError(409, "A completed non-recurring event cannot be added to the plan")

            before_recommendations = self._recommendations.recommend(employee_id, debug=True)
            rejected = {
                item.event_id: item.reason_code
                for item in before_recommendations.rejected or []
            }
            allowed = update.event_id not in rejected
            if update.status == "completed" and rejected.get(update.event_id) == "IN_PROGRESS":
                allowed = True
            if update.event_id in {item.event_id for item in before_recommendations.required}:
                allowed = True
            if update.event_id == RECURRING_EVENT_ID:
                role_match = (
                    employee.role in event.target_roles
                    or before_recommendations.target.role in event.target_roles
                )
                grade_match = (
                    employee.grade in event.target_grades
                    or before_recommendations.target.grade in event.target_grades
                )
                allowed = role_match and grade_match
            if not allowed:
                reason = rejected.get(update.event_id, "not eligible")
                raise DataStoreError(409, f"Event is not eligible for this employee: {reason}")

            before_progress = self._progress.calculate(employee_id)
            record_id = self._next_runtime_record_id()
            assigned_by = "self" if update.source == "self_report" else "hr"
            record = HistoryRecord(
                record_id=record_id,
                employee_id=employee_id,
                event_id=update.event_id,
                date=self._dataset.skills.meta.as_of_date,
                due_date=None,
                status=HistoryStatus(update.status),
                completion_pct=100 if update.status == "completed" else 0,
                score=update.score if update.status == "completed" else None,
                feedback_rating=(
                    update.feedback_rating if update.status == "completed" else None
                ),
                assigned_by=assigned_by,
            )
            candidate = self.loader.build_bundle(
                self._dataset.skills,
                self._dataset.employees,
                self._dataset.events,
                (*self._dataset.history, record),
            )
            self._dataset = candidate
            self._runtime_record_counter += 1
            self._refresh_services()

            after_progress = self._progress.calculate(employee_id)
            after_recommendations = self._recommendations.recommend(employee_id)
            before_levels = {item.skill_id: item.effective_level for item in before_progress.skills}
            skill_changes = [
                SkillChange(
                    skill_id=item.skill_id,
                    name=item.name,
                    from_level=before_levels.get(item.skill_id, 0),
                    to_level=item.effective_level,
                )
                for item in after_progress.skills
                if item.effective_level != before_levels.get(item.skill_id, 0)
            ]
            return ActivityUpdateResult(
                record_id=record_id,
                employee_id=employee_id,
                event_id=update.event_id,
                status=update.status,
                skills_changed=skill_changes,
                readiness=ReadinessChange(
                    from_pct=before_progress.readiness.readiness_pct,
                    to_pct=after_progress.readiness.readiness_pct,
                ),
                recommendations_before=[
                    item.event_id for item in before_recommendations.recommendations
                ],
                recommendations_after=[
                    item.event_id for item in after_recommendations.recommendations
                ],
                progress_note=(
                    "Activity progress updates the post-review estimate; "
                    "formal assessed levels remain unchanged."
                ),
            )

    def _next_runtime_record_id(self) -> str:
        existing = {item.record_id for item in self._dataset.history}
        while True:
            candidate = f"RT{self._runtime_record_counter:06d}"
            if candidate not in existing:
                return candidate
            self._runtime_record_counter += 1

    def reset(self) -> dict[str, int]:
        with self._lock:
            self._dataset = self._original
            self._runtime_record_counter = 1
            self._refresh_services()
            return self._dataset.counts
