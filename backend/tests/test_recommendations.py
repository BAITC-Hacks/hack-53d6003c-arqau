from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.data_loader import DatasetLoader
from app.main import create_app
from app.recommendations import RecommendationEngine


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DATA = PROJECT_ROOT / "dataset"


def copy_dataset(tmp_path: Path, name: str = "data") -> Path:
    destination = tmp_path / name
    shutil.copytree(SOURCE_DATA, destination)
    return destination


def role_requirements(data_dir: Path, role: str, grade: str) -> dict[str, int]:
    payload = json.loads((data_dir / "skills.json").read_text(encoding="utf-8"))
    return dict(
        next(
            profile["required_skills"]
            for profile in payload["role_profiles"]
            if profile["role"] == role and profile["grade"] == grade
        )
    )


def append_employee(
    data_dir: Path,
    employee_id: str,
    *,
    system_design: int = 2,
    language: str = "en",
    target_role: str = "Backend Engineer",
    target_grade: str = "Senior",
    skills: dict[str, int] | None = None,
    last_review_date: str = "2026-09-30",
    grade: str = "Middle",
) -> dict[str, object]:
    employees_path = data_dir / "employees.json"
    payload = json.loads(employees_path.read_text(encoding="utf-8"))
    employee_skills = skills or role_requirements(data_dir, "Backend Engineer", "Middle")
    employee_skills = dict(employee_skills)
    employee_skills["SK_SYSTEM_DESIGN"] = system_design
    employee_skills.setdefault("SK_PUBLIC_SPEAKING", 1)
    employee = {
        "employee_id": employee_id,
        "full_name": f"Jury Profile {employee_id}",
        "department": "Backend Development",
        "role": "Backend Engineer",
        "grade": grade,
        "manager_id": "E0050",
        "hire_date": "2023-10-01",
        "tenure_months": 36,
        "work_format": "hybrid",
        "preferred_language": language,
        "career_goal": {"target_role": target_role, "target_grade": target_grade},
        "skills": employee_skills,
        "last_review_date": last_review_date,
    }
    payload["employees"].append(employee)
    employees_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return employee


def append_history(
    data_dir: Path,
    employee_id: str,
    rows: list[dict[str, str]],
) -> None:
    history_path = data_dir / "activity_history.csv"
    with history_path.open(encoding="utf-8", newline="") as stream:
        fieldnames = list(csv.DictReader(stream).fieldnames or [])
    with history_path.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        for index, partial in enumerate(rows, start=1):
            row = {
                "record_id": f"T_{employee_id}_{index:03d}",
                "employee_id": employee_id,
                "event_id": partial["event_id"],
                "date": partial.get("date", "2026-09-15"),
                "due_date": partial.get("due_date", ""),
                "status": partial["status"],
                "completion_pct": partial.get(
                    "completion_pct", "100" if partial["status"] == "completed" else "0"
                ),
                "score": partial.get("score", ""),
                "feedback_rating": partial.get("feedback_rating", ""),
                "assigned_by": partial.get("assigned_by", "self"),
            }
            writer.writerow(row)


def load_engine(data_dir: Path) -> RecommendationEngine:
    return RecommendationEngine(DatasetLoader(data_dir).load())


def rejected_map(result: object) -> dict[str, str]:
    return {item.event_id: item.reason_code for item in result.rejected or []}


def test_trap_profile_prioritizes_critical_system_design(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    employee_id = "T_TRAP"
    append_employee(data_dir, employee_id, system_design=2)
    append_history(
        data_dir,
        employee_id,
        [
            {"event_id": "EV_036", "status": "no_show", "date": f"2026-0{month}-15"}
            for month in (6, 7, 8)
        ],
    )

    result = load_engine(data_dir).recommend(employee_id, debug=True)

    assert result.status == "OK"
    assert result.recommendations[0].event_id in {"EV_006", "EV_007"}
    assert result.recommendations[0].event_id != "EV_036"
    assert any(factor.code == "critical_gap_closure" for factor in result.recommendations[0].factors)
    assert "critical" in result.recommendations[0].explanation.lower()


def test_max_level_reached_rejects_event_that_cannot_close_gap(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    append_employee(data_dir, "T_MAX", system_design=3)

    result = load_engine(data_dir).recommend("T_MAX", debug=True)

    assert rejected_map(result)["EV_005"] == "MAX_LEVEL_REACHED"


def test_prerequisites_block_advanced_events_but_fundamentals_survive(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    append_employee(data_dir, "T_PREREQ", system_design=1)

    result = load_engine(data_dir).recommend("T_PREREQ", debug=True)
    rejected = rejected_map(result)

    assert rejected["EV_006"] == "PREREQUISITES"
    assert rejected["EV_007"] == "PREREQUISITES"
    assert "EV_005" in {item.event_id for item in result.recommendations}


def test_mandatory_events_never_appear_in_recommendations(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    append_employee(data_dir, "T_REQUIRED")

    result = load_engine(data_dir).recommend("T_REQUIRED", debug=True)

    recommendation_ids = {item.event_id for item in result.recommendations}
    assert recommendation_ids.isdisjoint({"EV_001", "EV_002", "EV_003", "EV_004"})
    rejected = rejected_map(result)
    assert all(rejected[event_id] == "MANDATORY" for event_id in ("EV_001", "EV_002", "EV_003", "EV_004"))
    assert {item.event_id for item in result.required} == {"EV_001", "EV_002", "EV_003", "EV_004"}


def test_completed_event_is_excluded_but_recurring_club_can_repeat(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    employee_id = "T_REPEAT"
    senior_skills = role_requirements(data_dir, "Backend Engineer", "Senior")
    senior_skills["SK_PUBLIC_SPEAKING"] = 1
    append_employee(data_dir, employee_id, skills=senior_skills)
    append_history(
        data_dir,
        employee_id,
        [
            {"event_id": "EV_006", "status": "completed"},
            {"event_id": "EV_036", "status": "completed"},
        ],
    )

    result = load_engine(data_dir).recommend(employee_id, debug=True)

    assert rejected_map(result)["EV_006"] == "ALREADY_COMPLETED"
    assert "EV_036" in {item.event_id for item in result.recommendations}


def test_in_progress_event_is_not_recommended(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    append_employee(data_dir, "T_ACTIVE")
    append_history(data_dir, "T_ACTIVE", [{"event_id": "EV_006", "status": "in_progress", "completion_pct": "45"}])

    result = load_engine(data_dir).recommend("T_ACTIVE", debug=True)

    assert rejected_map(result)["EV_006"] == "IN_PROGRESS"


def test_lateral_goal_uses_target_role_requirements(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    append_employee(
        data_dir,
        "T_LATERAL",
        target_role="Data Analyst",
        target_grade="Middle",
    )

    engine = load_engine(data_dir)
    result = engine.recommend("T_LATERAL")
    target_requirements = engine.dataset.indexes.role_profiles_by_key[
        ("Data Analyst", "Middle")
    ].required_skills

    assert result.target.role == "Data Analyst"
    assert result.target.is_lateral is True
    assert result.recommendations
    assert all(
        effect.skill_id in target_requirements
        for recommendation in result.recommendations
        for effect in recommendation.expected_effect
    )


def test_no_eligible_event_returns_status_and_rejection_reasons(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    employee_id = "T_NONE"
    senior_skills = role_requirements(data_dir, "Backend Engineer", "Senior")
    senior_skills["SK_SYSTEM_DESIGN"] = 2
    append_employee(data_dir, employee_id, skills=senior_skills)
    events = json.loads((data_dir / "events.json").read_text(encoding="utf-8"))["events"]
    append_history(
        data_dir,
        employee_id,
        [
            {"event_id": event["event_id"], "status": "completed", "date": "2026-09-01"}
            for event in events
            if not event["mandatory"] and event["event_id"] != "EV_036"
        ],
    )

    result = load_engine(data_dir).recommend(employee_id, debug=True)

    assert result.status == "NO_SUITABLE_ACTION"
    assert result.recommendations == []
    assert result.unclosed_gaps
    assert result.rejected and len(result.rejected) == 40


def test_post_review_completion_changes_recommendations(tmp_path: Path) -> None:
    data_dir = copy_dataset(tmp_path)
    append_employee(
        data_dir,
        "T_BEFORE",
        system_design=1,
        last_review_date="2026-09-01",
    )
    append_employee(
        data_dir,
        "T_AFTER",
        system_design=1,
        last_review_date="2026-09-01",
    )
    append_history(
        data_dir,
        "T_AFTER",
        [{"event_id": "EV_005", "status": "completed", "date": "2026-09-20", "score": "88"}],
    )

    engine = load_engine(data_dir)
    before = engine.recommend("T_BEFORE", debug=True)
    after = engine.recommend("T_AFTER", debug=True)

    assert rejected_map(before)["EV_006"] == "PREREQUISITES"
    assert rejected_map(after).get("EV_006") != "PREREQUISITES"
    assert before.recommendations[0].event_id != after.recommendations[0].event_id


@pytest.mark.parametrize("language", ["en", "ru", "kk"])
def test_explanations_are_localized_and_have_three_factors(
    tmp_path: Path, language: str
) -> None:
    data_dir = copy_dataset(tmp_path, f"data-{language}")
    employee_id = f"T_LANG_{language.upper()}"
    append_employee(data_dir, employee_id, language=language)

    result = load_engine(data_dir).recommend(employee_id)

    assert result.recommendations
    assert all(len(item.factors) >= 3 for item in result.recommendations)
    assert all(item.explanation.strip() for item in result.recommendations)
    if language == "ru":
        assert "уровень" in result.recommendations[0].explanation
    elif language == "kk":
        assert "деңгей" in result.recommendations[0].explanation
    else:
        assert "level" in result.recommendations[0].explanation


def test_all_official_employees_return_zero_to_three_recommendations() -> None:
    engine = load_engine(SOURCE_DATA)

    results = [
        engine.recommend(employee.employee_id)
        for employee in engine.dataset.employees.employees
    ]

    assert len(results) == 200
    assert all(0 <= len(result.recommendations) <= 3 for result in results)


def test_recommendations_api_and_debug_visibility() -> None:
    with TestClient(create_app(SOURCE_DATA)) as client:
        regular = client.get("/employees/E0028/recommendations")
        debug = client.get("/employees/E0028/recommendations?debug=true")
        missing = client.get("/employees/E9999/recommendations")

    assert regular.status_code == 200
    assert "rejected" not in regular.json()
    assert debug.status_code == 200
    assert debug.json()["rejected"]
    assert missing.status_code == 404
