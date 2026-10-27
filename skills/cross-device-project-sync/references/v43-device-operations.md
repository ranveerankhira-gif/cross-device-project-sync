# v43 Device Aggregation And Caller-Only Receiving

This is the operator workflow for the multi-instance v2 device index. Editing
the Skill does not authorize a real upload, download, task creation, or desktop
operation. v43 retains the v42 data formats and adds two fail-closed gates:
device readiness before either direction and final receive acceptance after the
caller-local ledger commit.

## Prove Device Readiness

Run `scripts/device-readiness.py --registry <machine-state/instances.json>
--installed-skill-root <public Skill> --expected-skill-version 20261015-v43
--output <machine-state/readiness/report.json>`. Add `--expected-skill-package
<verified package directory>` for exact package binding. Add `--check-cloud`
only when live GitHub authentication and private-repository access should also
be tested.

The report must say `status: ready`. It checks the actual process
`CODEX_HOME`, Windows computer name, registry device label, every allow-listed
home and its local device-label file, the shared Skill inventory, absence of
private same-name Skills, and the global machine lock. It never reads or copies
credentials. Two devices may exchange `readiness_sha256` and Skill tree/package
hashes; a report from one device never proves the other device's local state.

## Upload

Use `run-multi-instance-upload.ps1 -InstanceRegistry <registry>
-CallerCodexHome <actual CODEX_HOME> -WorkRoot <new migration directory>
-MigrationId <new immutable ID>`. `-PreflightOnly` performs local preparation
without publishing. Do not set `CODEX_HOME` to impersonate another instance.

The wrapper holds one machine lock, snapshots every allow-listed instance,
scans active and archived user-visible sessions, deduplicates real project
roots, handles safe Projectless directories separately, audits secrets, builds
project/conversation objects, and publishes one immutable index. Credentials,
databases, whole Codex homes, receiver ledgers, and machine state are excluded.
Changing logs are retried until stable; the frozen cutoff defines the snapshot.

Resume an interrupted upload only with the same migration directory and ID. A
mandatory re-scan must match before an old plan can continue. Never mutate a
published index or steal a lock based on PID death; inspect and explicitly
recover an abandoned operation ID with an audit reason.

## Choose And Restore

1. Run `device-index-receiver.py discover --output
   <CODEX_HOME>/sync/versions.json`. Show every verified candidate's source
   device/computer, publication time, tag, project count, and conversation
   count. Ask the user to choose even when only one candidate exists.
2. Run `receive` with the exact selected tag, selection file, registry,
   caller-owned download/restore roots, and public Skill root. Receiver storage
   must remain under the deterministic account-specific receiver root.
3. Each payload is verified through its index-bound historical runtime and
   restored into a fresh stage. An unchanged prior baseline may use the audited
   replacement transaction; modified, divergent, or uncertain content restores
   beside it. Never overlay or auto-delete rollback copies.

The byte-restore report deliberately remains
`restored-verified-not-registered`. That status is not a completed download.

## Register Projects And Build The Queue

Register only `kind: registered-project` roots in the calling account and prove
exact path matches with `list_projects`. Never register `projectless-root` or a
conversation-only group as a fake project. Any desktop registration helper
still requires the user's desktop consent and exact account/window ownership.

Build the filtered task catalog, receiver state, plan, and queue in this order:

```text
build-receiving-task-catalog.py --restore-report <report> --project-map <map> --output <catalog>
multi-instance-sync.py build-receiver-state --registry <registry> --catalog <catalog> --output <state>
conversation-sync-ledger.py plan --catalog <catalog> --receiver-state <state> --ledger <ledger> --output <plan>
multi-instance-sync.py build-receive-queue --registry <registry> --caller-home <actual CODEX_HOME> --catalog <catalog> --plan <plan> --output <queue>
```

All artifacts after byte restore are caller-local. The other allow-listed
accounts must remain unchanged.

## Claim, Dispatch, Verify, Resume

Claim before every app write with `next-receive-batch --queue <queue> --claim`.
At most eight operations are claimed. A preview is not authorization to create.

- `register-new`: create one task with the exact queue title, receipt marker,
  prompt, and project or Projectless target.
- `update-existing` and `apply-retained-update`: update the recorded task; never
  create a replacement.
- Hidden, unchanged, and divergent decisions perform no app write. Preserve
  manual archive/delete state.

Verify the exact receipt marker, current account, project binding, real task ID,
and archive disposition before recording a queue result. A `clientThreadId` is
not a task ID. Ambiguous API outcomes remain `uncertain`; absence from one list
does not prove no creation. Retry only after definitive no-side-effect evidence.

After all claims settle, produce ledger results from the exact queue and commit
with a durable receipt:

```text
multi-instance-sync.py queue-ledger-results --queue <queue> --output <results>
conversation-sync-ledger.py commit --plan <plan> --ledger <ledger> --result <results> --receipt <CODEX_HOME>/sync/ledger-commit-receipt.json --destination-instance-id <calling instance id>
```

## Final Acceptance

Run `scripts/receive-final-acceptance.py` with the registry, exact downloaded
device index, byte-restore report, current project map, filtered catalog, plan,
queue, queue-derived results, caller-local ledger, commit receipt, and a new
caller-local output path.

The gate requires:

- exact current-account ownership and exact project mappings;
- Projectless groups and tasks remaining Projectless;
- no `pending`, `in-flight`, `uncertain`, `failed`, or divergent queue item;
- only verified lazy, context-loaded, skipped, or retained-hidden states;
- one hash chain from index through catalog/plan/queue/results/commit receipt to
  the final ledger;
- exact reconciliation of projects, conversations, source archive states,
  exclusions, and cloud-managed references.

Only `status: receive-complete-verified` may be reported as “下载完成”. A
`blocked` report or the earlier byte-restore status must be described as partial
progress with the exact remaining item.

## Report

Keep byte restore, project registration, task registration, context loading,
ledger commit, and final acceptance as distinct states. Report source and
destination device/account, migration ID, index tag/hash, project/group counts,
active/archive counts, exclusions, cloud references, lazy/context-loaded/hidden
task counts, failures, and final `acceptance_sha256`. Never turn partial success
into a complete result.
