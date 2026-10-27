#!/usr/bin/env python3
"""Fail-closed preflight for high-confidence credentials in a sync catalog.

This is a risk-reduction gate, not a proof that arbitrary binaries or every
possible secret format are clean.  Findings contain paths/categories only and
never echo matched credential values.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Any


FORMAT = "cross-device-project-sync-secret-audit-v2"
# Boundaries matter: without them, ordinary words such as ``task-picker`` and
# ``risk-metrics`` contain the substring ``sk-`` and look like API keys.  The
# AWS access-key ID is not itself a secret, so only a secret-key assignment is
# a blocker.
SECRET_PATTERNS = {
    # A header string occurs legitimately in parsers, documentation, compiled
    # programs, and generated caches.  Require a complete PEM/OpenSSH block so
    # those references do not become upload-stopping false positives.
    "private-key": re.compile(
        rb"-----BEGIN (?P<label>(?:RSA |EC |OPENSSH )?PRIVATE KEY)-----"
        rb"\s+[A-Za-z0-9+/=\r\n]{64,}\s+"
        rb"-----END (?P=label)-----"
    ),
    "github-token": re.compile(rb"(?<![A-Za-z0-9_])gh[pousr]_[A-Za-z0-9]{30,}(?![A-Za-z0-9_])"),
    "github-fine-grained-token": re.compile(rb"(?<![A-Za-z0-9_])github_pat_[A-Za-z0-9_]{50,}(?![A-Za-z0-9_])"),
    "openai-style-key": re.compile(rb"(?<![A-Za-z0-9_-])sk-(?:proj-)?(?=[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-]))(?=[A-Za-z0-9_-]*[A-Z])(?=[A-Za-z0-9_-]*[a-z])(?=[A-Za-z0-9_-]*[0-9])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"),
    "google-api-key": re.compile(rb"(?<![A-Za-z0-9_-])AIza[0-9A-Za-z_-]{30,}(?![A-Za-z0-9_-])"),
    "slack-token": re.compile(rb"(?<![A-Za-z0-9_-])xox[baprs]-[0-9A-Za-z-]{20,}(?![A-Za-z0-9_-])"),
    "aws-secret-assignment": re.compile(rb"(?i)(?:aws_)?secret_access_key\s*[:=]\s*['\"]?[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])"),
}
GIT_GREP_RE = (
    r"BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY|"
    r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,}|"
    r"sk-(proj-)?[A-Za-z0-9_-]{20,}|AIza[0-9A-Za-z_-]{30,}|"
    r"xox[baprs]-[0-9A-Za-z-]{20,}|[Ss][Ee][Cc][Rr][Ee][Tt]_[Aa][Cc][Cc][Ee][Ss][Ss]_[Kk][Ee][Yy]"
)
SAFE_ENV_SUFFIXES = (".example", ".sample", ".template", ".dist")
SENSITIVE_EXACT = {
    "auth.json", "state_5.sqlite", "state_5.sqlite-wal", "state_5.sqlite-shm",
    ".npmrc", ".pypirc", ".netrc", "credentials", "credentials.json",
    "application_default_credentials.json", "id_rsa", "id_ed25519",
    "cookies", "cookies.sqlite", "login data", "web data",
}
SENSITIVE_SUFFIXES = (".p12", ".pfx", ".key", ".kdbx")
BINARY_SUFFIXES = {
    ".7z", ".a", ".avi", ".bin", ".bmp", ".bz2", ".class", ".dll",
    ".dylib", ".eot", ".exe", ".flac", ".gif", ".gz", ".ico", ".jar",
    ".jpeg", ".jpg", ".m4a", ".mkv", ".mov", ".mp3", ".mp4", ".o",
    ".obj", ".ogg", ".otf", ".pdf", ".png", ".pyc", ".pyd", ".pyo",
    ".rar", ".so", ".tar", ".tif", ".tiff", ".ttf", ".wav", ".webm",
    ".webp", ".woff", ".woff2", ".xz", ".zip",
}
TEST_WORD_RE = re.compile(r"(?:^|[-_.])(test|tests|testing|fixture|fixtures|sample|samples|example|examples|dummy)(?:$|[-_.])")
AUDIT_CORPUS_RE = re.compile(r"(?:skill|external-skill|security|mcp-windows|winapp[^/]*)[-_.]?audit", re.IGNORECASE)
VENDOR_PARTS = {"node_modules", "site-packages", "pkgs", "vendor"}


def is_sensitive_name(relative: str) -> str | None:
    name = PurePosixPath(relative).name.casefold()
    if name == ".env" or (name.startswith(".env.") and not name.endswith(SAFE_ENV_SUFFIXES)):
        return "environment-file"
    if name in SENSITIVE_EXACT:
        return "sensitive-auth-file"
    if name.endswith(SENSITIVE_SUFFIXES):
        return "key-or-keystore-file"
    if re.fullmatch(r"(?:secret|secrets|token|tokens|credential|credentials)\.json", name):
        return "credential-named-json"
    return None


def secret_categories(data: bytes) -> list[str]:
    return [category for category, pattern in SECRET_PATTERNS.items() if pattern.search(data)]


def normalized_path_parts(relative: str) -> list[str]:
    return [part.casefold() for part in PurePosixPath(relative.replace("\\", "/")).parts]


def explicit_test_fixture_reason(relative: str) -> str | None:
    """Return narrow, auditable evidence that a finding is test-only.

    A generic ``tests`` directory alone is not enough: real credentials are
    sometimes accidentally committed there.  We require either an explicit
    secret-scanner fixture, a third-party package's own test corpus, or a
    locally retained security/Skill audit corpus.
    """
    parts = normalized_path_parts(relative)
    if any("test" in part and any(word in part for word in ("secret", "credential", "token", "auth")) for part in parts):
        return "explicit-secret-test-fixture"
    has_test = any(TEST_WORD_RE.search(part) or part in {"testdata", "examplefiles", "dummyserver"} for part in parts)
    if has_test and any(part in VENDOR_PARTS for part in parts):
        return "third-party-package-test-fixture"
    joined = "/".join(parts)
    if AUDIT_CORPUS_RE.search(joined):
        return "security-audit-corpus"
    return None


def binary_candidate(path: Path, sample: bytes) -> bool:
    if path.suffix.casefold() in BINARY_SUFFIXES:
        return True
    if sample.startswith((b"MZ", b"\x7fELF", b"PK\x03\x04", b"\x1f\x8b", b"%PDF-")):
        return True
    return b"\x00" in sample[:65536]


def scan_file(path: Path, chunk_bytes: int) -> tuple[list[str], int, int, bool]:
    found: set[str] = set()
    scanned = 0
    tail = b""
    overlap = 256 * 1024
    size = path.stat().st_size
    if path.suffix.casefold() in BINARY_SUFFIXES:
        return [], 0, size, True
    with path.open("rb") as handle:
        first = handle.read(chunk_bytes)
        if binary_candidate(path, first):
            return [], min(len(first), 65536), size, True
        chunk = first
        while True:
            if not chunk:
                break
            scanned += len(chunk)
            window = tail + chunk
            found.update(secret_categories(window))
            tail = window[-overlap:]
            chunk = handle.read(chunk_bytes)
    return sorted(found), scanned, size, False


def normalized_git_patch(data: bytes) -> bytes:
    """Remove patch prefixes so complete multi-line PEM blocks can be seen."""
    payload: list[bytes] = []
    for line in data.splitlines(keepends=True):
        if line.startswith((b"+++ ", b"--- ", b"@@ ", b"diff --git ", b"index ")):
            continue
        if line[:1] in {b"+", b"-", b" "}:
            payload.append(line[1:])
        else:
            payload.append(line)
    return b"".join(payload)


def sorted_findings(items: list[dict[str, str]]) -> list[dict[str, str]]:
    return sorted(items, key=lambda item: (item["project_id"].casefold(), item["path"].casefold(), item["surface"], item.get("category", "")))


def git_roots(project: dict[str, Any]) -> list[Path]:
    source_path = str(project.get("source_path") or "").strip()
    if not source_path:
        return []
    root = Path(source_path)
    roots: set[Path] = set()
    for entry in project.get("entries") or []:
        relative = str(entry.get("path") or "")
        if entry.get("type") in {"dir", "file"} and PurePosixPath(relative).name == ".git":
            parent = PurePosixPath(relative).parent
            repository = root if str(parent) == "." else root.joinpath(*parent.parts)
            marker = repository / ".git"
            if marker.is_file() or (marker.is_dir() and (marker / "HEAD").is_file() and (marker / "config").is_file()):
                roots.add(repository)
    return sorted(roots, key=lambda item: str(item).casefold())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--git", default="git.exe")
    parser.add_argument("--max-text-bytes", type=int, default=8 * 1024 * 1024)
    args = parser.parse_args()
    if args.max_text_bytes < 4096:
        parser.error("--max-text-bytes must be at least 4096 bytes")

    catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8"))
    blocking_findings: list[dict[str, str]] = []
    nonblocking_test_findings: list[dict[str, str]] = []
    limits: list[dict[str, str]] = []
    scanned_files = 0
    scanned_bytes = 0
    binary_skipped_files = 0
    binary_skipped_bytes = 0
    git_repository_count = 0

    def record_finding(item: dict[str, str], fixture_reason: str | None) -> None:
        if fixture_reason:
            nonblocking_test_findings.append({**item, "reason": fixture_reason})
        else:
            blocking_findings.append(item)

    def scan_catalog_file(project_id: str, relative: str, path: Path, name_surface: str, content_surface: str) -> None:
        nonlocal scanned_files, scanned_bytes, binary_skipped_files, binary_skipped_bytes
        fixture_reason = explicit_test_fixture_reason(relative)
        name_category = is_sensitive_name(relative)
        try:
            categories, bytes_read, size, skipped_binary = scan_file(path, args.max_text_bytes)
        except OSError as exc:
            limits.append({"project_id": project_id, "path": relative, "reason": f"read-error:{type(exc).__name__}"})
            return
        scanned_files += 1
        scanned_bytes += bytes_read
        if skipped_binary:
            binary_skipped_files += 1
            binary_skipped_bytes += size
        if name_category:
            record_finding({"project_id": project_id, "path": relative, "category": name_category, "surface": name_surface}, fixture_reason)
        for category in categories:
            record_finding({"project_id": project_id, "path": relative, "category": "high-confidence-secret-pattern", "secret_kind": category, "surface": content_surface}, fixture_reason)

    for project in catalog.get("projects") or []:
        project_id = str(project["project_id"])
        for conversation in project.get("conversations") or []:
            for history in conversation.get("inherited_histories") or []:
                history_root = Path(history["source_path"])
                for entry in history.get("files") or []:
                    relative = str(entry["path"])
                    before_limits = len(limits)
                    scan_catalog_file(project_id, relative, history_root.joinpath(*PurePosixPath(relative).parts), "inherited-history-name", "inherited-history-content")
                    if len(limits) > before_limits:
                        limits[-1]["reason"] = limits[-1]["reason"].replace("read-error:", "inherited-history-read-error:", 1)
        source_path = str(project.get("source_path") or "").strip()
        if not source_path:
            if project.get("entries"):
                limits.append({"project_id": project_id, "path": "(none)", "reason": "conversation-only-group-has-project-entries"})
            continue
        root = Path(source_path)
        for entry in project.get("entries") or []:
            if entry.get("type") != "file":
                continue
            relative = str(entry["path"])
            path = root.joinpath(*PurePosixPath(relative).parts)
            scan_catalog_file(project_id, relative, path, "current-tree-name", "current-tree-content")

        for repository in git_roots(project):
            git_repository_count += 1
            try:
                result = subprocess.run(
                    [args.git, "-c", f"safe.directory={repository}", "-C", str(repository), "log", "--all", "--full-history", "-p", "--pretty=format:%H", "-G", GIT_GREP_RE],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=600,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                limits.append({"project_id": project_id, "path": str(repository), "reason": f"git-history-scan-error:{type(exc).__name__}"})
                continue
            if result.returncode != 0:
                limits.append({"project_id": project_id, "path": str(repository), "reason": "git-history-scan-nonzero"})
            else:
                fixture_reason = explicit_test_fixture_reason(str(repository))
                for secret_category in secret_categories(normalized_git_patch(result.stdout)):
                    record_finding({"project_id": project_id, "path": str(repository), "category": "high-confidence-secret-pattern", "secret_kind": secret_category, "surface": "git-history-diff"}, fixture_reason)

    report = {
        "format": FORMAT,
        "catalog_sha256": catalog.get("catalog_sha256"),
        "status": "blocked" if blocking_findings or limits else "passed",
        # ``findings`` remains a compatibility alias for blocking findings.
        "findings": sorted_findings(blocking_findings),
        "blocking_findings": sorted_findings(blocking_findings),
        "nonblocking_test_findings": sorted_findings(nonblocking_test_findings),
        "limits": sorted(limits, key=lambda item: (item["project_id"].casefold(), item["path"].casefold())),
        "scanned_file_count": scanned_files,
        "scanned_byte_count": scanned_bytes,
        "binary_skipped_file_count": binary_skipped_files,
        "binary_skipped_byte_count": binary_skipped_bytes,
        "git_repository_count": git_repository_count,
        "disclaimer": "High-confidence text/path/history gate only. Recognized binary payloads are not regex-scanned, but protected authentication filenames still block. Narrow test/audit fixtures are reported without blocking; unknown secret formats cannot be proven clean.",
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("status", "scanned_file_count", "scanned_byte_count", "git_repository_count")}, ensure_ascii=False, sort_keys=True))
    return 2 if report["status"] != "passed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
