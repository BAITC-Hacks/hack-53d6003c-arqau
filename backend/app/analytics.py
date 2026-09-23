from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, timedelta
from statistics import mean
from typing import Any

from .career_progress import EmployeeProgress, SkillProgressService
from .data_loader import DatasetBundle
from .models import Employee, Grade, HistoryRecord, HistoryStatus
from .recommendations import EmployeeRecommendations, RecommendationEngine


ANALYTICS_THRESHOLDS: dict[str, float | int] = {
    "active_window_days": 90,
    "baseline_window_days": 365,
    "participation_drop_min_baseline": 6,
    "participation_drop_ratio": 0.5,
    "repeated_friction_min": 2,
    "reengaged_max_baseline": 1,
    "reengaged_min_recent": 2,
    "support_window_days": 365,
    "heatmap_top_skills": 10,
}

FRICTION_STATUSES = {
    HistoryStatus.NO_SHOW,
    HistoryStatus.DROPPED,
    HistoryStatus.DECLINED,
}


class AnalyticsService:
    """Deterministic HR aggregates calculated from the current dataset snapshot."""

    def __init__(self, dataset: DatasetBundle) -> None:
        self.dataset = dataset
        self.as_of_date = dataset.skills.meta.as_of_date
        self.progress_service = SkillProgressService(dataset)
        self.recommendation_engine = RecommendationEngine(dataset)
        self._progress: dict[str, EmployeeProgress] = {}
        self._recommendations: dict[str, EmployeeRecommendations] = {}

    def progress(self, employee_id: str) -> EmployeeProgress:
        if employee_id not in self._progress:
            self._progress[employee_id] = self.progress_service.calculate(employee_id)
        return self._progress[employee_id]

    def recommendations(self, employee_id: str) -> EmployeeRecommendations:
        if employee_id not in self._recommendations:
            self._recommendations[employee_id] = self.recommendation_engine.recommend(employee_id)
        return self._recommendations[employee_id]

    def _employees(
        self,
        department: str | None = None,
        role: str | None = None,
        grade: Grade | None = None,
    ) -> list[Employee]:
        return [
            employee
            for employee in self.dataset.employees.employees
            if (department is None or employee.department == department)
            and (role is None or employee.role == role)
            and (grade is None or employee.grade == grade)
        ]

    def skill_gaps(
        self,
        department: str | None = None,
        role: str | None = None,
        grade: Grade | None = None,
    ) -> dict[str, Any]:
        employees = self._employees(department, role, grade)
        aggregate: dict[str, dict[str, Any]] = {}
        for employee in employees:
            for gap in self.progress(employee.employee_id).gaps:
                row = aggregate.setdefault(
                    gap.skill_id,
                    {
                        "skill_id": gap.skill_id,
                        "name": gap.name,
                        "employees_below_target": 0,
                        "critical_gaps": 0,
                        "gap_total": 0,
                    },
                )
                row["employees_below_target"] += 1
                row["critical_gaps"] += int(gap.critical)
                row["gap_total"] += gap.gap
        items = []
        for row in aggregate.values():
            count = row.pop("employees_below_target")
            row["employees_below_target"] = count
            total = row.pop("gap_total")
            row["average_gap"] = round(total / count, 2)
            items.append(row)
        items.sort(
            key=lambda item: (
                -item["critical_gaps"],
                -item["employees_below_target"],
                -item["average_gap"],
                item["name"],
            )
        )
        return {
            "as_of_date": self.as_of_date.isoformat(),
            "employee_count": len(employees),
            "filters": {
                "department": department,
                "role": role,
                "grade": grade.value if grade else None,
            },
            "items": items,
        }

    def employees_for_skill_gap(self, skill_id: str) -> dict[str, Any]:
        """Return the people behind an aggregate skill-gap row for HR drill-down."""
        if skill_id not in self.dataset.indexes.skills_by_id:
            raise KeyError(skill_id)
        items: list[dict[str, Any]] = []
        for employee in self.dataset.employees.employees:
            gap = next(
                (item for item in self.progress(employee.employee_id).gaps if item.skill_id == skill_id),
                None,
            )
            if gap is None:
                continue
            items.append(
                {
                    "employee_id": employee.employee_id,
                    "full_name": employee.full_name,
                    "initials": self._initials(employee),
                    "role": employee.role,
                    "grade": employee.grade.value,
                    "department": employee.department,
                    "effective_level": gap.effective_level,
                    "target_level": gap.target_level,
                    "gap": gap.gap,
                    "critical": gap.critical,
                }
            )
        items.sort(key=lambda item: (-int(item["critical"]), -item["gap"], item["employee_id"]))
        return {
            "as_of_date": self.as_of_date.isoformat(),
            "skill_id": skill_id,
            "name": self.dataset.indexes.skills_by_id[skill_id].name,
            "count": len(items),
            "items": items,
        }

    def no_next_step(self) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        for employee in self.dataset.employees.employees:
            progress = self.progress(employee.employee_id)
            result = self.recommendations(employee.employee_id)
            blocked_critical = [item for item in result.blocked_gaps if item.critical]
            critical_ids = {item.skill_id for item in progress.critical_gaps}
            blocked_ids = {item.skill_id for item in blocked_critical}
            all_critical_blocked = bool(critical_ids) and critical_ids <= blocked_ids
            reasons: list[str] = []
            if result.status == "NO_SUITABLE_ACTION":
                reasons.append("NO_SUITABLE_ACTION")
            if all_critical_blocked:
                reasons.append("ALL_CRITICAL_GAPS_BLOCKED")
            if not reasons:
                continue
            items.append(
                {
                    "employee_id": employee.employee_id,
                    "initials": self._initials(employee),
                    "role": employee.role,
                    "grade": employee.grade.value,
                    "department": employee.department,
                    "reasons": reasons,
                    "blocked_critical_skills": [
                        {
                            "skill_id": item.skill_id,
                            "name": item.name,
                            "from_level": item.effective_level,
                            "target_level": item.target_level,
                        }
                        for item in blocked_critical
                    ],
                }
            )
        items.sort(key=lambda item: (item["department"], item["employee_id"]))
        return {"as_of_date": self.as_of_date.isoformat(), "count": len(items), "items": items}

    def participation(
        self,
        event_type: str | None = None,
        event_format: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        if start and end and start > end:
            raise ValueError("start must be on or before end")
        rows: list[dict[str, Any]] = []
        for event in self.dataset.events.events:
            if event_type and event.type != event_type:
                continue
            if event_format and event.format != event_format:
                continue
            records = [
                record
                for record in self.dataset.indexes.history_by_event.get(event.event_id, ())
                if (start is None or record.date >= start)
                and (end is None or record.date <= end)
            ]
            status_counts = Counter(record.status.value for record in records)
            completed = status_counts[HistoryStatus.COMPLETED.value]
            scores = [record.score for record in records if record.score is not None]
            feedback = [
                record.feedback_rating
                for record in records
                if record.feedback_rating is not None
            ]
            rows.append(
                {
                    "event_id": event.event_id,
                    "title": event.title,
                    "type": event.type,
                    "format": event.format,
                    "mandatory": event.mandatory,
                    "records": len(records),
                    "completed": completed,
                    "in_progress": status_counts[HistoryStatus.IN_PROGRESS.value],
                    "dropped": status_counts[HistoryStatus.DROPPED.value],
                    "no_show": status_counts[HistoryStatus.NO_SHOW.value],
                    "declined": status_counts[HistoryStatus.DECLINED.value],
                    "overdue": status_counts[HistoryStatus.OVERDUE.value],
                    "completion_rate": round(100 * completed / len(records), 1) if records else 0.0,
                    "average_score": round(mean(scores), 1) if scores else None,
                    "average_feedback": round(mean(feedback), 2) if feedback else None,
                    "voluntary": sum(record.assigned_by == "self" for record in records),
                    "assigned": sum(record.assigned_by != "self" for record in records),
                }
            )
        rows.sort(key=lambda item: (-item["records"], item["event_id"]))
        return {
            "as_of_date": self.as_of_date.isoformat(),
            "filters": {
                "type": event_type,
                "format": event_format,
                "start": start.isoformat() if start else None,
                "end": end.isoformat() if end else None,
            },
            "items": rows,
        }

    def dashboard(self) -> dict[str, Any]:
        active_cutoff = self.as_of_date - timedelta(
            days=int(ANALYTICS_THRESHOLDS["active_window_days"])
        )
        voluntary = [
            record
            for record in self.dataset.history
            if record.assigned_by == "self"
            and not self.dataset.indexes.events_by_id[record.event_id].mandatory
        ]
        active_employees = {
            record.employee_id
            for record in voluntary
            if active_cutoff <= record.date <= self.as_of_date
        }
        completed = sum(record.status == HistoryStatus.COMPLETED for record in voluntary)
        critical_gaps = sum(
            len(self.progress(employee.employee_id).critical_gaps)
            for employee in self.dataset.employees.employees
        )
        post_review = [
            record
            for record in self.dataset.history
            if record.status == HistoryStatus.COMPLETED
            and record.date
            > self.dataset.indexes.employees_by_id[record.employee_id].last_review_date
        ]
        return {
            "as_of_date": self.as_of_date.isoformat(),
            "employees": len(self.dataset.employees.employees),
            "development_active": len(active_employees),
            "completion_rate": round(100 * completed / len(voluntary), 1) if voluntary else 0.0,
            "critical_gaps": critical_gaps,
            "no_next_step": self.no_next_step()["count"],
            "support_signals": len(EngagementSignalService(self.dataset, self).signals()["items"]),
            "progress_awaiting_formal_review": len({record.employee_id for record in post_review}),
            "post_review_completions": len(post_review),
        }

    def heatmap(self, top_n: int | None = None) -> dict[str, Any]:
        top_n = top_n or int(ANALYTICS_THRESHOLDS["heatmap_top_skills"])
        gap_rows = self.skill_gaps()["items"]
        # Keep the matrix intentionally small: the UI is a comparison, not a data dump.
        chosen = {item["skill_id"] for item in gap_rows[:top_n]}
        critical_ids: set[str] = set()
        for employee in self.dataset.employees.employees:
            critical_ids.update(item.skill_id for item in self.progress(employee.employee_id).critical_gaps)
        skills = [
            {
                "skill_id": skill_id,
                "name": self.dataset.indexes.skills_by_id[skill_id].name,
                "critical_somewhere": skill_id in critical_ids,
            }
            for skill_id in sorted(chosen)
        ]
        departments = sorted({item.department for item in self.dataset.employees.employees})
        rows = []
        for department in departments:
            employees = self._employees(department=department)
            gap_sets = {
                employee.employee_id: {item.skill_id for item in self.progress(employee.employee_id).gaps}
                for employee in employees
            }
            rows.append(
                {
                    "department": department,
                    "employees": len(employees),
                    "skills": [
                        {
                            "skill_id": skill["skill_id"],
                            "below_requirement": sum(
                                skill["skill_id"] in gap_sets[employee.employee_id]
                                for employee in employees
                            ),
                            "percentage": round(
                                100
                                * sum(
                                    skill["skill_id"] in gap_sets[employee.employee_id]
                                    for employee in employees
                                )
                                / len(employees),
                                1,
                            )
                            if employees
                            else 0.0,
                        }
                        for skill in skills
                    ],
                }
            )
        return {"as_of_date": self.as_of_date.isoformat(), "skills": skills, "rows": rows}

    def trend(self) -> dict[str, Any]:
        months: dict[str, Counter[str]] = defaultdict(Counter)
        for record in self.dataset.history:
            event = self.dataset.indexes.events_by_id[record.event_id]
            if record.assigned_by != "self" or event.mandatory:
                continue
            key = record.date.strftime("%Y-%m")
            months[key]["records"] += 1
            months[key][record.status.value] += 1
        return {
            "as_of_date": self.as_of_date.isoformat(),
            "items": [
                {
                    "month": month,
                    "records": counts["records"],
                    "completed": counts[HistoryStatus.COMPLETED.value],
                    "in_progress": counts[HistoryStatus.IN_PROGRESS.value],
                    "friction": sum(counts[status.value] for status in FRICTION_STATUSES),
                }
                for month, counts in sorted(months.items())
            ],
        }

    def pipeline(self) -> dict[str, Any]:
        buckets: Counter[str] = Counter()
        for employee in self.dataset.employees.employees:
            progress = self.progress(employee.employee_id)
            if progress.readiness.status == "grade_ceiling":
                bucket = "grade_ceiling"
            elif progress.readiness.status == "ready":
                bucket = "ready"
            elif len(progress.critical_gaps) == 1:
                bucket = "one_critical_gap"
            else:
                bucket = "developing"
            buckets[bucket] += 1
        return {
            "as_of_date": self.as_of_date.isoformat(),
            "total": sum(buckets.values()),
            "buckets": {
                key: buckets[key]
                for key in ("ready", "one_critical_gap", "developing", "grade_ceiling")
            },
        }

    def opportunity_gaps(self) -> dict[str, Any]:
        groups: dict[tuple[str, str, str, int, int], dict[str, Any]] = {}
        for employee in self.dataset.employees.employees:
            result = self.recommendations(employee.employee_id)
            for gap in result.blocked_gaps:
                key = (
                    gap.skill_id,
                    result.target.role,
                    result.target.grade.value,
                    gap.effective_level,
                    gap.target_level,
                )
                row = groups.setdefault(
                    key,
                    {
                        "skill_id": gap.skill_id,
                        "name": gap.name,
                        "target_role": result.target.role,
                        "target_grade": result.target.grade.value,
                        "from_level": gap.effective_level,
                        "target_level": gap.target_level,
                        "employees": 0,
                        "critical_gaps": 0,
                    },
                )
                row["employees"] += 1
                row["critical_gaps"] += int(gap.critical)
        items = list(groups.values())
        for row in items:
            row["summary"] = (
                f"{row['employees']} employee(s) targeting {row['target_role']} "
                f"{row['target_grade']} need {row['name']} {row['target_level']}; "
                f"no eligible catalog event can move level {row['from_level']} to that target."
            )
        items.sort(
            key=lambda item: (-item["critical_gaps"], -item["employees"], item["skill_id"])
        )
        return {"as_of_date": self.as_of_date.isoformat(), "items": items}

    def journey(self, employee_id: str) -> dict[str, Any]:
        employee = self.dataset.indexes.employees_by_id[employee_id]
        progress = self.progress(employee_id)
        history = sorted(
            self.dataset.indexes.history_by_employee.get(employee_id, ()),
            key=lambda item: (item.date, item.record_id),
        )
        months: dict[str, Counter[str]] = defaultdict(Counter)
        for record in history:
            month = record.date.strftime("%Y-%m")
            months[month]["records"] += 1
            months[month][record.status.value] += 1
        recent_cutoff = self.as_of_date - timedelta(
            days=int(ANALYTICS_THRESHOLDS["active_window_days"])
        )
        recent_voluntary = [
            record
            for record in history
            if record.assigned_by == "self"
            and not self.dataset.indexes.events_by_id[record.event_id].mandatory
            and recent_cutoff <= record.date <= self.as_of_date
        ]
        recent_friction = [record for record in recent_voluntary if record.status in FRICTION_STATUSES]
        if recent_friction:
            formats = Counter(
                self.dataset.indexes.events_by_id[record.event_id].format
                for record in recent_friction
            )
            common_format, count = formats.most_common(1)[0]
            message = (
                f"Your progress is kept. {count} recent activity issue(s) involved {common_format} "
                "events; an online or self-paced alternative may fit better."
            )
        elif not recent_voluntary:
            alternatives = [
                item
                for item in self.recommendations(employee_id).recommendations
                if item.format in {"self_paced", "online"}
            ]
            if alternatives:
                choice = min(alternatives, key=lambda item: item.duration_hours)
                message = (
                    f"Your progress is kept. {choice.title} is a {choice.duration_hours:g}-hour "
                    f"{choice.format.replace('_', '-')} step available when you're ready."
                )
            else:
                message = "Your progress is kept. Continue when the timing feels right for you."
        else:
            message = "Your progress since the last review is kept; a break never resets it."
        return {
            "employee_id": employee_id,
            "as_of_date": self.as_of_date.isoformat(),
            "progress_since_review": [
                {
                    "skill_id": skill.skill_id,
                    "name": skill.name,
                    "assessed_level": skill.assessed_level,
                    "effective_level": skill.effective_level,
                    "gain": skill.post_review_development,
                    "evidence_event_ids": skill.evidence_event_ids,
                }
                for skill in progress.skills
                if skill.post_review_development > 0
            ],
            "monthly_activity": [
                {
                    "month": month,
                    "records": counts["records"],
                    "completed": counts[HistoryStatus.COMPLETED.value],
                    "in_progress": counts[HistoryStatus.IN_PROGRESS.value],
                    "friction": sum(counts[status.value] for status in FRICTION_STATUSES),
                }
                for month, counts in sorted(months.items())
            ],
            "message": message,
        }

    @staticmethod
    def _initials(employee: Employee) -> str:
        return "".join(part[0] for part in employee.full_name.split()[:2]).upper()


class EngagementSignalService:
    """Surface factual support patterns without inferring employee motivation."""

    def __init__(
        self,
        dataset: DatasetBundle,
        analytics: AnalyticsService | None = None,
    ) -> None:
        self.dataset = dataset
        self.analytics = analytics or AnalyticsService(dataset)
        self.as_of_date = dataset.skills.meta.as_of_date

    def signals(self) -> dict[str, Any]:
        recent_days = int(ANALYTICS_THRESHOLDS["active_window_days"])
        baseline_days = int(ANALYTICS_THRESHOLDS["baseline_window_days"])
        recent_start = self.as_of_date - timedelta(days=recent_days - 1)
        baseline_start = recent_start - timedelta(days=baseline_days)
        support_start = self.as_of_date - timedelta(
            days=int(ANALYTICS_THRESHOLDS["support_window_days"])
        )
        items: list[dict[str, Any]] = []
        for employee in self.dataset.employees.employees:
            history = list(self.dataset.indexes.history_by_employee.get(employee.employee_id, ()))
            voluntary = [
                record
                for record in history
                if not self.dataset.indexes.events_by_id[record.event_id].mandatory
            ]
            baseline = [
                record for record in voluntary if baseline_start <= record.date < recent_start
            ]
            recent = [
                record for record in voluntary if recent_start <= record.date <= self.as_of_date
            ]
            baseline_count = len(baseline)
            recent_count = len(recent)
            if (
                baseline_count >= int(ANALYTICS_THRESHOLDS["participation_drop_min_baseline"])
                and recent_count / 3
                < float(ANALYTICS_THRESHOLDS["participation_drop_ratio"])
                * (baseline_count / 12)
            ):
                items.append(
                    self._signal(
                        employee,
                        "participation_drop",
                        f"Voluntary participation changed from {baseline_count} records in the prior 12 months to {recent_count} in the last 90 days.",
                        [
                            f"Prior 12-month baseline: {baseline_count} voluntary records.",
                            f"Last 90 days: {recent_count} voluntary records.",
                        ],
                        "Offer a low-pressure 1:1 about timing and flexible formats.",
                    )
                )

            support_records = [
                record for record in voluntary if support_start <= record.date <= self.as_of_date
            ]
            for status in sorted(FRICTION_STATUSES, key=lambda item: item.value):
                matching = [record for record in support_records if record.status == status]
                if len(matching) < int(ANALYTICS_THRESHOLDS["repeated_friction_min"]):
                    continue
                events = [self.dataset.indexes.events_by_id[record.event_id] for record in matching]
                formats = {event.format for event in events}
                types = {event.type for event in events}
                shared = []
                if len(formats) == 1:
                    shared.append(f"all {next(iter(formats))}")
                if len(types) == 1:
                    shared.append(f"all {next(iter(types))}")
                context = f" ({', '.join(shared)})" if shared else ""
                items.append(
                    self._signal(
                        employee,
                        "repeated_friction",
                        f"{len(matching)} voluntary {status.value} records were observed in the last 12 months{context}.",
                        [
                            f"Status: {status.value}; count: {len(matching)}.",
                            f"Formats represented: {', '.join(sorted(formats))}.",
                            f"Activity types represented: {', '.join(sorted(types))}.",
                        ],
                        "Offer a self-paced or online alternative and ask privately about timing.",
                    )
                )

            overdue = [
                record
                for record in history
                if record.status == HistoryStatus.OVERDUE
                and self.dataset.indexes.events_by_id[record.event_id].mandatory
            ]
            if overdue:
                items.append(
                    self._signal(
                        employee,
                        "mandatory_overdue",
                        f"{len(overdue)} mandatory activity record(s) are overdue.",
                        [f"Overdue mandatory records: {len(overdue)}."],
                        "Check for process or access barriers and help complete the requirement.",
                    )
                )

            recommendation = self.analytics.recommendations(employee.employee_id)
            progress = self.analytics.progress(employee.employee_id)
            blocked = recommendation.blocked_gaps
            critical_ids = {item.skill_id for item in progress.critical_gaps}
            blocked_critical_ids = {item.skill_id for item in blocked if item.critical}
            all_critical_blocked = bool(critical_ids) and critical_ids <= blocked_critical_ids
            if recommendation.status == "NO_SUITABLE_ACTION" or all_critical_blocked:
                items.append(
                    self._signal(
                        employee,
                        "no_relevant_opportunity",
                        f"The current catalog leaves {len(blocked)} skill gap(s) without an eligible next step.",
                        [
                            f"Recommendation status: {recommendation.status}.",
                            f"Blocked catalog gaps: {len(blocked)}.",
                        ],
                        "Treat this as a catalog gap; offer mentoring or on-the-job practice.",
                    )
                )

            if (
                baseline_count <= int(ANALYTICS_THRESHOLDS["reengaged_max_baseline"])
                and recent_count >= int(ANALYTICS_THRESHOLDS["reengaged_min_recent"])
            ):
                items.append(
                    self._signal(
                        employee,
                        "re_engaged",
                        f"Voluntary participation increased to {recent_count} records in the last 90 days.",
                        [
                            f"Prior 12-month baseline: {baseline_count} voluntary records.",
                            f"Last 90 days: {recent_count} voluntary records.",
                        ],
                        "Recognize the renewed momentum privately and keep options flexible.",
                    )
                )
        kind_order = {
            "mandatory_overdue": 0,
            "no_relevant_opportunity": 1,
            "repeated_friction": 2,
            "participation_drop": 3,
            "re_engaged": 4,
        }
        items.sort(
            key=lambda item: (
                kind_order[item["kind"]],
                item["employee_id"],
                item["why_surfaced"],
            )
        )
        return {"as_of_date": self.as_of_date.isoformat(), "count": len(items), "items": items}

    def _signal(
        self,
        employee: Employee,
        kind: str,
        why: str,
        facts: list[str],
        approach: str,
    ) -> dict[str, Any]:
        return {
            "employee_id": employee.employee_id,
            "initials": AnalyticsService._initials(employee),
            "role": employee.role,
            "grade": employee.grade.value,
            "department": employee.department,
            "kind": kind,
            "why_surfaced": why,
            "what_we_know": facts,
            "what_we_dont_know": [
                "The reason is unknown; workload, timing, format, relevance, or another factor may be involved."
            ],
            "suggested_approach": approach,
        }
