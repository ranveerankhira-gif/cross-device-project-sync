# Whole-Device All-Projects Migration

Use this mode for unqualified requests such as “上传云端”, “开始上传”, or “开始上传 skill”, as well as explicit requests to migrate, upload, restore, or synchronize every project on one device. A single-project upload is allowed only when the user explicitly says “只上传当前项目”, identifies one project by exact name/path, or otherwise clearly narrows the scope. This mode coordinates the existing single-project backup and conversation workflows; it does not turn multiple project roots into one archive.

## Scope Boundary

"All projects" means every eligible accessible local project registered by any allow-listed Codex instance on the named source device, deduplicated by normalized real path. It also means every user-visible active, archived, and Projectless conversation in those allow-listed homes. Account-managed ChatGPT projects remain cloud-managed references; they are not local folders. A Projectless working directory is packaged only when narrow, independent, accessible, non-reparse, and non-overlapping; otherwise its conversation is still migrated in a conversation-only group. Test, retired, and unlisted Codex homes and arbitrary disk scans are outside scope. Broad roots are recorded, not packaged.

Before transfer, show a device inventory containing the source computer and device label; every unique local project ID, label, normalized absolute path, Git status, associated Codex task count, estimated bytes, and accessibility state; and every account-managed ChatGPT project ID and label. Display `Scope: all registered local projects` prominently. A device-wide request must never silently degrade to the current working directory. If discovery is incomplete or two project IDs resolve to the same path with conflicting identity, stop before upload.

For v48, an unqualified cloud-upload phrase selects every eligible unique project folder and every transferable user-visible conversation across the complete instance allow-list. Project archives and conversation packs remain separate verified objects. Enumerate source instance, task ID, title, active/archive state, Projectless directory decision, and every exclusion. A request explicitly limited to files excludes conversations.

## Source Device Workflow

For v48 execute `v48-device-operations.md`. The legacy manifest commands in steps 4–7 below apply only to historical device-migration indexes; v48 uses the incremental upload state and multi-instance wrapper instead. Never mix both state machines in one migration.

1. Verify `COMPUTERNAME`, device label, the configured instance registry, and the calling `CODEX_HOME`. The caller must be allow-listed.
2. Acquire the machine upload lock once. Run `multi-instance-sync.py snapshot-discovery` to freeze `.codex-global-state.json`, `sessions`, and `archived_sessions` for every allow-listed home. The helper rejects unlisted homes, deduplicates project roots, keeps every registration and conversation, classifies Projectless roots, and hashes the discovery document. Never copy auth files or SQLite databases.
3. Create a migration ID such as `<device-label>-<yyyyMMdd-HHmmss>`. Keep the migration work directory under the device migration root and outside every project/Codex home. `run-multi-instance-upload.ps1` may create the snapshot, v2 catalog, plan, persistent upload state, exact Skill package, and final index under that directory while holding the same machine lock.
4. Before starting or resuming work, run `device-migration-manifest.py inspect`. Reuse every `completed_release_tag`; process `failed_project_ids` first and then `pending_project_ids`. A matching completed result is idempotent and must not be uploaded again. Keep the same migration ID and state path across retries.
5. Run the normal complete-backup preparation separately for every selected project. Use a separate staging and prepared-assets directory per project. Each new v2 project manifest must report the same `source_device` and actual `source_computer` as the migration state. Never reuse a prepared directory, put one project inside another, or overlay any source folder.
6. Upload each project as a new immutable Release in the canonical repository. Use a unique tag that contains the source device, a stable collision-resistant project slug, and timestamp. After remote verification, create one project-result JSON containing the exact project ID/label/path, source identity, `split-tar-pax-v2` format, Release tag, `BACKUP_MANIFEST.json` SHA-256, UTC snapshot time, and conversation identity. Atomically record it with `device-migration-manifest.py record-success`.
7. If a project fails, run `device-migration-manifest.py record-failure` with its exact project ID and a concise error. Preserve successful immutable Releases and stop before publishing the final index. A later run records success over that failed entry after the same project succeeds; it never discards earlier completed results.
8. Only after every required project Release and conversation pack verifies remotely, build incremental device index v2. It binds the exact instance list, unique project roots, conversation-only groups, lineage branches, source states, exclusions, cloud references, content hashes, and exact Skill Release. Re-download and reverify the index before releasing the machine lock or claiming completion.

The device index is an index, not a backup archive. Do not add project files, credentials, the local device-label file, or `<CODEX_HOME>` data to it.

## Manifest Input And Format

Prepare a discovery input JSON object for `init`:

```json
{
  "migration_id": "A-device-20260924-210000",
  "source_device": "A设备",
  "source_computer": "WORKSTATION-A",
  "created_utc": "2026-09-24T13:00:00Z",
  "inventory": [
    {
      "project_id": "app-project-id",
      "label": "project label",
      "project_kind": "local",
      "source_path": "C:\\absolute\\project",
      "disposition": "pending",
      "reason": null,
      "canonical_project_id": null
    },
    {
      "project_id": "chatgpt-project-id",
      "label": "cloud project",
      "project_kind": "chatgpt",
      "source_path": null,
      "disposition": "cloud-managed",
      "reason": null,
      "canonical_project_id": null
    }
  ]
}
```

For a successfully uploaded local project, the legacy v2 result JSON passed to `record-success` contains `project_id`, `label`, `source_path`, `source_device`, `source_computer`, `project_manifest_format: split-tar-pax-v2`, `release_tag`, `project_manifest_sha256`, `snapshot_created_utc`, and `conversation`. A legacy single-project package may carry one selected conversation. For the default whole-device workflow, use the incremental workflow in `incremental-device-sync.md`: upload each project snapshot once and bind all selected associated tasks into one separately verified conversation pack. Never duplicate a multi-gigabyte project archive once per task.

## Receiving Device Workflow

For v48 use the selection receipt, caller-owned storage, readable collision-safe project naming, rollback transaction, claimed queue, rollout-backed live app proof, explicit divergence resolution, queue-bound ledger commit receipt, and two-phase final acceptance gate in `v48-device-operations.md`. The following steps describe the common verification contract; any single-project-only bindings apply only to registered projects, not Projectless payloads.

1. Select a named index Release. Use `device-index-receiver.py` to require `Content-Kind: device-index`, verify repository privacy and `CONTENT_RELEASE_BINDING.json`, and discover the manifest from the bound JSON assets' `format` values instead of hard-coding one filename. It supports both `DEVICE_MIGRATION_MANIFEST.json` legacy formats and the incremental `INCREMENTAL_DEVICE_INDEX.json` format, and stops on unknown or damaged formats. If the index declares `required_skill_version` and `skill_release`, always download, verify, and invoke that exact hash-bound Skill package from an isolated migration runtime outside every `.agents\skills` or `<CODEX_HOME>\skills` discovery directory, even when the public installed Skill reports the same version string. Hash the public Skill tree before and after and require it to remain unchanged. Never replace or downgrade the shared installed Skill to process a historical index. For a legacy device-migration index, also run `device-migration-manifest.py verify` with the expected source device and computer. A legacy v1 index verifies only its listed Releases and must be reported as `inventory-unproven`; only v2 proves the declared full discovery inventory.
2. Preflight destination space for the sum of all referenced projects plus verification overhead. Use one new empty destination per project under a migration-specific parent directory. Resolve duplicate labels with stable suffixes; never merge by label.
3. For each entry, independently download and verify every asset from the exact referenced Release. Restore with the installed current helper and the exact project-manifest SHA-256. Never restore one project inside another or over an existing project.
4. Modify only the calling Codex account. Register each restored project root there; leave conversation-only groups unregistered. Build the filtered v2 receiving catalog and receiver-ledger plan, then create at most eight unfinished items per queue batch. Project conversations bind to that exact project; Projectless conversations remain Projectless. Use `[源设备·源账号] 原标题`, retain source active/archive state, and distinguish `registered-lazy` from `context-loaded`.
5. Verify account-managed ChatGPT project IDs are visible on the receiving account. Their account synchronization is distinct from local folder restore and must be reported separately.
6. Reconcile the final result against the complete v2 inventory: every project payload is verified-restored and registered; every Projectless conversation has a Projectless receiving task; every exclusion retains its reason; every cloud-managed entry is reported as an account reference. A project conversation landing Projectless, or a Projectless conversation being placed in a fake project, is a failure. Partial success remains partial.

Do not delete earlier single-project Releases. They remain valid snapshots but do not prove that a whole-device migration completed.
