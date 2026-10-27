# v48 Device Operations

Use this reference for every current whole-device upload or download. Read
`v47-device-operations.md` in full first: v48 preserves its readiness report,
machine lock, selection receipt, secret audit, rollback transaction, claimed
receive queue, rollout-backed app proof, divergence resolution, ledger commit,
and two-phase final acceptance contract. Wherever that historical reference
supplies `20261017-v47`, use the current exact Skill version `20261019-v48`.

## Readable Project Destination Names

For a new project installation, use the verified project Release manifest's
`source_name` as the receiving directory name after Windows-safe sanitization.
The readable name is presentation only; identity remains the exact source
computer plus source project ID, and content verification remains hash-based.

- Prefer `<source_name>` when it is free.
- On a name collision, use `<source_name>（来自<source_device>）`.
- If that also collides, append a stable eight-character source-project
  identity hash. A final content hash or random suffix may be added only to
  avoid overwriting another existing path.
- Never adopt, overlay, or merge an existing directory merely because its name
  matches. Distinct source identities always retain distinct ledger entries,
  restore roots, and Codex project mappings.
- Deduplicate bytes only for source registrations that resolve to the same
  normalized real path during source discovery. Equal display names are not a
  deduplication key.
- Register only `registered-project` roots as Codex projects. A restored
  `projectless-root` may have a readable directory but remains Projectless;
  `projectless-conversation-group` has no invented project directory.
- Keep an already-ledgered legacy hash-named destination in place during an
  unchanged reuse or safe replacement. Renaming it behind Codex's registered
  path would break its association; migrate and re-register such an existing
  path only as an explicit separate operation.

## Current Readiness Version

For direct readiness verification use:

```text
--installed-skill-root <public Skill> --expected-skill-version 20261019-v48
```

All remaining operator commands, recovery rules, and receiver acceptance gates
are inherited unchanged from v47.
