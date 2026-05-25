# [Cluster Name] — [Project Name]

> One-line summary of what this cluster handles.

---

## Files

| File | Purpose |
|------|---------|
| `path/to/main_file.py` | Main router / entry |
| `path/to/models.py` | DB models for this domain |
| `path/to/service.py` | Business logic |

---

## Key Functions / Endpoints

```
GET  /prefix/list          -> list_items()       -- list with tenant filter
POST /prefix/create        -> create_item()      -- create + audit log
PUT  /prefix/{id}          -> update_item()      -- update, validate ownership
DEL  /prefix/{id}          -> soft_delete_item() -- sets deleted_at, NOT physical delete
```

---

## Data Model

```
TableName
  id          INT PK
  owner_id    INT FK -> users (TENANT FILTER)
  name        VARCHAR
  deleted_at  DATETIME nullable  (soft delete)
  created_at  DATETIME
```

---

## Critical Rules

- **Rule 1** — specific gotcha for this cluster (e.g. "expense IDs are UUID strings, NOT integers")
- **Rule 2** — ordering rule (e.g. "static routes before /{id}")
- **Rule 3** — calculation rule (e.g. "total is computed from items, no total_amount column")

---

## Connections

- Uses: [[graph/auth]] — for current_user dependency
- Used by: [[graph/frontend]] — renders this data
- Related: [[graph/shared-patterns]] — tenant filter pattern
