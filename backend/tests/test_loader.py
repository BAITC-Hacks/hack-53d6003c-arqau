from __future__ import annotations

import csv
import json
import shutil
from pathlib import Path

import pytest

from app.data_loader import DatasetLoader, DatasetValidationError
from app.main import configured_data_dir


DATA_DIR = configured_data_dir()


def test_official_dataset_loads_with_expected_counts() -> None:
    bundle = DatasetLoader(DATA_DIR).load()

    assert bundle.counts == {
        "skills": 60,
        "role_profiles": 32,
        "employees": 200,
        "events": 40,
        "history_records": 2743,
    }
    assert len(bundle.indexes.history_by_employee["E0001"]) > 0
    assert bundle.indexes.events_by_id["EV_036"].title == "Public Speaking Club"
    assert bundle.indexes.role_profiles_by_key[("Backend Engineer", "Senior")]


def test_unknown_history_employee_fails_with_actionable_message(tmp_path: Path) -> None:
    broken_data = tmp_path / "data"
    shutil.copytree(DATA_DIR, broken_data)
    history_path = broken_data / "activity_history.csv"

    with history_path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
        fieldnames = list(rows[0])
    rows[0]["employee_id"] = "E_DOES_NOT_EXIST"
    with history_path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    with pytest.raises(DatasetValidationError, match="R000001.*unknown employee_id E_DOES_NOT_EXIST"):
        DatasetLoader(broken_data).load()


def test_missing_required_file_fails_clearly(tmp_path: Path) -> None:
    with pytest.raises(DatasetValidationError, match="missing required files"):
        DatasetLoader(tmp_path).load()


def test_manager_convention_deviation_warns_instead_of_failing(tmp_path: Path) -> None:
    data = tmp_path / "data"
    shutil.copytree(DATA_DIR, data)
    employees_path = data / "employees.json"
    payload = json.loads(employees_path.read_text(encoding="utf-8"))
    employees = payload["employees"]
    non_lead = next(item for item in employees if item["grade"] != "Lead")
    report = next(
        item
        for item in employees
        if item["employee_id"] != non_lead["employee_id"] and item["manager_id"] is not None
    )
    report["manager_id"] = non_lead["employee_id"]
    employees_path.write_text(json.dumps(payload), encoding="utf-8")

    bundle = DatasetLoader(data).load()

    assert any(
        report["employee_id"] in warning and "not a Lead" in warning for warning in bundle.warnings
    )
    assert report["employee_id"] in bundle.indexes.employees_by_id
