## Cross-Device Workflow

This project is developed across multiple Windows machines. GitHub shares version-controlled commits; complete cloud snapshots preserve the agreed whole-project state. A Git push alone is not a complete backup.

For an unqualified “上传云端”, “开始上传”, or “开始上传 skill”, enumerate and migrate every eligible registered local project as an independent complete snapshot; do not select only the current project or place unrelated projects inside it. Drive roots, the user-profile root, Desktop, Documents, and Downloads are broad containers and require explicit exact-root authorization instead of automatic inclusion. A single-project upload requires an explicit project name/path or “只上传当前项目”. For each complete backup, include media, ignored/uncommitted files, dependencies, caches, existing backups, hidden files, empty directories, and `.git`; do not apply `.gitignore` as a backup filter. Preserve source bytes, exact relative filenames, directory names, nesting, and layout. Preflight long-path support and fail rather than rename, flatten, or omit paths. Identify protected/sensitive or inaccessible entries explicitly and resolve them before claiming completeness. Configure a suitable private destination with the user when none exists; do not require them to research technical addresses.

Before starting significant work:

1. Check `git status`.
2. Fetch remote changes.
3. Only when the working tree is clean and fast-forward is possible, apply the fetched, checked upstream commit using `git merge --ff-only --no-overwrite-ignore --no-autostash <checked-commit>` or the Skill's guarded helper. Stop on failure; preserve ignored local files instead of removing them to make the update succeed.

Before switching devices:

1. Review local changes.
2. Check for secrets and machine-local files.
3. Commit a logical unit of work.
4. Push to the remote repository.

If the session calls for a complete backup, also upload and verify the complete snapshot against its manifest and check restoration. Report Git sync and full-backup status separately.

Do not develop inside a live file-sync folder such as OneDrive or Dropbox. A stable backup archive may be stored there without live synchronization of the working `.git` directory.
