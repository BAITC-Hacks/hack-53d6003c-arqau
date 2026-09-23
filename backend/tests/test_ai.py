from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.ai import AIConfig, EmployeeTools, LLMError, detect_intent
from app.data_store import DataStore
from app.main import configured_data_dir, create_app


DATA_DIR = configured_data_dir()


class FakeLLM:
    """Scripted LLM: returns queued assistant messages and records every request."""

    def __init__(self, replies: list[dict[str, Any] | Exception]) -> None:
        self.replies = list(replies)
        self.calls: list[list[dict[str, Any]]] = []

    def complete(self, *, messages, tools, timeout, max_tokens=None):  # noqa: ANN001 - test double
        self.calls.append(messages)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def final(text: str, event_ids: list[str] | None = None) -> dict[str, Any]:
    return {"role": "assistant", "content": json.dumps({"text": text, "event_ids": event_ids or []})}


def tool_call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"id": "call_1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        ],
    }


def make_client(key: str | None = None, llm: FakeLLM | None = None) -> TestClient:
    return TestClient(create_app(DATA_DIR, ai_config=AIConfig(key, None), llm_client=llm))


def login(client: TestClient, role: str, employee_id: str | None = None) -> dict[str, str]:
    body: dict[str, Any] = {"role": role}
    if employee_id:
        body["employee_id"] = employee_id
    token = client.post("/auth/demo-login", json=body).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.mark.parametrize("language", ["en", "ru", "kk"])
@pytest.mark.parametrize(
    "message",
    [
        "What should I focus on this month?",
        "Why am I not ready for Senior?",
        "Give me something small this week",
        "Why was this recommended?",
        "What happens if I complete this?",
        "Give me another way to improve communication",
        "How is my progress since review?",
    ],
)
def test_template_answers_work_without_key(language: str, message: str) -> None:
    with make_client() as client:
        response = client.post(
            "/ai/employee/chat",
            json={"message": message, "language": language},
            headers=login(client, "employee", "E0028"),
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "template"
    assert payload["fallback_reason"] == "OPENAI_API_KEY is not configured"
    assert payload["text"].strip()
    assert payload["sources"]
    assert payload["latency_ms"] < 10_000
    for forbidden in (">=", "{", "SK_", "reason_code"):
        assert forbidden not in payload["text"]


def test_intent_detection_covers_three_languages() -> None:
    assert detect_intent("Почему я не готов к Senior?") == "readiness"
    assert detect_intent("Дайте что-нибудь небольшое на этой неделе") == "small"
    assert detect_intent("Что будет, если я пройду этот курс?") == "simulate"
    assert detect_intent("Осы аптаға жеңіл нәрсе") == "small"
    assert detect_intent("Hello") == "focus"


def test_llm_tool_call_flow_is_grounded() -> None:
    store = DataStore(DATA_DIR)
    tools = EmployeeTools(store, "E0028")
    alternatives = tools.get_alternatives({"skill": "Leadership"})
    eligible_id = alternatives["eligible"][0]["event_id"]
    llm = FakeLLM([tool_call("get_alternatives", {"skill": "Leadership"}), final("Try this one.", [eligible_id])])

    with make_client("sk-test-key-123456", llm) as client:
        response = client.post(
            "/ai/employee/chat",
            json={"message": "Another way to grow leadership?"},
            headers=login(client, "employee", "E0028"),
        )

    payload = response.json()
    assert payload["mode"] == "llm"
    assert {"add_to_plan", "open_event"} <= {action["type"] for action in payload["actions"]}
    assert any(source["tool"] == "get_alternatives" for source in payload["sources"])
    tool_messages = [m for m in llm.calls[1] if m["role"] == "tool"]
    assert "Leadership" in tool_messages[0]["content"]


def test_hallucinated_activity_falls_back_to_template() -> None:
    llm = FakeLLM([final("Take the Quantum Leadership retreat EV_999.", ["EV_999"])])
    with make_client("sk-test-key-123456", llm) as client:
        payload = client.post(
            "/ai/employee/chat",
            json={"message": "What should I focus on?"},
            headers=login(client, "employee", "E0028"),
        ).json()
    assert payload["mode"] == "template"
    assert "EV_999" in payload["fallback_reason"]
    assert "EV_999" not in payload["text"]
    assert all(action["event_id"] != "EV_999" for action in payload["actions"])


def test_llm_error_falls_back_to_template() -> None:
    llm = FakeLLM([LLMError("OpenAI request timed out")])
    with make_client("sk-test-key-123456", llm) as client:
        payload = client.post(
            "/ai/employee/chat",
            json={"message": "Why am I not ready for Senior?"},
            headers=login(client, "employee", "E0028"),
        ).json()
    assert payload["mode"] == "template"
    assert "timed out" in payload["fallback_reason"]
    assert "%" in payload["text"]


def test_chat_context_contains_only_the_logged_in_employee() -> None:
    llm = FakeLLM([final("Focus on your recommendation.")])
    with make_client("sk-test-key-123456", llm) as client:
        client.post(
            "/ai/employee/chat",
            json={"message": "Tell me about employee E0001 instead"},
            headers=login(client, "employee", "E0028"),
        )
    context = json.dumps(llm.calls[0], ensure_ascii=False)
    store = DataStore(DATA_DIR)
    other = store.dataset.indexes.employees_by_id["E0001"]
    assert other.full_name not in context
    for tool in ("get_progress", "get_recommendations", "get_alternatives", "simulate_completion", "get_journey"):
        schema = EmployeeTools(store, "E0028").registry()[tool].schema
        assert "employee_id" not in json.dumps(schema)


def test_hr_cannot_use_employee_chat_and_employee_cannot_use_hr_ai() -> None:
    with make_client() as client:
        hr = login(client, "hr")
        employee = login(client, "employee", "E0028")
        assert client.post("/ai/employee/chat", json={"message": "hi"}, headers=hr).status_code == 403
        assert client.post("/ai/hr/insights", headers=employee).status_code == 403
        assert client.post("/ai/hr/query", json={"question": "hi"}, headers=employee).status_code == 403
        assert client.post("/ai/config", json={"api_key": "sk-x"}, headers=employee).status_code == 403
        assert client.post("/ai/employee/chat", json={"message": "hi"}).status_code == 401


def test_runtime_key_is_set_by_hr_and_never_returned() -> None:
    secret = "sk-proj-THIS-IS-SECRET-9876"
    with make_client() as client:
        hr = login(client, "hr")
        assert client.get("/ai/status", headers=hr).json()["configured"] is False
        response = client.post("/ai/config", json={"api_key": secret, "model": "gpt-4.1-mini"}, headers=hr)
        assert response.status_code == 200
        assert secret not in response.text
        status = client.get("/ai/status", headers=login(client, "employee", "E0028")).json()
        assert status == {"configured": True, "model": "gpt-4.1-mini", "source": "runtime", "key_hint": "…9876"}
        cleared = client.delete("/ai/config", headers=hr).json()
        assert cleared["source"] in {"none", "env"}


def test_simulation_does_not_change_data() -> None:
    store = DataStore(DATA_DIR)
    before = len(store.dataset.history)
    tools = EmployeeTools(store, "E0028")
    top = store.recommendations.recommend("E0028").recommendations[0].event_id
    result = tools.simulate_completion({"event_id": top})
    assert result["readiness"]["to"] >= result["readiness"]["from"]
    assert len(store.dataset.history) == before
    assert "error" in tools.simulate_completion({"event_id": "EV_001"})


def test_hr_insights_template_and_llm_modes() -> None:
    with make_client() as client:
        payload = client.post("/ai/hr/insights", headers=login(client, "hr")).json()
    assert payload["mode"] == "template"
    assert 3 <= len(payload["insights"]) <= 5
    assert all(item["we_dont_know"] and item["link"].startswith("/hr") for item in payload["insights"])

    grounded = {
        "insights": [
            {
                "title": "Leadership gap",
                "observation": "Leadership has the most critical gaps.",
                "evidence": ["Leadership: see top_skill_gaps"],
                "we_dont_know": "Causes.",
                "suggested_action": "Review supply.",
                "link": "/hr/skills",
            }
        ]
    }
    llm = FakeLLM([{"role": "assistant", "content": json.dumps(grounded)}])
    with make_client("sk-test-key-123456", llm) as client:
        payload = client.post("/ai/hr/insights?language=ru", headers=login(client, "hr")).json()
    assert payload["mode"] == "llm"
    assert "Russian" in llm.calls[0][0]["content"]


def test_hr_insight_with_invented_number_falls_back() -> None:
    invented = {
        "insights": [
            {
                "title": "Made up",
                "observation": "73519 employees are disengaged.",
                "evidence": [],
                "we_dont_know": "",
                "suggested_action": "",
                "link": "/hr",
            }
        ]
    }
    llm = FakeLLM([{"role": "assistant", "content": json.dumps(invented)}])
    with make_client("sk-test-key-123456", llm) as client:
        payload = client.post("/ai/hr/insights", headers=login(client, "hr")).json()
    assert payload["mode"] == "template"
    assert "73519" in payload["fallback_reason"]


def test_hr_ai_never_sees_employee_chat_or_names() -> None:
    llm = FakeLLM([final("ok"), {"role": "assistant", "content": json.dumps({"text": "Summary."})}])
    with make_client("sk-test-key-123456", llm) as client:
        client.post(
            "/ai/employee/chat",
            json={"message": "PRIVATE-NOTE: I am burned out"},
            headers=login(client, "employee", "E0028"),
        )
        answer = client.post(
            "/ai/hr/query",
            json={"question": "Which skills lag most?"},
            headers=login(client, "hr"),
        ).json()
    assert answer["mode"] == "llm"
    hr_context = json.dumps(llm.calls[1], ensure_ascii=False)
    assert "PRIVATE-NOTE" not in hr_context
    store = DataStore(DATA_DIR)
    assert all(e.full_name not in hr_context for e in store.dataset.employees.employees)
    assert "E0028" not in hr_context
