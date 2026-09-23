from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_DATA = PROJECT_ROOT / "dataset"


def login(client: TestClient, role: str, employee_id: str | None = None) -> dict[str, str]:
    payload = {"role": role}
    if employee_id is not None:
        payload["employee_id"] = employee_id
    response = client.post("/auth/demo-login", json=payload)
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_health_and_demo_identity_picker_stay_public() -> None:
    with TestClient(create_app(SOURCE_DATA)) as client:
        health = client.get("/health")
        picker = client.get("/auth/demo-employees?search=E0028")

    assert health.status_code == 200
    assert picker.status_code == 200
    assert [item["employee_id"] for item in picker.json()["items"]] == ["E0028"]


def test_no_token_returns_clear_401_on_protected_route() -> None:
    with TestClient(create_app(SOURCE_DATA)) as client:
        response = client.get("/employees/E0028")

    assert response.status_code == 401
    assert "Authentication required" in response.json()["detail"]
    assert response.headers["www-authenticate"] == "Bearer"


def test_employee_can_read_own_profile_but_not_another_employee() -> None:
    with TestClient(create_app(SOURCE_DATA)) as client:
        headers = login(client, "employee", "E0028")
        own = client.get("/employees/E0028/progress", headers=headers)
        other = client.get("/employees/E0029/progress", headers=headers)
        directory = client.get("/employees", headers=headers)

    assert own.status_code == 200
    assert other.status_code == 403
    assert "only your own" in other.json()["detail"]
    assert directory.status_code == 403


def test_employee_cannot_call_hr_or_admin_routes_but_hr_can() -> None:
    with TestClient(create_app(SOURCE_DATA)) as client:
        employee_headers = login(client, "employee", "E0028")
        hr_headers = login(client, "hr")

        employee_hr = client.get("/hr/status", headers=employee_headers)
        employee_reset = client.post("/admin/reset", headers=employee_headers)
        hr_status = client.get("/hr/status", headers=hr_headers)
        hr_profile = client.get("/employees/E0029", headers=hr_headers)

    assert employee_hr.status_code == 403
    assert employee_reset.status_code == 403
    assert hr_status.status_code == 200
    assert hr_status.json()["employees"] == 200
    assert hr_profile.status_code == 200


def test_employee_can_write_only_own_self_reported_activity() -> None:
    with TestClient(create_app(SOURCE_DATA)) as client:
        headers = login(client, "employee", "E0028")
        recommendation = client.get(
            "/employees/E0028/recommendations", headers=headers
        ).json()["recommendations"][0]
        payload = {
            "event_id": recommendation["event_id"],
            "status": "in_progress",
            "source": "self_report",
        }

        own = client.post("/employees/E0028/activities", json=payload, headers=headers)
        other = client.post("/employees/E0029/activities", json=payload, headers=headers)
        forged_source = client.post(
            "/employees/E0028/activities",
            json={**payload, "source": "hr_confirmed"},
            headers=headers,
        )

    assert own.status_code == 200
    assert other.status_code == 403
    assert forged_source.status_code == 403
    assert "self-reported" in forged_source.json()["detail"]


def test_tampered_and_unknown_employee_tokens_are_rejected() -> None:
    with TestClient(create_app(SOURCE_DATA)) as client:
        unknown = client.post(
            "/auth/demo-login",
            json={"role": "employee", "employee_id": "E9999"},
        )
        token = login(client, "employee", "E0028")["Authorization"].removeprefix(
            "Bearer "
        )
        replacement = "A" if token[-1] != "A" else "B"
        tampered = client.get(
            "/employees/E0028",
            headers={"Authorization": f"Bearer {token[:-1]}{replacement}"},
        )

    assert unknown.status_code == 404
    assert tampered.status_code == 401
    assert tampered.json()["detail"] == "Invalid authentication token"
