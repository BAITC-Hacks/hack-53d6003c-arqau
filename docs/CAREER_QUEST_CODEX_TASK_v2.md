# Career Quest — Codex Task v2 (replaces the remaining hours of v1)

Read this whole file once, then implement **one step at a time**, in order.
Each step ends with green tests, a README update and one commit. Do not start
the next step in the same session unless asked.

Before any step: read `dataset/README.md`, `README.md`, and the existing code
in `backend/app/` (`models.py`, `data_loader.py`, `career_progress.py`,
`recommendations.py`, `main.py`). Reuse these services; do not duplicate logic.

---

## 0. Hackathon task: what the jury checks (source of truth)

This file is written to satisfy the official HackAlem AI / Halyk Bank
"Career Quest" task. When anything here conflicts with the official task, the
task wins.

**Core** (quoted in spirit from the task): quality and explainability of the
recommendation, **not** the UI. Gamification is optional.

| Must-have (official) | How the jury checks it | Where it lives |
|---|---|---|
| Profile and career trajectory | Opens **any** employee: role, grade, skills, completed activities, available next steps | done (API) + Step 5 (UI) |
| AI recommendation of the next step | System suggests **1–3** relevant activities | done + Step 1 fixes |
| Explanation of the recommendation | Based on **≥ 3 factors**: grade, skill gaps, participation history, next-level requirements | Step 1 |
| Progress update | Mark an activity completed → skill progress and trajectory move | Step 2 |
| Simple HR view | Which skills lag most, **who has no recommended step**, participation by activity | Step 4 + Step 5 |
| Test profiles | Jury **loads 3 extra profiles + history in the dataset format** at the defense; single-factor rules fail on them | Step 2 |

Constraints (official): no single-field recommendation presented as AI; **no
public performance rankings/leaderboards**; no mechanics around mandatory
processes (e.g. points for timesheets / compliance); no real personal data.

Must consider (official): privacy (engagement data not visible to other
employees without consent); **employee/HR permission separation**;
explainability (why a step was suggested and **how progress was
calculated**); **one-command startup**; voluntariness.

Latency (official): UI response ≤ 2 s; AI recommendation ≤ 10 s.

Scoring: fit & working scenario 25 · technical implementation incl. AI 25 ·
**README & reproducibility 25** · value 15 · originality 10.

---

## 1. Status vs. the original v1 plan

| v1 hour | Status on `main` (commit 44a8c7e) |
|---|---|
| H1 Foundation + dataset loader | ✅ done (strict validation, warnings for conventions, indexes, `/health`) |
| H2 Employee domain + progress | ✅ done (assessed vs post-review, gaps, critical gaps, null/lateral goals) |
| H3 Recommendation engine | ⚠️ done with **bugs** → Step 1 |
| Import of jury employees/history | ❌ not started → Step 2 (**must-have**) |
| Completion updates progress | ❌ not started → Step 2 (**must-have**) |
| Employee/HR permissions | ❌ not started → Step 3 |
| H5 HR analytics, support signals, opportunity gaps | ❌ not started → Step 4 |
| H4 Employee UI / HR UI | ❌ not started → Step 5 (with the design package) |
| H6 QR check-in | ❌ not started → Step 6 (lightweight) |
| H7 Career AI + HR insights (LLM) | ❌ not started → Step 7 |
| H8 Language handling, polish | ❌ partially (templates in en/ru/kk) → Steps 1, 5, 7 |
| H9 README, one-command startup | ⚠️ partial (backend only) → Step 8 |
| H10 Final QA | ❌ → Step 9 |
| Manager Copilot (P1) | ❌ → Step 10, only if time remains |

---

## Global rules (apply to every step)

- "Today" is always `dataset.skills.meta.as_of_date` (2026-10-01). Never `date.today()`.
- Never hard-code employees, events, skills or numbers in code or UI. Everything
  is computed from the loaded dataset + runtime changes, so imported profiles work.
- Business logic stays deterministic and testable. The LLM only phrases,
  converses and summarizes **structured results produced by the backend**.
- User-facing text never contains developer strings (`score >=80`, `cap 1`,
  Python dicts, reason codes). Codes stay in structured fields; text is templated.
- Keep formal assessment separate: an activity never changes `assessed_level`;
  it changes the post-review estimate. Say "estimate", never "certified".
- Detect patterns, never diagnose motives. No red for low participation. No
  shame lists, no leaderboards, no streaks that reset, no points for mandatory events.
- Weights and thresholds live in one module-level dict per service.
- All existing tests stay green. `make test` before every commit.

---

## Step 1 — Fix and harden the recommendation engine

Verified problems on the current `main`:

1. **Same-event friction is ignored.** `_friction()` skips records where
   `record.event_id == event.event_id`. Result on real data: E0051 no-showed
   EV_036 three times and still gets EV_036 recommended with **no** friction
   factor; E0028 dropped EV_009 twice and still gets EV_009. This is exactly
   the jury's trap ("missed similar activities three times").
   Fix: count the same event too, with a higher weight than "similar"
   (e.g. ×1.5), and mention it in the explanation
   ("you missed Public Speaking Club 3 times — ranked lower, alternatives first").
2. **Critical gaps without any eligible event are silent.** E0028 (the task's
   own example: Backend Middle → Senior) has critical gap System Design 3→4, but
   EV_006/EV_007 are completed and EV_005 caps at 3, so the engine quietly
   recommends Leadership. Add `blocked_gaps: [{skill_id, name, effective_level,
   target_level, critical, blocking_reasons: [{event_id, reason_code}],
   suggestion}]` for every gap (critical first) that no eligible event can move.
   `suggestion` is a template such as "No catalog activity can move System Design
   from 3 to 4. Discuss on-the-job practice or mentoring with your manager; HR sees
   this as a catalog gap." The top-level response gets a short `summary` saying the
   critical gap is blocked, so recommending a non-critical skill is not misleading.
   These feed HR "opportunity gaps" in Step 4.
3. **EV_004 onboarding is listed as "required / not_started" for 120 long-tenured
   employees.** Onboarding only applies to new hires. Treat a mandatory
   `onboarding` event as required only when `tenure_months <= 3` (constant in
   code). Other mandatory events: required when the employee is in the audience and
   there is no completed record in the last 12 months before as_of_date (annual
   compliance), status `overdue` / `in_progress` / `due` with `due_date`.
4. **Explanations leak developer text.** Replace the raw `positive.detail` in
   ru/kk/en templates with proper sentences. Every explanation must name, in the
   employee's language:
   - current grade → target role/grade (**grade**, **next-level requirement**),
   - the skill gap with numbers and whether it's critical (**skill gap**),
   - the expected effect (from → to, as an estimate),
   - **participation history** (positive evidence, friction, or "no similar history yet" — always present),
   - effort/format/next session.
   Also translate `format` values (online/offline/self-paced) in ru/kk.
5. **`response_model_exclude_none=True` drops `next_session: null` and
   `due_date: null`.** Keep nulls; only omit `rejected` when `debug=false`.
6. **Add `readiness` math to `/employees/{id}/progress`** (needed by UI and
   "how was progress calculated"):
   - `requirements_met / requirements_total` (already there),
   - `readiness_pct = round(100 * Σ min(effective, target) / Σ target)` over target skills,
   - `formula` string and per-skill contributions so the UI can show the math.
   The UI must never show a percentage that contradicts "X of Y requirements met".

Tests to add (build profiles by appending to a tmp copy of the dataset, as the
existing tests do):
- same-event friction: 3× `no_show` on EV_036 → EV_036 has a `friction` factor,
  is not ranked first when another eligible event closes a gap, explanation mentions it;
- blocked critical gap: a Backend Middle with SD=3 and EV_006, EV_007 completed →
  `blocked_gaps` contains SK_SYSTEM_DESIGN with reasons for EV_005/006/007;
- EV_004 is required only for tenure ≤ 3 months;
- no user-facing explanation contains `>=`, `{`, `cap`, or a reason code, in all 3 languages;
- `next_session` key is present (null) for self-paced recommendations;
- readiness_pct is consistent with requirements_met on the trap profiles.

Commit: `fix: same-event friction, blocked critical gaps, clean explanations`

---

## Step 2 — Runtime state: jury import + mark completed (**must-have**)

Introduce a `DataStore` that owns the current `DatasetBundle` and can rebuild
it after changes (in-memory is fine; rebuild is cheap: 200 employees take
~0.05 s). All endpoints read through the store; services are recreated or
cache-invalidated on every change.

### Import (jury path — must be bulletproof)
- `POST /import` (HR only) accepts `employees.json` and/or `activity_history.csv`
  (multipart upload **and** JSON body). Same schema as the dataset.
  Optionally also `events.json` / `skills.json` if supplied.
- Merge semantics: new IDs are added; an existing `employee_id` / `record_id` is
  **replaced** only with `?mode=upsert`, otherwise reported as a conflict.
- Validate with the existing loader rules on the **merged** dataset: unknown
  employee/event/skill IDs, duplicates, bad grades/statuses, malformed skills,
  history rows referencing employees from the same upload (must work).
- Atomic: if anything fails, nothing changes; return a report
  `{added_employees, added_history, warnings, errors: [{file, row/id, message}]}`.
- `POST /import/dry-run` returns the same report without applying.
- Also support startup import: `CAREER_QUEST_EXTRA_DATA_DIR=/path` (files with
  the same names, any subset) is merged at startup, and `make import DIR=...`
  posts files to a running server. Document both in README.
- `POST /admin/reset` (HR only) restores the original dataset for repeated demos.

### Mark completed
- `POST /employees/{id}/activities` with `{event_id, status: "completed", score?,
  feedback_rating?, source: "self_report" | "qr_verified" | "hr_confirmed"}`
  appends a history record dated as_of_date (new `record_id`, e.g. `RT000001`).
  Allowed: completing a recommended or eligible event; EV_036 may repeat;
  rejects duplicates of a non-recurring completed event.
- Also allow `status: "in_progress"` ("Add to plan") so the event leaves recommendations.
- After the change: progress, readiness, recommendations and HR analytics all
  reflect it on the next request (no restart).
- Response returns a **diff**: `{skills_changed: [{skill_id, from, to}],
  readiness: {from, to}, recommendations_before: [...ids], recommendations_after: [...ids]}`
  so the UI can show "what changed".
- Because `employees.skills` is the formal assessment at `last_review_date`,
  completions change the **estimate** only; the response says so.

Tests:
- import a new employee + history (in one upload) → `/employees/{new}/recommendations` works;
- import with an unknown event_id fails atomically with a row-level error;
- duplicate employee_id without upsert → conflict, nothing applied;
- HR "no suitable next step" count changes after an import (after Step 4, add that assertion);
- complete an eligible event → effective level rises by `min(gain, max_level - level)`,
  readiness updates, the completed event disappears from recommendations, and the next
  recommendation changes;
- completing the same non-recurring event twice → 409; EV_036 twice → allowed;
- reset restores the original counts.

Commit: `feat: jury data import and activity completion with live recalculation`

---

## Step 3 — Roles and permissions (employee / HR)

Hackathon-grade, but real enforcement on the server:
- `POST /auth/demo-login` with `{role: "employee", employee_id}` or `{role: "hr"}`
  (optional `{role: "manager", employee_id}` for Step 10) → signed token
  (HMAC with `CAREER_QUEST_SECRET`, default generated at startup; document it).
- Employee token: may read/write **only their own** `/employees/{id}/*`, chat with
  their own Career AI, generate their own QR. Cannot list other employees or call `/hr/*`.
- HR token: `/hr/*`, `/import`, `/admin/*`, read any profile. HR endpoints never
  return employee AI chat text or employee self-reported capacity.
- Engagement/support signals are HR-only; employees see only their own, phrased supportively.
- 401/403 responses are JSON with a clear message.
- The UI login screen is a simple role + employee picker (search by name/ID), so
  **any** employee, including imported ones, can be opened.

Tests: employee A cannot read employee B (403); employee cannot call `/hr/*`;
HR can; no token → 401 on protected routes; `/health` stays public.

Commit: `feat: employee and HR permissions`

---

## Step 4 — HR analytics, support signals, opportunity gaps (backend)

New services: `AnalyticsService`, `EngagementSignalService`. All computed from
the current store; recomputed after import/completion. Thresholds in one dict.

### Required HR data (official must-have)
- `GET /hr/skill-gaps` — skills that lag most: for each skill, count of employees
  below their target requirement, count of critical gaps, average gap; filterable by
  department, role, grade. Sorted by critical gaps.
- `GET /hr/no-next-step` — employees with `NO_SUITABLE_ACTION` **and** employees whose
  critical gaps are all blocked (`blocked_gaps`), with the reason.
- `GET /hr/participation` — per event: records, completed, in_progress, dropped,
  no_show, declined, overdue, completion rate, avg score, avg feedback, voluntary vs
  assigned (`assigned_by`). Filter by type/format/period.

### Additional analytics (for the design screens)
- `GET /hr/dashboard` — KPI cards: employees, development-active (≥1 voluntary
  activity in last 90 days), completion rate, critical gaps, no-next-step count,
  support signals count, progress awaiting formal review (completions after last_review_date).
- `GET /hr/heatmap` — department × skill: % of employees below requirement (use the
  8 real departments; skills = union of critical skills, top N by gap).
- `GET /hr/trend` — monthly voluntary participation over the history window.
- `GET /hr/pipeline` — counts by readiness bucket (ready / one critical gap /
  developing / grade ceiling).
- `GET /hr/opportunity-gaps` — aggregate of blocked gaps by skill × role/grade:
  "12 Backend Middles need System Design 4; no catalog event can move 3→4". This is an
  L&D catalog problem, not an employee problem.

### Support signals (`GET /hr/support-signals`) — detect patterns, never diagnose
Use only voluntary (non-mandatory) records unless stated. Window = as_of_date.
Thresholds (validated on the dataset — they flag a reasonable minority):
- `participation_drop`: baseline = records in the 12 months before the last 90 days,
  baseline ≥ 6, and the last-90-day monthly rate < 50% of the baseline monthly rate
  (≈ 26 employees on the current data);
- `repeated_friction`: ≥ 2 records of the same status (`no_show`, `dropped`,
  `declined`) in the last 12 months (≈ 11–26 per status); include the shared
  format/type if there is one ("3 no-shows, all offline evening meetups");
- `mandatory_overdue`: any overdue mandatory event → process issue, shown
  separately, no gamification;
- `no_relevant_opportunity`: from blocked gaps / NO_SUITABLE_ACTION;
- `re_engaged` (positive): baseline ≤ 1 and ≥ 2 records in the last 90 days.

Each signal: `{employee_id, initials, role, grade, department, kind, why_surfaced,
what_we_know: [...facts with numbers], what_we_dont_know: [...], suggested_approach}`.
All text is templated from facts. `what_we_dont_know` always states that the reason
is unknown (workload, timing, format, relevance are all possible).
`suggested_approach` is supportive (e.g. "offer a self-paced alternative",
"low-pressure 1:1 about timing"), never punitive.

### Employee-facing reassurance (`GET /employees/{id}/journey`)
For the employee's own Home screen:
- progress since review: skills moved, with evidence events;
- a monthly activity rhythm (heatmap data), where a break never resets progress;
- one gentle message chosen by rules, e.g. after a quiet period: "Your progress
  is kept. A 3-hour self-paced step is available when you're ready.";
  after friction in a format: suggest the self-paced/online alternative.
No comparison with other employees.

Tests: each signal rule on a synthetic profile; signals never contain words from a
banned list ("lazy", "unmotivated", "low performer", …); no-next-step count updates
after import; opportunity gaps contain SK_SYSTEM_DESIGN for the blocked profile
from Step 1; employee journey never includes other employees' data.

Commit: `feat: HR analytics, support signals and opportunity gaps`

---

## Step 5 — Frontend (upload the design package at this step)

Stack: Vite + React + TypeScript + Tailwind + React Router, in `frontend/`.
The design package goes in `docs/design/` (tokens, SCREENS.md, reference HTML).
**Delete `data/fixtures.json` from the package; it must not be used.**

Design override (read before the design package):
- Use tokens, layouts and reference HTML for **visuals only**.
- Every value comes from the backend API. No names, skills, events, numbers or
  texts from the fixtures. Replace "Alex Kim" with the logged-in employee.
- Skill levels are integers 0–5. Show "assessed L2 · +1 since review → L3 (estimate)",
  never decimals like 2.6.
- Readiness % and "X of Y requirements met" come from the backend formula, with a
  "How is this calculated?" drawer showing the per-skill math.
- Growth Web axes = the target role's required skills (critical marked gold), not a
  fixed 6-axis list; layers: assessed, estimate since review, target.
- Events tabs: For you (recommendations), Explore (other eligible), Required
  (mandatory, separate). Recommendation card: activity, skills, duration, format,
  next session, match label, expected effect, "Why this?" (all factors), "Add to
  plan", "Not for me" (reasons: timing, already know it, format, irrelevant,
  workload, other; stored per employee and used as a friction/preference factor).
- Show `blocked_gaps` honestly on Home/Path ("No activity in the catalog can move
  System Design 3→4 yet — talk to your manager").
- HR screens: Overview (KPIs, heatmap, trend, pipeline), People (tabs: support
  signals, no next step), Skills (top gaps, opportunity gaps), Events
  (participation by activity from history statuses), Import modal (real upload to
  `/import`, dry-run preview, row-level errors).
- Heatmap uses the 8 real departments; event funnel uses real statuses
  (registered = all records → completed; also no-show/dropped/declined).
- Demo flow: employee Home → recommendation → Why this? → mark completed (or QR in
  Step 6) → "What changed" (diff from the API) → Growth Web animates from old to new
  → Home shows the refreshed recommendation.
- Loading/error/empty states everywhere; UI actions respond < 2 s.
- UI language switch en/ru/kk; default = employee's `preferred_language`. Translate
  UI chrome; data names (skills/events) may stay in English if no translation exists.
- Accessibility: real buttons/links, 44 px targets, contrast ≥ 4.5:1, no horizontal
  scroll at 360 px.

Tests: `npm run build` passes; a few component tests (recommendation card renders
factors; readiness drawer shows the formula); one Playwright (or similar) e2e: login as
employee → complete recommendation → readiness changes.

Commit: `feat: employee and HR web app`

---

## Step 6 — Lightweight QR verification (optional polish of Step 2)

Only after Step 5 works. Keep it small; QR proves participation, not mastery.
- `POST /checkin/token` (employee): short-lived (60 s) single-use HMAC token for
  `{employee_id, event_id}`; QR encodes only the token, never personal data.
- `POST /checkin/verify` (HR/organizer): validates, then calls the same completion
  logic as Step 2 with `source: "qr_verified"`; returns the diff.
- UI: My QR sheet with countdown; HR "Scan / paste token" screen (camera optional;
  a paste field is enough for the demo).
Tests: expired token rejected; reused token rejected; verification updates progress
exactly once.

Commit: `feat: QR check-in for verified participation`

---

## Step 7 — AI layer (OpenAI), grounded and optional

The deterministic engine decides; the LLM explains and converses.
- Provider: OpenAI via `OPENAI_API_KEY` (model in `OPENAI_MODEL`, documented).
  **If the key is missing or a call fails/times out (8 s), fall back to the
  template text.** The app must be fully demoable without a key.
- `POST /ai/employee/chat` (employee only, own data): tool/function calling over
  backend functions only: `get_progress`, `get_recommendations`,
  `explain_recommendation(event_id)`, `simulate_completion(event_id)` (dry-run of
  Step 2's diff), `get_alternatives(skill_id)`, `get_journey`. System prompt: answer
  only from tool results; never invent events, levels or dates; if asked for
  something not in the catalog, say so; answer in `preferred_language` (kk/ru/en).
  Returns `{text, actions: [{type: "add_to_plan" | "open_event" | "open_skill", id}],
  sources: [...]}`; the UI renders a "Why this answer?" from `sources`.
  Must handle: "What should I focus on this month?", "Why am I not ready for
  Senior?", "Give me something small this week", "Why was this recommended?",
  "What happens if I complete this?", "Another way to improve communication".
- `POST /ai/explain/{employee_id}/{event_id}` — optional LLM rephrasing of the
  structured explanation; must keep every number from the structured factors
  (test: numbers in output ⊆ numbers in input).
- `POST /ai/hr/insights` (HR only): 3–5 evidence-backed observations generated from
  `/hr/*` aggregates (not raw personal data); each links to the filter that supports
  it, with a "we know / we don't know" structure.
- Privacy: employee chat text is not stored server-side beyond the session and is
  never exposed to HR endpoints.
- Latency: stream or return within 10 s; the UI shows the template answer
  immediately and upgrades it when the LLM responds.

Tests (mock the LLM): tools called with the right employee only; the fallback works
with no key; chat can't access another employee's data; the HR insight prompt
contains no chat text.

Commit: `feat: grounded Career AI and HR insights with offline fallback`

---

## Step 8 — One-command startup and README (25% of the score)

- `docker compose up --build` starts everything at <http://localhost:8000>
  (FastAPI serves the built frontend; one container is fine). Also keep `make dev`
  (backend + Vite) and `make test` (backend + frontend tests).
- `.env.example` with `OPENAI_API_KEY` (optional), `OPENAI_MODEL`,
  `CAREER_QUEST_SECRET`, `CAREER_QUEST_DATA_DIR`, `CAREER_QUEST_EXTRA_DATA_DIR`.
- README sections: problem → solution; architecture diagram; data semantics
  (assessed vs estimate); **recommendation logic** (filters, factors, weights, labels,
  friction, blocked gaps); readiness formula; HR analytics and signal rules with
  thresholds; AI layer and fallback; privacy & permissions; **how the jury loads test
  profiles** (UI import, API, env var — with exact commands); demo script; tests;
  limitations and next steps.
- Data policy: the task says data must not leave the hackathon. If the repo is or
  will be public, move `dataset/` to `.gitignore` and explain placement in README
  (the app already auto-detects `data/` or `dataset/`).

Commit: `docs: one-command startup and complete README`

---

## Step 9 — Final QA (fix only P0/P1 bugs, no new features)

Checklist, run from a **fresh clone**:
- `docker compose up --build` works; `make test` green.
- The three jury-style trap profiles (write them as sample files in
  `samples/jury_like/`), imported through the UI, get correct, multi-factor recommendations:
  1. lowest skill is not the critical gap + repeated no-shows on its events;
  2. critical gap where the only event's `max_level` can't help;
  3. unmet prerequisites / lateral goal / no goal.
- Mark completed → skills, readiness, trajectory, recommendations and HR counts all change.
- Employee cannot see another employee or HR data.
- Every screen works with the API key removed.
- UI < 2 s; AI < 10 s.
- Demo script runs end to end (below).

Commit: `fix: harden end-to-end demo`

---

## Step 10 — Manager view (P1, only if everything above is done)

Manager (a Lead with direct reports via `manager_id`) sees their direct reports'
development themes, upcoming planned activities, team skill coverage, and suggested
1:1 topics. No AI chat text, no personal engagement signals unless also HR.

---

## Out of scope until Step 9 passes

Reward shop, points/currency, badges, "Wrapped", leaderboards, streaks,
animations beyond the Growth Web morph, rotating event QR, calendar/messenger integration.

---

## Demo script (≈ 5 min)

1. Log in as an employee (e.g. a Backend Middle with a Senior goal). Show profile, Growth Web vs target.
2. Open the top recommendation → "Why this?": grade, critical gap with numbers, next-level requirement, history.
3. Show why the lowest skill was **not** chosen (friction / not critical) using the debug "rejected" view.
4. Show a blocked critical gap (E0028: System Design 3→4) — honest "no activity exists" message.
5. Mark completed (or QR verify) → "What changed" diff → Growth Web moves → new recommendation.
6. Ask Career AI "What should I focus on this month?" (then show it still works without the key).
7. Switch to HR: lagging skills, participation by event, people with no next step, a support signal (we know / we don't know / supportive approach), an opportunity gap.
8. Import the jury-like sample profiles → open one → correct recommendations immediately.
