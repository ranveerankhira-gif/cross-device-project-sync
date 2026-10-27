# After Work Or Before Switching Devices

Use this when the user says they are done, saving progress, pushing latest work, switching computers, or shutting down.

First honor the established scope. An unqualified “上传云端”, “开始上传”, or “开始上传 skill” means the all-project workflow in `all-projects-migration.md`; use `full-cloud-backup.md` separately for each eligible discovered local project. Exclude broad registered roots such as Desktop unless the user explicitly names the exact root and confirms its whole contents. Use the single-project path only when the user explicitly names or narrows to one project. The Git steps below are not sufficient for either complete-backup scope. For a device switch, check whether media, ignored files, uncommitted work, exact path inventory, and long-path support also need preservation before declaring any project ready on another computer.

## Review First

Start by inspecting changes:

```powershell
git status --short --branch
git diff --stat
git diff
git diff --staged
```

Use concise summaries for the user. Do not dump long diffs unless requested.

Run the secret scan helper when available:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\secret-scan.ps1 -Path <project-root>
```

If high-confidence secrets or risky local files appear, stop before commit.

## Context Updates

Update `AGENTS.md` only for durable project facts: architecture, commands, constraints, cross-device workflow, or repository structure.

Use `docs/project-state.md` for temporary but useful handoff state: completed work, in-progress tasks, known issues, and next suggested tasks. Update it at meaningful milestones, not after every small edit.

## Commit

Do not automatically `git add .` without reviewing what changed. Stage the files that belong to the current logical unit of work.

Generate a specific commit message from the diff. Prefer conventional prefixes when natural:

- `feat: ...`
- `fix: ...`
- `refactor: ...`
- `docs: ...`
- `test: ...`
- `chore: ...`

Then commit and push:

```powershell
git add <reviewed-files>
git commit -m "<meaningful message>"
git push
```

If push is rejected because the remote has new commits, do not force push. Fetch, inspect ahead/behind, and follow `references/safety-handling.md`.

When push succeeds, report that the selected Git commits reached the remote and identify the established device label, local date/time and timezone, branch, and full commit hash. Do not call the entire project backed up or ready on another device unless the agreed non-Git files have also been preserved and verified.
