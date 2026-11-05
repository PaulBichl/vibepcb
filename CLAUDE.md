# CLAUDE.md

@AGENTS.md

## Claude Code specifics

- The project instructions live in `AGENTS.md` (tool-agnostic, read by other agents too); edit them there.
- Long builds (10+ min): run `hatch run pcb build|all <name>` with the Bash tool's `run_in_background`, and wait for
  the completion notification instead of polling or starting a second build.
- The tscircuit skill is vendored in `.claude/skills/tscircuit/` (`hatch run update-skill` re-vendors it).

## Agent skills

### Issue tracker

GitHub Issues on PaulBichl/vibepcb via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: `GLOSSARY.md` + `docs/adr/` at the repo root. See `docs/agents/domain.md`.
