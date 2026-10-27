# v46 Device Aggregation And Caller-Only Receiving

This is the operator workflow for the multi-instance v2 device index. Editing
the Skill does not authorize a real upload, download, task creation, or desktop
operation. v46 keeps the v42 aggregation model and v45 two-phase acceptance,
then closes repeat-update proof, context-load proof, blocked-divergence,
post-release recovery, and commit-path alias gaps.

## Prove Device Readiness

Run `scripts/device-readiness.py --registry <machine-state/instances.json>
--installed-skill-root <public Skill> --expected-skill-version 20261016-v46
--output <machine-state/readiness/report.json>`. Add `--expected-skill-package
<verified package directory>` for exact package binding. Add `--check-cloud`
only when live GitHub authentication and private-repository access should also
be tested.

The report must say `status: ready`. The installed root must be exactly
`%USERPROFILE%\.agents\skills\cross-device-project-sync`. Readiness rejects
same-name account-private or registered-project-local copies, an unlisted
caller, inconsistent device labels, a changed registry, and an occupied lock.
It never reads or copies credentials.

The machine-wide wrapper generates readiness before taking the upload lock.
`run-incremental-upload.ps1` then independently runs `device-readiness.py
--verify-report` and binds the report to the current caller, registry, public
Skill tree, freshness window, migration ID, and exact held upload lock. A v2
upload with a missing, stale, self-edited, or mismatched report must stop.

## Upload

Use `run-multi-instance-upload.ps1 -InstanceRegistry <registry>
-CallerCodexHome <actual CODEX_HOME> -WorkRoot <new migration directory>
-MigrationId <new immutable ID>`. `-PreflightOnly` performs local preparation
without publishing. Do not set `CODEX_HOME` to impersonate another instance.

The wrapper snapshots every allow-listed instance, scans active and archived
user-visible sessions, deduplicates real project roots, treats safe Projectless
directories separately, audits secrets, builds project/conversation objects,
and publishes one immutable index. Credentials, databases, whole Codex homes,
receiver ledgers, and machine state are excluded. Resume only with the same
migration directory and ID after a mandatory content re-scan.

## Choose And Restore

1. Run `device-index-receiver.py discover --output
   <CODEX_HOME>/sync/versions.json`. Show all verified candidates and require
   the user to select one, even if only one exists.
2. Run `receive` with the exact selected tag, selection file, instance registry,
   caller-owned roots, and canonical public Skill root.
3. The receiver obtains a `download` machine lock before network or restore
   work. A controlled failure releases it; a successful byte restore retains
   it through final acceptance; a hard crash leaves it for audited recovery.
4. A legacy index that returns to a separate restore workflow releases the
   newly acquired v46 lock before returning and reports deterministic release
   evidence. It must never leave a successful inspection holding the device.
5. Project replacement is permitted only when the current installed tree still
   equals its recorded baseline. Any local change, divergence, or uncertainty
   restores beside the existing directory. An exact unchanged reuse deletes
   only the redundant verified staging tree inside the caller-owned base.

The byte-restore report remains `restored-verified-not-registered`. It binds
the selection, immutable Release envelope, required Skill, readiness report,
public Skill tree, download-lock token, installed project-tree fingerprints,
and extracted conversation-pack paths. This is not a completed download.

## Register Projects And Build The Queue

Register only restored `registered-project` roots in the calling account and
prove exact path matches. Projectless groups remain Projectless. Build the
filtered catalog, complete receiver state, exact ledger plan, and receive Queue
in that order. All artifacts are caller-local; do not modify the other
allow-listed accounts.

Claim no more than eight app-write operations. Every observation must bind the
calling `CODEX_HOME`, real task ID, exact receipt marker, target kind/project,
and active/archive disposition. A `clientThreadId`, planned status, Queue
boolean, or synthetic acknowledgement is not evidence that a task exists.

## Prove Real App Writes And Context Loads

After the Queue has no pending app writes:

1. Run `receiver-app-proof.py capture --queue <queue> --output <new proof>`.
2. For `register-new`, the live SQLite row must contain the exact receipt marker
   in `first_user_message`.
3. For `update-existing`, `apply-retained-update`, and resolved-divergence
   writes, SQLite must supply the exact task's `rollout_path`. The path must be
   an ordinary, stable file below the calling account's `sessions` or
   `archived_sessions` tree. The proof parser accepts the marker only from a
   role-parsed user message. An assistant echo or raw byte match never counts.
4. `registered-lazy` proves only registration/update. To record
   `context-loaded`, the observation must also carry a caller-local
   `context_evidence_report_path`, its `context_evidence_report_sha256`, and the exact deterministic
   `context_load_marker`. That independent marker must appear in a real user
   message in the same rollout. The proof records the rollout SHA-256 and
   evidence type; final acceptance rechecks both against current live state.
5. Generate results with `multi-instance-sync.py queue-ledger-results`, then run
   `conversation-sync-ledger.py commit` with the exact Queue and app proof.

The commit's plan, ledger, results, Queue, app proof, receipt, and ledger-lock
paths must all be different after path resolution. The receipt must be new; an
existing receipt is accepted only for a byte-identical idempotent retry bound
to the same before/after ledger hashes. Alias rejection happens before any
receipt or ledger write.

## Resolve A Pending/Incoming Divergence

`preserve-pending-source-diverged` intentionally produces
`blocked-divergence`. It cannot be claimed or committed. Do not edit the Queue
or receiver ledger by hand.

After the user explicitly chooses which version to apply, run:

```text
conversation-sync-ledger.py resolve-divergence --plan <blocked-plan> --ledger <caller-ledger> --queue <blocked-queue> --source-key <exact-source-key> --choice incoming|retained-pending --reason <durable-audit-reason> --output <new-caller-local-resolution.json>
```

The command holds the receiver-ledger lock, verifies the exact applied,
pending, and incoming hashes, writes a hash-bound resolution, and marks only
the matching old Queue item `superseded-divergence`. Resolve every blocked item
before replacing the active Queue.

Rebuild the plan with one `--divergence-resolution <resolution.json>` argument
per resolved item, then build a fresh Queue. `incoming` applies the selected
snapshot and preserves pending as history. `retained-pending` applies the
retained handoff and preserves incoming as history. Commit stores the
non-selected hash, path, source, resolution choice, and resolution SHA-256 in
`divergent_histories`; it never concatenates or discards either branch. The
same explicitly non-selected incoming hash is subsequently
`skip-resolved-divergence`, not silently applied on the next run.

## Final Acceptance And Recovery

Run `scripts/receive-final-acceptance.py` with the exact registry, selected
device index, byte-restore report, current project map, filtered catalog, plan,
Queue, Queue-derived results, live app proof, caller ledger, commit receipt,
and a new output path inside the calling `CODEX_HOME`.

The v46 gate re-reads and verifies:

- canonical public Skill, readiness proof, local device labels, and the held
  download lock or its deterministic release archive;
- selection catalog, Release metadata, content binding, index, repository,
  Release tag, and required Skill version;
- restored project fingerprints and conversation-pack files;
- catalog/plan/Queue cardinality, task target and archive state, receipt
  markers, rollout evidence, independent context-load evidence, Queue-derived
  results, divergence receipts, ledger entries, and commit receipt.

Before releasing the lock, the gate writes a hash-bound `.prepared` acceptance.
Lock release atomically archives the exact lock. If final report writing fails
after release, the exception path must not create a `blocked` report at the
final output. Rerun with unchanged inputs: it verifies the prepared record and
release archive, then writes the final report without inventing another lock.
A legacy matching `blocked` output left by v45 may be replaced only when the
matching prepared record exists. Validation failures may write `blocked` only
before prepared acceptance and while the exact download lock is still held.

Only `status: receive-complete-verified` means “下载完成”. Keep byte restore,
project registration, lazy task registration, context loading, live app proof,
divergence resolution, ledger commit, and final acceptance distinct. Report
source/destination, migration and Release identity, project/group/task counts,
exclusions, cloud references, failures, release evidence, and final
`acceptance_sha256`. Never report partial success as complete.
