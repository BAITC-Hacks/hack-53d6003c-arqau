from fastapi.testclient import TestClient

from app.main import configured_data_dir, create_app


DATA_DIR = configured_data_dir()


def test_health_reports_loaded_dataset() -> None:
    with TestClient(create_app(DATA_DIR)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["dataset"]["counts"]["employees"] == 200
    assert payload["dataset"]["counts"]["history_records"] == 2743
    assert payload["dataset"]["warnings"] == []


def test_employee_progress_endpoint_supports_arbitrary_employee() -> None:
    with TestClient(create_app(DATA_DIR)) as client:
        response = client.get("/employees/E0028/progress")

    assert response.status_code == 200
    payload = response.json()
    assert payload["employee_id"] == "E0028"
    assert payload["target"]["role"]
    assert payload["readiness"]["requirements_total"] > 0
    assert all("assessed_level" in item for item in payload["skills"])


def test_unknown_employee_returns_404() -> None:
    with TestClient(create_app(DATA_DIR)) as client:
        response = client.get("/employees/E9999/progress")

    assert response.status_code == 404
    assert response.json()["detail"] == "Employee E9999 not found"
