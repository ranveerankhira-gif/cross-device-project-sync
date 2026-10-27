# Project Context Files

Git synchronizes files, not Codex local chat history. Preserve long-lived context in repository files so any device can understand the project.

Complete backup scope is the selected project root, not all personal Codex data. Record confirmed nonsecret backup destination/scope and manifest/restore information when useful, following `setup-existing-project.md`. Do not record credentials or declare a Git push equivalent to a full filesystem backup.

## AGENTS.md

Use `AGENTS.md` for stable information:

- Project goal
- Architecture
- Tech stack
- Important commands
- Development rules
- Important constraints
- Repository structure
- Cross-device workflow notes

Avoid repeatedly rewriting this file for transient progress.

## docs/project-state.md

Use `docs/project-state.md` for current handoff context:

- Last updated date
- Completed work
- In progress
- Known issues
- Next suggested tasks

Update it after meaningful development phases or before switching devices when the next Codex session would benefit.
