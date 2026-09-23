# Career Quest

Career Quest is an explainable employee-development platform for the HackAlem AI Halyk Bank track. The product will connect career targets, skill gaps, suitable development activities, verified participation, and updated career progress.

This repository currently contains the **Hour 1-2 backend foundation**: a FastAPI service, strict typed dataset models, cross-file validation, in-memory indexes, employee profiles, career-target resolution, assessed-versus-current skill progress, gap analysis, health endpoints, and automated tests. The presentation layer is intentionally not fixed yet.

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
FastAPI /health
```

Business logic will be added as independent domain services so eligibility, progress, and recommendations remain deterministic and testable rather than prompt-driven.

## Dataset

Place these official source files in `data/`:

- `skills.json`
- `employees.json`
- `events.json`
- `activity_history.csv`

The loader preserves all supplied fields and validates schemas, value ranges, unique IDs, role/grade combinations, manager relationships, career goals, skill references, and history references. Invalid data stops startup with a targeted error.

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
- `GET /career/requirements?role=...&grade=...` - role/grade requirements

Progress keeps formal assessment separate from later development:

```text
assessed_level + verified post-review activity gains = effective_level
gap = max(target_level - effective_level, 0)
```

Each post-review gain includes its supporting event IDs. Activity progress is
shown as development evidence and is not represented as a new formal assessment.

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

The Hour 1-2 suite verifies:

- the official four source files load;
- the expected dataset counts are exposed;
- indexes are created;
- missing source files fail clearly;
- broken cross-file employee references fail with the exact record ID;
- the health endpoint reports the loaded snapshot.
- arbitrary employee progress can be calculated;
- null goals resolve to the next grade;
- lateral goals use the target role's requirements;
- Lead employees without a goal are handled safely;
- post-review completions change effective progress without changing the assessed level.
