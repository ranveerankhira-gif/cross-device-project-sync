from pathlib import Path
import hashlib
import tempfile
import unittest

from test_multi_instance_v42 import load, seal_queue_inputs, write_json, multi, ledger_module

queue = load("queue_engine_v42_test", "receive-queue-engine.py")


class QueueTests(unittest.TestCase):
    def setup_queue(self, root, count=3):
        home = root / "calling-home"
        home.mkdir()
        catalog = root / "catalog.json"
        write_json(catalog, {"receiving_tasks": [{"thread_id": f"origin-{i}", "source_disposition": "archived" if i % 2 else "active"} for i in range(count)]})
        plan = seal_queue_inputs(catalog)
        path = home / "queue.json"
        queue.build(catalog, {"codex_home": str(home), "instance_id": "c"}, path, plan)
        return path, home, plan

    def observation(self, task, home):
        return {"destination_codex_home": str(home), "status": "registered-lazy",
                "receiving_thread_id": "receiver-" + task["operation_key"],
                "receipt_marker": task["receipt_marker"], "receiver_disposition": task["expected_disposition"],
                "target_kind": task["target_kind"], "target_project_id": task["target_project_id"]}

    def test_hundreds_of_tasks_crash_after_create_before_receipt_no_redispatch(self):
        with tempfile.TemporaryDirectory() as raw:
            path, home, plan = self.setup_queue(Path(raw), 257)
            created = {}
            batch = queue.batch(path, home, claim=True)
            self.assertEqual(batch["batch_size"], 8)
            for task in batch["tasks"]:
                created[task["operation_key"]] = self.observation(task, home)
            # Simulate app success followed by process death, with no local receipts.
            retry = queue.batch(path, home, claim=True)
            self.assertEqual(retry["batch_size"], 0)
            self.assertEqual(len(retry["unresolved_claims"]), 8)
            for task in retry["unresolved_claims"]:
                queue.record(path, home, task["operation_key"], task["claim_token"], created[task["operation_key"]])
            while True:
                batch = queue.batch(path, home, claim=True)
                if batch["complete"]:
                    break
                self.assertLessEqual(len(batch["tasks"]), 8)
                self.assertTrue(batch["tasks"])
                for task in batch["tasks"]:
                    self.assertNotIn(task["operation_key"], created)
                    observed = self.observation(task, home)
                    created[task["operation_key"]] = observed
                    queue.record(path, home, task["operation_key"], task["claim_token"], observed)
            self.assertEqual(len(created), 257)
            results = queue.ledger_results(path, home)
            committed = ledger_module.commit_plan(multi.read_json(plan), {"format": multi.LEDGER_V2, "entries": {}}, results)
            self.assertEqual(committed["updated"], 257)
            self.assertEqual(committed["failed"], 0)
            self.assertEqual(sum(row["receiver_disposition"] == "archived" for row in committed["ledger"]["entries"].values()), 128)

    def test_reject_other_account_corruption_missing_id_and_wrong_archive(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path, home, _ = self.setup_queue(root)
            with self.assertRaisesRegex(ValueError, "calling CODEX_HOME"):
                queue.batch(path, root / "other-home", claim=True)
            task = queue.batch(path, home, claim=True)["tasks"][1]
            observed = self.observation(task, home)
            for update in ({"receiving_thread_id": None}, {"receiver_disposition": "active"}, {"receipt_marker": "incorrect"}, {"destination_codex_home": str(root / "other-home")}):
                with self.assertRaises(ValueError):
                    queue.record(path, home, task["operation_key"], task["claim_token"], {**observed, **update})
            value = multi.read_json(path)
            value["tasks"][0]["status"] = "registered-lazy"
            write_json(path, value)
            with self.assertRaisesRegex(ValueError, "hash"):
                queue.batch(path, home)

    def test_ambiguous_failure_never_retries_and_lazy_can_upgrade(self):
        with tempfile.TemporaryDirectory() as raw:
            path, home, _ = self.setup_queue(Path(raw), 1)
            task = queue.batch(path, home, claim=True)["tasks"][0]
            failure = {"destination_codex_home": str(home), "status": "failed"}
            with self.assertRaisesRegex(ValueError, "definitive"):
                queue.record(path, home, task["operation_key"], task["claim_token"], failure)
            queue.record(path, home, task["operation_key"], task["claim_token"], {**failure, "status": "uncertain"})
            self.assertEqual(queue.batch(path, home, claim=True)["batch_size"], 0)
            observed = self.observation(task, home)
            queue.record(path, home, task["operation_key"], task["claim_token"], observed)
            with self.assertRaisesRegex(ValueError, "context evidence"):
                queue.record(path, home, task["operation_key"], task["claim_token"], {
                    **observed, "status": "context-loaded", "full_context_verified": True,
                })
            evidence_path = home / "context-evidence.json"
            evidence_path.write_bytes(b"context evidence")
            evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            queue.record(path, home, task["operation_key"], task["claim_token"], {
                **observed, "status": "context-loaded", "full_context_verified": True,
                "context_evidence_report_sha256": evidence_sha256,
                "context_evidence_report_path": str(evidence_path),
                "context_load_marker": queue.context_load_marker(task, evidence_sha256),
            })
            self.assertEqual(queue.batch(path, home)["counts"], {"context-loaded": 1})

    def test_blocked_divergence_is_not_complete_or_claimable_before_resolution(self):
        with tempfile.TemporaryDirectory() as raw:
            path, home, _ = self.setup_queue(Path(raw), 1)
            value = multi.read_json(path)
            value["tasks"][0].update(
                action="preserve-pending-source-diverged", status="blocked-divergence",
                receiving_thread_id="receiver-existing", claim_token=None,
            )
            queue.save(path, value)
            result = queue.batch(path, home, claim=True)
            self.assertFalse(result["complete"])
            self.assertEqual(result["batch_size"], 0)
            self.assertEqual(result["unresolved_claims"], [])
            with self.assertRaisesRegex(ValueError, "unfinished or unresolved"):
                queue.ledger_results(path, home)

    def test_legacy_139_migrated_entries_match_v1_catalog_without_new_tasks(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            entries, tasks, states = {}, [], {}
            for i in range(139):
                thread = f"t-{i}"
                content = multi.digest(thread)
                entries[ledger_module.source_key("SOURCE", thread)] = {"source_computer": "SOURCE", "original_thread_id": thread,
                    "receiving_thread_id": f"r-{i}", "receiver_disposition": "archived" if i % 2 else "active",
                    "conversation_sha256": content, "applied_state_version": 2}
                tasks.append({"thread_id": thread, "conversation_sha256": content})
                states[thread] = {"receiving_thread_id": f"r-{i}", "receiver_disposition": "archived" if i % 2 else "active"}
            ledger_path = root / "ledger.json"
            write_json(ledger_path, {"format": multi.LEDGER_V1, "entries": entries})
            multi.migrate_ledger(ledger_path, "a", root / "backups")
            catalog_path = root / "catalog.json"
            write_json(catalog_path, {"receiving_tasks": tasks})
            seal_queue_inputs(catalog_path)
            catalog = multi.read_json(catalog_path)
            catalog["format"] = ledger_module.RECEIVING_CATALOG_FORMAT_V1
            for task in catalog["receiving_tasks"]:
                task.pop("lineage_id")
                task.pop("branch_id")
            catalog.pop("catalog_sha256")
            catalog["catalog_sha256"] = multi.digest(catalog)
            migrated = multi.read_json(ledger_path)
            plan = ledger_module.build_plan(catalog, migrated, states)
            self.assertEqual({row["action"] for row in plan["decisions"]}, {"skip-unchanged"})
            result = ledger_module.commit_plan(plan, migrated, {"plan_sha256": plan["plan_sha256"], "results": []})
            self.assertEqual(len(result["ledger"]["entries"]), 139)
            self.assertEqual(result["skipped"], 139)


if __name__ == "__main__":
    unittest.main()
