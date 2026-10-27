#!/usr/bin/env python3
"""Validate public repository structure, metadata, privacy, and invocation policy."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parents[1]
HIGH = {"cross-device-project-sync"}
EXPLICIT_ONLY = set(HIGH)
REQUIRED_ROOT = {"README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "catalog.json", "provenance.json"}
SECRET_PATTERNS = {
    "GitHub token": re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "OpenAI key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    "real Windows user path": re.compile(r"\b[A-Za-z]:\\Users\\(?!<)[^\\\s\"']+", re.I),
}
RUNTIME_STATE_NAMES = {"state", "__pycache__", ".pytest_cache", "logs", "cache"}
RUNTIME_FILE_NAMES = {"write.lock", ".rename-in-progress.json"}


def fail(message: str, errors: list[str]) -> None:
    errors.append(message)


def main() -> int:
    errors: list[str] = []
    for name in REQUIRED_ROOT:
        if not (ROOT / name).is_file():
            fail(f"missing root file: {name}", errors)
    catalog = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
    provenance = json.loads((ROOT / "provenance.json").read_text(encoding="utf-8"))
    rows = catalog.get("skills") or []
    names = [row.get("name") for row in rows]
    provenance_rows = provenance.get("skills") or []
    provenance_names = [row.get("name") for row in provenance_rows]
    excluded_names = {row.get("name") for row in provenance.get("excluded") or []}
    directories = sorted(path.name for path in (ROOT / "skills").iterdir() if path.is_dir())
    if directories != ["cross-device-project-sync"]:
        fail(f"unexpected Skill publication set: {directories}", errors)
    if not rows or len(set(names)) != len(rows):
        fail("catalog must contain at least one Skill and all names must be unique", errors)
    if sorted(names) != directories:
        fail("catalog and skills directory differ", errors)
    if len(set(provenance_names)) != len(provenance_names) or sorted(provenance_names) != directories:
        fail("provenance approval and skills directory differ", errors)
    if excluded_names & set(directories):
        fail(f"excluded Skill still present: {sorted(excluded_names & set(directories))}", errors)
    allowed_classifications = set((provenance.get("policy") or {}).get("allowed_classifications") or [])
    if allowed_classifications != {"self-authored", "substantially-adapted"}:
        fail("provenance policy must allow only self-authored and substantially-adapted", errors)
    for item in provenance_rows:
        name = item.get("name")
        classification = item.get("classification")
        if classification not in allowed_classifications:
            fail(f"unapproved provenance classification for {name}: {classification}", errors)
        if item.get("license") != "MIT" or not item.get("evidence"):
            fail(f"incomplete provenance evidence or license for {name}", errors)
        root = ROOT / "skills" / str(name)
        if classification == "substantially-adapted":
            upstream = item.get("upstream") or {}
            if not all(upstream.get(field) for field in ("repository", "path", "revision")):
                fail(f"adapted Skill lacks pinned upstream metadata: {name}", errors)
            if not (root / "SOURCE.md").is_file() or not (root / "LICENSE.md").is_file():
                fail(f"adapted Skill lacks SOURCE.md or LICENSE.md: {name}", errors)
        elif item.get("upstream") is not None:
            fail(f"self-authored Skill must not declare an absorbed upstream: {name}", errors)
    required_fields = {"name", "display_name_zh", "purpose_zh", "release", "platforms", "risk_level", "permissions", "dependencies", "invocation_policy", "license", "version", "archive_sha256"}
    for row in rows:
        name = row.get("name")
        if set(row) != required_fields:
            fail(f"catalog fields differ for {name}", errors)
        root = ROOT / "skills" / str(name)
        skill_md = root / "SKILL.md"
        yaml = root / "agents" / "openai.yaml"
        if not skill_md.is_file() or not yaml.is_file():
            fail(f"missing SKILL.md or agents/openai.yaml: {name}", errors)
            continue
        skill_text = skill_md.read_text(encoding="utf-8")
        if not skill_text.startswith("---\n") or not re.search(rf"(?m)^name:\s*[\"']?{re.escape(str(name))}[\"']?\s*$", skill_text):
            fail(f"frontmatter name mismatch: {name}", errors)
        yaml_text = yaml.read_text(encoding="utf-8")
        if name in EXPLICIT_ONLY and "allow_implicit_invocation: false" not in yaml_text:
            fail(f"explicit-only Skill permits implicit invocation: {name}", errors)
        if name in HIGH:
            security = root / "references" / "security-and-permissions.md"
            if not security.is_file() or "security-and-permissions.md" not in skill_text:
                fail(f"missing linked permission disclosure: {name}", errors)
        for path in root.rglob("*"):
            relative_parts = path.relative_to(root).parts
            if any(part.casefold() in RUNTIME_STATE_NAMES for part in relative_parts):
                fail(f"runtime state directory is not allowed: {path.relative_to(ROOT)}", errors)
                continue
            if path.name.casefold() in RUNTIME_FILE_NAMES:
                fail(f"runtime state file is not allowed: {path.relative_to(ROOT)}", errors)
            if path.is_symlink():
                fail(f"symlink is not allowed: {path.relative_to(ROOT)}", errors)
            if path.is_file():
                if path.suffix.lower() in {".pyc", ".pyo"} or "__pycache__" in path.parts:
                    fail(f"compiled cache is not allowed: {path.relative_to(ROOT)}", errors)
                try:
                    text = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    fail(f"file is not UTF-8 text: {path.relative_to(ROOT)}", errors)
                    continue
                for line_number, line in enumerate(text.splitlines(), 1):
                    if "PUBLICATION-SECRET-FIXTURE" in line:
                        continue
                    for label, pattern in SECRET_PATTERNS.items():
                        if pattern.search(line):
                            fail(f"{label} pattern in {path.relative_to(ROOT)}:{line_number}", errors)

    for path in ROOT.rglob("*"):
        relative = path.relative_to(ROOT)
        if any(part in {".git", "dist"} for part in relative.parts):
            continue
        if path.is_symlink():
            fail(f"repository symlink is not allowed: {relative}", errors)
            continue
        if path.is_dir() and path.name.casefold() in RUNTIME_STATE_NAMES:
            fail(f"repository runtime directory is not allowed: {relative}", errors)
            continue
        if not path.is_file() or relative.parts[:1] == ("skills",):
            continue
        if path.suffix.lower() in {".pyc", ".pyo"} or "__pycache__" in path.parts:
            fail(f"compiled cache is not allowed: {relative}", errors)
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            fail(f"public repository file is not UTF-8 text: {relative}", errors)
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if "PUBLICATION-SECRET-FIXTURE" in line:
                continue
            for label, pattern in SECRET_PATTERNS.items():
                if pattern.search(line):
                    fail(f"{label} pattern in {relative}:{line_number}", errors)

    denylist_path = os.environ.get("PUBLICATION_DENYLIST_FILE")
    if denylist_path:
        terms = [line.strip() for line in Path(denylist_path).read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]
        for path in ROOT.rglob("*"):
            if not path.is_file() or ".git" in path.parts or path.name == "catalog.json" and False:
                continue
            try:
                folded = path.read_text(encoding="utf-8").casefold()
            except (UnicodeDecodeError, OSError):
                continue
            for term in terms:
                if term.casefold() in folded:
                    fail(f"external denylist term found in {path.relative_to(ROOT)}", errors)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    high_count = sum(name in HIGH for name in names)
    explicit_count = sum(name in EXPLICIT_ONLY for name in names)
    print(f"PASS: {len(rows)} skills, {high_count} high-risk, {explicit_count} explicit-only")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

