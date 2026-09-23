# Career Quest — screen spec

Source of truth for layout: `design/reference/*.dc.html` (one file per screen).
Source of truth for numbers: `data/fixtures.json`. Source of truth for style: `design/tokens.json`.

## How to read the reference files

They are HTML with a small template layer. Treat them as annotated markup, not as runnable code:

| In the file | Means |
|---|---|
| `<x-dc>` ... `</x-dc>` | the screen's root |
| `<helmet>` | page-level `<style>` / font link |
| `{{name}}` | value computed in the `renderVals()` script at the bottom |
| `<sc-for list="{{items}}" as="item">` | `items.map(item => ...)` |
| `<sc-if value="{{cond}}">` | `{cond && ...}` |
| `<dc-import name="X">` | render component from `X.dc.html` |
| `<a href="M-Path.dc.html">` | route link to that screen |

Inline styles carry the exact values (px, colors, radii). Copy them into Tailwind classes or CSS modules; do not approximate.

## Employee (mobile web first, 390 px design width)

Bottom tab bar on every tab screen: Home, Growth, Path, Events, AI (AI icon always violet). My QR sits in the Home header. Profile/Rewards open from the avatar.

| Route | Reference | Content |
|---|---|---|
| `/` | `M-Home` | Gradient header: greeting, "Building toward Senior Backend Engineer", readiness 72%, "3 of 5 requirements met", "2 critical gaps", Junior → Middle → Senior track (links to /path). Cards: Next step (Case Lab, Add to plan, Why this? → /ai), Career AI entry, 12-week rhythm. |
| `/growth` | `M-Growth` | Growth Web with 4 toggleable layers (Assessed solid teal, Since review dashed green, Target dashed ink, 6 months ago dotted grey), gold dots on critical gaps. Readiness line chart. Skill-by-skill bars (assessed / estimate / target tick). Recent evidence list. Prop `updated` switches to post-verification state (banner, System Design 2.9, readiness 76%). |
| `/path` | `M-Path` | Segmented My path / Explore roles. Readiness card with 5 segments (3 green, 2 gold). Vertical timeline: Tech Lead (later) → Senior (target) → Middle (you, expanded with 2 critical gaps first, then 3 met, then locked track) → Junior. "What if: Data Engineer 54%" card. |
| `/events` | `M-Events` | Search, tabs For you / Explore / Required, quick chips, 3 recommended cards, separate Required section. |
| `/ai` | `M-AI` | Chat. Answer contains an action card (Add to plan / Alternative), collapsible "Why this answer?", links Open skill / Open career path. Suggested prompts above input. |
| sheet | `M-QR` | Bottom sheet: dynamic QR, name, role, 00:42 refresh countdown, "Checking in to: System Design Case Lab". |
| sheet | `M-Verified` | "Participation verified", change list (System Design 2.6 → 2.9, readiness 72% → 76%), See what changed → `/growth?updated=1`. |

## HR (desktop, 1280 design width)

Left sidebar 232 px (Overview, People, Skills, Events, Career Pipeline; Import data at bottom). Top bar: persistent "Ask AI" input (violet) + role switcher.

| Route | Reference | Content |
|---|---|---|
| `/hr` | `W-HR-Overview` | 5 KPI cards, dept × skill heatmap with values + legend (cells link to /hr/skills), AI insights panel (We know / We don't know / Suggest), development-active trend, career pipeline bars. |
| `/hr/people` | `W-HR-People` | Tabs All / Support signals / Opportunity gaps. List with initials only (no photos), neutral colored dot per signal kind. Right panel for selected row: Why surfaced, What we know, What we don't know, AI suggested approach, Plan a 1:1 / Dismiss, privacy footnote. |
| `/hr/skills` | `W-HR-Skills` | Org Growth Web (current avg vs required), clickable "Skills requiring attention" list; selected skill drives cohort breakdown, Demand ↔ supply and AI insight cards. |
| `/hr/events` | `W-HR-Events` | Event list with verified-rate bars; selected event: 4-step funnel (Registered → QR verified → Completed → Development progress), no-show, declined, feedback, AI note, "attendance ≠ mastery" footnote. |
| modal | `W-HR-Import` | Drop zone, Use sample dataset, detected files with status, column-mapping row, privacy note, Cancel / Import. |

## The demo flow (must work end to end)

`/` → My QR → (tap QR = simulated organizer scan) → Participation verified → See what changed → `/growth` animates the "Since review" polygon from the old to the new points (600 ms) and counts readiness 72 → 76 → new top recommendation on `/` becomes Presentation Clinic (Communication is now the biggest gap).

## Product rules to keep in code

- Formal assessment and post-review estimate are always visually distinct; an activity never changes the formal level.
- No red for low participation; no photos in HR signal lists; no single "employee score".
- Every metric has a "why" affordance (tooltip, drawer or AI answer).
- Employee's weekly capacity check-in (if added) is never exposed to HR endpoints.
