"""Ownership-marked temporary-directory creation and bounded recursive cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import tempfile


FORMAT = "cross-device-project-sync-owned-temp-v1"
MARKER_SUFFIX = ".cross-device-owned-temp.json"


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path))


def _is_link_like(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _marker(path: Path) -> Path:
    path = _absolute(path)
    return path.parent / f".{path.name}{MARKER_SUFFIX}"


def _inside(path: Path, root: Path) -> bool:
    try:
        _absolute(path).relative_to(root.resolve())
        return True
    except ValueError:
        return False


def claim_existing(path: Path, controlled_root: Path) -> Path:
    path = _absolute(path)
    root = controlled_root.resolve()
    if not path.is_dir() or path == root or not _inside(path, root) or _is_link_like(path):
        raise ValueError(f"cannot claim unsafe temporary directory: {path}")
    marker = _marker(path)
    if marker.exists():
        raise ValueError(f"temporary ownership marker already exists: {marker}")
    marker.write_text(json.dumps({"format": FORMAT, "path": str(path)}, sort_keys=True), encoding="utf-8")
    return path


def make_temp(parent: Path, prefix: str) -> Path:
    parent = parent.resolve()
    parent.mkdir(parents=True, exist_ok=True)
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=str(parent)))
    return claim_existing(path, parent)


def release(path: Path) -> None:
    _marker(path).unlink(missing_ok=True)


def remove(path: Path, controlled_root: Path) -> None:
    path = _absolute(path)
    root = controlled_root.resolve()
    marker = _marker(path)
    expected = {"format": FORMAT, "path": str(path)}
    if path == root or not _inside(path, root) or _is_link_like(path):
        raise ValueError(f"refusing unsafe recursive cleanup: {path}")
    if not marker.is_file() or json.loads(marker.read_text(encoding="utf-8")) != expected:
        raise ValueError(f"refusing unowned recursive cleanup: {path}")
    if path.exists():
        shutil.rmtree(path)  # noqa: SEC-AUDITOR -- exact owned path under validated temporary root
    marker.unlink(missing_ok=True)
