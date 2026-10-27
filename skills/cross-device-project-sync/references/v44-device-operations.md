# v44 Device Aggregation And Caller-Only Receiving

This is the operator workflow for the multi-instance v2 device index. Editing
the Skill does not authorize a real upload, download, task creation, or desktop
operation. v44 keeps the v42 data model and strengthens the v43 gates so a
self-consistent report cannot substitute for an observed app result or current
restored bytes.

## Prove Device Readiness

Run `scripts/device-readiness.py --registry <machine-state/instances.json>
--installed-skill-root <public Skill> --expected-skill-version 20261015-v44
--output <machine-state/readiness/report.json>`. Add `--expected-skill-package
<verified package directory>` for exact package binding. Add `--check-cloud`
only when live GitHub authentication and private-repository access should also
be tested.

The report must say `status: ready`. The installed root must be exactly
`%USERPROFILE%\.agents\skills\cross-device-project-sync`. Readiness rejects
same-name account-private or registered-project-local copies, an unlisted
caller, inconsistent device labels, a changed registry, and an occupied lock.
It never reads or copies credentials.

## Upload

Use `run-multi-instance-upload.ps1 -InstanceRegistry <registry>
-CallerCodexHome <actual CODEX_HOME> -WorkRoot <new migration directory>
-MigrationId <new immutable ID>`. `-PreflightOnly` performs local preparation
without publishing. The wrapper must obtain a passing v44 readiness report
before taking the upload lock. Do not set `CODEX_HOME` to impersonate another
instance.

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
4. Project replacement is permitted only when the current installed tree still
   equals its recorded baseline. Any local change, divergence, or uncertainty
   restores beside the existing directory.

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
and status. A `clientThreadId`, planned status, or synthetic acknowledgement is
not evidence that the task exists. No-write decisions must have their exact
deterministic status and no app observation.

Generate ledger results only from the completed queue, then commit through
`conversation-sync-ledger.py commit` with its durable receipt. Never rewrite
the ledger directly.

## Final Acceptance

Run `scripts/receive-final-acceptance.py` with the exact registry, selected
device index, byte-restore report, current project map, filtered catalog, plan,
queue, queue-derived results, caller ledger, commit receipt, and a new output
path inside the calling `CODEX_HOME`.

The v44 gate re-reads and verifies:

- the canonical public Skill, readiness proof, every local device label, and
  still-held download lock;
- the complete selection catalog, Release metadata, content binding, device
  index, canonical repository, Release tag, and required Skill version;
- every restored project directory's current tree fingerprint and every
  extracted conversation-pack manifest/file hash;
- per-group conversation membership, complete catalog/plan/queue cardinality,
  exact task identities and target bindings, deterministic receipt markers,
  real app observations, results, ledger entries, and commit receipt;
- that the new acceptance output does not exist and cannot alias the ledger or
  another input.

Only `status: receive-complete-verified` means “下载完成”. Success releases the
download lock. Any failure keeps the lock and reports partial progress so the
same operation can be inspected or explicitly recovered.

## Report

Keep byte restore, project registration, lazy task registration, context load,
ledger commit, and final acceptance distinct. Report source and destination,
migration and Release identity, project/group/task counts, exclusions, cloud
references, failures, and final `acceptance_sha256`. Never report partial
success as complete.
