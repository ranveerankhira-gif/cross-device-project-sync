# First-Time Cloud Configuration

Help the user establish a usable private destination. Missing configuration is part of the task, not a reason to ask the user to research repository URLs.

## Fixed Complete-Backup Destination

Complete project and conversation backup Releases use the explicitly configured private repository. Confirm that exact repository exists, is private, and is accessible before transfer. Do not discover or create another repository during ordinary operation.

A different historical repository is read-only for this workflow. Use it only when the user explicitly supplies that repository and requests a named historical Release.

This fixed destination does not apply to ordinary Git development repositories. Git sync continues to use the current project's configured remotes.

## Discover Before Asking

Inspect the project root, applicable instructions, established backup settings, Git status/remotes if Git exists, available connectors, and installed transfer tools. Use supported account/status interfaces without dumping tokens or reading browser authentication files. A linked GitHub connector does not prove that the local Git/CLI session is authenticated or has write access.

Reuse the fixed complete-backup destination above when the requested scope is a complete snapshot. For Git development sync or an explicit destination change, reuse a confirmed account and destination when it matches the project, privacy, and requested scope. Do not assume a similarly named repository belongs to this project. Do not expose secrets embedded in a remote URL when reporting it.

If no destination exists:

- For a **complete backup**, measure the full inventory first and find an available private storage route that supports its size, all entry types, and restoration. Do not insist on GitHub if it would exclude project contents. Read `full-cloud-backup.md`.
- For **Git sync**, prefer a private GitHub repository. Check for an existing matching repository before proposing a new one. Preserve existing branches, remote configuration, history, and local changes.

Use plain language: “你的项目还没有网上的私人存放位置。我会帮你配置；需要你登录或授权时再请你操作。” If choosing a service is unavoidable, explain the concrete choice in one short question. Do not ask a novice to provide a token, bucket endpoint, or repository URL as the first step.

## Authentication And Authorization

Do all permitted inspection and preparation first. Before the first GitHub operation in a run, call `gh auth status` once. When it reports a valid login, reuse the current credentials and do not call `gh auth logout` or start another login flow. Only an explicit missing/invalid status permits one official interactive login attempt; after it succeeds, do not repeat authentication during the same run. Never ask for passwords or tokens in chat. A missing CLI is a setup issue: inspect available alternatives, or install it through the appropriate permission flow if needed. Do not assume a plugin can create repositories or upload local files without checking its actual tools.

An explicit instruction to configure the destination authorizes routine configuration within the chosen scope. Do not re-ask permission already given. If creating a new cloud resource needs a decision not established by the session, present the proposed private destination and scope once for approval. Do not purchase storage, subscribe to a paid plan, publish content, or silently change provider/accounts. Honor actual tool/network/filesystem permission gates.

When blocked, state the exact missing action and leave the concrete prepared result ready to resume. Do not say “configured” merely because an account can be viewed.

## Git-Specific Setup

Use this section only for Git development sync, not as a substitute for full backup.

Check Git identity, status, branch, and remotes. Do not overwrite global identity or rename a branch by default. If no repository exists and Git sync is intended, initialize it using the applicable branch convention. Review changes and secrets before staging explicitly selected files; do not blindly `git add .`.

`.gitignore` can exclude Git-inappropriate files for the Git workflow, but must never be used to narrow an agreed complete backup. The secret-review helper covers only its documented Git candidates and is not a scan of every historical object.

If a suitable private repository is already authorized and `gh` is available and authenticated, create it without bundling an unreviewed push, for example:

```powershell
gh repo create <confirmed-name> --private --source . --remote origin
```

Use another remote name if `origin` already exists; do not replace it without an established reason. Confirm privacy, ownership, and access. Commit reviewed changes only as appropriate, then push the current intended branch and verify the remote commit. If rejected, inspect divergence; never force-push by default.

## Keep Configuration Reusable

Store confirmed, nonsecret project settings only when useful: backup mode/scope, provider, account identifier, destination folder/object prefix, privacy, transfer method, and manifest/restore location. Any user-approved exceptions must be explicit; default backup scope is the entire project, independent of Git filters. Do not store credentials or encryption keys. Preserve existing settings and do not invent a provider-specific schema.

Report separately whether the account is connected, the destination is configured, bytes are uploaded, and the backup/remote commit is verified. These are different milestones.
