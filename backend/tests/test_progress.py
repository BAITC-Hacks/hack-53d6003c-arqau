from app.career_progress import SkillProgressService
from app.data_loader import DatasetLoader
from app.main import configured_data_dir


DATASET = DatasetLoader(configured_data_dir()).load()
SERVICE = SkillProgressService(DATASET)


def test_null_goal_resolves_to_next_grade() -> None:
    employee = next(
        item
        for item in DATASET.employees.employees
        if item.career_goal is None and item.grade.value != "Lead"
    )

    progress = SERVICE.calculate(employee.employee_id)

    assert progress.target.source == "next_grade"
    assert progress.target.role == employee.role
    assert progress.target.grade != employee.grade
    assert progress.readiness.requirements_total > 0


def test_lateral_goal_uses_target_role_requirements() -> None:
    employee = next(
        item
        for item in DATASET.employees.employees
        if item.career_goal is not None and item.career_goal.target_role != item.role
    )

    progress = SERVICE.calculate(employee.employee_id)

    assert progress.target.source == "career_goal"
    assert progress.target.is_lateral is True
    assert progress.target.role == employee.career_goal.target_role
    assert progress.target.grade == employee.career_goal.target_grade
    expected = DATASET.indexes.role_profiles_by_key[
        (employee.career_goal.target_role, employee.career_goal.target_grade.value)
    ]
    assert {item.skill_id for item in progress.skills if item.target_level > 0} == set(
        expected.required_skills
    )


def test_post_review_completion_changes_effective_progress() -> None:
    progress = next(
        (
            SERVICE.calculate(employee.employee_id)
            for employee in DATASET.employees.employees
            if any(
                record.status.value == "completed" and record.date > employee.last_review_date
                for record in DATASET.indexes.history_by_employee.get(employee.employee_id, ())
            )
        ),
        None,
    )

    assert progress is not None
    changed = [item for item in progress.skills if item.post_review_development > 0]
    assert changed
    assert all(item.effective_level > item.assessed_level for item in changed)
    assert all(item.evidence_event_ids for item in changed)


def test_lead_without_goal_is_handled_as_grade_ceiling() -> None:
    employee = next(
        item
        for item in DATASET.employees.employees
        if item.career_goal is None and item.grade.value == "Lead"
    )

    progress = SERVICE.calculate(employee.employee_id)

    assert progress.target.source == "current_grade_ceiling"
    assert progress.target.grade.value == "Lead"
    assert progress.readiness.status == "grade_ceiling"

