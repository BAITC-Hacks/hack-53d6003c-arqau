"""Grounded AI layer: the deterministic services decide, the LLM only phrases.

The employee Career AI and the HR insight generator call OpenAI through a small
HTTP client with function calling. Every tool reads from the current DataStore,
is bound to the authenticated principal, and returns structured data. When no
API key is configured, the call fails, times out, or the answer references
activities the tools never returned, the same questions are answered by
deterministic templates, so the product works fully offline.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from threading import RLock
from typing import Any, Callable, Literal, Protocol

import httpx
from pydantic import BaseModel, Field

from .analytics import AnalyticsService, EngagementSignalService
from .data_store import DataStore
from .models import HistoryRecord, HistoryStatus


AI_SETTINGS: dict[str, float | int] = {
    "deadline_seconds": 8.0,
    "max_tool_rounds": 4,
    "max_history_messages": 10,
    "max_message_chars": 1000,
    "temperature": 0.2,
}

DEFAULT_MODEL = "gpt-4.1-mini"
DEFAULT_BASE_URL = "https://api.openai.com/v1"

Language = Literal["en", "ru", "kk"]
LANGUAGE_NAMES = {"en": "English", "ru": "Russian", "kk": "Kazakh"}


# --------------------------------------------------------------------------- #
# Configuration and HTTP client
# --------------------------------------------------------------------------- #


class AIStatus(BaseModel):
    configured: bool
    model: str
    source: Literal["env", "runtime", "none"]
    key_hint: str | None = None


class AIConfigUpdate(BaseModel):
    api_key: str = Field(min_length=1, max_length=400)
    model: str | None = Field(default=None, max_length=100)


class AIConfig:
    """Holds the OpenAI key in memory; it is never returned by any endpoint."""

    def __init__(self, api_key: str | None, model: str | None, base_url: str | None = None) -> None:
        self._lock = RLock()
        self._api_key = (api_key or "").strip() or None
        self._model = (model or "").strip() or DEFAULT_MODEL
        self._base_url = (base_url or "").strip() or DEFAULT_BASE_URL
        self._source: Literal["env", "runtime", "none"] = "env" if self._api_key else "none"

    @classmethod
    def from_env(cls) -> "AIConfig":
        return cls(
            os.getenv("OPENAI_API_KEY"),
            os.getenv("OPENAI_MODEL"),
            os.getenv("OPENAI_BASE_URL"),
        )

    @property
    def api_key(self) -> str | None:
        return self._api_key

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base_url

    def set_runtime(self, update: AIConfigUpdate) -> AIStatus:
        with self._lock:
            self._api_key = update.api_key.strip()
            if update.model and update.model.strip():
                self._model = update.model.strip()
            self._source = "runtime"
            return self.status()

    def clear_runtime(self) -> AIStatus:
        with self._lock:
            env = AIConfig.from_env()
            self._api_key, self._model, self._source = env._api_key, env._model, env._source
            return self.status()

    def status(self) -> AIStatus:
        hint = f"…{self._api_key[-4:]}" if self._api_key and len(self._api_key) > 8 else None
        return AIStatus(
            configured=self._api_key is not None,
            model=self._model,
            source=self._source,
            key_hint=hint,
        )


class LLMClient(Protocol):
    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        timeout: float,
    ) -> dict[str, Any]:
        """Return the assistant message dict of the first choice."""


class LLMError(RuntimeError):
    pass


class OpenAIChatClient:
    """Minimal Chat Completions client (function calling + JSON output)."""

    def __init__(self, config: AIConfig) -> None:
        self.config = config

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        timeout: float,
    ) -> dict[str, Any]:
        if not self.config.api_key:
            raise LLMError("OPENAI_API_KEY is not configured")
        body: dict[str, Any] = {
            "model": self.config.model,
            "messages": messages,
            "temperature": AI_SETTINGS["temperature"],
            "response_format": {"type": "json_object"},
        }
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        try:
            response = httpx.post(
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                headers={"Authorization": f"Bearer {self.config.api_key}"},
                json=body,
                timeout=timeout,
            )
        except httpx.TimeoutException as exc:
            raise LLMError("OpenAI request timed out") from exc
        except httpx.HTTPError as exc:
            raise LLMError(f"OpenAI request failed: {type(exc).__name__}") from exc
        if response.status_code != 200:
            try:
                message = response.json().get("error", {}).get("message", "")
            except ValueError:
                message = ""
            raise LLMError(f"OpenAI returned HTTP {response.status_code}: {message[:200]}")
        try:
            return response.json()["choices"][0]["message"]
        except (ValueError, KeyError, IndexError) as exc:
            raise LLMError("OpenAI returned an unexpected response") from exc


# --------------------------------------------------------------------------- #
# Response models
# --------------------------------------------------------------------------- #


class ChatTurn(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(max_length=4000)


class EmployeeChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=int(AI_SETTINGS["max_message_chars"]))
    history: list[ChatTurn] = Field(default_factory=list)
    language: Language | None = None


class AIAction(BaseModel):
    type: Literal["add_to_plan", "open_event"]
    event_id: str
    title: str


class AISource(BaseModel):
    tool: str
    summary: str


class AIAnswer(BaseModel):
    text: str
    actions: list[AIAction] = Field(default_factory=list)
    sources: list[AISource] = Field(default_factory=list)
    mode: Literal["llm", "template"]
    model: str | None = None
    fallback_reason: str | None = None
    latency_ms: int


class HRQueryRequest(BaseModel):
    question: str = Field(min_length=1, max_length=int(AI_SETTINGS["max_message_chars"]))
    language: Language = "en"


class HRInsight(BaseModel):
    title: str
    observation: str
    evidence: list[str]
    we_dont_know: str
    suggested_action: str
    link: str


class HRInsightsResponse(BaseModel):
    insights: list[HRInsight]
    mode: Literal["llm", "template"]
    model: str | None = None
    fallback_reason: str | None = None
    latency_ms: int


# --------------------------------------------------------------------------- #
# Employee tools (bound to one employee; the model cannot choose whose data)
# --------------------------------------------------------------------------- #


@dataclass
class _Tool:
    schema: dict[str, Any]
    handler: Callable[[dict[str, Any]], dict[str, Any]]


def _function(name: str, description: str, properties: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                "required": list((properties or {}).keys()),
                "additionalProperties": False,
            },
        },
    }


class EmployeeTools:
    """Deterministic, read-only views of one employee's data for the LLM and templates."""

    def __init__(self, store: DataStore, employee_id: str) -> None:
        self.store = store
        self.dataset = store.dataset
        self.employee_id = employee_id
        self.employee = self.dataset.indexes.employees_by_id[employee_id]
        self.progress = store.progress.calculate(employee_id)
        self.recommendations = store.recommendations.recommend(employee_id, debug=True)
        self.allowed_event_ids: set[str] = set()
        self.used: list[AISource] = []

    # Shared helpers --------------------------------------------------------- #

    def _remember(self, event_ids: list[str]) -> None:
        self.allowed_event_ids.update(event_ids)

    def _source(self, tool: str, summary: str) -> None:
        if not any(item.tool == tool and item.summary == summary for item in self.used):
            self.used.append(AISource(tool=tool, summary=summary))

    def rejected_codes(self) -> dict[str, str]:
        return {item.event_id: item.reason_code for item in self.recommendations.rejected or []}

    def eligible_event_ids(self) -> list[str]:
        rejected = self.rejected_codes()
        return [event.event_id for event in self.dataset.events.events if event.event_id not in rejected]

    # Tools ------------------------------------------------------------------ #

    def get_progress(self, _: dict[str, Any] | None = None) -> dict[str, Any]:
        progress = self.progress
        readiness = progress.readiness
        self._source(
            "get_progress",
            f"{self.employee.role} {self.employee.grade.value} → {progress.target.role} "
            f"{progress.target.grade.value}; readiness {readiness.readiness_pct}%",
        )
        return {
            "employee": {
                "role": self.employee.role,
                "grade": self.employee.grade.value,
                "tenure_months": self.employee.tenure_months,
                "work_format": self.employee.work_format,
                "last_review_date": self.employee.last_review_date.isoformat(),
            },
            "target": progress.target.model_dump(mode="json"),
            "readiness": {
                "status": readiness.status,
                "readiness_pct": readiness.readiness_pct,
                "requirements_met": readiness.requirements_met,
                "requirements_total": readiness.requirements_total,
                "critical_requirements_met": readiness.critical_requirements_met,
                "critical_requirements_total": readiness.critical_requirements_total,
                "formula": readiness.formula,
            },
            "gaps": [
                {
                    "skill_id": item.skill_id,
                    "name": item.name,
                    "assessed_level": item.assessed_level,
                    "estimate_since_review": item.effective_level,
                    "target_level": item.target_level,
                    "gap": item.gap,
                    "critical": item.critical,
                }
                for item in progress.gaps
            ],
            "met_requirements": [
                {"name": item.name, "estimate": item.effective_level, "target_level": item.target_level}
                for item in progress.skills
                if item.target_level > 0 and item.gap == 0
            ],
            "note": "Activity gains are post-review estimates; formal levels change only at reassessment.",
        }

    def get_recommendations(self, _: dict[str, Any] | None = None) -> dict[str, Any]:
        recs = self.recommendations
        self._remember([item.event_id for item in recs.recommendations])
        self._remember([item.event_id for item in recs.required])
        self._source(
            "get_recommendations",
            f"{len(recs.recommendations)} recommendation(s): "
            + (", ".join(item.title for item in recs.recommendations) or "none"),
        )
        return {
            "status": recs.status,
            "summary": recs.summary,
            "recommendations": [
                {
                    "event_id": item.event_id,
                    "title": item.title,
                    "type": item.type,
                    "format": item.format,
                    "duration_hours": item.duration_hours,
                    "next_session": item.next_session.isoformat() if item.next_session else None,
                    "match_label": item.match_label,
                    "expected_effect": [effect.model_dump() for effect in item.expected_effect],
                    "factors": [{"code": f.code, "detail": f.detail} for f in item.factors],
                    "explanation": item.explanation,
                }
                for item in recs.recommendations
            ],
            "required_mandatory": [item.model_dump(mode="json") for item in recs.required],
            "blocked_gaps": [
                {
                    "name": gap.name,
                    "estimate": gap.effective_level,
                    "target_level": gap.target_level,
                    "critical": gap.critical,
                    "suggestion": gap.suggestion,
                }
                for gap in recs.blocked_gaps
            ],
        }

    def get_alternatives(self, arguments: dict[str, Any]) -> dict[str, Any]:
        skill = self._resolve_skill(str(arguments.get("skill", "")))
        if skill is None:
            return {"error": "Unknown skill. Use a skill name from get_progress gaps."}
        rejected = self.rejected_codes()
        effective = {item.skill_id: item.effective_level for item in self.progress.skills}
        eligible: list[dict[str, Any]] = []
        not_available: list[dict[str, Any]] = []
        for event in self.dataset.events.events:
            development = next((d for d in event.develops_skills if d.skill_id == skill.skill_id), None)
            if development is None:
                continue
            current = effective.get(skill.skill_id, 0)
            entry = {
                "event_id": event.event_id,
                "title": event.title,
                "format": event.format,
                "duration_hours": event.duration_hours,
                "moves_skill": f"{current} → {min(current + development.gain, development.max_level)}",
            }
            if event.event_id in rejected:
                not_available.append({**entry, "reason_code": rejected[event.event_id]})
            else:
                eligible.append(entry)
        self._remember([item["event_id"] for item in eligible + not_available])
        self._source("get_alternatives", f"{skill.name}: {len(eligible)} eligible activity(ies)")
        return {
            "skill": skill.name,
            "eligible": eligible,
            "not_available_for_you": not_available,
        }

    def simulate_completion(self, arguments: dict[str, Any]) -> dict[str, Any]:
        event_id = str(arguments.get("event_id", ""))
        event = self.dataset.indexes.events_by_id.get(event_id)
        if event is None or event_id not in set(self.eligible_event_ids()):
            return {"error": f"{event_id} is not an eligible activity for this employee."}
        record = HistoryRecord(
            record_id="SIMULATION",
            employee_id=self.employee_id,
            event_id=event_id,
            date=self.dataset.skills.meta.as_of_date,
            status=HistoryStatus.COMPLETED,
            completion_pct=100,
            assigned_by="self",
        )
        bundle = self.store.loader.build_bundle(
            self.dataset.skills,
            self.dataset.employees,
            self.dataset.events,
            (*self.dataset.history, record),
        )
        from .career_progress import SkillProgressService
        from .recommendations import RecommendationEngine

        after = SkillProgressService(bundle).calculate(self.employee_id)
        after_recs = RecommendationEngine(bundle).recommend(self.employee_id)
        before_levels = {item.skill_id: item.effective_level for item in self.progress.skills}
        changes = [
            {"skill": item.name, "from": before_levels.get(item.skill_id, 0), "to": item.effective_level}
            for item in after.skills
            if item.effective_level != before_levels.get(item.skill_id, 0)
        ]
        self._remember([event_id, *[item.event_id for item in after_recs.recommendations]])
        self._source(
            "simulate_completion",
            f"{event.title}: readiness {self.progress.readiness.readiness_pct}% → "
            f"{after.readiness.readiness_pct}%",
        )
        return {
            "event_id": event_id,
            "title": event.title,
            "skill_changes": changes,
            "readiness": {
                "from": self.progress.readiness.readiness_pct,
                "to": after.readiness.readiness_pct,
            },
            "next_recommendations": [
                {"event_id": item.event_id, "title": item.title} for item in after_recs.recommendations
            ],
            "note": "Simulation only; nothing was saved. Formal levels change only at reassessment.",
        }

    def get_journey(self, _: dict[str, Any] | None = None) -> dict[str, Any]:
        journey = AnalyticsService(self.dataset).journey(self.employee_id)
        self._source("get_journey", "Progress since review and monthly activity rhythm")
        return {
            "progress_since_review": journey.get("progress_since_review", []),
            "recent_months": journey.get("monthly_activity", [])[-6:],
            "message": journey.get("message"),
        }

    def _resolve_skill(self, query: str):
        query = query.strip().lower()
        if not query:
            return None
        for skill in self.dataset.skills.skills:
            if query in {skill.skill_id.lower(), skill.name.lower()}:
                return skill
        for skill in self.dataset.skills.skills:
            if query in skill.name.lower() or skill.name.lower() in query:
                return skill
        return None

    def registry(self) -> dict[str, _Tool]:
        return {
            "get_progress": _Tool(
                _function("get_progress", "Current role/grade, career target, readiness formula, skill gaps (assessed vs post-review estimate) and met requirements."),
                self.get_progress,
            ),
            "get_recommendations": _Tool(
                _function("get_recommendations", "The deterministic engine's 1-3 recommended activities with factors and explanations, mandatory items, and blocked gaps."),
                self.get_recommendations,
            ),
            "get_alternatives": _Tool(
                _function(
                    "get_alternatives",
                    "Catalog activities that develop one skill: eligible ones and those not available with the reason code.",
                    {"skill": {"type": "string", "description": "Skill name or skill_id, e.g. 'Communication'."}},
                ),
                self.get_alternatives,
            ),
            "simulate_completion": _Tool(
                _function(
                    "simulate_completion",
                    "Dry-run: what changes (skill estimates, readiness, next recommendations) if the employee completes an eligible activity. Saves nothing.",
                    {"event_id": {"type": "string", "description": "Event ID such as EV_006."}},
                ),
                self.simulate_completion,
            ),
            "get_journey": _Tool(
                _function("get_journey", "Progress since the last review and the recent monthly activity rhythm."),
                self.get_journey,
            ),
        }


# --------------------------------------------------------------------------- #
# Templates (offline fallback, also used when the LLM answer is not grounded)
# --------------------------------------------------------------------------- #

_INTENT_KEYWORDS: dict[str, tuple[str, ...]] = {
    "simulate": ("what happens", "what if", "if i complete", "что будет", "если я", "если пройду", "не болады", "аяқтасам"),
    "why_recommended": ("why was", "why this", "why is this", "почему рекоменд", "почему это", "почему именно", "неге ұсын", "неліктен ұсын"),
    "readiness": ("ready", "readiness", "senior", "promotion", "not ready", "готов", "повышен", "дайын", "жоғарыла"),
    "small": ("small", "this week", "light", "quick", "short", "небольш", "на этой неделе", "легк", "быстр", "кішкентай", "осы апта", "жеңіл"),
    "alternatives": ("another way", "alternative", "other way", "instead", "другой способ", "альтернатив", "иначе", "вместо", "басқа жол", "балама"),
    "journey": ("progress", "how am i doing", "since review", "прогресс", "как у меня", "прогрес", "қалай"),
}

_T: dict[str, dict[str, str]] = {
    "focus_intro": {
        "en": "Focus this month on {title}.",
        "ru": "В этом месяце сфокусируйтесь на «{title}».",
        "kk": "Осы айда «{title}» іс-шарасына назар аударыңыз.",
    },
    "no_action": {
        "en": "There is no eligible catalog activity for your open gaps right now.",
        "ru": "Сейчас в каталоге нет подходящей активности для ваших открытых пробелов.",
        "kk": "Қазір каталогта ашық олқылықтарыңызға сай іс-шара жоқ.",
    },
    "readiness": {
        "en": "Your readiness for {role} {grade} is {pct}%: {met} of {total} requirements are met, {cmet} of {ctotal} critical ones.",
        "ru": "Ваша готовность к {role} {grade} — {pct}%: выполнено {met} из {total} требований, критических — {cmet} из {ctotal}.",
        "kk": "{role} {grade} деңгейіне дайындығыңыз — {pct}%: {total} талаптың {met}-і орындалды, сынилардың {ctotal}-ның {cmet}-і.",
    },
    "gaps_head": {
        "en": "Largest gaps:",
        "ru": "Главные пробелы:",
        "kk": "Негізгі олқылықтар:",
    },
    "gap_line": {
        "en": "{name}: estimate {level} vs {target} required{critical}",
        "ru": "{name}: оценка {level} при требовании {target}{critical}",
        "kk": "{name}: бағалау {level}, талап {target}{critical}",
    },
    "critical_mark": {"en": " (critical)", "ru": " (критический)", "kk": " (сыни)"},
    "ready": {
        "en": "All target requirements are met by your current estimate; the next step is a formal review.",
        "ru": "По текущей оценке все требования цели выполнены; следующий шаг — формальная оценка.",
        "kk": "Ағымдағы бағалау бойынша барлық талап орындалды; келесі қадам — ресми бағалау.",
    },
    "small": {
        "en": "A light option this week: {title} ({hours} h, {format}). {effect}",
        "ru": "Небольшой шаг на этой неделе: «{title}» ({hours} ч, {format}). {effect}",
        "kk": "Осы аптаға жеңіл қадам: «{title}» ({hours} сағ, {format}). {effect}",
    },
    "effect": {
        "en": "It can move {skill} from {from_level} to {to_level} (estimate).",
        "ru": "Это может поднять {skill} с {from_level} до {to_level} (оценка).",
        "kk": "Бұл {skill} дағдысын {from_level}-ден {to_level}-ге көтеруі мүмкін (бағалау).",
    },
    "simulate": {
        "en": "If you complete {title}: {changes}. Readiness {before}% → {after}%. Formal levels change only at reassessment.",
        "ru": "Если вы пройдёте «{title}»: {changes}. Готовность {before}% → {after}%. Формальные уровни меняются только на переоценке.",
        "kk": "«{title}» аяқтасаңыз: {changes}. Дайындық {before}% → {after}%. Ресми деңгейлер тек қайта бағалауда өзгереді.",
    },
    "simulate_next": {
        "en": "Next recommendation would be: {titles}.",
        "ru": "Следующей рекомендацией станет: {titles}.",
        "kk": "Келесі ұсыныс: {titles}.",
    },
    "blocked": {
        "en": "Note: no catalog activity can move {name} from {level} to {target} yet; discuss practice or mentoring with your manager.",
        "ru": "Важно: в каталоге пока нет активности, которая поднимет {name} с {level} до {target}; обсудите практику или наставничество с руководителем.",
        "kk": "Ескерту: каталогта {name} дағдысын {level}-ден {target}-ге көтеретін іс-шара әзірге жоқ; басшымен практика не тәлімгерлікті талқылаңыз.",
    },
    "no_change": {"en": "no skill estimate changes", "ru": "оценки навыков не изменятся", "kk": "дағды бағалары өзгермейді"},
    "alternatives": {
        "en": "Other ways to develop {skill}: {items}.",
        "ru": "Другие способы развить {skill}: {items}.",
        "kk": "{skill} дағдысын дамытудың басқа жолдары: {items}.",
    },
    "no_alternatives": {
        "en": "The catalog has no other eligible activity for {skill} right now. On-the-job practice or mentoring with your manager is the realistic path.",
        "ru": "Сейчас в каталоге нет других подходящих активностей для {skill}. Реалистичный путь — практика на работе или наставничество с руководителем.",
        "kk": "Қазір каталогта {skill} үшін басқа лайықты іс-шара жоқ. Шынайы жол — жұмыстағы практика немесе басшымен тәлімгерлік.",
    },
    "journey": {
        "en": "Since your review: {items}. Breaks never erase this progress.",
        "ru": "С момента оценки: {items}. Перерывы не стирают этот прогресс.",
        "kk": "Бағалаудан бері: {items}. Үзілістер бұл прогресті өшірмейді.",
    },
    "journey_empty": {
        "en": "No completed activity since your last review yet. A small first step is enough to start.",
        "ru": "После последней оценки пока нет завершённых активностей. Для начала достаточно одного небольшого шага.",
        "kk": "Соңғы бағалаудан кейін аяқталған іс-шара әзірге жоқ. Бастау үшін бір шағын қадам жеткілікті.",
    },
}

_FORMAT = {
    "en": {"online": "online", "offline": "in person", "self_paced": "self-paced"},
    "ru": {"online": "онлайн", "offline": "очно", "self_paced": "в своём темпе"},
    "kk": {"online": "онлайн", "offline": "офлайн", "self_paced": "өз қарқынымен"},
}


_HOURS: dict[str, Callable[[float, str], str]] = {
    "en": lambda hours, fmt: f"{hours:g} h, {_FORMAT['en'].get(fmt, fmt)}",
    "ru": lambda hours, fmt: f"{hours:g} ч, {_FORMAT['ru'].get(fmt, fmt)}",
    "kk": lambda hours, fmt: f"{hours:g} сағ, {_FORMAT['kk'].get(fmt, fmt)}",
}


def _t(key: str, language: str, **values: Any) -> str:
    return _T[key].get(language, _T[key]["en"]).format(**values)


def detect_intent(message: str) -> str:
    text = message.lower()
    for intent, keywords in _INTENT_KEYWORDS.items():
        if any(keyword in text for keyword in keywords):
            return intent
    return "focus"


class EmployeeTemplateResponder:
    def __init__(self, tools: EmployeeTools, language: str) -> None:
        self.tools = tools
        self.language = language

    def answer(self, message: str) -> tuple[str, list[str]]:
        intent = detect_intent(message)
        handler = getattr(self, f"_{intent}")
        return handler(message)

    def _mentioned_event(self, message: str) -> str | None:
        match = re.search(r"EV_\d{3}", message.upper())
        if match:
            return match.group(0)
        lowered = message.lower()
        for event in self.tools.dataset.events.events:
            if event.title.lower() in lowered:
                return event.event_id
        return None

    def _explain(self, rec: dict[str, Any]) -> str:
        """Engine explanations use the profile language; rebuild when the chat language differs."""
        if self.language == self.tools.employee.preferred_language or not rec["expected_effect"]:
            return rec["explanation"]
        effect = rec["expected_effect"][0]
        gap = _t(
            "gap_line",
            self.language,
            name=effect["name"],
            level=effect["from_level"],
            target=effect["target_level"],
            critical=_t("critical_mark", self.language) if effect["critical"] else "",
        )
        move = _t(
            "effect",
            self.language,
            skill=effect["name"],
            from_level=effect["from_level"],
            to_level=effect["to_level"],
        )
        return f"{gap}. {move} {_HOURS[self.language](rec['duration_hours'], rec['format'])}."

    def _top(self):
        recs = self.tools.get_recommendations()["recommendations"]
        return recs[0] if recs else None

    def _focus(self, _: str) -> tuple[str, list[str]]:
        data = self.tools.get_recommendations()
        top = data["recommendations"][0] if data["recommendations"] else None
        if top is None:
            blocked = [gap["suggestion"] for gap in data["blocked_gaps"][:1]]
            return " ".join([_t("no_action", self.language), *blocked]), []
        text = f"{_t('focus_intro', self.language, title=top['title'])} {self._explain(top)}"
        if data["blocked_gaps"] and data["blocked_gaps"][0]["critical"]:
            gap = data["blocked_gaps"][0]
            text += " " + _t("blocked", self.language, name=gap["name"], level=gap["estimate"], target=gap["target_level"])
        return text, [top["event_id"]]

    def _why_recommended(self, message: str) -> tuple[str, list[str]]:
        data = self.tools.get_recommendations()
        event_id = self._mentioned_event(message)
        chosen = next((r for r in data["recommendations"] if r["event_id"] == event_id), None)
        chosen = chosen or (data["recommendations"][0] if data["recommendations"] else None)
        if chosen is None:
            return _t("no_action", self.language), []
        return self._explain(chosen), [chosen["event_id"]]

    def _readiness(self, _: str) -> tuple[str, list[str]]:
        progress = self.tools.get_progress()
        r = progress["readiness"]
        target = progress["target"]
        lines = [
            _t(
                "readiness",
                self.language,
                role=target["role"],
                grade=target["grade"],
                pct=r["readiness_pct"],
                met=r["requirements_met"],
                total=r["requirements_total"],
                cmet=r["critical_requirements_met"],
                ctotal=r["critical_requirements_total"],
            )
        ]
        gaps = sorted(progress["gaps"], key=lambda g: (-int(g["critical"]), -g["gap"]))[:3]
        if gaps:
            lines.append(_t("gaps_head", self.language))
            lines.extend(
                "• "
                + _t(
                    "gap_line",
                    self.language,
                    name=g["name"],
                    level=g["estimate_since_review"],
                    target=g["target_level"],
                    critical=_t("critical_mark", self.language) if g["critical"] else "",
                )
                for g in gaps
            )
        else:
            lines.append(_t("ready", self.language))
        top = self._top()
        if top:
            lines.append(self._explain(top))
        return "\n".join(lines), [top["event_id"]] if top else []

    def _small(self, _: str) -> tuple[str, list[str]]:
        self.tools.get_recommendations()
        recs = self.tools.recommendations.recommendations
        if not recs:
            return self._focus("")
        shortest = min(recs, key=lambda item: item.duration_hours)
        effect = shortest.expected_effect[0]
        effect_text = _t(
            "effect",
            self.language,
            skill=effect.name,
            from_level=effect.from_level,
            to_level=effect.to_level,
        )
        return (
            _t(
                "small",
                self.language,
                title=shortest.title,
                hours=f"{shortest.duration_hours:g}",
                format=_FORMAT[self.language].get(shortest.format, shortest.format),
                effect=effect_text,
            ),
            [shortest.event_id],
        )

    def _simulate(self, message: str) -> tuple[str, list[str]]:
        event_id = self._mentioned_event(message)
        if event_id is None:
            top = self._top()
            if top is None:
                return _t("no_action", self.language), []
            event_id = top["event_id"]
        result = self.tools.simulate_completion({"event_id": event_id})
        if "error" in result:
            return self._focus("")
        changes = ", ".join(f"{c['skill']} {c['from']} → {c['to']}" for c in result["skill_changes"])
        text = _t(
            "simulate",
            self.language,
            title=result["title"],
            changes=changes or _t("no_change", self.language),
            before=result["readiness"]["from"],
            after=result["readiness"]["to"],
        )
        if result["next_recommendations"]:
            text += " " + _t(
                "simulate_next",
                self.language,
                titles=", ".join(item["title"] for item in result["next_recommendations"]),
            )
        return text, [event_id]

    def _alternatives(self, message: str) -> tuple[str, list[str]]:
        skill = None
        lowered = message.lower()
        for item in self.tools.dataset.skills.skills:
            if item.name.lower() in lowered:
                skill = item.name
                break
        if skill is None:
            gaps = self.tools.progress.gaps
            if not gaps:
                return self._focus("")
            skill = gaps[0].name
        result = self.tools.get_alternatives({"skill": skill})
        top_ids = {r.event_id for r in self.tools.recommendations.recommendations[:1]}
        eligible = [item for item in result.get("eligible", []) if item["event_id"] not in top_ids] or result.get("eligible", [])
        if not eligible:
            return _t("no_alternatives", self.language, skill=result.get("skill", skill)), []
        items = "; ".join(
            f"{item['title']} ({_HOURS[self.language](item['duration_hours'], item['format'])}, {item['moves_skill']})"
            for item in eligible[:3]
        )
        return (
            _t("alternatives", self.language, skill=result["skill"], items=items),
            [item["event_id"] for item in eligible[:3]],
        )

    def _journey(self, _: str) -> tuple[str, list[str]]:
        journey = self.tools.get_journey()
        progress = journey["progress_since_review"]
        if not progress:
            return _t("journey_empty", self.language), []
        items = ", ".join(
            f"{item['name']} {item['assessed_level']} → {item['effective_level']}" for item in progress[:4]
        )
        return _t("journey", self.language, items=items), []


# --------------------------------------------------------------------------- #
# Employee Career AI service
# --------------------------------------------------------------------------- #

EMPLOYEE_SYSTEM_PROMPT = """You are Career AI inside Career Quest, a voluntary employee-development app of a bank.
You help ONE employee understand their development. Rules:
- Use ONLY facts returned by the tools. Call tools before answering anything about skills, readiness, activities, dates or effects.
- Never invent activities, event IDs, levels, dates, percentages or policies. If the catalog has nothing suitable, say so plainly.
- Recommendations come from the deterministic engine (get_recommendations). You may explain, compare or simulate them; you must not replace them with activities that tools did not return.
- Skill gains after the last review are ESTIMATES, not certification. Formal levels change only at reassessment.
- Be supportive and concise (max ~120 words). Never judge motivation, never compare with colleagues, never pressure.
- Answer in {language}.
Return a JSON object: {{"text": "<answer for the employee>", "event_ids": ["EV_..."]}} where event_ids lists only activities you suggest acting on (from tool results), or [] if none."""


class EmployeeAIService:
    def __init__(self, store: DataStore, config: AIConfig, client: LLMClient | None = None) -> None:
        self.store = store
        self.config = config
        self.client = client or OpenAIChatClient(config)

    def chat(self, employee_id: str, request: EmployeeChatRequest) -> AIAnswer:
        started = time.monotonic()
        tools = EmployeeTools(self.store, employee_id)
        language = request.language or tools.employee.preferred_language
        fallback_reason: str | None = None
        if self.config.api_key:
            try:
                text, event_ids = self._llm_answer(tools, request, language, started)
                return self._build(tools, text, event_ids, "llm", started, None)
            except LLMError as exc:
                fallback_reason = str(exc)
        else:
            fallback_reason = "OPENAI_API_KEY is not configured"
        template_tools = EmployeeTools(self.store, employee_id)
        text, event_ids = EmployeeTemplateResponder(template_tools, language).answer(request.message)
        return self._build(template_tools, text, event_ids, "template", started, fallback_reason)

    def _llm_answer(
        self,
        tools: EmployeeTools,
        request: EmployeeChatRequest,
        language: str,
        started: float,
    ) -> tuple[str, list[str]]:
        registry = tools.registry()
        # Pre-load the two core views so most questions need one model round trip.
        context = {
            "progress": tools.get_progress(),
            "recommendations": tools.get_recommendations(),
        }
        messages: list[dict[str, Any]] = [
            {
                "role": "system",
                "content": EMPLOYEE_SYSTEM_PROMPT.format(language=LANGUAGE_NAMES.get(language, "English")),
            },
            {
                "role": "system",
                "content": "Preloaded tool results (JSON): " + json.dumps(context, ensure_ascii=False, default=str),
            },
        ]
        history = request.history[-int(AI_SETTINGS["max_history_messages"]):]
        messages.extend({"role": turn.role, "content": turn.content} for turn in history)
        messages.append({"role": "user", "content": request.message})
        schemas = [tool.schema for tool in registry.values()]

        for _ in range(int(AI_SETTINGS["max_tool_rounds"])):
            remaining = float(AI_SETTINGS["deadline_seconds"]) - (time.monotonic() - started)
            if remaining <= 0.5:
                raise LLMError("AI deadline exceeded")
            message = self.client.complete(messages=messages, tools=schemas, timeout=remaining)
            tool_calls = message.get("tool_calls") or []
            if not tool_calls:
                return self._parse_final(message.get("content") or "", tools)
            messages.append(
                {"role": "assistant", "content": message.get("content"), "tool_calls": tool_calls}
            )
            for call in tool_calls:
                name = call.get("function", {}).get("name", "")
                try:
                    arguments = json.loads(call.get("function", {}).get("arguments") or "{}")
                except json.JSONDecodeError:
                    arguments = {}
                tool = registry.get(name)
                result = tool.handler(arguments) if tool else {"error": f"Unknown tool {name}"}
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.get("id", ""),
                        "content": json.dumps(result, ensure_ascii=False, default=str),
                    }
                )
        raise LLMError("AI used too many tool rounds")

    @staticmethod
    def _parse_final(content: str, tools: EmployeeTools) -> tuple[str, list[str]]:
        try:
            payload = json.loads(content)
            text = str(payload.get("text", "")).strip()
            event_ids = [str(item) for item in payload.get("event_ids", []) if isinstance(item, str)]
        except (json.JSONDecodeError, AttributeError):
            text, event_ids = content.strip(), []
        if not text:
            raise LLMError("AI returned an empty answer")
        # Grounding check: every activity the answer names must come from tool results.
        mentioned = set(re.findall(r"EV_\d{3}", text)) | set(event_ids)
        unknown = mentioned - tools.allowed_event_ids
        if unknown:
            raise LLMError(f"AI answer referenced activities outside tool results: {sorted(unknown)}")
        return text, event_ids

    def _build(
        self,
        tools: EmployeeTools,
        text: str,
        event_ids: list[str],
        mode: Literal["llm", "template"],
        started: float,
        fallback_reason: str | None,
    ) -> AIAnswer:
        eligible = set(tools.eligible_event_ids())
        actions: list[AIAction] = []
        for event_id in dict.fromkeys(event_ids):
            event = tools.dataset.indexes.events_by_id.get(event_id)
            if event is None:
                continue
            if event_id in eligible:
                actions.append(AIAction(type="add_to_plan", event_id=event_id, title=event.title))
            actions.append(AIAction(type="open_event", event_id=event_id, title=event.title))
        return AIAnswer(
            text=text,
            actions=actions,
            sources=tools.used,
            mode=mode,
            model=self.config.model if mode == "llm" else None,
            fallback_reason=fallback_reason,
            latency_ms=int((time.monotonic() - started) * 1000),
        )


# --------------------------------------------------------------------------- #
# HR insights (aggregates only; no names, no chat text)
# --------------------------------------------------------------------------- #

HR_SYSTEM_PROMPT = """You are the HR insight assistant of Career Quest, a voluntary development platform.
You receive ONLY aggregated, de-identified analytics (JSON). Rules:
- Every statement must be backed by numbers present in the data. Do not invent numbers, people, events or causes.
- Detect patterns; never diagnose motives of employees. Separate catalog/supply problems from people problems.
- Suggest supportive, non-punitive actions (no rankings, no shaming, no points for mandatory processes).
- Answer in {language}.
{shape}"""

HR_INSIGHTS_SHAPE = """Return a JSON object {"insights": [ ... 3 to 5 items ... ]}. Each item:
{"title": str, "observation": str, "evidence": [str, ...], "we_dont_know": str, "suggested_action": str,
 "link": one of "/hr", "/hr/people", "/hr/skills", "/hr/events"}"""

HR_QUERY_SHAPE = """Return a JSON object {"text": "<concise answer, max ~150 words>"}."""


class HRInsightService:
    def __init__(self, store: DataStore, config: AIConfig, client: LLMClient | None = None) -> None:
        self.store = store
        self.config = config
        self.client = client or OpenAIChatClient(config)

    def aggregates(self) -> dict[str, Any]:
        analytics = AnalyticsService(self.store.dataset)
        dashboard = analytics.dashboard()
        gaps = analytics.skill_gaps(None, None, None)["items"][:8]
        opportunity = analytics.opportunity_gaps()["items"][:8]
        no_step = analytics.no_next_step()
        reasons: dict[str, int] = {}
        for item in no_step["items"]:
            for reason in item["reasons"]:
                reasons[reason] = reasons.get(reason, 0) + 1
        participation = [
            item
            for item in analytics.participation(None, None, None, None)["items"]
            if not item["mandatory"] and item["records"] >= 10
        ]
        participation.sort(key=lambda item: item["completion_rate"])
        signals = EngagementSignalService(analytics.dataset, analytics).signals()["items"]
        signal_kinds: dict[str, int] = {}
        for item in signals:
            signal_kinds[item["kind"]] = signal_kinds.get(item["kind"], 0) + 1

        def event_view(item: dict[str, Any]) -> dict[str, Any]:
            keys = ("title", "type", "format", "records", "completed", "no_show", "dropped", "declined", "completion_rate", "average_feedback")
            return {key: item.get(key) for key in keys}

        return {
            "as_of_date": dashboard["as_of_date"],
            "dashboard": {k: v for k, v in dashboard.items() if k != "as_of_date"},
            "top_skill_gaps": [
                {k: item[k] for k in ("name", "employees_below_target", "critical_gaps", "average_gap")}
                for item in gaps
            ],
            "catalog_opportunity_gaps": [item["summary"] for item in opportunity],
            "no_next_step": {"count": no_step["count"], "reasons": reasons},
            "voluntary_events_lowest_completion": [event_view(item) for item in participation[:4]],
            "voluntary_events_highest_completion": [event_view(item) for item in participation[-3:]],
            "support_signal_counts": signal_kinds,
        }

    def insights(self, language: str) -> HRInsightsResponse:
        started = time.monotonic()
        data = self.aggregates()
        fallback_reason: str | None
        if self.config.api_key:
            try:
                payload = self._ask(data, language, HR_INSIGHTS_SHAPE, "Generate the insights.", started)
                items = [HRInsight.model_validate(item) for item in payload.get("insights", [])][:5]
                if len(items) < 1:
                    raise LLMError("AI returned no insights")
                self._check_numbers(" ".join(i.observation + " " + " ".join(i.evidence) for i in items), data)
                return HRInsightsResponse(
                    insights=items,
                    mode="llm",
                    model=self.config.model,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            except (LLMError, ValueError) as exc:
                fallback_reason = str(exc)[:300]
        else:
            fallback_reason = "OPENAI_API_KEY is not configured"
        return HRInsightsResponse(
            insights=self._template_insights(data),
            mode="template",
            fallback_reason=fallback_reason,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def query(self, request: HRQueryRequest) -> AIAnswer:
        started = time.monotonic()
        data = self.aggregates()
        sources = [AISource(tool="hr_aggregates", summary=f"De-identified analytics as of {data['as_of_date']}")]
        fallback_reason: str | None
        if self.config.api_key:
            try:
                payload = self._ask(data, request.language, HR_QUERY_SHAPE, request.question, started)
                text = str(payload.get("text", "")).strip()
                if not text:
                    raise LLMError("AI returned an empty answer")
                self._check_numbers(text, data)
                return AIAnswer(
                    text=text,
                    sources=sources,
                    mode="llm",
                    model=self.config.model,
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
            except LLMError as exc:
                fallback_reason = str(exc)[:300]
        else:
            fallback_reason = "OPENAI_API_KEY is not configured"
        text = "\n".join(f"• {item.title}: {item.observation}" for item in self._template_insights(data))
        return AIAnswer(
            text=text,
            sources=sources,
            mode="template",
            fallback_reason=fallback_reason,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _ask(self, data: dict[str, Any], language: str, shape: str, question: str, started: float) -> dict[str, Any]:
        remaining = float(AI_SETTINGS["deadline_seconds"]) - (time.monotonic() - started)
        message = self.client.complete(
            messages=[
                {
                    "role": "system",
                    "content": HR_SYSTEM_PROMPT.format(language=LANGUAGE_NAMES.get(language, "English"), shape=shape),
                },
                {"role": "system", "content": "Analytics JSON: " + json.dumps(data, ensure_ascii=False, default=str)},
                {"role": "user", "content": question},
            ],
            tools=None,
            timeout=max(remaining, 1.0),
        )
        try:
            payload = json.loads(message.get("content") or "")
        except json.JSONDecodeError as exc:
            raise LLMError("AI returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise LLMError("AI returned invalid JSON")
        return payload

    @staticmethod
    def _check_numbers(text: str, data: dict[str, Any]) -> None:
        """Reject answers containing multi-digit numbers that are not in the analytics."""
        known = set(re.findall(r"\d+(?:\.\d+)?", json.dumps(data, default=str)))
        known |= {str(int(float(n))) for n in known}
        for number in re.findall(r"\d+(?:\.\d+)?(?!\s*%)", text):
            if len(number.replace(".", "")) >= 2 and number not in known and str(int(float(number))) not in known:
                raise LLMError(f"AI answer contained an unsupported number: {number}")

    @staticmethod
    def _template_insights(data: dict[str, Any]) -> list[HRInsight]:
        insights: list[HRInsight] = []
        d = data["dashboard"]
        if data["top_skill_gaps"]:
            top = data["top_skill_gaps"][0]
            insights.append(
                HRInsight(
                    title=f"{top['name']} is the most common critical gap",
                    observation=(
                        f"{top['critical_gaps']} employees have a critical {top['name']} gap; "
                        f"{top['employees_below_target']} are below their target level."
                    ),
                    evidence=[
                        f"{g['name']}: {g['critical_gaps']} critical, {g['employees_below_target']} below target"
                        for g in data["top_skill_gaps"][:3]
                    ],
                    we_dont_know="Whether these gaps block current work or only the next grade.",
                    suggested_action=f"Review {top['name']} supply and cohort timing with L&D.",
                    link="/hr/skills",
                )
            )
        if data["no_next_step"]["count"]:
            insights.append(
                HRInsight(
                    title="Some people have no suitable next step",
                    observation=(
                        f"{data['no_next_step']['count']} employees have no eligible activity for their critical gaps. "
                        "This is a catalog supply issue, not an engagement issue."
                    ),
                    evidence=data["catalog_opportunity_gaps"][:3],
                    we_dont_know="Whether mentoring or on-the-job practice is already happening outside the catalog.",
                    suggested_action="Add or re-level activities for the listed skill/grade pairs; offer mentoring meanwhile.",
                    link="/hr/skills",
                )
            )
        if data["voluntary_events_lowest_completion"]:
            low = data["voluntary_events_lowest_completion"][0]
            insights.append(
                HRInsight(
                    title=f"Low completion: {low['title']}",
                    observation=(
                        f"{low['title']} ({low['format']}) has a {low['completion_rate']}% completion rate "
                        f"across {low['records']} records ({low['no_show']} no-shows, {low['dropped']} drops)."
                    ),
                    evidence=[
                        f"{e['title']}: {e['completion_rate']}% ({e['format']})"
                        for e in data["voluntary_events_lowest_completion"][:3]
                    ],
                    we_dont_know="Whether timing, format or content causes the drop-off.",
                    suggested_action="Ask participants about timing and format; test a self-paced or shorter variant.",
                    link="/hr/events",
                )
            )
        if data["support_signal_counts"]:
            kinds = ", ".join(f"{k.replace('_', ' ')}: {v}" for k, v in sorted(data["support_signal_counts"].items()))
            insights.append(
                HRInsight(
                    title="Support signals are patterns, not verdicts",
                    observation=f"Current support signals by type — {kinds}.",
                    evidence=[f"{k.replace('_', ' ')}: {v}" for k, v in data["support_signal_counts"].items()],
                    we_dont_know="The reasons behind any individual pattern.",
                    suggested_action="Use low-pressure 1:1s about timing and format; handle overdue mandatory items as a process issue.",
                    link="/hr/people",
                )
            )
        if d.get("progress_awaiting_formal_review"):
            insights.append(
                HRInsight(
                    title="Progress is waiting for formal review",
                    observation=(
                        f"{d['progress_awaiting_formal_review']} employees completed relevant activities after their last review."
                    ),
                    evidence=[f"Post-review completions: {d.get('post_review_completions', 0)}"],
                    we_dont_know="Whether the estimated gains will be confirmed at reassessment.",
                    suggested_action="Prioritise reassessment for people close to their target.",
                    link="/hr",
                )
            )
        return insights[:5]
