"""Compare Career Quest recommendations with single-factor "lowest skill" baselines.

Run: make evaluate   (or: python scripts/evaluate.py [data_dir])

Baselines (the rule the case jury warns about):
  naive      — take the target skill with the lowest current level and recommend the
               first catalog event (by event_id) that develops it; no other checks.
  naive+     — same lowest-skill rule, but only among events that pass the same
               eligibility filters as Career Quest (fair comparison of the ranking).

All numbers are computed on the loaded dataset; nothing is sampled or hand-picked.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.career_progress import SkillProgressService  # noqa: E402
from app.data_loader import DatasetLoader  # noqa: E402
from app.main import configured_data_dir  # noqa: E402
from app.recommendations import RecommendationEngine  # noqa: E402

FRICTION = {"no_show", "dropped", "declined"}


def pct(part: int, total: int) -> str:
    return f"{100 * part / total:.0f}% ({part}/{total})" if total else "n/a"


def main() -> None:
    data_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else configured_data_dir()
    dataset = DatasetLoader(data_dir).load()
    progress_service = SkillProgressService(dataset)
    engine = RecommendationEngine(dataset)
    events = sorted((e for e in dataset.events.events if not e.mandatory), key=lambda e: e.event_id)

    stats = {
        "employees": 0,
        "naive_non_critical": 0,
        "naive_ineligible": 0,
        "naive_friction": 0,
        "naive_bad": 0,
        "naive_plus_non_critical": 0,
        "naive_plus_friction": 0,
        "naive_plus_with_pick": 0,
        "cq_ineligible": 0,
        "cq_with_pick": 0,
        "cq_top_friction": 0,
        "closable_critical": 0,
        "cq_targets_critical": 0,
        "naive_targets_critical": 0,
        "naive_plus_targets_critical": 0,
    }

    started = time.perf_counter()
    for employee in dataset.employees.employees:
        progress = progress_service.calculate(employee.employee_id)
        if not progress.gaps:
            continue
        stats["employees"] += 1
        result = engine.recommend(employee.employee_id, debug=True)
        rejected = {item.event_id: item.reason_code for item in result.rejected or []}
        history = dataset.indexes.history_by_employee.get(employee.employee_id, ())
        friction_events = {r.event_id for r in history if r.status.value in FRICTION}
        has_critical = any(g.critical for g in progress.gaps)
        closable_critical = {g.skill_id for g in progress.gaps if g.critical} - {
            g.skill_id for g in result.blocked_gaps
        }

        # naive: lowest current level among the target's gaps, first event that develops it
        gaps = sorted(progress.gaps, key=lambda g: (g.effective_level, -g.gap, g.name))
        lowest = gaps[0]
        if has_critical and not lowest.critical:
            stats["naive_non_critical"] += 1
        naive_event = next(
            (e for e in events if any(d.skill_id == lowest.skill_id for d in e.develops_skills)), None
        )
        if naive_event is not None:
            ineligible = naive_event.event_id in rejected
            friction = naive_event.event_id in friction_events
            stats["naive_ineligible"] += ineligible
            stats["naive_friction"] += friction
            stats["naive_bad"] += ineligible or friction

        # naive+: lowest gap that has at least one eligible event, first eligible event
        eligible = [e for e in events if e.event_id not in rejected]
        pick = None
        for gap in gaps:
            pick = next((e for e in eligible if any(d.skill_id == gap.skill_id for d in e.develops_skills)), None)
            if pick is not None:
                if has_critical and not gap.critical:
                    stats["naive_plus_non_critical"] += 1
                if closable_critical:
                    stats["naive_plus_targets_critical"] += gap.skill_id in closable_critical
                break
        if pick is not None:
            stats["naive_plus_with_pick"] += 1
            stats["naive_plus_friction"] += pick.event_id in friction_events

        # Career Quest
        stats["cq_ineligible"] += sum(1 for r in result.recommendations if r.event_id in rejected)
        if result.recommendations:
            top = result.recommendations[0]
            stats["cq_with_pick"] += 1
            stats["cq_top_friction"] += top.event_id in friction_events
        if closable_critical:
            stats["closable_critical"] += 1
            stats["naive_targets_critical"] += lowest.skill_id in closable_critical
            if result.recommendations and any(
                effect.skill_id in closable_critical for effect in result.recommendations[0].expected_effect
            ):
                stats["cq_targets_critical"] += 1
    elapsed_ms = (time.perf_counter() - started) * 1000

    s = stats
    n = s["employees"]
    print(f"Dataset: {data_dir} · employees with open gaps: {n} · snapshot {dataset.skills.meta.as_of_date}")
    print()
    print("| Metric | naive (lowest skill) | naive+ (lowest skill, eligible only) | Career Quest |")
    print("|---|---|---|---|")
    print(
        "| Top pick targets a closable critical skill (employees with one) | "
        f"{pct(s['naive_targets_critical'], s['closable_critical'])} | "
        f"{pct(s['naive_plus_targets_critical'], s['closable_critical'])} | "
        f"{pct(s['cq_targets_critical'], s['closable_critical'])} |"
    )
    print(
        "| Picks a non-critical skill while a critical gap exists | "
        f"{pct(s['naive_non_critical'], n)} | {pct(s['naive_plus_non_critical'], n)} | — |"
    )
    print(
        "| Recommends an event the employee cannot take (completed, prerequisites, audience…) | "
        f"{pct(s['naive_ineligible'], n)} | 0% by construction | {s['cq_ineligible']} of all recommendations |"
    )
    print(
        "| Top pick is an event the employee already missed, dropped or declined | "
        f"{pct(s['naive_friction'], n)} | {pct(s['naive_plus_friction'], s['naive_plus_with_pick'])} | "
        f"{pct(s['cq_top_friction'], s['cq_with_pick'])} |"
    )
    print(
        "| Unusable or previously failed recommendation | "
        f"{pct(s['naive_bad'], n)} | {pct(s['naive_plus_friction'], s['naive_plus_with_pick'])} | "
        f"{pct(s['cq_top_friction'], s['cq_with_pick'])} |"
    )
    print()
    print(f"Career Quest engine time for all {n} employees: {elapsed_ms:.0f} ms (includes baselines)")


if __name__ == "__main__":
    main()
