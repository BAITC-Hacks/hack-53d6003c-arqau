from __future__ import annotations

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .data_store import (
    ActivityUpdate,
    ActivityUpdateResult,
    DataStore,
    DataStoreError,
    ImportReport,
)
from .models import Grade
from .recommendations import EmployeeRecommendations


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


def configured_extra_data_dir() -> Path | None:
    configured = os.getenv("CAREER_QUEST_EXTRA_DATA_DIR")
    return Path(configured) if configured else None


def store_from(request: Request) -> DataStore:
    return request.app.state.store


async def parse_import_request(request: Request) -> dict[str, object]:
    aliases = {
        "skills": "skills",
        "skills.json": "skills",
        "employees": "employees",
        "employees.json": "employees",
        "events": "events",
        "events.json": "events",
        "history": "history",
        "activity_history": "history",
        "activity_history.csv": "history",
    }
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("multipart/form-data"):
        payload: dict[str, object] = {}
        form = await request.form()
        for field_name, value in form.multi_items():
            if not hasattr(value, "read"):
                continue
            filename = Path(getattr(value, "filename", "") or field_name).name
            key = aliases.get(filename, aliases.get(field_name))
            if key is None:
                continue
            content = (await value.read()).decode("utf-8-sig")
            try:
                parsed = (
                    DataStore.parse_history_csv(content)
                    if key == "history"
                    else json.loads(content)
                )
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise HTTPException(status_code=400, detail=f"Invalid {filename}: {exc}") from exc
            payload[key] = parsed
        return payload

    try:
        raw = await request.json()
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise HTTPException(status_code=400, detail="Import body must be a JSON object")
    if "meta" in raw and "employees" in raw:
        return {"employees": raw}
    return {aliases.get(key, key): value for key, value in raw.items()}


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    source_dir = Path(data_dir) if data_dir is not None else configured_data_dir()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.store = DataStore(source_dir, configured_extra_data_dir())
        for warning in app.state.store.dataset.warnings:
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
        dataset = store_from(request).dataset
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
        dataset = store_from(request).dataset
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
        store = store_from(request)
        dataset = store.dataset
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
        store = store_from(request)
        dataset = store.dataset
        if employee_id not in dataset.indexes.employees_by_id:
            raise HTTPException(status_code=404, detail=f"Employee {employee_id} not found")
        return store.progress.calculate(employee_id).model_dump(mode="json")

    @application.get(
        "/employees/{employee_id}/recommendations",
        tags=["employees"],
        response_model=EmployeeRecommendations,
        response_model_exclude_unset=True,
    )
    def employee_recommendations(
        employee_id: str,
        request: Request,
        debug: bool = Query(default=False),
    ) -> EmployeeRecommendations:
        store = store_from(request)
        dataset = store.dataset
        if employee_id not in dataset.indexes.employees_by_id:
            raise HTTPException(status_code=404, detail=f"Employee {employee_id} not found")
        return store.recommendations.recommend(employee_id, debug=debug)

    async def run_import(
        request: Request,
        mode: Literal["add", "upsert"],
        dry_run: bool,
    ) -> ImportReport | JSONResponse:
        payload = await parse_import_request(request)
        report = store_from(request).import_data(payload, mode=mode, dry_run=dry_run)
        if report.errors and not dry_run:
            status_code = 409 if all("already exists" in item.message for item in report.errors) else 422
            return JSONResponse(status_code=status_code, content=report.model_dump(mode="json"))
        return report

    @application.post("/import", tags=["runtime"], response_model=ImportReport)
    async def import_dataset(
        request: Request,
        mode: Literal["add", "upsert"] = Query(default="add"),
    ) -> ImportReport | JSONResponse:
        return await run_import(request, mode, False)

    @application.post("/import/dry-run", tags=["runtime"], response_model=ImportReport)
    async def dry_run_import(
        request: Request,
        mode: Literal["add", "upsert"] = Query(default="add"),
    ) -> ImportReport | JSONResponse:
        return await run_import(request, mode, True)

    @application.post(
        "/employees/{employee_id}/activities",
        tags=["employees"],
        response_model=ActivityUpdateResult,
        response_model_by_alias=True,
    )
    def record_activity(
        employee_id: str,
        update: ActivityUpdate,
        request: Request,
    ) -> ActivityUpdateResult:
        try:
            return store_from(request).record_activity(employee_id, update)
        except DataStoreError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @application.post("/admin/reset", tags=["runtime"])
    def reset_runtime(request: Request) -> dict[str, object]:
        return {"status": "reset", "counts": store_from(request).reset()}

    @application.get("/career/requirements", tags=["career"])
    def career_requirements(
        request: Request,
        role: str = Query(min_length=1),
        grade: Grade = Query(),
    ) -> dict[str, object]:
        dataset = store_from(request).dataset
        profile = dataset.indexes.role_profiles_by_key.get((role, grade.value))
        if profile is None:
            raise HTTPException(
                status_code=404,
                detail=f"Role/grade profile {role}/{grade.value} not found",
            )
        return profile.model_dump(mode="json")

    return application


app = create_app()
