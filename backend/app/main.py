from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request

from .career_progress import SkillProgressService
from .data_loader import DatasetBundle, DatasetLoader
from .models import Grade


PROJECT_ROOT = Path(__file__).resolve().parents[2]
logger = logging.getLogger(__name__)


def configured_data_dir() -> Path:
    configured = os.getenv("CAREER_QUEST_DATA_DIR")
    if configured:
        return Path(configured)
    for directory_name in ("data", "dataset"):
        candidate = PROJECT_ROOT / directory_name
        if candidate.is_dir():
            return candidate
    return PROJECT_ROOT / "data"


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    source_dir = Path(data_dir) if data_dir is not None else configured_data_dir()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.dataset = DatasetLoader(source_dir).load()
        for warning in app.state.dataset.warnings:
            logger.warning("Dataset warning: %s", warning)
        yield

    application = FastAPI(
        title="Career Quest API",
        version="0.1.0",
        description="Explainable employee development platform for HackAlem AI.",
        lifespan=lifespan,
    )

    @application.get("/health", tags=["system"])
    def health(request: Request) -> dict[str, object]:
        dataset: DatasetBundle = request.app.state.dataset
        return {
            "status": "ok",
            "service": "career-quest-api",
            "dataset": {
                "name": dataset.skills.meta.dataset,
                "version": dataset.skills.meta.version,
                "as_of_date": dataset.skills.meta.as_of_date.isoformat(),
                "source": str(dataset.data_dir),
                "counts": dataset.counts,
                "warnings": list(dataset.warnings),
            },
        }

    @application.get("/employees", tags=["employees"])
    def list_employees(
        request: Request,
        limit: int = Query(default=50, ge=1, le=200),
        offset: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        dataset: DatasetBundle = request.app.state.dataset
        employees = dataset.employees.employees[offset : offset + limit]
        return {
            "total": len(dataset.employees.employees),
            "offset": offset,
            "limit": limit,
            "items": [
                {
                    "employee_id": employee.employee_id,
                    "full_name": employee.full_name,
                    "department": employee.department,
                    "role": employee.role,
                    "grade": employee.grade,
                    "preferred_language": employee.preferred_language,
                }
                for employee in employees
            ],
        }

    @application.get("/employees/{employee_id}", tags=["employees"])
    def employee_profile(employee_id: str, request: Request) -> dict[str, object]:
        dataset: DatasetBundle = request.app.state.dataset
        employee = dataset.indexes.employees_by_id.get(employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail=f"Employee {employee_id} not found")
        history = dataset.indexes.history_by_employee.get(employee_id, ())
        completed = [record for record in history if record.status.value == "completed"]
        return {
            **employee.model_dump(mode="json"),
            "activity_summary": {
                "history_records": len(history),
                "completed": len(completed),
                "completed_event_ids": [record.event_id for record in completed],
            },
        }

    @application.get("/employees/{employee_id}/progress", tags=["employees"])
    def employee_progress(employee_id: str, request: Request) -> dict[str, object]:
        dataset: DatasetBundle = request.app.state.dataset
        if employee_id not in dataset.indexes.employees_by_id:
            raise HTTPException(status_code=404, detail=f"Employee {employee_id} not found")
        return SkillProgressService(dataset).calculate(employee_id).model_dump(mode="json")

    @application.get("/career/requirements", tags=["career"])
    def career_requirements(
        request: Request,
        role: str = Query(min_length=1),
        grade: Grade = Query(),
    ) -> dict[str, object]:
        dataset: DatasetBundle = request.app.state.dataset
        profile = dataset.indexes.role_profiles_by_key.get((role, grade.value))
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"Role/grade profile {role}/{grade.value} not found",
            )
        return profile.model_dump(mode="json")

    return application


app = create_app()
