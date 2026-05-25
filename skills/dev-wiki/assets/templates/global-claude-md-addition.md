# Add this to your ~/.claude/CLAUDE.md

Copy the section below into your global CLAUDE.md file.
Replace `<projects-path>` and `<wiki-path>` with your actual paths.

---

## Dev Wiki — Knowledge Base Protocol

Every project in `<projects-path>` may have a wiki in `<wiki-path>`.

**Session start (skip for trivial tasks — typo fix, single line):**
1. `{wiki}/wiki/SPRINT.md` — current tasks
2. `{wiki}/wiki/sources/learnings.md` — gotchas (if exists)
3. `{wiki}/wiki/log.md` — last ~30 lines

**Session end (after meaningful work):**
- Update SPRINT.md + log.md. New gotcha → learnings (universal → `_shared/patterns.md`).

**No wiki?** → suggest `python ~/.claude/skills/dev-wiki/scripts/init_dev_wiki.py`
