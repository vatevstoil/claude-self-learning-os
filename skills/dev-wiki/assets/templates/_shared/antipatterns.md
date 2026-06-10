# Cross-Project Antipatterns

> Mistakes from real projects. Read when encountering repeated issues.

| Antipattern | What happened | Correct approach |
|-------------|--------------|-----------------|
| Deploy without verification | Assumed "deployed" without HTTP check | Always `curl -w "%{http_code}"` |
| `useState(true)` + async gate | Infinite spinner when owner not loaded at mount | Gate check in `useEffect`, loading starts `false` |
| New DB column only in ORM | Column silently missing in production | Always add migration step alongside ORM change |
| Static route after catch-all | `/stats` interpreted as `/{id}` parameter | Define static routes before parameterized ones |

---

*Last updated: YYYY-MM-DD*
