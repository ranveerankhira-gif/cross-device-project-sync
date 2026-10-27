# v45 Device Aggregation And Caller-Only Receiving

This is the operator workflow for the multi-instance v2 device index. Editing
the Skill does not authorize a real upload, download, task creation, or desktop
operation. v45 keeps the v42 data model and adds independently observed live
Codex task evidence, queue-bound ledger results, transactional lock release,
and mandatory lower-level readiness verification.

## Prove Device Readiness

Run `scripts/device-readiness.py --registry <machine-state/instances.json>
--installed-skill-root <public Skill> --expected-skill-version 20261016-v45
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
4. A Legacy index that returns to a separate restore workflow releases the
   newly acquired v45 lock before returning and reports deterministic release
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
filtered catalog, complete receiver state, exact ledger plan, and receive queue
in that order. All artifacts are caller-local; do not modify the other
allow-listed accounts.

## Claim, Dispatch, Verify, Resume

Claim no more than eight operations before app writes. For every
`register-new`, `update-existing`, or `apply-retained-update`, verify and store a
`last_observation` containing the calling `CODEX_HOME`, real task ID, exact
receipt marker, target kind/project ID, resulting active/archive disposition,
and status. This observation drives queue recovery but is not final evidence.
A `clientThreadId`, planned status, or synthetic acknowledgement is not
evidence that the task exists. No-write decisions must have their exact
deterministic status and no app observation.

After the queue is complete:

1. Run `receiver-app-proof.py capture --queue <queue> --output <new proof>`.
   It reads the calling account's live `state_5.sqlite` in a read-only
   transaction and verifies every app-write task ID, receipt marker, exact
   project/Projectless binding, and active/archive disposition.
2. Generate results with `multi-instance-sync.py queue-ledger-results`. The v2
   result binds `plan_sha256`, `queue_sha256`, and `results_sha256`.
3. Run `conversation-sync-ledger.py commit` with `--queue`, `--app-proof`, and
   `--receipt`. Commit re-reads live app state under the ledger lock and stops
   before changing the ledger if the proof, Queue, Results, Plan, or prior
   ledger changed.

Never rewrite the ledger directly. A self-consistent Queue observation cannot
substitute for a task found in the current calling account's live database.

## Final Acceptance

Run `scripts/receive-final-acceptance.py` with the exact registry, selected
device index, byte-restore report, current project map, filtered catalog, plan,
queue, queue-derived results, live app proof, caller ledger, commit receipt,
and a new output path inside the calling `CODEX_HOME`.

The v45 gate re-reads and verifies:

- the canonical public Skill, readiness proof, every local device label, and
  the held download lock or its deterministic release archive;
- the complete selection catalog, Release metadata, content binding, device
  index, canonical repository, Release tag, and required Skill version;
- every restored project directory's current tree fingerprint and every
  extracted conversation-pack manifest/file hash;
- per-group conversation membership, complete catalog/plan/queue cardinality,
  exact task identities and target bindings, deterministic receipt markers,
  current live Codex task rows, queue-bound results, ledger entries, and commit
  receipt;
- that the new acceptance output does not exist and cannot alias the ledger or
  another input.

Before releasing the lock, the gate writes a hash-bound prepared acceptance.
Lock release atomically archives the exact lock under `machine-state`; it is
idempotent for the same operation and token. If the process stops after release
but before the final report, rerunning with unchanged inputs verifies the
prepared record and release archive and completes without inventing another
lock or ledger commit.

Only `status: receive-complete-verified` means “下载完成”. Any failure before
prepared acceptance keeps the held lock. A failure after deterministic release
keeps the prepared record and release archive for exact recovery.

## Report

Keep byte restore, project registration, lazy task registration, live app
proof, ledger commit, context load, and final acceptance distinct. Report
source and destination, migration and Release identity, project/group/task
counts, exclusions, cloud references, failures, release evidence, and final
`acceptance_sha256`. Never report partial success as complete.
