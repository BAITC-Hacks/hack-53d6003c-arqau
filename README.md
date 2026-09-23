# Career Quest

Career Quest is an explainable employee-development platform for the HackAlem AI Halyk Bank track. The product will connect career targets, skill gaps, suitable development activities, verified participation, and updated career progress.

This repository currently contains the **Hour 1-3 backend foundation**: a FastAPI service, strict typed dataset models, cross-file validation, in-memory indexes, employee profiles, career-target resolution, assessed-versus-current skill progress, gap analysis, deterministic explainable recommendations, health endpoints, and automated tests. The presentation layer is intentionally not fixed yet.

## Current architecture

```text
data/*.json + data/*.csv
        |
        v
DatasetLoader -> Pydantic schema validation -> cross-file validation
        |
        v
DatasetBundle + indexes
        |
        v
Progress service -> Recommendation engine -> FastAPI
```

Business logic will be added as independent domain services so eligibility, progress, and recommendations remain deterministic and testable rather than prompt-driven.

## Dataset

Place these official source files in `data/`:

- `skills.json`
- `employees.json`
- `events.json`
- `activity_history.csv`

The loader preserves all supplied fields and validates schemas, value ranges, unique IDs, role/grade combinations, manager relationships, career goals, skill references, and history references. Structural problems (duplicate IDs, unknown references, bad schema) stop startup with a targeted error. Deviations from dataset conventions, such as a manager outside the employee's department or below Lead, are logged and listed under `warnings` in `/health`, so additional evaluation profiles never block startup.

The existing repository's `dataset/` directory is also detected automatically,
so the current GitHub layout works without duplicating the source files. To
load another compatible dataset, set `CAREER_QUEST_DATA_DIR` to its directory.

## Install and run

Python 3.11 or newer is required.

```bash
make install
make test
make run
```

Open:

- Health and dataset counts: <http://127.0.0.1:8000/health>
- Interactive API documentation: <http://127.0.0.1:8000/docs>

Hour 2 endpoints:

- `GET /employees` - paginated employee directory
- `GET /employees/{employee_id}` - full profile and activity summary
- `GET /employees/{employee_id}/progress` - career target, current progress, gaps, critical gaps, and readiness
- `GET /employees/{employee_id}/recommendations?debug=false` - 1-3 explainable recommendations plus required events
- `GET /career/requirements?role=...&grade=...` - role/grade requirements

Progress keeps formal assessment separate from later development:

```text
assessed_level + verified post-review activity gains = effective_level
gap = max(target_level - effective_level, 0)
```

Each post-review gain includes its supporting event IDs. Activity progress is
shown as development evidence and is not represented as a new formal assessment.

## Recommendation logic

The recommendation engine is deterministic. It uses the dataset snapshot date,
never the computer's current date, and reuses `SkillProgressService` for the
career target, effective levels, and gaps.

Events pass these hard filters in order:

1. Mandatory events are removed from personal recommendations and shown under `required`.
2. The audience must match the employee's current or target role and grade.
3. Completed non-recurring events and events already in progress are removed.
4. Every prerequisite must be met using effective skill levels.
5. The event must develop an unclosed target gap and be able to improve it below `max_level`.
6. Scheduled events need a session on or after the dataset snapshot date; self-paced events are always available.

With `debug=true`, every rejected event includes its first decisive reason:
`MANDATORY`, `AUDIENCE`, `ALREADY_COMPLETED`, `IN_PROGRESS`, `PREREQUISITES`,
`NO_RELEVANT_GAP`, `MAX_LEVEL_REACHED`, or `NOT_AVAILABLE`. If no event survives,
the response is `NO_SUITABLE_ACTION` and includes the unclosed gaps.

Eligible events use the centralized weights in `SCORING_WEIGHTS`:

| Factor | Weight |
|---|---:|
| Useful gain on a critical target skill | +12.00 per level |
| Useful gain on another target skill | +4.00 per level |
| Full target role/grade relevance | +2.50 |
| Positive similar-history evidence | +1.50 per record, capped at 3 |
| Similar scheduled no-show friction | -1.25, with a 1.5× scheduled multiplier |
| Similar dropped-event friction | -1.00, with a 1.5× long-event multiplier |
| Similar declined-event friction | -0.75 |
| Remote employee with an offline event | -0.75 |
| Effort | -0.04 per hour |
| New-gap diversity while selecting recommendations 2-3 | +1.00 per new gap |

For positive evidence, similar means the same type, the same format, or a shared
developed skill. For friction, similar means a shared developed skill or the same
type-and-format pair, and the same `event_id` is deliberately excluded. No-show
friction weighs more for scheduled activities; dropped history weighs more for
long activities.

The engine greedily selects up to three events and rewards coverage of different
gaps. Scores map to labels without probabilities: `Strong match` at 12 or above,
`Good match` at 5 or above, and `Exploratory` below 5. Every result contains
structured numerical factors, expected skill effects, and an English, Russian,
or Kazakh template explanation based on the employee's preferred language.

Example health response:

```json
{
  "status": "ok",
  "service": "career-quest-api",
  "dataset": {
    "name": "Career Quest",
    "version": "1.0",
    "as_of_date": "2026-10-01",
    "counts": {
      "skills": 60,
      "role_profiles": 32,
      "employees": 200,
      "events": 40,
      "history_records": 2743
    }
  }
}
```

## Tests

The Hour 1-3 suite verifies:

- the official four source files load;
- the expected dataset counts are exposed;
- indexes are created;
- missing source files fail clearly;
- broken cross-file employee references fail with the exact record ID;
- manager convention deviations produce warnings instead of failing;
- the health endpoint reports the loaded snapshot.
- arbitrary employee progress can be calculated;
- null goals resolve to the next grade;
- lateral goals use the target role's requirements;
- Lead employees without a goal are handled safely;
- post-review completions change effective progress without changing the assessed level.
- critical career gaps outrank superficially lower non-critical skills;
- max-level, prerequisite, mandatory, completed, in-progress, and availability filters;
- recurring `EV_036`, lateral goals, no-suitable-action responses, and diversity;
- structured explanations in English, Russian, and Kazakh;
- all 200 official employees return safely with zero to three recommendations.
