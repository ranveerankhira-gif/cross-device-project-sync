# Conflict, Divergence, And Secret Handling

Use this reference when the repository is dirty, branches diverge, a push is rejected, or the scan finds risky files.

These Git remedies do not alter complete-backup scope. For a full snapshot, preserve uncommitted and ignored work without requiring a commit or stash. Follow `full-cloud-backup.md` for protected paths, encryption, explicit unresolved entries, and byte-for-byte restore verification.

## Uncommitted Local Work

Do not pull immediately. Inspect:

```powershell
git status --short --branch
git diff --stat
git diff
git diff --staged
```

Choose the safest path:

- Commit reviewed local work if it is a coherent change.
- Stash only after reviewing what will be stashed and explaining why.
- Ask the user when files are ambiguous, generated, or risky.

## Diverged History

Never solve divergence with force push or hard reset by default.

Inspect:

```powershell
git fetch --prune
git status --short --branch
git log --oneline --graph --decorate --all --max-count=40
```

Explain whether merge, rebase, or manual conflict resolution is appropriate. Use the repository's existing convention when clear.

## Rejected Push

If `git push` is rejected because the remote has new commits:

1. Run `git fetch --prune`.
2. Inspect ahead/behind and recent graph.
3. If the local branch can fast-forward and the working tree is clean, use the guarded helper or `git merge --ff-only --no-overwrite-ignore --no-autostash <checked-upstream-commit>` after resolving the fetched commit. Do not fall back to plain `git pull --ff-only`: it can overwrite ignored local files.
4. If not, explain options and avoid rewriting remote history without explicit authorization.

## Secrets

Risky names include `.env`, `.env.*`, `*.pem`, `*.key`, `credentials.json`, `service-account*.json`, `token*`, `secret*`, `cookies*`, and `auth*`.

High-confidence content includes private key blocks, GitHub tokens, OpenAI-style API keys, AWS access keys, Slack tokens, and Google API keys.

When secrets are suspected:

- Stop before committing.
- Check whether the file is tracked with `git ls-files <path>`.
- Add local secret files to `.gitignore` when appropriate for Git sync. This is not permission to silently omit them from a complete-backup request; resolve that scope and authorization explicitly before declaring completion.
- If the secret may have been committed or pushed, tell the user to rotate or revoke it.
