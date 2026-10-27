#!/usr/bin/env python3
"""Build the stable public catalog for the cross-device-project-sync repository."""

from __future__ import annotations

import json
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
NAMES = ["cross-device-project-sync"]
PURPOSE = {
    "cross-device-project-sync": "在 Windows 设备间可验证地备份、恢复完整项目与允许迁移的 Codex 对话，并处理明确要求的 Git 同步",
}
WRITES = {"cross-device-project-sync"}
CREDENTIALS = {"cross-device-project-sync"}
COST = {"cross-device-project-sync"}
DEPENDENCIES = {
    "cross-device-project-sync": [
        "Windows",
        "Python 3.10+",
        "PowerShell",
        "Git and GitHub CLI for GitHub-backed transfer modes",
        "User-configured private repository and external machine/instance registry for complete backups",
    ],
}


def display_name(name: str) -> str:
    text = (SKILLS / name / "agents" / "openai.yaml").read_text(encoding="utf-8")
    match = re.search(r'^\s*display_name:\s*["\']?(.+?)["\']?\s*$', text, re.M)
    return match.group(1) if match else name


def main() -> int:
    provenance = json.loads((ROOT / "provenance.json").read_text(encoding="utf-8"))
    approved = {row["name"] for row in provenance.get("skills", [])}
    planned = set(NAMES)
    directories = {path.name for path in SKILLS.iterdir() if path.is_dir()}
    if approved != planned or directories != approved:
        raise ValueError(
            "publication set differs from provenance approval: "
            f"unplanned={sorted(directories - approved)}, "
            f"missing={sorted(approved - directories)}, "
            f"metadata_gap={sorted(approved ^ planned)}"
        )
    rows = []
    for name in NAMES:
        rows.append({
            "name": name,
            "display_name_zh": display_name(name),
            "purpose_zh": PURPOSE[name],
            "release": "v0.1.0",
            "platforms": ["windows"],
            "risk_level": "high",
            "permissions": {
                "network": True,
                "filesystem_read": True,
                "filesystem_write": name in WRITES,
                "credentials": name in CREDENTIALS,
                "external_state": True,
                "cost_or_quota": name in COST,
            },
            "dependencies": DEPENDENCIES[name],
            "invocation_policy": "explicit-only",
            "license": "MIT",
            "version": "0.1.0",
            "archive_sha256": None,
        })
    payload = {
        "schema_version": 1,
        "repository": "ranveerankhira-gif/cross-device-project-sync",
        "skills": rows,
    }
    target = ROOT / "catalog.json"
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {target} with {len(rows)} skills")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

