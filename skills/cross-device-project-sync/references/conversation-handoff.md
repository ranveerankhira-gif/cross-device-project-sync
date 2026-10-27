# Detailed Conversation Handoff

Use this mode only when the user asks to carry the selected Codex task's context to another device or explicitly includes chat history in the backup. It supplements a verified project snapshot; it is not a replacement for project files and it does not import an original Codex UI conversation.

For an explicitly authorized whole-device migration containing multiple tasks per project, read `incremental-device-sync.md` and use one independent verified conversation pack per project root. The exact transcript requirements remain unchanged; project bytes must not be duplicated once per task.

## Required Outcome

The receiving Codex must be able to reconstruct detailed prior work without relying on a short summary. Preserve these complementary artifacts:

1. A verbatim chronological record of every available user and assistant message present at the recorded capture cutoff, including assistant progress updates and final answers already written by then. Embedded conversation images are decoded into hashed attachment files. An active task cannot archive its own future messages.
2. A detailed semantic handoff that explains chronology, decisions and rationale, concrete work, files and versions, failed or rejected approaches, corrections, current state, unresolved work, user preferences, and verification evidence.
3. A verified `RECEIVER_START.md` that tells the newly created receiving task exactly which materials to read before it changes the project.

Do not load the entire transcript into the receiving model's initial context when it is large. Read `DETAILED_HANDOFF.md` and `CONVERSATION_INDEX.json` first, then retrieve exact messages from `FULL_TRANSCRIPT.jsonl` when details need confirmation. Complete storage plus selective reading preserves more detail than forcing a long transcript through one context window.

## What Is And Is Not Exported

Export the selected Codex task identified by `CODEX_THREAD_ID`, `CODEX_SESSION_ID`, or an independently confirmed thread ID, together with the previously received history explicitly associated with this project. Do not scan or include unrelated tasks. The exporter reads the matching per-task rollout log under `<CODEX_HOME>/sessions` and requires a stable file during capture.

The verbatim transcript includes user and assistant message records through its recorded cutoff. It intentionally excludes runtime-injected environment/plugin blocks, system/developer instructions, hidden reasoning, tool-call arguments, and raw tool outputs. The detailed handoff must preserve important tool actions and observable results in ordinary language, including affected paths, versions, hashes, errors, and test outcomes. Transfer completion after the cutoff is established independently from the immutable Release, verified restore result, and durable receiving-task receipt; never imply that future messages were already in the snapshot. Do not copy the entire Codex database or another task's records.

If the selected task log is missing, ambiguous, changing continuously, contains an unexportable image reference, or cannot establish its thread identity, stop. Do not label a reconstruction from memory as a complete transcript. The final report must state the cutoff timestamp and excluded record categories.

## Prepare And Finalize

First create and fully verify the ordinary project backup with `prepare-full-backup.ps1`. Keep conversation working files outside both the source project and `prepared-release-assets`.

Prepare the exact task transcript:

```powershell
python -X utf8 scripts/export-conversation-handoff.py prepare `
  --prepared-backup '<prepared-release-assets>' `
  --project-root '<actual-working-project-directory>' `
  --work-dir '<new-conversation-work-directory>' `
  --thread-id '<confirmed-thread-id>' `
  --thread-title '<task-title>' `
  --device-label '<confirmed-current-device-label>'
```

The required project root automatically discovers its sibling `_codex-conversation-handoff`. The exporter verifies and copies that entire finalized directory, including all ancestors, beneath `history/<metadata-sha256>` and records it in `BUNDLE_METADATA.json` `inherited_handoffs`. Original messages, task IDs, sequence values, timestamps and attachments remain byte-identical. Repeated identical inputs are deduplicated. The final ZIP inventory binds every inherited file, and the installed verifier validates every generation during upload and restore.

If the project was moved, use repeated `--inherit-handoff '<retained-directory>'` arguments for the history directories identified in the receiving task's first prompt. Check those references before export; a missing or damaged referenced history directory must stop the upload. Never replace missing original history with a reconstruction or silently export only the current task. History not present in the original old backup cannot be recovered by upgrading this skill.

Read the generated `FULL_TRANSCRIPT.jsonl` and `CONVERSATION_INDEX.json`, plus all recursively inherited handoffs and relevant original records. Replace every guidance comment in `DETAILED_HANDOFF.md` with detailed, evidence-based content across the whole history. Do not compress requirement reversals, corrections, user rejections, failures, file identities, or unresolved ambiguity into vague prose. Cite the relative transcript path and sequence so different tasks' message numbers cannot be confused.

Finalize only after every required handoff section is substantive:

```powershell
python -X utf8 scripts/export-conversation-handoff.py finalize `
  --prepared-backup '<prepared-release-assets>' `
  --work-dir '<conversation-work-directory>'

python -X utf8 scripts/verify-conversation-handoff.py `
  '<prepared-release-assets>' `
  --expected-project-manifest-sha256 '<BACKUP_MANIFEST.json SHA-256>'
```

The result is `CONVERSATION_HANDOFF.zip` plus `CONVERSATION_HANDOFF_MANIFEST.json`. Format v3 records both newest-task counts and verified aggregate counts across every retained task generation, so a report cannot quietly describe only the latest leg of an A→B→A handoff. It records project-source and conversation-source devices separately because a device may continue a conversation over another device's restored snapshot. The upload helper accepts both files or neither, verifies them with the installed helper, and binds the conversation-manifest SHA-256 into `RELEASE_BINDING.json`. Never add only one file manually or alter a finalized sidecar; create a new backup run instead.

The exporter performs a limited credential-pattern check over textual dialogue and refuses likely API tokens, GitHub tokens, bearer credentials, and private keys. This is not proof that screenshots or natural-language secrets are safe. Review sensitive conversations before uploading even to a private repository. Do not silently remove a detected secret and still call the transcript verbatim.

## Restore And Continue

Download every Release asset and use the locally installed current restore helper. When the Release contains a bound conversation sidecar, the helper verifies and extracts it to a new `_codex-conversation-handoff` sibling inside the restore destination after the project tree itself passes full verification. It never overlays another device's task data.

Pass `-OpenInCodex` only when the user asked to open the restored project. The helper launches `codex app <restored-project-root>` after all locks and verification complete. This registers or opens the restored workspace, but it does not create the receiving task, recreate the original task ID, or import old chat bubbles. It also writes a hashed `_codex-receiving-task-plan.json` beside the project and handoff directories. That v2 plan binds the exact paths, Release, hashes, lineage counts, bootstrap kind, deterministic operation key, receipt path and initial prompt for the app-level step. Its JSON result deliberately reports `codex_receiving_task_created: false` until Codex performs that step.

For the receiving task, use an initial prompt that names the absolute `receiver_bootstrap_path`, handoff directory, Release tag, and project-manifest SHA-256. Read `receiver_bootstrap_kind`: a v2 handoff uses `RECEIVER_START.md`; a legacy v1 handoff starts directly with `DETAILED_HANDOFF.md` and must not be told that `RECEIVER_START.md` exists. In either case use this order:

1. Read `DETAILED_HANDOFF.md` completely.
2. Read `BUNDLE_METADATA.json` and `CONVERSATION_INDEX.json` to establish source device, task identity, cutoff, counts, and available exact history.
3. Consult relevant records in `FULL_TRANSCRIPT.jsonl` before making a claim about prior wording, decisions, or rejected options.
4. Inspect the restored project's current files and Git state read-only, then compare them with the handoff before changing anything.
5. State the inherited snapshot version and conversation cutoff. Do not imply knowledge of work after either cutoff.

The handoff directory is intentionally outside the restored project so it does not alter the verified snapshot or create untracked project files. The receiving task must test that every required handoff file is readable. If its filesystem scope excludes the sibling handoff directory, request read-only permission for that exact directory with the available permission tool. Do not request write access. If permission is unavailable or denied, stop and report that automatic continuation is incomplete.

Before any project modification, the receiving task must return an **Inherited Context Report** containing:

- Actual byte sizes and SHA-256 values for `DETAILED_HANDOFF.md` and `FULL_TRANSCRIPT.jsonl`.
- Actual byte sizes and SHA-256 values for every retained attachment, plus a substantive inspection observation tied to its source transcript and message sequence.
- A substantive chronology, decisions and rationale, corrections or rejected approaches, completed work, current state, unresolved actions, and relevant user constraints.
- Exact transcript `sequence` references supporting material decisions, corrections, and rejections. If the transcript has at least three messages, use at least three distinct sequences and both roles when both are present.
- Confirmation that project inspection was read-only during the context-loading turn.

Apply the evidence requirement separately to every inherited transcript, recursively following `inherited_handoffs`. The receiver returns a JSON report in its response, without writing into the verified trees:

```json
{
  "thread_id": "original-task-id-from-current-bundle",
  "receiving_thread_id": "new-task-id",
  "release_tag": "verified-release-tag",
  "project_manifest_sha256": "verified-project-manifest-sha256",
  "restored_project_root": "absolute-restored-project-path",
  "conversation_cutoff": "verified-cutoff",
  "conversation_source_device": "verified-source-device",
  "lineage_task_count": 3,
  "lineage_message_count": 120,
  "lineage_attachment_count": 4,
  "read_only": true,
  "chronology": "Detailed events with source references",
  "decisions_and_rationale": "Decisions, reasons and later changes",
  "corrections_and_rejections": "What was corrected or rejected and why",
  "current_state": "Current verified state and uncertainties",
  "unresolved_work": "Remaining actions",
  "user_constraints": "Applicable user requirements",
  "files": [{"path": "FULL_TRANSCRIPT.jsonl", "size": 123, "sha256": "actual-sha256"}],
  "attachments": [{"path": "attachments/message-000001-part-02.png", "transcript": "FULL_TRANSCRIPT.jsonl", "sequence": 1, "inspection_status": "inspected", "observation": "The screenshot visibly shows the relevant project state and error text."}],
  "citations": [{"transcript": "FULL_TRANSCRIPT.jsonl", "sequence": 1, "quote": "exact source excerpt", "claim": "the conclusion supported by this excerpt"}]
}
```

Expand `files` to every current/inherited transcript, detailed handoff, and attachment. Expand `attachments` to exactly every retained attachment, using full inherited paths when applicable. Inspect the actual file; filenames, hashes and surrounding text do not prove visual or media content. For citations into old history, use the full relative path such as `history/<id>/FULL_TRANSCRIPT.jsonl`. For each transcript with text, cite at least three distinct text messages (or all available when fewer), covering both roles when present. For image-only evidence disclose the visual inspection and do not fabricate text quotes. Explicitly state absent decisions or corrections instead of inventing them to fill a section.

The controlling task stores this report outside the verified directories and runs:

```powershell
python -X utf8 scripts/verify-handoff-evidence.py '<handoff-directory>' '<report.json>' `
  --expected-release-tag '<release-tag>' `
  --expected-project-manifest-sha256 '<sha256>' `
  --expected-project-root '<restored-project-root>' `
  --expected-receiving-thread-id '<new-task-id>'
```

This verifies identity, all task/message/attachment lineage totals, read-only confirmation, byte evidence, complete attachment-inspection declarations and meaningful exact-quote matching, but returns `semantic_review_required: true` and `continuation_complete: false`. Next independently compare each claim with its original record and later corrections, inspect every attachment observation against the actual file, verify current state and source identity, and only then approve understanding. A matching hash or an existing sequence number is not a substitute for that semantic review. Include this contract in the initial prompt even when an older package's startup file does not mention it.

If the user explicitly requested automatic continuation, completion requires the app-level sequence in `SKILL.md`: inspect the durable receipt, find the exact project by restored path, recover any existing task by exact project association and operation key before creating one, wait for its context report, verify its project association and inherited identity, atomically record the completed receipt, then navigate to it. Do not create a projectless task. If task creation tools are unavailable, open the workspace and report the single remaining UI action. A new receiving task is expected on the first run; later runs must resume it rather than duplicate it. Do not claim the original sidebar conversation was imported.

After semantic approval, create the receipt with the installed helper and both explicit approval flags:

```powershell
python -X utf8 scripts/receiver-task-state.py complete `
  --plan '<_codex-receiving-task-plan.json>' `
  --evidence-report '<report.json>' `
  --receiving-thread-id '<new-task-id>' `
  --project-id '<exact-local-project-id>' `
  --semantic-review-approved `
  --exact-project-association-verified
```

On every retry, run `receiver-task-state.py inspect --plan '<plan>'` first. A valid `verified-complete` receipt means the task must be revalidated and opened, not recreated. If no receipt exists, inspect existing tasks in the exact project for the plan's operation key before calling `create_thread`.
