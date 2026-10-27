from pathlib import Path
import shutil
import tempfile
import unittest
from test_multi_instance_v42 import load

tx = load("project_tx_tests", "project-receive-transaction.py")


class ProjectTransactionTests(unittest.TestCase):
    def stage(self, base, name, content):
        path = base / "staged" / name
        path.mkdir(parents=True)
        (path / "content.txt").write_text(content, encoding="utf-8")
        (path / "empty").mkdir()
        return path, tx.fingerprint(path)

    def test_safe_replace_keeps_rollback_and_local_edits_get_sibling(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            base, home = root / "account-storage", root / "home"
            stage, sha = self.stage(base, "first", "original")
            first = tx.install(stage, sha, home, "A:project", base)
            dest = Path(first["destination"])
            stage, sha = self.stage(base, "second", "updated")
            second = tx.install(stage, sha, home, "A:project", base)
            self.assertEqual(second["action"], "transactional-safe-replace")
            self.assertEqual(Path(second["destination"]), dest)
            self.assertEqual((Path(second["rollback_path"]) / "content.txt").read_text(), "original")
            (dest / "content.txt").write_text("local work", encoding="utf-8")
            stage, sha = self.stage(base, "third", "new cloud")
            third = tx.install(stage, sha, home, "A:project", base)
            self.assertEqual(third["action"], "restore-side-by-side")
            self.assertNotEqual(third["destination"], str(dest))
            self.assertEqual((dest / "content.txt").read_text(), "local work")

    def test_crash_at_each_rename_recovers_without_data_loss(self):
        for crash_phase in ("prepared", "old-renamed", "new-installed"):
            with self.subTest(phase=crash_phase), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                base, home = root / "storage", root / "home"
                stage, sha = self.stage(base, "first", "original")
                first = tx.install(stage, sha, home, "p", base)
                stage, sha = self.stage(base, "second", "updated")
                captured = {}
                def crash(phase, journal):
                    captured.update(journal)
                    if phase == crash_phase:
                        raise RuntimeError("simulated crash")
                with self.assertRaisesRegex(RuntimeError, "simulated"):
                    tx.install(stage, sha, home, "p", base, fault=crash)
                journal = home / "cross-device-project-transactions" / (tx.io.digest("p") + ".json")
                result = tx.recover(journal, home, base)
                self.assertEqual(result["phase"], "complete" if crash_phase == "new-installed" else "rolled-back")
                contents = [(p / "content.txt").read_text() for p in (stage, Path(first["destination"]), Path(captured["rollback"])) if p.is_dir()]
                self.assertCountEqual(contents, ["original", "updated"])

    def test_edit_just_before_swap_stops_and_wrong_account_base_rejected(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            base, home = root / "storage", root / "home"
            stage, sha = self.stage(base, "one", "old")
            first = tx.install(stage, sha, home, "p", base)
            stage, sha = self.stage(base, "two", "new")
            def edit(phase, journal):
                if phase == "prepared":
                    (Path(journal["destination"]) / "content.txt").write_text("concurrent edit", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "changed before"):
                tx.install(stage, sha, home, "p", base, fault=edit)
            self.assertEqual((Path(first["destination"]) / "content.txt").read_text(), "concurrent edit")
            with self.assertRaisesRegex(ValueError, "outside"):
                tx.install(stage, sha, home, "p", root / "other-account", fault=edit)

    def test_tampered_staging_is_never_installed(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            stage, sha = self.stage(root / "storage", "one", "expected")
            (stage / "extra.txt").write_text("unexpected")
            with self.assertRaisesRegex(ValueError, "selected source tree"):
                tx.install(stage, sha, root / "home", "p", root / "storage")

    def test_reuse_unchanged_removes_only_redundant_staging_copies(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            base, home = root / "storage", root / "home"
            stage, sha = self.stage(base, "initial", "same")
            first = tx.install(stage, sha, home, "p", base)
            destination = Path(first["destination"])
            for index in range(5):
                stage = base / "staged" / f"repeat-{index}"
                shutil.copytree(destination, stage, copy_function=shutil.copy2)
                repeated_sha = tx.fingerprint(stage)
                result = tx.install(stage, repeated_sha, home, "p", base)
                self.assertEqual(result["action"], "reuse-unchanged")
                self.assertEqual(result["staging_cleanup"]["status"], "deleted-redundant-stage")
                self.assertFalse(stage.exists())
                self.assertTrue(destination.is_dir())
            leftovers = list((base / "staged").glob("*.cleanup-pending-*"))
            self.assertEqual(leftovers, [])

    def test_readable_source_names_and_collisions_keep_distinct_projects_separate(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            base, home = root / "storage", root / "home"
            installed = []
            for number, content in enumerate(("one", "two", "three"), 1):
                stage, sha = self.stage(base, f"stage-{number}", content)
                result = tx.install(
                    stage, sha, home, f"WORKSTATION-B:project-{number}", base,
                    source_name="商品选购2", source_device="B设备",
                )
                installed.append(Path(result["destination"]))
            self.assertEqual(installed[0].name, "商品选购2")
            self.assertEqual(installed[1].name, "商品选购2（来自B设备）")
            self.assertRegex(installed[2].name, r"^商品选购2（来自B设备）-[0-9a-f]{8}$")
            self.assertEqual(len({path.resolve() for path in installed}), 3)
            self.assertEqual([(path / "content.txt").read_text(encoding="utf-8") for path in installed], ["one", "two", "three"])

    def test_windows_unsafe_or_reserved_source_names_are_sanitized_and_bounded(self):
        safe = tx.safe_project_component('CON<>:"/\\|?* .', "fallback")
        self.assertNotEqual(safe.split(".", 1)[0].upper(), "CON")
        self.assertFalse(any(character in safe for character in '<>:"/\\|?*'))
        self.assertFalse(safe.endswith((" ", ".")))
        long_name = tx.safe_project_component("项目" * 100, "fallback")
        self.assertLessEqual(len(long_name.encode("utf-16-le")) // 2, tx.MAX_PROJECT_COMPONENT_UTF16)


if __name__ == "__main__":
    unittest.main()
