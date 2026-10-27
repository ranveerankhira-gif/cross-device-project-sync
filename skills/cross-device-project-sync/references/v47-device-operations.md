# v47 Device Operations

Use this reference for every current whole-device upload or download. Read
`v46-device-operations.md` in full first: v47 preserves its readiness report,
machine lock, selection receipt, rollback transaction, claimed receive queue,
rollout-backed app proof, divergence resolution, ledger commit, and two-phase
final acceptance contract. Wherever that historical reference supplies
`20261016-v46`, use the current exact Skill version `20261017-v47`.

## Upload Audit Change

Before packaging or contacting GitHub, run `scripts/audit-catalog-secrets.py`
against the exact verified catalog. Accept only a v2 report bound to the same
`catalog_sha256`.

- `blocking_findings` and `limits` are hard stops. `findings` is the same
  blocking list retained for compatibility.
- `nonblocking_test_findings` are retained evidence, not omitted files. They
  may proceed only because the path proves an explicit secret-scanner fixture,
  a third-party package test corpus, or a retained security/Skill audit corpus.
- Recognized executables, libraries, bytecode, archives, media, and other
  binary payloads are not searched with text regexes. The report records their
  count and size. A protected name such as `Cookies`, `Login Data`, `Web Data`,
  `auth.json`, `.env`, or a key/keystore filename is still evaluated and can
  block even when its payload is binary.
- A private-key content finding requires a complete PEM/OpenSSH private-key
  block. Parser constants, documentation, and compiled copies that merely
  contain `BEGIN ... PRIVATE KEY` are not credentials.
- A generic `tests` directory alone does not prove safety. Real-looking
  credentials outside the narrow fixture classes continue to block, as do
  unreadable files and incomplete Git-history scans.

The uploader hashes the audit policy script into
`SECRET_AUDIT_BINDING.json`. Any Skill change invalidates an older passed audit
and forces a new audit against the same catalog before upload can resume.

## Current Readiness Version

For direct readiness verification use:

```text
--installed-skill-root <public Skill> --expected-skill-version 20261017-v47
```

All remaining operator commands, recovery rules, and receiver acceptance gates
are unchanged from v46.
