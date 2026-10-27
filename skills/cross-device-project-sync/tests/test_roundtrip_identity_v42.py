import argparse
import json
from pathlib import Path
import tempfile
import unittest
import zipfile
from test_multi_instance_v42 import load, multi, incremental, packer, write_json, write_session

portable = load("identity_test", "conversation_identity.py")
catalog_builder = load("roundtrip_catalog_builder", "build-receiving-task-catalog.py")


class PortableRoundTripTests(unittest.TestCase):
    def inventory(self, session, thread, inherited, lineage):
        return incremental.conversation_inventory(incremental.load_exporter(Path(incremental.__file__).parent), session, thread, "test",
            inherited_handoff_paths=inherited, generation=incremental.CONVERSATION_PACK_GENERATION_V2,
            lineage={"lineage_id": lineage, "branch_id": "main", "source_instance_id": "a", "source_instance_name": "a号",
                     "source_disposition": "active", "origin_thread_id": "original-A", "origin_computer": "A-PC"})

    def package(self, root, row, name):
        project = {"project_id": name, "source_path": None, "label": "Projectless", "conversation_pack_generation": incremental.CONVERSATION_PACK_GENERATION_V2,
                   "conversations": [row]}
        project["conversation_set_sha256"] = multi.digest(incremental.conversation_set_identity(project, project["conversation_pack_generation"], True))
        catalog = root / (name + ".catalog.json")
        write_json(catalog, {"source_device": "A设备", "source_computer": "A-PC", "projects": [project]})
        output = root / (name + "-pack")
        packer.build(argparse.Namespace(catalog=str(catalog), project_id=name, output=str(output)))
        packer.verify(argparse.Namespace(pack=str(output)))
        extracted = root / (name + "-received")
        with zipfile.ZipFile(output / packer.ZIP_NAME) as archive:
            archive.extractall(extracted)  # already verified isolated test artifact
        thread = extracted / "threads" / row["thread_id"]
        verified, _ = catalog_builder.conversation_fingerprint(thread, row["thread_id"], project["conversation_pack_generation"], row["lineage_id"], row["branch_id"])
        self.assertEqual(verified, row["conversation_sha256"])
        return thread

    def test_real_export_pack_restore_b_lazy_then_continue_and_return_a_other_account(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            original = "01a00000-0000-7000-8000-000000000001"
            receiving = "01b00000-0000-7000-8000-000000000002"
            returning = "01a00000-0000-7000-8000-000000000003"
            lineage = multi.lineage_id("A-PC", original)
            a_session = root / "a.jsonl"
            write_session(a_session, original, root, "Original question", "Original answer")
            a = self.inventory(a_session, original, [], lineage)
            a_tree = self.package(root, a, "A-upload")
            b_session = root / "b.jsonl"
            marker = "[cross-device-receipt:" + "a" * 64 + "]\nRegister lazily."
            write_session(b_session, receiving, root, marker, "registered-lazy")
            b_lazy = self.inventory(b_session, receiving, [str(a_tree)], lineage)
            self.assertEqual(b_lazy["conversation_sha256"], a["conversation_sha256"])
            continuation = root / "continuation.jsonl"
            write_session(continuation, receiving, root, "New B question", "New B answer")
            with b_session.open("a", encoding="utf-8") as stream:
                stream.write("\n".join(continuation.read_text(encoding="utf-8").splitlines()[1:]) + "\n")
            b = self.inventory(b_session, receiving, [str(a_tree)], lineage)
            self.assertNotEqual(a["conversation_sha256"], b["conversation_sha256"])
            self.assertEqual(b["message_fingerprints"][:2], a["message_fingerprints"])
            b_tree = self.package(root, b, "B-upload")
            c_session = root / "a-other-account.jsonl"
            write_session(c_session, returning, root, marker, "registered-lazy")
            c = self.inventory(c_session, returning, [str(b_tree)], lineage)
            self.assertEqual(c["conversation_sha256"], b["conversation_sha256"])
            self.assertEqual(len(c["message_fingerprints"]), 4)
            c_tree = self.package(root, c, "A-other-account-upload")
            self.assertEqual(portable.flatten(c_tree), b["message_fingerprints"])

    def test_branch_ids_independent_of_source_account_and_persist_when_other_branch_absent(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            lineage = "a" * 64
            def project(thread, sequence):
                return {"project_id": thread, "conversations": [{"thread_id": thread, "lineage_id": lineage, "branch_id": "main", "source_instance_id": thread,
                    "message_fingerprints": sequence, "messages_sha256": "b" * 64, "session_log": thread}]}
            first = [project("a", ["one", "two", "left"]), project("b", ["one", "two", "right"])]
            incremental.reconcile_lineage_branches(first, [])
            left_branch = first[0]["conversations"][0]["branch_id"]
            second = [project("c", ["one", "two", "left", "new"]), project("d", ["one", "two", "right"])]
            incremental.reconcile_lineage_branches(second, [])
            self.assertEqual(second[0]["conversations"][0]["branch_id"], left_branch)
            state_path = root / "branches.json"
            state = incremental.apply_branch_state([], state_path)
            incremental.persist_branch_state(first, state_path, state)
            sole = [project("e", ["one", "two", "left", "further"])]
            incremental.apply_branch_state(sole, state_path)
            self.assertEqual(sole[0]["conversations"][0]["branch_id"], left_branch)


if __name__ == "__main__":
    unittest.main()
