from __future__ import annotations

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from .analytics import AnalyticsService, EngagementSignalService
from .auth import (
    AuthError,
    AuthPrincipal,
    AuthService,
    DemoLoginRequest,
    TokenResponse,
)
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


def configured_secret() -> str:
    """Use a stable configured secret, or an ephemeral secret for this process."""
    return os.getenv("CAREER_QUEST_SECRET") or secrets.token_urlsafe(32)


def store_from(request: Request) -> DataStore:
    return request.app.state.store


def auth_from(request: Request) -> AuthService:
    return request.app.state.auth


def analytics_from(request: Request) -> AnalyticsService:
    return AnalyticsService(store_from(request).dataset)


def authenticated_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
) -> AuthPrincipal:
    if not authorization:
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Use a Bearer token from /auth/demo-login.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    scheme, separator, token = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer" or not token:
        raise HTTPException(
            status_code=401,
            detail="Authorization must use a Bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return auth_from(request).verify(token)
    except AuthError as exc:
        raise HTTPException(
            status_code=401,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc


def hr_principal(
    principal: Annotated[AuthPrincipal, Depends(authenticated_principal)],
) -> AuthPrincipal:
    if principal.role != "hr":
        raise HTTPException(status_code=403, detail="HR access is required for this endpoint.")
    return principal


def profile_principal(
    employee_id: str,
    principal: Annotated[AuthPrincipal, Depends(authenticated_principal)],
) -> AuthPrincipal:
    if principal.role == "hr" or principal.employee_id == employee_id:
        return principal
    raise HTTPException(
        status_code=403,
        detail="You may access only your own employee profile.",
    )


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
        app.state.auth = AuthService(configured_secret())
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

    @application.post("/auth/demo-login", tags=["auth"], response_model=TokenResponse)
    def demo_login(credentials: DemoLoginRequest, request: Request) -> TokenResponse:
        if credentials.employee_id is not None:
            if credentials.employee_id not in store_from(request).dataset.indexes.employees_by_id:
                raise HTTPException(
                    status_code=404,
                    detail=f"Employee {credentials.employee_id} not found",
                )
        principal = AuthPrincipal(
            role=credentials.role,
            employee_id=credentials.employee_id,
        )
        return TokenResponse(
            access_token=auth_from(request).issue(principal),
            role=principal.role,
            employee_id=principal.employee_id,
        )

    @application.get("/auth/demo-employees", tags=["auth"])
    def demo_employee_picker(
        request: Request,
        search: str | None = Query(default=None),
        limit: int = Query(default=200, ge=1, le=500),
    ) -> dict[str, object]:
        """Synthetic demo identities for the login picker, including runtime imports."""
        employees = store_from(request).dataset.employees.employees
        if search:
            needle = search.casefold()
            employees = [
                employee
                for employee in employees
                if needle in employee.employee_id.casefold()
                or needle in employee.full_name.casefold()
            ]
        return {
            "total": len(employees),
            "items": [
                {
                    "employee_id": employee.employee_id,
                    "full_name": employee.full_name,
                    "role": employee.role,
                    "grade": employee.grade,
                }
                for employee in employees[:limit]
            ],
        }

    @application.get("/employees", tags=["employees"])
    def list_employees(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
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
    def employee_profile(
        employee_id: str,
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(profile_principal)],
    ) -> dict[str, object]:
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
    def employee_progress(
        employee_id: str,
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(profile_principal)],
    ) -> dict[str, object]:
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
        _principal: Annotated[AuthPrincipal, Depends(profile_principal)],
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
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
        mode: Literal["add", "upsert"] = Query(default="add"),
    ) -> ImportReport | JSONResponse:
        return await run_import(request, mode, False)

    @application.post("/import/dry-run", tags=["runtime"], response_model=ImportReport)
    async def dry_run_import(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
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
        principal: Annotated[AuthPrincipal, Depends(profile_principal)],
    ) -> ActivityUpdateResult:
        if principal.role in {"employee", "manager"} and update.source != "self_report":
            raise HTTPException(
                status_code=403,
                detail="Employees may record only self-reported activities.",
            )
        if principal.role == "hr" and update.source == "self_report":
            raise HTTPException(
                status_code=403,
                detail="HR may not submit an activity as an employee self-report.",
            )
        try:
            return store_from(request).record_activity(employee_id, update)
        except DataStoreError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc

    @application.post("/admin/reset", tags=["runtime"])
    def reset_runtime(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
    ) -> dict[str, object]:
        return {"status": "reset", "counts": store_from(request).reset()}

    @application.get("/hr/status", tags=["hr"])
    def hr_status(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
    ) -> dict[str, object]:
        return {
            "status": "ok",
            "employees": store_from(request).dataset.counts["employees"],
        }

    @application.get("/hr/skill-gaps", tags=["hr"])
    def hr_skill_gaps(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
        department: str | None = Query(default=None),
        role: str | None = Query(default=None),
        grade: Grade | None = Query(default=None),
    ) -> dict[str, object]:
        return analytics_from(request).skill_gaps(department, role, grade)

    @application.get("/hr/no-next-step", tags=["hr"])
    def hr_no_next_step(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
    ) -> dict[str, object]:
        return analytics_from(request).no_next_step()

    @application.get("/hr/participation", tags=["hr"])
    def hr_participation(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
        event_type: str | None = Query(default=None, alias="type"),
        event_format: str | None = Query(default=None, alias="format"),
        start: date | None = Query(default=None),
        end: date | None = Query(default=None),
        period: int | None = Query(default=None, ge=1, le=730),
    ) -> dict[str, object]:
        analytics = analytics_from(request)
        if period is not None and start is None:
            start = analytics.as_of_date - timedelta(days=period - 1)
        if period is not None and end is None:
            end = analytics.as_of_date
        try:
            return analytics.participation(
                event_type,
                event_format,
                start,
                end,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @application.get("/hr/dashboard", tags=["hr"])
    def hr_dashboard(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
    ) -> dict[str, object]:
        return analytics_from(request).dashboard()

    @application.get("/hr/heatmap", tags=["hr"])
    def hr_heatmap(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
        top_n: int = Query(default=10, ge=1, le=60),
    ) -> dict[str, object]:
        return analytics_from(request).heatmap(top_n)

    @application.get("/hr/trend", tags=["hr"])
    def hr_trend(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
    ) -> dict[str, object]:
        return analytics_from(request).trend()

    @application.get("/hr/pipeline", tags=["hr"])
    def hr_pipeline(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
    ) -> dict[str, object]:
        return analytics_from(request).pipeline()

    @application.get("/hr/opportunity-gaps", tags=["hr"])
    def hr_opportunity_gaps(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
    ) -> dict[str, object]:
        return analytics_from(request).opportunity_gaps()

    @application.get("/hr/support-signals", tags=["hr"])
    def hr_support_signals(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(hr_principal)],
    ) -> dict[str, object]:
        analytics = analytics_from(request)
        return EngagementSignalService(analytics.dataset, analytics).signals()

    @application.get("/employees/{employee_id}/journey", tags=["employees"])
    def employee_journey(
        employee_id: str,
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(profile_principal)],
    ) -> dict[str, object]:
        if employee_id not in store_from(request).dataset.indexes.employees_by_id:
            raise HTTPException(status_code=404, detail=f"Employee {employee_id} not found")
        return analytics_from(request).journey(employee_id)

    @application.get("/career/requirements", tags=["career"])
    def career_requirements(
        request: Request,
        _principal: Annotated[AuthPrincipal, Depends(authenticated_principal)],
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
