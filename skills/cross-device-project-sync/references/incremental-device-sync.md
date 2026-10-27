# Incremental Device Synchronization

Use this workflow after the user authorizes a device migration and wants unchanged data to avoid repeat upload. It supplements the complete-backup and conversation workflows; it does not weaken their verification requirements.

## Content Boundaries

For v48 operator commands, readiness, queue recovery, readable collision-safe project naming, rollout-backed live app proof, divergence resolution, ledger commit, final acceptance, and audit-report interpretation, read `v48-device-operations.md`.

Treat project files and conversations as independent immutable objects for each unique project root. A v42 conversation-only Projectless group has no project object or project Release:

1. The project-tree identity binds every included relative path, entry type, file size, restorable timestamp, file SHA-256, and the exact exclusion list.
2. v42 conversation identity binds lineage, branch and the ordered user-visible message fingerprints (including image hashes). It recursively reads ancestors before descendants and ignores verified receiving-registration wrapper turns. Byte-exact transcripts, attachments and inherited trees remain separately hash-bound in the pack manifest. Conversation-set identity also binds source instance and archive state so state/provenance changes cannot reuse a stale pack. v1 retains its historical raw-transcript identity.

Changing a conversation must not force re-upload of unchanged project bytes. Changing project files must not force re-upload of unchanged conversations. A new device index may reference immutable Releases from earlier migrations when they still match.

## Reuse Rules

For v42, `run-multi-instance-upload.ps1` freezes allow-listed session logs and inherited histories, verifies read-only app metadata and holds the device lock. Do not run the legacy single-instance snapshot/prune helpers in addition. For historical single-instance inputs only, freeze every live Codex session log with
`scripts/snapshot-conversation-sources.py` and scan the derived discovery file.
The catalog keeps the original path for provenance and the immutable snapshot
path for packaging. This is mandatory for the currently active conversation,
whose source JSONL continues growing while the sync runs.

Run `scripts/prune-empty-conversation-snapshots.py` before the catalog scan.
It may remove only logs that produce zero user-visible messages after the
required system/reasoning/tool/injected-context filters. Every such shell log
must remain in the catalog's `conversation_exclusions` and the receiver handoff;
all other validation errors are fatal.

Run `scripts/add-protected-browser-exclusions.py` on the validated discovery
before scanning project bytes. It uses the same protected test-profile naming
rule as the complete-backup helper, records each root and reason, and never
descends into the matched directory.

Before packaging or uploading, run `scripts/audit-catalog-secrets.py` against
the finished catalog. Its v2 report separates `blocking_findings` from
`nonblocking_test_findings`; `findings` remains a compatibility alias for the
blocking list. Complete credential patterns and protected authentication files
outside narrowly proven test/audit fixtures, unreadable candidates, and
incomplete Git-history scans block the migration. Recognized binary payloads
are not searched with text regexes, but sensitive filenames still block. A PEM
header mentioned by parser source or documentation is not a private key: a
complete block is required. Third-party package tests, explicit secret-scanner
fixtures, and retained security-audit corpora remain visible in the report but
do not block. The audit is a gate, not proof that unknown binary or secret
formats are impossible; file size must never silently create an unreported
text-scan omission.

Run `scripts/incremental-device-sync.py scan` against the complete approved discovery input. A checkpoint directory may retain progress evidence, but completed checkpoint data is never accepted as current source state: every new invocation re-reads project and conversation bytes. Compare the resulting catalog only with a previously downloaded, canonically encoded, hash-verified content catalog or device index from the canonical private repository.

Before authentication or upload, run `incremental-device-sync.py verify` for the catalog and `verify-plan` for the exact catalog/plan pair. `run-incremental-upload.ps1 -PreflightOnly` performs both checks plus local device-identity verification without contacting GitHub. The normal upload path repeats the same preflight automatically.

Reuse is allowed only when all of these hold:

- The normalized project identity is the same.
- The current content SHA-256 equals the prior catalog value.
- The prior Release is in the explicitly configured canonical repository, is published, and is still private.
- The Release binding, manifest SHA-256, asset count, asset sizes, and remote `sha256:` digests verify.
- The new device index records the reused Release explicitly.

Do not reuse based only on names, timestamps, sizes, local cache entries, a prior successful command, or the existence of a tag. If a prior catalog is absent or cannot be verified, upload a new immutable object.

## Multi-Conversation Packs

For each unique project root, build one `cross-device-project-sync-conversation-pack-v1` using the v42 generator. It may contain conversations from several source Codex instances. Build a separate pack per source instance for conversation-only Projectless groups. Every conversation record carries source instance/state plus stable lineage and branch identity. The pack is independent of project bytes.

The generated archival handoff provides navigation and chronology but does not replace semantic reading. Before actual work, a receiving task must read the exact transcript, inspect attachments and inherited history and review project files if applicable. Projectless tasks remain Projectless. Never claim that old chat bubbles or original task IDs were imported.

If a conversation contains a likely credential or an attachment cannot be exported exactly, block that conversation pack before upload. Do not silently omit the task and still describe the device conversation set as complete.

## Publishing And Resume

Upload changed project snapshots with the normal complete-backup helper. Upload changed conversation packs and the final device index with `scripts/upload-immutable-assets.ps1`. Use new immutable tags; never replace a mismatched asset.

The final incremental device index v2 must contain the current catalog SHA-256 and exact allow-listed instance inventory. Every project payload has a project Release reference; every project or conversation-only group has a conversation-pack Release reference. A conversation-only group must carry `project_release: null`, `source_path: null`, and `has_project_payload: false`. Each reference records uploaded versus reused state and the index binds the exact Skill package.

Build and publish `RECEIVER_READ_FIRST.md` beside the machine-readable index. It must spell out the repository, migration ID, required skill version, every project and conversation-pack Release, uploaded-versus-reused status, content hashes, source paths, conversation cutoffs, explicit exclusions and reasons, cloud-managed references, and categories that are never transferred. The receiving device reads this file before restoration; “not uploaded again because reused” must never be confused with “omitted”.

Publish the index only after every reference verifies remotely. Re-download both index and handoff, compare their SHA-256 values, and revalidate every referenced Release before claiming completion.

Keep the local work directory and catalog until the receiving device has restored all payloads, unpacked every pack, registered every required project or Projectless task, and reported cloud-managed references separately.

## Per-Conversation Receiver Ledger

An unchanged conversation must not be registered again merely because another device index or conversation pack was downloaded. The receiver must maintain `<CODEX_HOME>/cross-device-conversation-sync-ledger.json` and plan against the verified current content catalog with `scripts/conversation-sync-ledger.py`.

1. Read the complete current receiving-thread state first, including every current page and every archived page. Emit a `cross-device-receiver-state-v1` JSON object with the exact `source_computer`, timezone-aware `captured_at_utc`, `complete: true`, `active_inventory_complete: true`, `archived_inventory_complete: true`, exact `thread_count`, and a `threads` array containing `original_thread_id`, `receiving_thread_id`, and `receiver_disposition` (`active`, `archived`, or `deleted`). Include every receiver already known to that source's local ledger; a deleted task remains an explicit `deleted` row. This read captures manual archive/delete actions made after the prior sync. Match migrated tasks through the ledger's exact `receiving_thread_id`; never infer disposition from title or recency. Duplicate receiving IDs, missing known ledger rows, incomplete flags, mismatched source computers, and stale partial files are hard stops.
2. After verified extraction, run `build-receiving-task-catalog.py`. In v42 the stable identity is `lineage_id + branch_id`; source computer/thread remain provenance. The advancement proof is `conversation_sha256`. Projectless rows have `target_kind: projectless` and no project ID. Run the ledger plan only against this exact hash-bound filtered catalog and a complete calling-account receiver-state document.
3. `skip-unchanged` performs no app write. `update-existing` targets the recorded receiving thread. A project-bootstrap uses a stable source-computer plus source-project identity and the same ledger, so an unchanged or manually hidden bootstrap is never recreated. `download-update-keep-hidden` verifies and retains the advanced handoff as a pending hash without creating, unarchiving, pinning, or returning a manually hidden thread to Recent. A repeated hidden sync becomes `keep-hidden-already-downloaded`. When the user explicitly makes that receiver active, `apply-retained-update` applies the pending handoff to the same receiving task before advancing the applied hash. If an unapplied pending hash and a later source hash diverge, `preserve-pending-source-diverged` performs no app write and preserves the pending hash/path until explicit resolution. `retain-local-no-source-delete` never deletes local history.
4. The plan binds the exact filtered-catalog, complete receiver-state, and receiver-ledger SHA-256 values observed before app actions. After the exact action verifies, write a result document whose top-level `plan_sha256` equals the current plan. Every app-write result row must repeat its `source_key`, planned `action`, exact `conversation_sha256`, status, unique receiving task ID when one exists, and a receiver disposition from `active`, `archived`, or `deleted`; stale rows from an older plan must fail. `commit` acquires a receiver-local cross-process lock before it reloads, validates, and atomically replaces the ledger, so a concurrent run must observe the changed hash and stop instead of overwriting another successful update. Run `commit` even when every decision is `skip-unchanged` or `keep-hidden-already-downloaded`, because it persists newly observed manual archive/delete dispositions without changing the app. Failed/interrupted actions are not committed and remain retryable. Never copy the ledger or its lock file to the cloud or into a reusable Skill package.

Legacy v34 ledger entries do not distinguish an applied hash from a hidden downloaded hash. For an archived or deleted legacy entry, retain the current verified handoff as pending once without surfacing the task. Do not mark that ambiguity as applied merely because the source hash matches the old field. A later explicit reactivation uses `apply-retained-update`; an active legacy entry may be adopted as applied after its current receiver state and unchanged source hash are verified.

A changed conversation set may require a new immutable pack because one member advanced. That does not authorize re-registering every unchanged member of the pack: the receiver plans each thread independently.

## Receiving Discovery And Bound-Skill Bootstrap

Do not discover by guessing one index filename or trusting notes alone. Run `device-index-receiver.py discover --output <CODEX_HOME>/sync/versions.json`; it verifies bound index assets and lists source identity, time, tag and counts. Increase the limit if discovery is incomplete. The user must explicitly select a version, even if only one exists. Pass `--selection-file` to `receive`; use the caller-owned storage and full invocation described in `v48-device-operations.md`.

The receiver downloads `CONTENT_RELEASE_BINDING.json` first, requires its repository, Release tag, kind, identity, asset names, sizes, and SHA-256 values to agree with the published Release, and discovers the index from bound JSON `format` values. It accepts both legacy device-migration formats and incremental index v1/v2. A v2 receive restores only entries with project payloads and extracts every conversation-only Projectless group without inventing a project directory. Unknown, ambiguous, noncanonical, or hash-damaged indexes stop safely.

For every incremental index, compare the complete `YYYYMMDD-vN` value from `required_skill_version` with the installed Skill for reporting only; version equality is not content identity. Always download only the exact `skill_release.tag` named by the index and verify its `Content-Kind: skill-package` notes, canonical repository, `CONTENT_RELEASE_BINDING.json`, package-manifest SHA-256, ZIP SHA-256, member inventory, member hashes, safe paths, and packaged `SKILL.md` version. Extract it into a content-addressed migration runtime outside every `.agents\skills` or `<CODEX_HOME>\skills` discovery directory and invoke its verifier directly. Hash the shared installed Skill tree before and after and require byte-for-byte stability. A historically bound package may contain verified generated `__pycache__`, `.pyc`, or `.pyo` members; never execute or extract those cache members. Newly built packages must omit them entirely. Never preserve historical copies beside the shared Skill because Codex may discover them as duplicate Skills.
