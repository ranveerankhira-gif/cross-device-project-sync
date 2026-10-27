#!/usr/bin/env python3
"""Create deterministic cross-device-project-sync release archives with verification."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
MARKER = DIST / ".cross-device-project-sync-release-output"
ROOT_FILES = ["README.md", "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "catalog.json", "provenance.json"]


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def files(root: Path):
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix().casefold()):
        if path.is_symlink():
            raise ValueError(f"symlink is not allowed: {path}")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix.lower() not in {".pyc", ".pyo"}:
            yield path


def is_reparse_point(path: Path) -> bool:
    try:
        attributes = path.lstat().st_file_attributes
    except AttributeError:
        return path.is_symlink()
    return bool(attributes & 0x400)


def add_bytes(archive: zipfile.ZipFile, name: str, data: bytes) -> None:
    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)


def write_zip(target: Path, entries: list[tuple[str, Path]]) -> None:
    with zipfile.ZipFile(target, "x", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
        for name, source in sorted(entries, key=lambda item: item[0].casefold()):
            add_bytes(archive, name, source.read_bytes())


def verify_zip(path: Path, expected_hashes: dict[str, str], top: str) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        normalized = {PurePosixPath(name).as_posix() for name in names}
        if len(names) != len(normalized) or normalized != set(expected_hashes):
            raise ValueError(f"archive inventory mismatch: {path.name}")
        if any(PurePosixPath(name).is_absolute() or ".." in PurePosixPath(name).parts or PurePosixPath(name).parts[0] != top for name in names):
            raise ValueError(f"archive path or top-level directory is unsafe: {path.name}")
        for name in names:
            if any(ord(character) < 32 or ord(character) == 127 for character in name):
                raise ValueError(f"archive path contains a control character: {path.name}: {name!r}")
            if hashlib.sha256(archive.read(name)).hexdigest() != expected_hashes[name]:
                raise ValueError(f"archive member hash mismatch: {path.name}: {name}")
        with tempfile.TemporaryDirectory(prefix="cross-device-project-sync-verify-") as raw:
            destination = Path(raw)
            archive.extractall(destination)
            extracted = {item.relative_to(destination).as_posix() for item in files(destination)}
            if extracted != set(expected_hashes):
                raise ValueError(f"archive re-extraction mismatch: {path.name}")
            for name, expected_sha256 in expected_hashes.items():
                if digest(destination / PurePosixPath(name)) != expected_sha256:
                    raise ValueError(f"re-extracted member hash mismatch: {path.name}: {name}")


def reset_dist() -> None:
    if DIST.exists():
        if not MARKER.is_file() or MARKER.read_text(encoding="utf-8") != "owned release output\n":
            raise ValueError("refusing to replace unmarked dist directory")
        if DIST.resolve() != (ROOT / "dist").resolve() or DIST.parent.resolve() != ROOT.resolve():
            raise ValueError("refusing to replace release output outside repository root")
        for path in [DIST, *DIST.rglob("*")]:
            if path.is_symlink() or is_reparse_point(path):
                raise ValueError(f"refusing to replace release output containing a symlink or reparse point: {path}")
        shutil.rmtree(DIST)
    DIST.mkdir()
    MARKER.write_text("owned release output\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--release", choices=("v0.1.0", "all"), default="all")
    parser.add_argument("--verify-only", action="store_true", help="Build in a temporary directory and leave catalog/dist unchanged.")
    args = parser.parse_args()
    catalog_path = ROOT / "catalog.json"
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    provenance = json.loads((ROOT / "provenance.json").read_text(encoding="utf-8"))
    approved = {row["name"] for row in provenance.get("skills", [])}
    catalog_names = {row["name"] for row in catalog.get("skills", [])}
    directory_names = {path.name for path in (ROOT / "skills").iterdir() if path.is_dir()}
    if catalog_names != approved or directory_names != approved:
        raise ValueError("refusing to package Skills that are not explicitly approved by provenance.json")
    available_releases = {row["release"] for row in catalog["skills"]}
    selected_releases = available_releases if args.release == "all" else {args.release}
    if not selected_releases.issubset(available_releases):
        raise ValueError(f"requested release is not present in catalog: {sorted(selected_releases - available_releases)}")
    original_dist = None
    if args.verify_only:
        temporary = tempfile.TemporaryDirectory(prefix="cross-device-project-sync-package-")
        output = Path(temporary.name)
    else:
        reset_dist()
        output = DIST
    sums: dict[str, str] = {}
    manifests = {}
    for row in catalog["skills"]:
        if row["release"] not in selected_releases:
            continue
        skill = ROOT / "skills" / row["name"]
        target = output / f"{row['name']}-{row['version']}.zip"
        entries = [(f"{row['name']}/{path.relative_to(skill).as_posix()}", path) for path in files(skill)]
        write_zip(target, entries)
        expected = {name: digest(source) for name, source in entries}
        verify_zip(target, expected, row["name"])
        row["archive_sha256"] = digest(target)
        sums[target.name] = row["archive_sha256"]
    if not args.verify_only:
        catalog_path.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for release in sorted(selected_releases):
        version = release.removeprefix("v")
        top = f"cross-device-project-sync-{release}"
        release_rows = [row for row in catalog["skills"] if row["release"] == release]
        entries = []
        for filename in ROOT_FILES:
            entries.append((f"{top}/{filename}", ROOT / filename))
        for row in release_rows:
            skill = ROOT / "skills" / row["name"]
            entries.extend((f"{top}/skills/{row['name']}/{path.relative_to(skill).as_posix()}", path) for path in files(skill))
        bundle = output / f"cross-device-project-sync-{release}.zip"
        write_zip(bundle, entries)
        expected = {name: digest(source) for name, source in entries}
        verify_zip(bundle, expected, top)
        sums[bundle.name] = digest(bundle)
        manifest = {
            "schema_version": 1,
            "release": release,
            "skill_count": len(release_rows),
            "bundle": {"name": bundle.name, "sha256": sums[bundle.name]},
            "skills": [{"name": row["name"], "version": row["version"], "archive": f"{row['name']}-{row['version']}.zip", "sha256": row["archive_sha256"]} for row in release_rows],
        }
        manifest_path = output / f"manifest-{release}.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        sums[manifest_path.name] = digest(manifest_path)
        manifests[release] = manifest
        checksum_path = output / f"SHA256SUMS-{release}.txt"
        relevant = {name: sha for name, sha in sums.items() if name == bundle.name or name == manifest_path.name or any(name == item["archive"] for item in manifest["skills"])}
        checksum_path.write_text("".join(f"{sha}  {name}\n" for name, sha in sorted(relevant.items())), encoding="utf-8")
        sums[checksum_path.name] = digest(checksum_path)
    if args.verify_only:
        temporary.cleanup()
    else:
        manifest_path = DIST / "manifest.json"
        manifest_path.write_text(json.dumps({"schema_version": 1, "releases": manifests}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        sums[manifest_path.name] = digest(manifest_path)
        (DIST / "SHA256SUMS.txt").write_text("".join(f"{sha}  {name}\n" for name, sha in sorted(sums.items())), encoding="utf-8")
    print(json.dumps({"status": "verified" if args.verify_only else "built", "releases": sorted(selected_releases), "skill_archives": sum(len(value["skills"]) for value in manifests.values())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

