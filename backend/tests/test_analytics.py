from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from app.analytics import AnalyticsService, EngagementSignalService
from app.data_loader import DatasetLoader
from app.main import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DATA = PROJECT_ROOT / "dataset"


def copy_dataset(tmp_path: Path) -> Path:
    destination = tmp_path / "dataset"
    shutil.copytree(SOURCE_DATA, destination)
    return destination


def employee_template(data_dir: Path, employee_id: str, *, all_skills: bool = False) -> dict:
    employees = json.loads((data_dir / "employees.json").read_text(encoding="utf-8"))
    employee = dict(employees["employees"][27])
    employee["employee_id"] = employee_id
    employee["full_name"] = f"Synthetic {employee_id}"
    employee["manager_id"] = None
    if all_skills:
        skills = json.loads((data_dir / "skills.json").read_text(encoding="utf-8"))
        employee["skills"] = {item["skill_id"]: 5 for item in skills["skills"]}
    return employee


def append_employees(data_dir: Path, employees: list[dict]) -> None:
    path = data_dir / "employees.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["employees"].extend(employees)
    path.write_text(json.dumps(payload), encoding="utf-8")


def append_history(data_dir: Path, rows: list[dict]) -> None:
    path = data_dir / "activity_history.csv"
    with path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(
            stream,
            fieldnames=[
                "record_id",
                "employee_id",
                "event_id",
                "date",
                "due_date",
                "status",
                "completion_pct",
                "score",
                "feedback_rating",
                "assigned_by",
            ],
        )
        writer.writerows(rows)


def history_row(
    record_id: str,
    employee_id: str,
    date: str,
    status: str,
    *,
    event_id: str = "EV_036",
) -> dict:
    return {
        "record_id": record_id,
        "employee_id": employee_id,
        "event_id": event_id,
        "date": date,
        "due_date": "2026-09-01" if status == "overdue" else "",
        "status": status,
        "completion_pct": 20 if status in {"dropped", "overdue"} else 0,
        "score": "",
        "feedback_rating": "",
        "assigned_by": "hr" if status == "overdue" else "self",
    }


def hr_headers(client: TestClient) -> dict[str, str]:
    token = client.post("/auth/demo-login", json={"role": "hr"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_required_hr_analytics_and_filters() -> None:
    with TestClient(create_app(SOURCE_DATA)) as client:
        headers = hr_headers(client)
        gaps = client.get(
            "/hr/skill-gaps?department=Engineering&grade=Middle", headers=headers
        )
        participation = client.get(
            "/hr/participation?format=self_paced&start=2026-01-01", headers=headers
        )
        dashboard = client.get("/hr/dashboard", headers=headers)
        pipeline = client.get("/hr/pipeline", headers=headers)

    assert gaps.status_code == 200
    assert all(item["employees_below_target"] > 0 for item in gaps.json()["items"])
    assert participation.status_code == 200
    assert all(item["format"] == "self_paced" for item in participation.json()["items"])
    assert dashboard.status_code == 200
    assert dashboard.json()["employees"] == 200
    assert pipeline.json()["total"] == 200


def test_support_signal_rules_and_language_are_supportive(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    ids = ["T_DROP", "T_FRICTION", "T_OVERDUE", "T_REENGAGED", "T_NO_OPPORTUNITY"]
    append_employees(
        data_dir,
        [
            employee_template(data_dir, employee_id, all_skills=employee_id == "T_NO_OPPORTUNITY")
            for employee_id in ids
        ],
    )
    rows = [
        history_row(f"SD{i}", "T_DROP", f"2025-{month:02d}-15", "no_show")
        for i, month in enumerate(range(8, 13), start=1)
    ]
    rows.append(history_row("SD6", "T_DROP", "2026-01-15", "no_show"))
    rows.extend(
        [
            history_row("SF1", "T_FRICTION", "2026-08-01", "no_show"),
            history_row("SF2", "T_FRICTION", "2026-09-01", "no_show"),
            history_row("SO1", "T_OVERDUE", "2026-09-01", "overdue", event_id="EV_001"),
            history_row("SR1", "T_REENGAGED", "2026-08-15", "no_show"),
            history_row("SR2", "T_REENGAGED", "2026-09-15", "no_show"),
        ]
    )
    append_history(data_dir, rows)
    dataset = DatasetLoader(data_dir).load()
    signals = EngagementSignalService(dataset).signals()["items"]
    kinds_by_employee: dict[str, set[str]] = {}
    for signal in signals:
        kinds_by_employee.setdefault(signal["employee_id"], set()).add(signal["kind"])

    assert "participation_drop" in kinds_by_employee["T_DROP"]
    assert "repeated_friction" in kinds_by_employee["T_FRICTION"]
    assert "mandatory_overdue" in kinds_by_employee["T_OVERDUE"]
    assert "re_engaged" in kinds_by_employee["T_REENGAGED"]
    assert "no_relevant_opportunity" in kinds_by_employee["T_NO_OPPORTUNITY"]
    rendered = json.dumps(signals).lower()
    for banned in ("lazy", "unmotivated", "low performer", "bad employee"):
        assert banned not in rendered
    assert all("reason is unknown" in signal["what_we_dont_know"][0] for signal in signals)


def test_opportunity_gaps_include_real_blocked_system_design() -> None:
    analytics = AnalyticsService(DatasetLoader(SOURCE_DATA).load())
    items = analytics.opportunity_gaps()["items"]

    assert any(item["skill_id"] == "SK_SYSTEM_DESIGN" for item in items)
    assert all("catalog" in item["summary"] for item in items)


def test_no_next_step_count_updates_after_runtime_import(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    employee = employee_template(data_dir, "T_IMPORTED_NO_STEP", all_skills=True)
    meta = json.loads((data_dir / "employees.json").read_text(encoding="utf-8"))["meta"]
    with TestClient(create_app(data_dir)) as client:
        headers = hr_headers(client)
        before = client.get("/hr/no-next-step", headers=headers).json()["count"]
        imported = client.post(
            "/import",
            headers=headers,
            json={"employees": {"meta": meta, "employees": [employee]}},
        )
        after = client.get("/hr/no-next-step", headers=headers).json()["count"]

    assert imported.status_code == 200
    assert after == before + 1


def test_employee_journey_is_self_only_and_contains_no_peer_data() -> None:
    with TestClient(create_app(SOURCE_DATA)) as client:
        token = client.post(
            "/auth/demo-login", json={"role": "employee", "employee_id": "E0028"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}
        own = client.get("/employees/E0028/journey", headers=headers)
        other = client.get("/employees/E0029/journey", headers=headers)

    assert own.status_code == 200
    assert own.json()["employee_id"] == "E0028"
    assert "E0029" not in own.text
    assert "comparison" not in own.text.lower()
    assert other.status_code == 403


def test_hr_routes_reject_employee_tokens() -> None:
    with TestClient(create_app(SOURCE_DATA)) as client:
        token = client.post(
            "/auth/demo-login", json={"role": "employee", "employee_id": "E0028"}
        ).json()["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        responses = [
            client.get(path, headers=headers)
            for path in (
                "/hr/skill-gaps",
                "/hr/no-next-step",
                "/hr/participation",
                "/hr/dashboard",
                "/hr/support-signals",
            )
        ]

    assert all(response.status_code == 403 for response in responses)
