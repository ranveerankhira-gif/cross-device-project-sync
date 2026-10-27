from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
import warnings
import zipfile


SCRIPT = Path(__file__).resolve().parents[1] / "package_release.py"
SPEC = importlib.util.spec_from_file_location("package_release_under_test", SCRIPT)
package_release = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(package_release)


class PackageReleaseTests(unittest.TestCase):
    def test_unicode_paths_round_trip_with_member_hashes(self):
        with tempfile.TemporaryDirectory(prefix="cross-device-project-sync-release-unicode-") as raw:
            root = Path(raw)
            source = root / "中文目录" / "样例.txt"
            source.parent.mkdir()
            source.write_text("中文内容\n", encoding="utf-8")
            archive = root / "unicode.zip"
            name = "example-skill/中文目录/样例.txt"
            package_release.write_zip(archive, [(name, source)])
            package_release.verify_zip(
                archive,
                {name: hashlib.sha256(source.read_bytes()).hexdigest()},
                "example-skill",
            )

    def test_zip_slip_member_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="package-release-zipslip-") as raw:
            archive = Path(raw) / "unsafe.zip"
            data = b"synthetic"
            name = "example-skill/../escape.txt"
            with zipfile.ZipFile(archive, "w") as handle:
                package_release.add_bytes(handle, name, data)
            with self.assertRaisesRegex(ValueError, "unsafe"):
                package_release.verify_zip(
                    archive,
                    {name: hashlib.sha256(data).hexdigest()},
                    "example-skill",
                )

    def test_duplicate_same_name_different_content_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix="package-release-duplicate-") as raw:
            archive = Path(raw) / "duplicate.zip"
            name = "example-skill/file.txt"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(archive, "w") as handle:
                    package_release.add_bytes(handle, name, b"first")
                    package_release.add_bytes(handle, name, b"second")
            with self.assertRaisesRegex(ValueError, "inventory mismatch"):
                package_release.verify_zip(
                    archive,
                    {name: hashlib.sha256(b"second").hexdigest()},
                    "example-skill",
                )


if __name__ == "__main__":
    unittest.main()
