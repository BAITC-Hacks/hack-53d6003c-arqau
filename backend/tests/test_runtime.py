from __future__ import annotations

import csv
import json
import shutil
from io import StringIO
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DATA = PROJECT_ROOT / "dataset"


def copy_dataset(tmp_path: Path) -> Path:
    destination = tmp_path / "dataset"
    shutil.copytree(SOURCE_DATA, destination)
    return destination


def employee_document(employee_id: str) -> dict[str, object]:
    payload = json.loads((SOURCE_DATA / "employees.json").read_text(encoding="utf-8"))
    employee = dict(payload["employees"][27])
    employee["employee_id"] = employee_id
    employee["full_name"] = f"Jury Import {employee_id}"
    employee["manager_id"] = None
    return {"meta": payload["meta"], "employees": [employee]}


def history_row(employee_id: str, event_id: str = "EV_036") -> dict[str, object]:
    return {
        "record_id": f"JH_{employee_id}",
        "employee_id": employee_id,
        "event_id": event_id,
        "date": "2026-09-01",
        "due_date": None,
        "status": "no_show",
        "completion_pct": 0,
        "score": None,
        "feedback_rating": None,
        "assigned_by": "self",
    }


def history_csv(row: dict[str, object]) -> str:
    stream = StringIO()
    writer = csv.DictWriter(stream, fieldnames=list(row))
    writer.writeheader()
    writer.writerow({key: "" if value is None else value for key, value in row.items()})
    return stream.getvalue()


def test_import_employee_and_history_together_then_recommend(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    employee_id = "JURY_JSON"
    with TestClient(create_app(data_dir)) as client:
        response = client.post(
            "/import",
            json={
                "employees": employee_document(employee_id),
                "activity_history": [history_row(employee_id)],
            },
        )
        recommendations = client.get(f"/employees/{employee_id}/recommendations")

    assert response.status_code == 200
    assert response.json()["added_employees"] == [employee_id]
    assert response.json()["added_history"] == [f"JH_{employee_id}"]
    assert recommendations.status_code == 200
    assert 0 <= len(recommendations.json()["recommendations"]) <= 3


def test_multipart_import_and_dry_run_do_not_require_restart(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    employee_id = "JURY_MULTIPART"
    employee_bytes = json.dumps(employee_document(employee_id)).encode()
    history_bytes = history_csv(history_row(employee_id)).encode()
    files = [
        ("files", ("employees.json", employee_bytes, "application/json")),
        ("files", ("activity_history.csv", history_bytes, "text/csv")),
    ]
    with TestClient(create_app(data_dir)) as client:
        dry_run = client.post("/import/dry-run", files=files)
        absent_after_dry_run = client.get(f"/employees/{employee_id}")
        applied = client.post("/import", files=files)
        present_after_import = client.get(f"/employees/{employee_id}")

    assert dry_run.status_code == 200
    assert dry_run.json()["errors"] == []
    assert absent_after_dry_run.status_code == 404
    assert applied.status_code == 200
    assert present_after_import.status_code == 200


def test_unknown_event_import_fails_atomically_with_record_error(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    employee_id = "JURY_BAD_EVENT"
    with TestClient(create_app(data_dir)) as client:
        before = client.get("/health").json()["dataset"]["counts"]
        response = client.post(
            "/import",
            json={
                "employees": employee_document(employee_id),
                "history": [history_row(employee_id, "EV_UNKNOWN")],
            },
        )
        after = client.get("/health").json()["dataset"]["counts"]
        missing = client.get(f"/employees/{employee_id}")

    assert response.status_code == 422
    issue = response.json()["errors"][0]
    assert issue["file"] == "activity_history.csv"
    assert issue["id"] == f"JH_{employee_id}"
    assert "EV_UNKNOWN" in issue["message"]
    assert after == before
    assert missing.status_code == 404


def test_duplicate_requires_upsert_and_conflict_changes_nothing(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    duplicate = employee_document("E0028")
    duplicate["employees"][0]["preferred_language"] = "en"
    with TestClient(create_app(data_dir)) as client:
        conflict = client.post("/import", json={"employees": duplicate})
        unchanged = client.get("/employees/E0028").json()
        upsert = client.post("/import?mode=upsert", json={"employees": duplicate})
        changed = client.get("/employees/E0028").json()

    assert conflict.status_code == 409
    assert conflict.json()["errors"]
    assert unchanged["preferred_language"] == "kk"
    assert upsert.status_code == 200
    assert changed["preferred_language"] == "en"


def test_completion_recalculates_progress_and_recommendations(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    employee_id = "E0028"
    with TestClient(create_app(data_dir)) as client:
        before_progress = client.get(f"/employees/{employee_id}/progress").json()
        before_recommendations = client.get(
            f"/employees/{employee_id}/recommendations"
        ).json()
        event_id = before_recommendations["recommendations"][0]["event_id"]
        response = client.post(
            f"/employees/{employee_id}/activities",
            json={
                "event_id": event_id,
                "status": "completed",
                "score": 92,
                "feedback_rating": 5,
                "source": "self_report",
            },
        )
        after_progress = client.get(f"/employees/{employee_id}/progress").json()
        after_recommendations = client.get(
            f"/employees/{employee_id}/recommendations"
        ).json()

    assert response.status_code == 200
    diff = response.json()
    assert diff["skills_changed"]
    assert diff["readiness"]["to"] >= diff["readiness"]["from"]
    assert event_id in diff["recommendations_before"]
    assert event_id not in diff["recommendations_after"]
    assert diff["recommendations_before"] != diff["recommendations_after"]
    assert after_progress["readiness"]["readiness_pct"] >= before_progress["readiness"]["readiness_pct"]
    assert event_id not in {item["event_id"] for item in after_recommendations["recommendations"]}
    assert "estimate" in diff["progress_note"]


def test_add_to_plan_removes_event_without_changing_skills(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    employee_id = "E0028"
    with TestClient(create_app(data_dir)) as client:
        recommendation = client.get(
            f"/employees/{employee_id}/recommendations"
        ).json()["recommendations"][0]
        response = client.post(
            f"/employees/{employee_id}/activities",
            json={
                "event_id": recommendation["event_id"],
                "status": "in_progress",
                "source": "self_report",
            },
        )

    assert response.status_code == 200
    diff = response.json()
    assert diff["skills_changed"] == []
    assert recommendation["event_id"] not in diff["recommendations_after"]


def test_non_recurring_completion_conflicts_but_ev036_can_repeat(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    with TestClient(create_app(data_dir)) as client:
        event_id = client.get("/employees/E0028/recommendations").json()["recommendations"][0][
            "event_id"
        ]
        payload = {"event_id": event_id, "status": "completed", "source": "self_report"}
        first = client.post("/employees/E0028/activities", json=payload)
        duplicate = client.post("/employees/E0028/activities", json=payload)
        recurring_payload = {
            "event_id": "EV_036",
            "status": "completed",
            "source": "self_report",
        }
        recurring_first = client.post("/employees/E0051/activities", json=recurring_payload)
        recurring_second = client.post("/employees/E0051/activities", json=recurring_payload)

    assert first.status_code == 200
    assert duplicate.status_code == 409
    assert recurring_first.status_code == 200
    assert recurring_second.status_code == 200
    assert recurring_first.json()["record_id"] != recurring_second.json()["record_id"]


def test_reset_restores_original_counts_and_profiles(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    employee_id = "JURY_RESET"
    with TestClient(create_app(data_dir)) as client:
        original = client.get("/health").json()["dataset"]["counts"]
        imported = client.post(
            "/import", json={"employees": employee_document(employee_id)}
        )
        changed = client.get("/health").json()["dataset"]["counts"]
        reset = client.post("/admin/reset")
        restored = client.get("/health").json()["dataset"]["counts"]
        missing = client.get(f"/employees/{employee_id}")

    assert imported.status_code == 200
    assert changed["employees"] == original["employees"] + 1
    assert reset.status_code == 200
    assert restored == original
    assert missing.status_code == 404


def test_startup_extra_data_directory_is_merged_and_resettable(
    tmp_path: Path, monkeypatch
) -> None:
    data_dir = copy_dataset(tmp_path)
    extra_dir = tmp_path / "extra"
    extra_dir.mkdir()
    employee_id = "JURY_STARTUP"
    (extra_dir / "employees.json").write_text(
        json.dumps(employee_document(employee_id)), encoding="utf-8"
    )
    monkeypatch.setenv("CAREER_QUEST_EXTRA_DATA_DIR", str(extra_dir))

    with TestClient(create_app(data_dir)) as client:
        loaded = client.get(f"/employees/{employee_id}")
        reset = client.post("/admin/reset")
        removed = client.get(f"/employees/{employee_id}")

    assert loaded.status_code == 200
    assert reset.status_code == 200
    assert removed.status_code == 404
