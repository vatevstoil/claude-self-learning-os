# Cross-Project Patterns

> Universal lessons valid across ALL projects. Read on-demand, NOT every session.
> Project-specific gotcha → stays in that project's learnings.
> Universal gotcha → comes here.

---

## Deployment

- **Verify with curl after EVERY deployment** — `curl -s -o /dev/null -w "%{http_code}"`. Never accept "deployed" without an HTTP status code.
- **Never leave .bak files in active config directories** — servers may load all files in the directory.

## Frontend

- **Static routes before parameterized** — `/users/me` BEFORE `/users/:id`
- **Loading state + async gate = potential infinite spinner** — if the gate check is in a callback with `useRef` guard, loading may never resolve. Put gate checks in `useEffect`.

## Backend

- **Monetary columns need explicit float handling** — ORM defaults may return Decimal objects that break JSON serialization.
- **New DB column = migration + ORM together** — ORM model alone is never enough. Always add the migration step.
- **Route ordering matters** — static paths before parameterized catch-alls.

## Process

- **Session start: 1 minute max** — SPRINT + learnings + last log entries.
- **Don't say "done" with known issues** — verify build/tests pass first.

---

*Last updated: YYYY-MM-DD*
*Rule: Add here ONLY patterns valid for 2+ projects.*
