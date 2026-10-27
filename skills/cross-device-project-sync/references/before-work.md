# Before Starting Work

Use this when the user is continuing the project, returned from another device, or is about to start meaningful edits.

After an update is actually applied, report the established device label, local date/time and timezone, branch, resulting full commit hash, and whether the guarded verification passed.

## Preferred Helper

From the skill folder, run:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\sync-before-work.ps1 -Path <project-root>
```

If the helper is not available, run the equivalent checks manually. The helper serializes its own update runs, but external Git commands do not honor that helper lock; never run it concurrently with branch switching, checkout, commit, merge, reset, or another Git-writing tool.

## Manual Flow

1. Inspect the repository:
   `git status --short --branch`
   `git branch --show-current`
   Use the redacted `sync-status.ps1` output for remote addresses; do not print raw credential-bearing URLs.
2. Fetch remote state:
   `git fetch --prune`
3. Inspect ahead/behind:
   `git status --short --branch`

Outcomes:

- Clean working tree and remote only ahead: resolve the fetched upstream to a commit ID (`git rev-parse --verify '@{u}^{commit}'`), then use `git merge --ff-only --no-overwrite-ignore --no-autostash <checked-commit>`. Check the exit code before continuing. This protects ignored files that normal Git status does not show; plain `git pull --ff-only` does not provide that protection. Use the helper when possible.
- Clean working tree and up to date: continue.
- Local uncommitted changes: do not pull. Inspect `git diff` and `git diff --staged`; decide whether to commit, stash after review, or ask the user.
- Local and remote diverged: do not run plain `git pull`. Inspect `git log --oneline --graph --decorate --all --max-count=40` and choose a safe merge/rebase strategy only after explaining it.
- No upstream configured: inspect remotes and either set upstream or ask for the intended branch.

If the guarded fast-forward fails (helper exit 26), preserve local files and inspect the conflict. Never work around it by deleting ignored media, enabling overwrite, auto-stashing, or resetting the tree. A `-NoPull` result only compares commits; it leaves filesystem collision checks pending.

Keep the user-facing report short:

```text
Cross-device sync status
Branch: main
Remote: origin/main
Working tree: clean
Local vs remote: up to date
Safe to continue working.
```
