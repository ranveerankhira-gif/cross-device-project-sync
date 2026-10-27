# v42 Device Aggregation And Caller-Only Receiving

This is the operator workflow for the multi-instance v2 device index. The older
single-project receipt workflow does not replace this queue. Editing the Skill
does not authorize a real upload, download, task creation, or desktop operation.

## Upload

Use `run-multi-instance-upload.ps1 -InstanceRegistry <machine-state/instances.json>
-CallerCodexHome <actual CODEX_HOME> -WorkRoot <new migration directory>
-MigrationId <new immutable ID>`. `-PreflightOnly` does local preparation only.
Do not set CODEX_HOME to impersonate another instance. Registry and caller must
match the actual process. The lower v2 uploader also requires the held lock.

The wrapper holds one machine lock, snapshots every allow-listed instance,
applies protected-browser exclusions, scans all unique roots, audits secrets,
builds separate project/conversation objects, and publishes one final index.
Session inventory is checked against a read-only projection of `state_5.sqlite`:
IDs, rollout paths, source kinds, archive flags, titles, and—when present—the
boolean `has_user_event` marker only; no message text is read. Databases,
credentials and complete homes are never copied. Internal sources and orphan
logs are explicit exclusions; unreadable visible tasks block completeness.
Equivalent Windows rollout paths with or without the `\\?\` extended-length
prefix are the same file identity. A database task with `has_user_event: false`
and no rollout is recorded as an empty non-conversation exclusion; a task with
user content and no readable matching rollout still blocks the upload.
Global project assignments and the task inventory are checked again after
capture. Each changing log is retried until stable. The frozen cutoff is the
scope of that migration; later messages belong to the next snapshot.

For an interrupted upload reuse the same migration directory and ID. A mandatory
re-scan checks live project bytes and frozen conversation evidence; changed
content stops the old migration and requires a new ID/directory. Earlier
verified Releases remain reusable. Never mutate an already published index.
`machine-state/lineage-branches.json` retains local branch assignments and is
never uploaded. A branch's distinguishing message prefix, not its receiving
account or thread ID, defines its stable ID.

## Choose And Restore

1. Run `device-index-receiver.py discover --output <CODEX_HOME>/sync/versions.json`.
   It verifies index bindings/assets and content hashes, not just Release notes.
   Show device, computer, publication time, tag, project count and conversation
   count. If `release_inventory_complete` is false, increase the scan limit.
   Always ask the user to select, even when exactly one candidate exists.
2. After the user selects, run `receive --release-tag <selected-tag>
   --selection-file <CODEX_HOME>/sync/versions.json --instance-registry <registry>
   --download-root <caller storage>/<migration>/download
   --restore-root <caller storage>/<migration>/staging
   --installed-skill-root <public Skill>`.
   Caller storage is `<machine-state parent>/receivers/<first 24 hex characters
   of SHA256(canonical JSON of actual absolute CODEX_HOME casefolded)>`. The
   helper reports the required root if a supplied path is outside it. Never
   redirect another account into this location or share its receiver ledger.
3. Every payload restores through the exact hash-bound historical runtime into
   a fresh stage. For v2, `project-receive-transaction.py` verifies it again and
   installs it under caller-owned storage. An unchanged receiver baseline can
   be replaced with an audited rename transaction; local edits/uncertainty get
   a new sibling. Rollback copies are retained, never automatically deleted.
   After an interrupted swap use `multi-instance-sync.py recover-project-update
   --registry <registry> --journal <caller-home journal>`. Do not manually overlay
   either directory. Ambiguous edits stop recovery without deleting data.

## Register Projects And Build The Queue

Register only `kind: registered-project` roots in the calling app account, then
verify exact path matches with `list_projects`. `projectless-root` may have a
directory payload but MUST NOT be registered as a project. Projectless tasks
use `target: {type: projectless}`. Account-managed ChatGPT project records are
references only; never claim they were copied across accounts.

Any desktop project-registration helper remains subject to the user's desktop
consent rule and exact account/window ownership verification. Do not select a
different Codex window simply because its label looks similar.

Build `build-receiving-task-catalog.py --restore-report <report> --project-map
<exact current-account project map> --output <CODEX_HOME>/sync/catalog.json`.
Then use the actual account's app metadata and ledger:

```text
multi-instance-sync.py build-receiver-state --registry <registry> --catalog <catalog> --output <CODEX_HOME>/sync/receiver-state.json
conversation-sync-ledger.py plan --catalog <catalog> --receiver-state <state> --ledger <CODEX_HOME>/cross-device-conversation-sync-ledger.json --output <CODEX_HOME>/sync/plan.json
multi-instance-sync.py build-receive-queue --registry <registry> --caller-home <actual CODEX_HOME> --catalog <catalog> --plan <plan> --output <CODEX_HOME>/sync/queue.json
```

The state builder recognizes native origin tasks when their descendants return
home and preserves manual archive/delete state. Migrated v41 ledger entries
continue matching old v1 catalogs. Queue creation requires exact catalog/plan
hashes and the current ledger snapshot; raw catalogs cannot bypass the ledger.

## Claim, Dispatch, Verify, Resume

Run `next-receive-batch --queue <queue> --claim` BEFORE app writes. It persists
up to eight claims and tokens. Read-only `next-receive-batch` does not claim.
One account may have only one unfinished queue. Do not create from a preview.

- `register-new`: call `create_thread` with the exact queue title and initial
  prompt. For projects use the mapped `projectId` and `environment: {type: local}`;
  for Projectless use `target: {type: projectless}`. Do not invent a fake project.
- `update-existing`/`apply-retained-update`: send the new verified lazy handoff
  pointer to the recorded receiving task; never call `create_thread` instead.
- Skipped/hidden/diverged decisions perform no app write. Pending hidden data
  remains on disk. Do not return a manually archived/deleted task to Recent.

The prompt's `[cross-device-receipt:<hash>]` is a durable identity marker. If a
call returns only a `clientThreadId`, wait for setup, then locate and read the
real task. Never use the client ID as a task ID and never create again because
setup is slow. Verify the marker in the original/sent user message, exact project
or Projectless binding, and actual account. Wait for the lazy acknowledgement.
If `expected_disposition` is archived, archive that task and verify archive
state before recording success; new active source tasks remain in Recent.

Write an observation JSON from verified app results with:
`destination_codex_home`, `status: registered-lazy`, `receiving_thread_id`,
`receipt_marker`, `receiver_disposition`, `target_kind`, and `target_project_id`
(null for Projectless). Then run `record-queue-result --queue <queue>
--operation-key <claimed-key> --claim-token <claimed-token> --observation <file>`.
Do not invent observations from intended outcomes or echo claim tokens in prose.

After a crash, outstanding claims block further dispatch. Reconcile against
complete active/archived task inventories and read exact receipt markers. One
match can be recorded; multiple matches are a conflict. No match is NOT proof
that asynchronous creation failed. Record `uncertain` and wait/reconcile. A
retryable `failed` result requires `definitive_no_side_effect: true` plus actual
`failure_evidence` from a settled rejected API call. There is no timer-based or
PID-based automatic duplicate creation. A genuine unresolved API outcome is a
reported blocker, never a fabricated completion.

Once all app writes settle, run `queue-ledger-results --queue <queue> --output
<CODEX_HOME>/sync/results.json`, then `conversation-sync-ledger.py commit --plan
<plan> --ledger <ledger> --result <results>`. Commit before starting another
queue. Do not call completed `registered-lazy` tasks context-loaded. A later
full-context read may upgrade the same receipt to `context-loaded` with
`full_context_verified: true`, only after reading all transcripts, inherited
history, handoffs and attachments and checking the evidence.

## Report

Distinguish registered projects, Projectless directory payloads, conversation-only
groups, retained branches, deduplicated copies/ancestors, internal/deleted or
unverifiable exclusions, cloud references, lazy/context-loaded tasks, source
archives, preserved local hidden tasks, uncertain calls, and failed operations.
An index upload, byte restore, app registration and context loading are four
different completion states. Report only the states actually verified.
