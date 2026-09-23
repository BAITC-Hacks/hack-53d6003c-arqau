# Career Quest

Career Quest is an explainable employee-development platform for the HackAlem AI Halyk Bank track. Employees often cannot see which action will actually move them toward a career goal, while HR sees participation data without a reliable view of blocked skills or catalog gaps. Career Quest turns assessed skills, goals, learning history, and the event catalog into transparent next actions and live workforce insights.

The solution is a working employee and HR web app backed by deterministic services: it resolves a career target, keeps formal assessment separate from estimated development, ranks useful activities with inspectable factors, shows mandatory work separately, recalculates after completion, and aggregates privacy-conscious HR signals. No LLM is required for the core decision path.

## Current architecture

```text
React + TypeScript web app (employee mobile / HR desktop)
                         |
                         v
                 FastAPI + HMAC auth
                  /       |        \
                 v        v         v
       Progress service  Recommendation  HR analytics/signals
                 \        |         /
                  v       v        v
              Runtime DataStore + immutable indexes
                         |
                         v
    DatasetLoader -> Pydantic schemas -> cross-file validation
                         |
                         v
        skills.json / employees.json / events.json / history.csv
```

FastAPI serves both the API and the compiled single-page application in the production container. Domain logic remains independent, deterministic, and directly testable.

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

## One-command demo

Docker is the fastest jury path:

```bash
cp .env.example .env
docker compose up --build
```

Open <http://localhost:8000>. The same origin serves the React app and FastAPI;
API documentation remains at <http://localhost:8000/docs>. Stop with `Ctrl+C`
and remove the container with `docker compose down`.

The checked-in `dataset/` is synthetic demo data. To boot against another
compatible base dataset without copying it into the repository:

```bash
CAREER_QUEST_DATA_DIR=/absolute/path/to/dataset docker compose up --build
```

## Local development

Python 3.11 or newer is required.

```bash
make install
make test
make dev
```

`make dev` starts FastAPI on port 8000 and Vite on port 5173; open
<http://127.0.0.1:5173>. `make run` starts only the backend with reload. To
exercise the production path locally, run `make build-frontend && make run`
and open port 8000.

Useful URLs:

- Health and dataset counts: <http://127.0.0.1:8000/health>
- Interactive API documentation: <http://127.0.0.1:8000/docs>

### Web app behavior

The Step 5 interface is a Vite + React + TypeScript + Tailwind application. It
uses the FastAPI service for every employee, skill, event, progress,
recommendation, journey, analytics, and import value; the design references in
`docs/design/` are visual guidance only.

The demo login offers the live synthetic employee
picker (including newly imported profiles) and the HR role. The Vite development
server proxies `/api` to `http://127.0.0.1:8000`.

Frontend verification:

```bash
cd frontend
npm test
npm run build
```

Employee routes are mobile-first: Home, Growth Web, Path, Events, and the
structured Career AI explanation view. HR routes are desktop-first: Overview,
People, Skills, Events, and the real dry-run/import flow. Completing an activity
uses the API diff, refreshes progress and recommendations, and shows the actual
readiness and skill changes. QR verification and generative AI are intentionally
not part of this build; the demo uses employee self-report and deterministic text.

Hour 2 endpoints:

- `GET /employees` - paginated employee directory
- `GET /employees/{employee_id}` - full profile and activity summary
- `GET /employees/{employee_id}/progress` - career target, current progress, gaps, critical gaps, and readiness
- `GET /employees/{employee_id}/recommendations?debug=false` - 1-3 explainable recommendations plus required events
- `POST /employees/{employee_id}/activities` - add an activity to the plan or mark it completed and return the resulting diff
- `POST /import` / `POST /import/dry-run` - atomically validate and merge jury-format files
- `POST /admin/reset` - restore the original startup dataset
- `POST /auth/demo-login` - issue a signed employee or HR demo token
- `GET /auth/demo-employees` - public synthetic-identity picker for demo login
- `GET /hr/status` - HR-only access check
- `GET /hr/skill-gaps` - aggregated lagging skills with department/role/grade filters
- `GET /hr/no-next-step` - employees blocked by the catalog or without a suitable action
- `GET /hr/participation` - event outcomes, completion, scores, feedback, and assignment source
- `GET /hr/dashboard` - HR KPI summary
- `GET /hr/heatmap`, `/hr/trend`, `/hr/pipeline` - visualization-ready aggregates
- `GET /hr/opportunity-gaps` - blocked catalog gaps grouped by skill and target
- `GET /hr/support-signals` - factual, supportive engagement patterns
- `GET /employees/{employee_id}/journey` - private progress and activity rhythm
- `GET /career/requirements?role=...&grade=...` - role/grade requirements

Progress keeps formal assessment separate from later development:

```text
assessed_level + verified post-review activity gains = effective_level
gap = max(target_level - effective_level, 0)
```

Each post-review gain includes its supporting event IDs. Activity progress is
shown as development evidence and is not represented as a new formal assessment.
Readiness is calculated across every target skill:

```text
readiness_pct = round(100 × Σ min(effective_level, target_level) / Σ target_level)
```

The progress response includes this formula and each skill's credited and target
levels. `requirements_met / requirements_total` remains a separate count of skills
that fully meet their target; partial skill progress contributes only to readiness.

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

Every unclosed gap that no eligible catalog event can improve appears in
`blocked_gaps`, critical gaps first. Each entry lists the event-level blocking
reason and a supportive suggestion for on-the-job practice or mentoring. The
top-level localized `summary` explicitly warns when a critical gap is blocked,
even if recommendations for other skills are available.

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
| Same-event friction | 1.5× the applicable friction penalty |
| Remote employee with an offline event | -0.75 |
| Effort | -0.04 per hour |
| New-gap diversity while selecting recommendations 2-3 | +1.00 per new gap |

For positive evidence, similar means the same type, the same format, or a shared
developed skill. For friction, similar means a shared developed skill or the same
type-and-format pair. Records for the exact same event are also counted and receive
a 1.5× penalty, because repeated friction with that activity is especially useful
evidence. No-show friction weighs more for scheduled activities; dropped history
weighs more for long activities.

The engine greedily selects up to three events and rewards coverage of different
gaps. Scores map to labels without probabilities: `Strong match` at 12 or above,
`Good match` at 5 or above, and `Exploratory` below 5. Every result contains
structured numerical factors, expected skill effects, and an English, Russian,
or Kazakh template explanation based on the employee's preferred language. The
explanation always covers the current grade and target, the numbered skill gap,
the estimated effect, participation history, effort, localized format, and next
session without exposing internal scoring syntax.

Mandatory events never compete with personal recommendations. Onboarding is
required only during an employee's first three months. Other mandatory compliance
events are annual: they are required when the employee is in the audience and has
no completion in the 365 days before the dataset snapshot. Required statuses are
`due`, `in_progress`, or `overdue`, with `due_date` retained even when it is null.

## Runtime imports and activity updates

`DataStore` owns the live `DatasetBundle`. Every accepted import or activity
update is validated with `DatasetLoader`, atomically swaps the bundle, and
recreates the progress and recommendation services. No server restart is needed.

The jury can import any subset of `employees.json`, `activity_history.csv`,
`events.json`, and `skills.json`. A request containing a new employee and that
employee's history is validated as one merged dataset, so cross-file references
within the same upload work. Existing IDs are conflicts by default; explicitly
use `?mode=upsert` to replace them. Invalid imports return file and row/ID errors
without changing live state.

Multipart upload:

```bash
TOKEN=$(curl --silent --fail-with-body -X POST \
  -H 'Content-Type: application/json' \
  -d '{"role":"hr"}' \
  http://127.0.0.1:8000/auth/demo-login | \
  .venv/bin/python -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')

curl --fail-with-body -X POST \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@/path/to/employees.json" \
  -F "files=@/path/to/activity_history.csv" \
  http://127.0.0.1:8000/import
```

Use `/import/dry-run` for the same report without applying it, or use the helper:

```bash
make import DIR=/path/to/extra-data
```

For startup import, point to a directory containing any subset of those files:

```bash
CAREER_QUEST_EXTRA_DATA_DIR=/path/to/extra-data make run
```

### How the jury loads test profiles

There are three supported paths, all using the same validation and live service
rebuild:

1. **UI:** sign in as HR, open **Import**, choose one or more compatible files,
   run **Validate**, review the dry-run report, then select **Import**.
2. **API:** use the HR-token multipart command above. Replace `/import` with
   `/import/dry-run` to validate without changing the live snapshot; add
   `?mode=upsert` only when intentionally replacing existing IDs.
3. **Startup directory:** run
   `CAREER_QUEST_EXTRA_DATA_DIR=/absolute/path/to/extra-data make run`. The
   directory may contain any subset of the four supported files and is merged
   before the first request.

For a repeatable presentation, HR can call `POST /admin/reset` with the same
Bearer token, or simply restart the Docker container.

`POST /employees/{id}/activities` accepts `completed` or `in_progress` with a
source of `self_report`, `qr_verified`, or `hr_confirmed`. Completion updates only
the post-review estimate, never the formal assessed level. The response includes
changed skills, readiness before/after, and recommendation IDs before/after.
Non-recurring duplicate completions return `409`; recurring EV_036 can repeat.
`POST /admin/reset` restores the original four source files for repeated demos.

## Roles and permissions

All employee, runtime, career, admin, and HR endpoints require a Bearer token;
`GET /health`, `POST /auth/demo-login`, and the synthetic demo employee picker
remain public. Tokens are compact JSON payloads signed with HMAC-SHA256. Set a
stable secret before starting the API:

```bash
CAREER_QUEST_SECRET='replace-with-a-long-random-secret' make run
```

When the variable is omitted, the server generates an ephemeral secret at
startup, so tokens stop working after a restart. The secret is never returned by
the API.

Employee login:

```bash
curl -X POST http://127.0.0.1:8000/auth/demo-login \
  -H 'Content-Type: application/json' \
  -d '{"role":"employee","employee_id":"E0028"}'
```

HR login uses `{"role":"hr"}`. Send the returned token as
`Authorization: Bearer <access_token>`. Employee tokens can read and update only
their own `/employees/{id}/*` resources and can submit only `self_report`
activities. They cannot list the employee directory or access `/hr/*`, `/import`,
or `/admin/*`. HR tokens can read any employee and access the HR, import, and
administrative routes. Current HR responses contain structured employee and
activity data only; private Career AI chat and self-reported capacity are not
part of any HR response.

## HR analytics and support signals

`AnalyticsService` and `EngagementSignalService` calculate every response from
the current `DataStore` snapshot, so imports, completions, and resets appear on
the next request. HR people-oriented endpoints use employee IDs and initials,
not full names; employee journey data is self-only.

Skill gaps use each employee's resolved career target and effective levels.
No-next-step includes both `NO_SUITABLE_ACTION` employees and employees whose
critical target gaps are all blocked. Opportunity gaps group those blocked
skills by target role, grade, current level, and required level, framing them as
L&D catalog issues rather than employee failures.

Support thresholds live together in `ANALYTICS_THRESHOLDS`:

- active/recent window: 90 days before the dataset snapshot;
- participation drop: at least 6 non-mandatory baseline records and a recent
  monthly rate below 50% of that prior 12-month rate;
- repeated friction: at least 2 no-show, dropped, or declined records of the
  same status in the last 12 months;
- re-engaged: at most 1 baseline record and at least 2 recent records;
- mandatory overdue and missing relevant opportunities stay separate.

Signals state only observed counts and shared formats/types. Every signal says
the reason is unknown and suggests a supportive response; the service never
diagnoses motivation or labels performance. The employee journey shows only the
logged-in employee's post-review evidence and monthly rhythm and states that a
break does not reset progress.

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

## AI layer and fallback

The current Career AI screen is an explainability view, not a generative model.
It renders the same structured factors, expected effects, blocked-gap reasons,
and localized templates returned by the recommendation API. This makes the full
demo work offline and ensures an unavailable provider can never block a career
recommendation. `OPENAI_API_KEY` and `OPENAI_MODEL` are reserved in
`.env.example` for a later, optional natural-language layer; no key or external
AI call is used in this build, and an eventual model must explain structured
results rather than make eligibility or ranking decisions.

## Privacy and data policy

- The repository's `dataset/` contains synthetic demo identities only. Official,
  evaluation, or employee data must stay outside Git and be loaded through the
  UI, API, or environment variable; `data/`, `private-data/`, and `.env` are
  ignored.
- Docker mounts the selected base dataset read-only. Runtime imports live only
  in process memory and disappear on reset or restart.
- Employee tokens are self-only. HR endpoints return initials/IDs for
  people-oriented analytics and never expose private Career AI content.
- Recommendations and support signals are decision support, not performance
  ratings. The system records factual evidence, never diagnoses motivation, and
  keeps formal assessed levels distinct from estimates inferred from activities.
- The core app sends no dataset content to an external AI service.

## Demo script

1. Run `docker compose up --build`, open <http://localhost:8000>, and choose
   employee **E0028** in the live picker.
2. On **Home**, show readiness and the critical blocked-gap summary. Open
   **Events**, expand **Why this?**, and point to the numbered gap, estimated
   gain, friction evidence, availability, and match label.
3. Mark an eligible recommended activity completed. The confirmation uses the
   API's real before/after diff; revisit **Home** and **Growth** to show changed
   effective skill/readiness and the refreshed recommendation list.
4. Sign out, enter the HR workspace, and show Overview, People support signals,
   Skills, Events, and the separation between employee needs and catalog gaps.
5. In **Import**, dry-run a new sample `employees.json` plus optional history,
   review validation, import, then sign in as that employee. Repeat **Why this?**
   and completion to demonstrate that nothing was scripted for E0028.
6. Use **Reset demo data** before repeating the presentation.

## Limitations and next steps

- QR attendance verification is deferred; the current demo supports employee
  self-report and HR-confirmed activity updates through the API.
- Generative Career AI is optional and deferred. Deterministic localized
  explanations remain the safe fallback.
- Runtime imports are in-memory by design; a production deployment would add a
  transactional database, SSO, audit logs, secrets management, and background
  event ingestion.
- Catalog capacity, waitlists, and calendar enrollment are not modeled. The
  engine ranks only the availability represented in the supplied snapshot.
- Frontend browser automation is provided separately from the default unit test
  path because it requires an installed Playwright browser.

## Tests

Run all default backend and frontend tests with one command:

```bash
make test
```

Build the production frontend and validate the Compose definition with:

```bash
make build-frontend
docker compose config
```

Optional browser tests: `npm --prefix frontend run e2e` after installing a
Playwright browser.

The backend suite verifies:

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
- JSON and multipart jury imports, dry runs, upserts, and atomic failures;
- startup extra-data merge and reset to the original snapshot;
- live completion and add-to-plan recalculation without restart;
- duplicate completion protection and recurring EV_036 behavior.
- public health plus HMAC token validation and clear 401/403 responses;
- employee self-only access and HR-only directory, import, admin, and HR routes.
- HR gap, participation, dashboard, heatmap, trend, pipeline, and catalog-opportunity aggregates;
- every support-signal rule, supportive-language guardrails, live no-next-step updates, and private journeys.
