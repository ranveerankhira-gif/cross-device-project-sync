import importlib.util
import hashlib
import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "conversation-sync-ledger.py"
spec = importlib.util.spec_from_file_location("conversation_sync_ledger", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(module)


def load_script(name, filename):
    path = SCRIPT.with_name(filename)
    child_spec = importlib.util.spec_from_file_location(name, path)
    child = importlib.util.module_from_spec(child_spec)
    assert child_spec and child_spec.loader
    child_spec.loader.exec_module(child)
    return child


queue_module = load_script("conversation_ledger_queue_test", "receive-queue-engine.py")
app_proof_module = load_script("conversation_ledger_app_proof_test", "receiver-app-proof.py")


def sha(char):
    return char * 64


def sealed_catalog(tasks, source_computer="WORKSTATION-A"):
    conversation_count = sum(task.get("kind") == "conversation" for task in tasks)
    value = {
        "format": module.RECEIVING_CATALOG_FORMAT,
        "source_device": "A设备",
        "source_computer": source_computer,
        "project_count": 1,
        "raw_session_count": conversation_count,
        "user_visible_conversation_count": conversation_count,
        "excluded_internal_session_count": 0,
        "bootstrap_task_count": sum(task.get("kind") == "project-bootstrap" for task in tasks),
        "target_receiving_task_count": len(tasks),
        "projects": [{"raw_session_count": conversation_count}],
        "receiving_tasks": tasks,
        "excluded_sessions": [],
    }
    value["catalog_sha256"] = hashlib.sha256(module.canonical_bytes(value)).hexdigest()
    return value


def catalog(conversations):
    return sealed_catalog([
        {**item, "kind": "conversation", "project_id": "p1", "project_label": "无项目"}
        for item in conversations
    ])


def receiver_state_document(threads, source_computer="WORKSTATION-A"):
    return {
        "format": module.RECEIVER_STATE_FORMAT,
        "source_computer": source_computer,
        "captured_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "complete": True,
        "active_inventory_complete": True,
        "archived_inventory_complete": True,
        "thread_count": len(threads),
        "threads": threads,
    }


def states_from_ledger(ledger):
    return {
        entry["original_thread_id"]: {
            "receiving_thread_id": entry.get("receiving_thread_id"),
            "receiver_disposition": entry.get("receiver_disposition", "active"),
        }
        for entry in ledger["entries"].values()
    }


def conversation(thread_id, digest, count=2):
    return {
        "thread_id": thread_id,
        "thread_title": thread_id,
        "conversation_sha256": digest,
        "messages_sha256": sha("b"),
        "message_count": count,
        "attachment_count": 0,
        "cutoff_timestamp": "2026-10-01T00:00:00Z",
    }


def results_for(plan, rows):
    decisions = {row["source_key"]: row for row in plan["decisions"]}
    bound = []
    for row in rows:
        item = dict(row)
        decision = decisions[item["source_key"]]
        item.setdefault("action", decision["action"])
        item.setdefault("conversation_sha256", decision["conversation_sha256"])
        bound.append(item)
    return {"plan_sha256": plan["plan_sha256"], "results": bound}


class ConversationSyncLedgerTests(unittest.TestCase):
    def write_rollout(self, home, thread_id, messages):
        session_root = home / "sessions" / "2026" / "10" / "16"
        session_root.mkdir(parents=True, exist_ok=True)
        path = session_root / f"{thread_id}.jsonl"
        rows = [{"type": "session_meta", "payload": {"id": thread_id}}]
        rows.extend({
            "type": "response_item",
            "payload": {
                "type": "message", "role": role,
                "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}],
            },
        } for role, text in messages)
        path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
        return path

    def app_proof_queue(self, home, action, status="registered-lazy", *, include_context=False):
        key = sha("d")
        conversation_sha256 = sha("a")
        marker = "[cross-device-receipt:" + queue_module.digest({
            "source_key": key, "conversation_sha256": conversation_sha256,
        }) + "]"
        task = {
            "kind": "conversation", "operation_key": key, "action": action,
            "conversation_sha256": conversation_sha256, "status": status,
            "receiving_thread_id": "receiver-1", "receipt_marker": marker,
            "initial_prompt": marker + "\ntest", "target_kind": "project",
            "target_project_id": "p1", "expected_disposition": "active", "attempts": 1,
            "last_observation": {
                "status": status, "destination_codex_home": str(home),
                "receiving_thread_id": "receiver-1", "receipt_marker": marker,
                "target_kind": "project", "target_project_id": "p1",
                "receiver_disposition": "active",
            },
        }
        if include_context:
            evidence_path = home / "context-evidence.json"
            evidence_path.write_bytes(b"verified context evidence")
            evidence_sha256 = hashlib.sha256(evidence_path.read_bytes()).hexdigest()
            task["last_observation"].update({
                "full_context_verified": True,
                "context_evidence_report_sha256": evidence_sha256,
                "context_evidence_report_path": str(evidence_path),
                "context_load_marker": queue_module.context_load_marker(task, evidence_sha256),
            })
        value = {
            "format": queue_module.FORMAT, "catalog_sha256": sha("b"), "plan_sha256": sha("c"),
            "destination_codex_home": str(home), "destination_instance_id": "test",
            "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "max_batch_size": 8,
            "tasks": [task],
        }
        path = home / "queue.json"
        queue_module.save(path, value)
        return path, task

    def test_update_existing_receipt_requires_a_real_rollout_user_message(self):
        with tempfile.TemporaryDirectory(prefix="app-proof-update-") as raw:
            home = Path(raw)
            queue_path, task = self.app_proof_queue(home, "update-existing")
            rollout = self.write_rollout(home, "receiver-1", [("assistant", task["receipt_marker"])])
            database = sqlite3.connect(home / "state_5.sqlite")
            database.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, archived INTEGER, project_id TEXT, "
                "first_user_message TEXT, rollout_path TEXT, has_user_event INTEGER)"
            )
            database.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("receiver-1", "app-server", 0, "p1", "old first prompt", str(rollout), 1),
            )
            database.commit()
            database.close()
            with self.assertRaisesRegex(app_proof_module.AppProofError, "no user message"):
                app_proof_module.build_proof(queue_path, home)
            self.write_rollout(home, "receiver-1", [("assistant", task["receipt_marker"]), ("user", task["receipt_marker"])])
            proof = app_proof_module.build_proof(queue_path, home)
            self.assertEqual(proof["tasks"][0]["receipt_evidence_type"], "rollout-user-message")
            self.assertEqual(len(proof["tasks"][0]["rollout_sha256"]), 64)

    def test_register_new_accepts_modern_desktop_project_state_and_rollout_marker(self):
        with tempfile.TemporaryDirectory(prefix="app-proof-modern-project-") as raw:
            home = Path(raw)
            restored = home / "restored"
            restored.mkdir()
            queue_path, task = self.app_proof_queue(home, "register-new")
            queue = queue_module.load(queue_path, str(home))
            queue["tasks"][0]["restored_project_root"] = str(restored)
            queue_module.save(queue_path, queue)
            rollout = self.write_rollout(home, "receiver-1", [("user", task["receipt_marker"])])
            database = sqlite3.connect(home / "state_5.sqlite")
            database.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, archived INTEGER, project_id TEXT, "
                "first_user_message TEXT, rollout_path TEXT, has_user_event INTEGER, cwd TEXT)"
            )
            database.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("receiver-1", "vscode", 0, None, "", str(rollout), 0, str(restored)),
            )
            database.commit()
            database.close()
            (home / ".codex-global-state.json").write_text(json.dumps({
                "thread-project-assignments": {
                    "receiver-1": {"projectKind": "local", "projectId": "p1"},
                },
                "projectless-thread-ids": [],
                "local-projects": {
                    "p1": {"id": "p1", "name": "Project", "rootPaths": [str(restored)]},
                },
            }), encoding="utf-8")
            proof = app_proof_module.build_proof(queue_path, home)
            self.assertEqual(proof["tasks"][0]["receipt_evidence_type"], "rollout-user-message")
            self.assertEqual(
                proof["tasks"][0]["project_binding_evidence_type"],
                "global-state-thread-project-assignment",
            )
            self.assertIs(proof["tasks"][0]["has_user_event"], False)

    def test_register_new_accepts_explicit_modern_projectless_state(self):
        with tempfile.TemporaryDirectory(prefix="app-proof-modern-projectless-") as raw:
            home = Path(raw)
            queue_path, task = self.app_proof_queue(home, "register-new")
            queue = queue_module.load(queue_path, str(home))
            queue["tasks"][0].update(target_kind="projectless", target_project_id=None)
            queue["tasks"][0]["last_observation"].update(target_kind="projectless", target_project_id=None)
            queue_module.save(queue_path, queue)
            rollout = self.write_rollout(home, "receiver-1", [("user", task["receipt_marker"])])
            database = sqlite3.connect(home / "state_5.sqlite")
            database.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, archived INTEGER, project_id TEXT, "
                "first_user_message TEXT, rollout_path TEXT, has_user_event INTEGER)"
            )
            database.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("receiver-1", "vscode", 0, None, "", str(rollout), 0),
            )
            database.commit()
            database.close()
            (home / ".codex-global-state.json").write_text(json.dumps({
                "thread-project-assignments": {},
                "projectless-thread-ids": ["receiver-1"],
                "local-projects": {},
            }), encoding="utf-8")
            proof = app_proof_module.build_proof(queue_path, home)
            self.assertEqual(proof["tasks"][0]["receipt_evidence_type"], "rollout-user-message")
            self.assertEqual(
                proof["tasks"][0]["project_binding_evidence_type"],
                "global-state-projectless-inventory",
            )

    def test_context_loaded_requires_its_independent_marker_in_a_rollout_user_message(self):
        with tempfile.TemporaryDirectory(prefix="app-proof-context-") as raw:
            home = Path(raw)
            queue_path, task = self.app_proof_queue(home, "register-new", "context-loaded", include_context=True)
            rollout = self.write_rollout(home, "receiver-1", [("user", task["receipt_marker"])])
            database = sqlite3.connect(home / "state_5.sqlite")
            database.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, archived INTEGER, project_id TEXT, "
                "first_user_message TEXT, rollout_path TEXT, has_user_event INTEGER)"
            )
            database.execute(
                "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?, ?)",
                ("receiver-1", "app-server", 0, "p1", task["initial_prompt"], str(rollout), 1),
            )
            database.commit()
            database.close()
            with self.assertRaisesRegex(app_proof_module.AppProofError, "context marker"):
                app_proof_module.build_proof(queue_path, home)
            context_marker = task["last_observation"]["context_load_marker"]
            self.write_rollout(home, "receiver-1", [
                ("user", task["receipt_marker"]), ("assistant", context_marker), ("user", context_marker),
            ])
            proof = app_proof_module.build_proof(queue_path, home)
            self.assertEqual(proof["tasks"][0]["context_evidence_type"], "rollout-user-message")
            self.assertEqual(
                proof["tasks"][0]["context_evidence_report_sha256"],
                hashlib.sha256((home / "context-evidence.json").read_bytes()).hexdigest(),
            )

    def test_cli_requires_fresh_receiver_state(self):
        with tempfile.TemporaryDirectory(prefix="conversation-ledger-cli-") as raw:
            base = Path(raw)
            catalog_path = base / "catalog.json"
            ledger_path = base / "ledger.json"
            catalog_path.write_text(json.dumps(catalog([])), encoding="utf-8")
            ledger_path.write_text(json.dumps({"format": module.LEDGER_FORMAT, "entries": {}}), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "plan", "--catalog", str(catalog_path),
                 "--ledger", str(ledger_path), "--output", str(base / "plan.json")],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env={**os.environ, "CODEX_HOME": str(base)},
            )
            self.assertEqual(completed.returncode, 2)
            self.assertIn("--receiver-state", completed.stderr)

    def test_cli_rejects_receiver_state_missing_a_known_ledger_task(self):
        with tempfile.TemporaryDirectory(prefix="conversation-ledger-state-") as raw:
            base = Path(raw)
            key = module.source_key("WORKSTATION-A", "t1")
            ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
                "source_device": "A设备", "source_computer": "WORKSTATION-A",
                "original_thread_id": "t1", "conversation_sha256": sha("a"),
                "receiving_thread_id": "r1", "receiver_disposition": "active",
                "applied_state_version": module.APPLIED_STATE_VERSION,
            }}}
            paths = {
                "catalog": base / "catalog.json",
                "ledger": base / "ledger.json",
                "state": base / "state.json",
                "plan": base / "plan.json",
            }
            paths["catalog"].write_bytes(module.canonical_bytes(catalog([conversation("t1", sha("b"))])))
            paths["ledger"].write_bytes(module.canonical_bytes(ledger))
            paths["state"].write_bytes(module.canonical_bytes(receiver_state_document([])))
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "plan", "--catalog", str(paths["catalog"]),
                 "--ledger", str(paths["ledger"]), "--receiver-state", str(paths["state"]),
                 "--output", str(paths["plan"])],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env={**os.environ, "CODEX_HOME": str(base)},
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("receiver state is incomplete", completed.stderr)
            self.assertFalse(paths["plan"].exists())

            complete_state = receiver_state_document([{
                "original_thread_id": "t1", "receiving_thread_id": "r1",
                "receiver_disposition": "archived",
            }])
            paths["state"].write_bytes(module.canonical_bytes(complete_state))
            completed = subprocess.run(
                [sys.executable, str(SCRIPT), "plan", "--catalog", str(paths["catalog"]),
                 "--ledger", str(paths["ledger"]), "--receiver-state", str(paths["state"]),
                 "--output", str(paths["plan"])],
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, env={**os.environ, "CODEX_HOME": str(base)},
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            plan = module.read_json(paths["plan"])
            self.assertEqual(plan["receiver_state_sha256"], hashlib.sha256(module.canonical_bytes(complete_state)).hexdigest())
            self.assertEqual(plan["catalog_sha256"], module.read_json(paths["catalog"])["catalog_sha256"])
            self.assertEqual(plan["decisions"][0]["action"], "download-update-keep-hidden")

    def test_receiver_state_rejects_duplicate_receiving_ids(self):
        with tempfile.TemporaryDirectory(prefix="conversation-ledger-state-") as raw:
            path = Path(raw) / "state.json"
            path.write_bytes(module.canonical_bytes(receiver_state_document([
                {"original_thread_id": "t1", "receiving_thread_id": "same", "receiver_disposition": "active"},
                {"original_thread_id": "t2", "receiving_thread_id": "same", "receiver_disposition": "archived"},
            ])))
            with self.assertRaisesRegex(module.LedgerError, "duplicate receiving_thread_id"):
                module.receiver_states(path)

            stale = receiver_state_document([])
            stale["captured_at_utc"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)).isoformat()
            path.write_bytes(module.canonical_bytes(stale))
            with self.assertRaisesRegex(module.LedgerError, "receiver state is stale"):
                module.receiver_states(path)

    def test_catalog_rejects_unknown_format_kind_and_tampered_counts(self):
        unknown_format = catalog([conversation("t1", sha("a"))])
        unknown_format["format"] = "cross-device-receiving-task-catalog-v999"
        with self.assertRaisesRegex(module.LedgerError, "only a filtered"):
            module.build_plan(unknown_format, {"format": module.LEDGER_FORMAT, "entries": {}}, {})

        unknown_kind = sealed_catalog([{
            "kind": "future-kind", "original_thread_id": "t1",
            "conversation_sha256": sha("a"), "messages_sha256": sha("b"),
            "message_count": 1, "attachment_count": 0,
        }])
        with self.assertRaisesRegex(module.LedgerError, "unsupported receiving task kind"):
            module.build_plan(unknown_kind, {"format": module.LEDGER_FORMAT, "entries": {}}, {})

        truncated = catalog([conversation("t1", sha("a"))])
        truncated["target_receiving_task_count"] = 2
        truncated.pop("catalog_sha256")
        truncated["catalog_sha256"] = hashlib.sha256(module.canonical_bytes(truncated)).hexdigest()
        with self.assertRaisesRegex(module.LedgerError, "target_receiving_task_count"):
            module.build_plan(truncated, {"format": module.LEDGER_FORMAT, "entries": {}}, {})

    def test_commit_rejects_invalid_disposition_and_duplicate_receiver_id(self):
        empty = {"format": module.LEDGER_FORMAT, "entries": {}}
        one = module.build_plan(catalog([conversation("t1", sha("a"))]), empty, {})
        key = one["decisions"][0]["source_key"]
        invalid = results_for(one, [{
            "source_key": key, "status": "applied", "receiving_thread_id": "r1",
            "receiver_disposition": "nonsense",
        }])
        with self.assertRaisesRegex(module.LedgerError, "invalid result receiver_disposition"):
            module.commit_plan(one, empty, invalid)

        mismatched = results_for(one, [{
            "source_key": key, "status": "applied", "receiving_thread_id": "r1",
            "receiver_disposition": "archived",
        }])
        with self.assertRaisesRegex(module.LedgerError, "disposition does not match"):
            module.commit_plan(one, empty, mismatched)

        two = module.build_plan(catalog([
            conversation("t1", sha("a")), conversation("t2", sha("b")),
        ]), empty, {})
        duplicate_results = results_for(two, [
            {"source_key": decision["source_key"], "status": "applied", "receiving_thread_id": "same"}
            for decision in two["decisions"]
        ])
        with self.assertRaisesRegex(module.LedgerError, "already bound to another source"):
            module.commit_plan(two, empty, duplicate_results)

    def test_concurrent_cli_commits_cannot_both_overwrite_the_ledger(self):
        with tempfile.TemporaryDirectory(prefix="conversation-ledger-race-") as raw:
            base = Path(raw)
            ledger_path = base / "ledger.json"
            empty = {"format": module.LEDGER_FORMAT, "entries": {}}
            ledger_path.write_bytes(module.canonical_bytes(empty))
            plan = module.build_plan(catalog([conversation("t1", sha("a"))]), empty, {})
            decision = plan["decisions"][0]
            marker = "[cross-device-receipt:" + queue_module.digest({
                "source_key": decision["source_key"], "conversation_sha256": decision["conversation_sha256"],
            }) + "]"
            queue_path = base / "queue.json"
            queue_value = {
                "format": queue_module.FORMAT, "catalog_sha256": plan["catalog_sha256"],
                "plan_sha256": plan["plan_sha256"], "destination_codex_home": str(base),
                "destination_instance_id": "test", "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                "max_batch_size": 8, "tasks": [{
                    **conversation("t1", sha("a")), "kind": "conversation", "project_id": "p1",
                    "target_kind": "project", "target_project_id": "p1", "operation_key": decision["source_key"],
                    "action": "register-new", "status": "registered-lazy", "receipt_marker": marker,
                    "initial_prompt": marker + "\ntest", "receiving_thread_id": "receiver-1",
                    "expected_disposition": "active", "attempts": 1,
                    "last_observation": {"status": "registered-lazy", "receiving_thread_id": "receiver-1",
                                         "receipt_marker": marker, "target_kind": "project", "target_project_id": "p1"},
                }],
            }
            queue_module.save(queue_path, queue_value)
            database = sqlite3.connect(base / "state_5.sqlite")
            database.execute("CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, archived INTEGER, project_id TEXT, first_user_message TEXT, has_user_event INTEGER)")
            database.execute("INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)", ("receiver-1", "app-server", 0, "p1", marker + "\ntest", 1))
            database.commit()
            database.close()
            proof_path = base / "app-proof.json"
            proof_path.write_bytes(module.canonical_bytes(app_proof_module.build_proof(queue_path, base)))
            result_path = base / "result.json"
            result_path.write_bytes(module.canonical_bytes(queue_module.ledger_results(queue_path, base)))
            plan_path = base / "plan.json"
            plan_path.write_bytes(module.canonical_bytes(plan))
            processes = []
            for _ in range(2):
                processes.append(subprocess.Popen(
                    [sys.executable, str(SCRIPT), "commit", "--plan", str(plan_path),
                     "--ledger", str(ledger_path), "--result", str(result_path),
                     "--queue", str(queue_path), "--app-proof", str(proof_path)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env={**os.environ, "CODEX_HOME": str(base)},
                ))
            completed = [process.communicate(timeout=30) + (process.returncode,) for process in processes]
            self.assertEqual(sorted(item[2] for item in completed), [0, 1], completed)
            final = module.load_ledger(ledger_path)
            self.assertEqual(len(final["entries"]), 1)

    def test_concurrent_distinct_lineage_branches_serialize_without_lost_update(self):
        with tempfile.TemporaryDirectory(prefix="conversation-ledger-branches-") as raw:
            base = Path(raw)
            ledger_path = base / "ledger.json"
            empty = {"format": module.LEDGER_FORMAT_V2, "entries": {}}
            ledger_path.write_bytes(module.canonical_bytes(empty))
            lineage = sha("e")

            database = sqlite3.connect(base / "state_5.sqlite")
            database.execute(
                "CREATE TABLE threads (id TEXT PRIMARY KEY, source TEXT, archived INTEGER, "
                "project_id TEXT, first_user_message TEXT, has_user_event INTEGER)"
            )

            cases = []
            for branch, thread_id, conversation_hash, receiver_id in (
                ("main", "t-main", sha("a"), "receiver-main"),
                ("feature", "t-feature", sha("c"), "receiver-feature"),
            ):
                task = {
                    **conversation(thread_id, conversation_hash),
                    "lineage_id": lineage,
                    "branch_id": branch,
                }
                catalog_value = catalog([task])
                catalog_value["format"] = module.RECEIVING_CATALOG_FORMAT_V2
                catalog_value.pop("catalog_sha256")
                catalog_value["catalog_sha256"] = hashlib.sha256(
                    module.canonical_bytes(catalog_value)
                ).hexdigest()
                plan = module.build_plan(catalog_value, empty, {})
                decision = plan["decisions"][0]
                marker = "[cross-device-receipt:" + queue_module.digest({
                    "source_key": decision["source_key"],
                    "conversation_sha256": decision["conversation_sha256"],
                }) + "]"
                database.execute(
                    "INSERT INTO threads VALUES (?, ?, ?, ?, ?, ?)",
                    (receiver_id, "app-server", 0, "p1", marker + "\ntest", 1),
                )
                cases.append({
                    "branch": branch,
                    "task": task,
                    "catalog": catalog_value,
                    "plan": plan,
                    "decision": decision,
                    "marker": marker,
                    "receiver_id": receiver_id,
                })
            database.commit()
            database.close()

            def write_commit_artifacts(case, suffix):
                plan_path = base / f"plan-{suffix}.json"
                queue_path = base / f"queue-{suffix}.json"
                proof_path = base / f"proof-{suffix}.json"
                result_path = base / f"result-{suffix}.json"
                plan_path.write_bytes(module.canonical_bytes(case["plan"]))
                queue_value = {
                    "format": queue_module.FORMAT,
                    "catalog_sha256": case["plan"]["catalog_sha256"],
                    "plan_sha256": case["plan"]["plan_sha256"],
                    "destination_codex_home": str(base),
                    "destination_instance_id": "test",
                    "created_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "max_batch_size": 8,
                    "tasks": [{
                        **case["task"],
                        "kind": "conversation",
                        "project_id": "p1",
                        "target_kind": "project",
                        "target_project_id": "p1",
                        "operation_key": case["decision"]["source_key"],
                        "action": "register-new",
                        "status": "registered-lazy",
                        "receipt_marker": case["marker"],
                        "initial_prompt": case["marker"] + "\ntest",
                        "receiving_thread_id": case["receiver_id"],
                        "expected_disposition": "active",
                        "attempts": 1,
                        "last_observation": {
                            "status": "registered-lazy",
                            "receiving_thread_id": case["receiver_id"],
                            "receipt_marker": case["marker"],
                            "target_kind": "project",
                            "target_project_id": "p1",
                        },
                    }],
                }
                queue_module.save(queue_path, queue_value)
                proof_path.write_bytes(module.canonical_bytes(app_proof_module.build_proof(queue_path, base)))
                result_path.write_bytes(module.canonical_bytes(queue_module.ledger_results(queue_path, base)))
                return plan_path, queue_path, proof_path, result_path

            commands = []
            for case in cases:
                plan_path, queue_path, proof_path, result_path = write_commit_artifacts(case, case["branch"])
                commands.append([
                    sys.executable, str(SCRIPT), "commit",
                    "--plan", str(plan_path), "--ledger", str(ledger_path),
                    "--result", str(result_path), "--queue", str(queue_path),
                    "--app-proof", str(proof_path),
                ])
            processes = [
                subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    text=True, encoding="utf-8", env={**os.environ, "CODEX_HOME": str(base)},
                )
                for command in commands
            ]
            completed = [process.communicate(timeout=30) + (process.returncode,) for process in processes]
            self.assertEqual(sorted(item[2] for item in completed), [0, 1], completed)
            after_race = module.load_ledger(ledger_path)
            self.assertEqual(len(after_race["entries"]), 1)

            missing = next(
                case for case in cases
                if case["decision"]["source_key"] not in after_race["entries"]
            )
            receiver_states = {
                key: {
                    "receiving_thread_id": entry["receiving_thread_id"],
                    "receiver_disposition": entry["receiver_disposition"],
                }
                for key, entry in after_race["entries"].items()
            }
            missing["plan"] = module.build_plan(missing["catalog"], after_race, receiver_states)
            missing["decision"] = missing["plan"]["decisions"][0]
            plan_path, queue_path, proof_path, result_path = write_commit_artifacts(missing, "retry")
            retry = subprocess.run([
                sys.executable, str(SCRIPT), "commit",
                "--plan", str(plan_path), "--ledger", str(ledger_path),
                "--result", str(result_path), "--queue", str(queue_path),
                "--app-proof", str(proof_path),
            ], capture_output=True, text=True, encoding="utf-8", timeout=30,
               env={**os.environ, "CODEX_HOME": str(base)})
            self.assertEqual(retry.returncode, 0, retry.stdout + retry.stderr)
            final = module.load_ledger(ledger_path)
            self.assertEqual(set(final["entries"]), {
                module.lineage_source_key(lineage, "main"),
                module.lineage_source_key(lineage, "feature"),
            })

    def test_receiver_thread_identity_conflict_is_rejected(self):
        key = module.source_key("WORKSTATION-A", "t1")
        ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "original_thread_id": "t1", "conversation_sha256": sha("a"),
            "receiving_thread_id": "r1", "receiver_disposition": "active",
        }}}
        states = {"t1": {"receiving_thread_id": "r2", "receiver_disposition": "active"}}
        with self.assertRaisesRegex(module.LedgerError, "receiver thread identity conflict"):
            module.build_plan(catalog([conversation("t1", sha("a"))]), ledger, states)

    def test_commit_rejects_ledger_changed_after_planning(self):
        key = module.source_key("WORKSTATION-A", "t1")
        ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "original_thread_id": "t1", "conversation_sha256": sha("a"),
            "receiving_thread_id": "r1", "receiver_disposition": "active",
            "applied_state_version": module.APPLIED_STATE_VERSION,
        }}}
        plan = module.build_plan(catalog([conversation("t1", sha("b"), 3)]), ledger, states_from_ledger(ledger))
        changed = json.loads(json.dumps(ledger))
        changed["entries"][key]["conversation_sha256"] = sha("c")
        result = results_for(plan, [{
            "source_key": key, "status": "applied", "receiving_thread_id": "r1",
        }])
        with self.assertRaisesRegex(module.LedgerError, "ledger changed after planning"):
            module.commit_plan(plan, changed, result)

    def test_source_rollback_does_not_clear_a_newer_pending_hash(self):
        key = module.source_key("WORKSTATION-A", "t1")
        ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "original_thread_id": "t1", "conversation_sha256": sha("a"),
            "pending_conversation_sha256": sha("c"),
            "receiving_thread_id": "r1", "receiver_disposition": "archived",
        }}}
        plan = module.build_plan(catalog([conversation("t1", sha("a"))]), ledger, states_from_ledger(ledger))
        self.assertEqual(plan["decisions"][0]["action"], "preserve-pending-source-diverged")
        committed = module.commit_plan(plan, ledger, {"plan_sha256": plan["plan_sha256"], "results": []})["ledger"]
        self.assertEqual(committed["entries"][key]["pending_conversation_sha256"], sha("c"))
        self.assertEqual(committed["entries"][key]["conversation_sha256"], sha("a"))

    def test_explicit_divergence_resolution_retires_old_queue_and_preserves_non_selected_history(self):
        for choice, expected_applied, expected_history in (
            ("incoming", sha("b"), sha("c")),
            ("retained-pending", sha("c"), sha("b")),
        ):
            with self.subTest(choice=choice), tempfile.TemporaryDirectory(prefix="divergence-resolution-") as raw:
                root = Path(raw)
                home = root / "home"
                home.mkdir()
                retained = home / "retained-handoff"
                retained.mkdir()
                key = module.source_key("WORKSTATION-A", "t1")
                ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
                    "source_device": "A设备", "source_computer": "WORKSTATION-A",
                    "original_thread_id": "t1", "conversation_sha256": sha("a"),
                    "messages_sha256": sha("1"), "message_count": 1, "attachment_count": 0,
                    "pending_conversation_sha256": sha("c"), "pending_messages_sha256": sha("3"),
                    "pending_message_count": 3, "pending_attachment_count": 0,
                    "retained_handoff_path": str(retained),
                    "receiving_thread_id": "r1", "receiver_disposition": "active",
                    "applied_state_version": module.APPLIED_STATE_VERSION,
                }}}
                ledger_path = home / "cross-device-conversation-sync-ledger.json"
                ledger_path.write_bytes(module.canonical_bytes(ledger))
                incoming = conversation("t1", sha("b"), 2)
                incoming.update(
                    original_thread_id="t1", initial_prompt="apply incoming",
                    conversation_path=str(home / "incoming-handoff"),
                )
                catalog_value = catalog([incoming])
                catalog_path = home / "catalog.json"
                catalog_path.write_bytes(module.canonical_bytes(catalog_value))
                states = states_from_ledger(ledger)
                blocked_plan = module.build_plan(catalog_value, ledger, states)
                self.assertEqual(blocked_plan["decisions"][0]["action"], "preserve-pending-source-diverged")
                blocked_plan_path = home / "blocked-plan.json"
                blocked_plan_path.write_bytes(module.canonical_bytes(blocked_plan))
                old_queue = home / "old-queue.json"
                queue_module.build(catalog_path, {"codex_home": str(home), "instance_id": "c"}, old_queue, blocked_plan_path)
                self.assertFalse(queue_module.batch(old_queue, home)["complete"])

                resolution = module.create_divergence_resolution(
                    blocked_plan, ledger, key, choice, "explicit user choice for divergence", home,
                )
                resolution_path = home / f"resolution-{choice}.json"
                resolution_path.write_bytes(module.canonical_bytes(resolution))
                retired = queue_module.resolve_blocked_divergence(old_queue, home, key, resolution)
                self.assertTrue(retired["complete"])
                loaded_resolution = module.load_resolution_files([resolution_path], home)
                fresh_plan = module.build_plan(catalog_value, ledger, states, divergence_resolutions=loaded_resolution)
                self.assertEqual(
                    fresh_plan["decisions"][0]["action"],
                    "resolve-divergence-incoming" if choice == "incoming" else "resolve-divergence-retained",
                )
                fresh_plan_path = home / "fresh-plan.json"
                fresh_plan_path.write_bytes(module.canonical_bytes(fresh_plan))
                fresh_queue = home / "fresh-queue.json"
                queue_module.build(catalog_path, {"codex_home": str(home), "instance_id": "c"}, fresh_queue, fresh_plan_path)
                claimed = queue_module.batch(fresh_queue, home, claim=True)["tasks"][0]
                observation = {
                    "destination_codex_home": str(home), "status": "registered-lazy",
                    "receiving_thread_id": "r1", "receipt_marker": claimed["receipt_marker"],
                    "receiver_disposition": "active", "target_kind": claimed["target_kind"],
                    "target_project_id": claimed["target_project_id"],
                }
                queue_module.record(fresh_queue, home, key, claimed["claim_token"], observation)
                committed = module.commit_plan(
                    fresh_plan, ledger, queue_module.ledger_results(fresh_queue, home),
                )["ledger"]
                entry = committed["entries"][key]
                self.assertEqual(entry["conversation_sha256"], expected_applied)
                self.assertTrue(any(row["conversation_sha256"] == expected_history for row in entry["divergent_histories"]))
                self.assertNotIn("pending_conversation_sha256", entry)
                repeated = module.build_plan(catalog_value, committed, states_from_ledger(committed))
                expected_action = "skip-unchanged" if choice == "incoming" else "skip-resolved-divergence"
                self.assertEqual(repeated["decisions"][0]["action"], expected_action)
    def test_new_thread_registers_once_then_skips_same_hash(self):
        empty = {"format": module.LEDGER_FORMAT, "entries": {}}
        first = module.build_plan(catalog([conversation("t1", sha("a"))]), empty, {})
        self.assertEqual(first["decisions"][0]["action"], "register-new")
        key = first["decisions"][0]["source_key"]
        ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "original_thread_id": "t1", "conversation_sha256": sha("a"),
            "receiving_thread_id": "r1", "receiver_disposition": "active",
        }}}
        second = module.build_plan(catalog([conversation("t1", sha("a"))]), ledger, states_from_ledger(ledger))
        self.assertEqual(second["decisions"][0]["action"], "skip-unchanged")
        self.assertEqual(second["decisions"][0]["receiving_thread_id"], "r1")

    def test_archived_thread_stays_hidden_when_source_advances(self):
        key = module.source_key("WORKSTATION-A", "t1")
        ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "original_thread_id": "t1", "conversation_sha256": sha("a"),
            "receiving_thread_id": "r1", "receiver_disposition": "active",
        }}}
        states = {"t1": {"receiving_thread_id": "r1", "receiver_disposition": "archived"}}
        plan = module.build_plan(catalog([conversation("t1", sha("c"), 3)]), ledger, states)
        decision = plan["decisions"][0]
        self.assertEqual(decision["action"], "download-update-keep-hidden")
        self.assertEqual(decision["receiver_disposition"], "archived")

    def test_active_thread_updates_only_on_hash_change(self):
        key = module.source_key("WORKSTATION-A", "t1")
        ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "original_thread_id": "t1", "conversation_sha256": sha("a"),
            "receiving_thread_id": "r1", "receiver_disposition": "active",
        }}}
        plan = module.build_plan(catalog([conversation("t1", sha("c"), 3)]), ledger, states_from_ledger(ledger))
        self.assertEqual(plan["decisions"][0]["action"], "update-existing")

    def test_commit_keeps_archived_disposition(self):
        key = module.source_key("WORKSTATION-A", "t1")
        ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "original_thread_id": "t1", "conversation_sha256": sha("a"),
            "receiving_thread_id": "r1", "receiver_disposition": "active",
        }}}
        states = {"t1": {"receiving_thread_id": "r1", "receiver_disposition": "archived"}}
        plan = module.build_plan(catalog([conversation("t1", sha("c"), 3)]), ledger, states)
        key = plan["decisions"][0]["source_key"]
        result = results_for(plan, [{
            "source_key": key, "status": "downloaded-kept-hidden",
            "receiving_thread_id": "r1", "receiver_disposition": "archived",
        }])
        committed = module.commit_plan(plan, ledger, result)
        self.assertEqual(committed["failed"], 0)
        self.assertEqual(committed["ledger"]["entries"][key]["receiver_disposition"], "archived")
        self.assertEqual(committed["ledger"]["entries"][key]["conversation_sha256"], sha("a"))
        self.assertEqual(committed["ledger"]["entries"][key]["pending_conversation_sha256"], sha("c"))
        self.assertEqual(committed["ledger"]["entries"][key]["origin_title"], "t1")

    def test_archived_download_becomes_retained_update_after_manual_unarchive(self):
        key = module.source_key("WORKSTATION-A", "t1")
        ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "original_thread_id": "t1", "conversation_sha256": sha("a"),
            "receiving_thread_id": "r1", "receiver_disposition": "archived",
        }}}
        current = catalog([conversation("t1", sha("c"), 3)])
        hidden = {"t1": {"receiving_thread_id": "r1", "receiver_disposition": "archived"}}
        first = module.build_plan(current, ledger, hidden)
        committed = module.commit_plan(first, ledger, results_for(first, [{
            "source_key": key, "status": "downloaded-kept-hidden",
            "receiving_thread_id": "r1", "receiver_disposition": "archived",
        }]))["ledger"]
        repeated = module.build_plan(current, committed, hidden)
        self.assertEqual(repeated["decisions"][0]["action"], "keep-hidden-already-downloaded")
        active = {"t1": {"receiving_thread_id": "r1", "receiver_disposition": "active"}}
        catch_up = module.build_plan(current, committed, active)
        self.assertEqual(catch_up["decisions"][0]["action"], "apply-retained-update")
        applied = module.commit_plan(catch_up, committed, results_for(catch_up, [{
            "source_key": key, "status": "applied",
            "receiving_thread_id": "r1", "receiver_disposition": "active",
        }]))["ledger"]
        self.assertEqual(applied["entries"][key]["conversation_sha256"], sha("c"))
        self.assertNotIn("pending_conversation_sha256", applied["entries"][key])

    def test_legacy_archived_entry_is_conservatively_retained_before_reactivation(self):
        key = module.source_key("WORKSTATION-A", "t1")
        ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "original_thread_id": "t1", "conversation_sha256": sha("a"),
            "receiving_thread_id": "r1", "receiver_disposition": "archived",
        }}}
        current = catalog([conversation("t1", sha("a"))])
        hidden = {"t1": {"receiving_thread_id": "r1", "receiver_disposition": "archived"}}
        plan = module.build_plan(current, ledger, hidden)
        self.assertEqual(plan["decisions"][0]["action"], "download-update-keep-hidden")
        self.assertIn("cannot-prove", plan["decisions"][0]["reason"])
        retained = module.commit_plan(plan, ledger, results_for(plan, [{
            "source_key": key, "status": "downloaded-kept-hidden",
            "receiving_thread_id": "r1", "receiver_disposition": "archived",
        }]))["ledger"]
        active = {"t1": {"receiving_thread_id": "r1", "receiver_disposition": "active"}}
        catch_up = module.build_plan(current, retained, active)
        self.assertEqual(catch_up["decisions"][0]["action"], "apply-retained-update")

    def test_existing_thread_is_reconciled_when_ledger_is_missing(self):
        empty = {"format": module.LEDGER_FORMAT, "entries": {}}
        active = {"t1": {"receiving_thread_id": "r1", "receiver_disposition": "active"}}
        hidden = {"t1": {"receiving_thread_id": "r1", "receiver_disposition": "archived"}}
        self.assertEqual(
            module.build_plan(catalog([conversation("t1", sha("a"))]), empty, active)["decisions"][0]["action"],
            "update-existing",
        )
        self.assertEqual(
            module.build_plan(catalog([conversation("t1", sha("a"))]), empty, hidden)["decisions"][0]["action"],
            "download-update-keep-hidden",
        )

    def test_tampered_plan_is_rejected(self):
        empty = {"format": module.LEDGER_FORMAT, "entries": {}}
        plan = module.build_plan(catalog([conversation("t1", sha("a"))]), empty, {})
        plan["decisions"][0]["conversation_sha256"] = sha("tampered")
        with self.assertRaisesRegex(module.LedgerError, "plan_sha256"):
            module.commit_plan(plan, empty, {"plan_sha256": plan["plan_sha256"], "results": []})

        legacy_plan = module.build_plan(catalog([conversation("t1", sha("a"))]), empty, {})
        legacy_plan.pop("receiver_state_sha256")
        legacy_plan.pop("plan_sha256")
        legacy_plan["plan_sha256"] = hashlib.sha256(module.canonical_bytes(legacy_plan)).hexdigest()
        with self.assertRaisesRegex(module.LedgerError, "receiver_state_sha256"):
            module.commit_plan(legacy_plan, empty, {"plan_sha256": legacy_plan["plan_sha256"], "results": []})

    def test_register_new_requires_receiving_thread_id(self):
        empty = {"format": module.LEDGER_FORMAT, "entries": {}}
        plan = module.build_plan(catalog([conversation("t1", sha("a"))]), empty, {})
        key = plan["decisions"][0]["source_key"]
        with self.assertRaisesRegex(module.LedgerError, "requires receiving_thread_id"):
            module.commit_plan(plan, empty, results_for(plan, [{"source_key": key, "status": "applied"}]))

    def test_duplicate_result_rows_are_rejected(self):
        empty = {"format": module.LEDGER_FORMAT, "entries": {}}
        plan = module.build_plan(catalog([conversation("t1", sha("a"))]), empty, {})
        key = plan["decisions"][0]["source_key"]
        row = {"source_key": key, "status": "applied", "receiving_thread_id": "r1"}
        with self.assertRaisesRegex(module.LedgerError, "duplicate result source_key"):
            module.commit_plan(plan, empty, results_for(plan, [row, dict(row)]))

    def test_stale_result_from_an_older_plan_is_rejected(self):
        key = module.source_key("WORKSTATION-A", "t1")
        ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "original_thread_id": "t1", "conversation_sha256": sha("a"),
            "receiving_thread_id": "r1", "receiver_disposition": "active",
            "applied_state_version": module.APPLIED_STATE_VERSION,
        }}}
        older = module.build_plan(catalog([conversation("t1", sha("b"), 3)]), ledger, states_from_ledger(ledger))
        stale = results_for(older, [{
            "source_key": key, "status": "applied", "receiving_thread_id": "r1",
        }])
        newer = module.build_plan(catalog([conversation("t1", sha("c"), 4)]), ledger, states_from_ledger(ledger))
        with self.assertRaisesRegex(module.LedgerError, "result plan_sha256"):
            module.commit_plan(newer, ledger, stale)

    def test_update_existing_cannot_replace_receiving_thread(self):
        key = module.source_key("WORKSTATION-A", "t1")
        ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "original_thread_id": "t1", "conversation_sha256": sha("a"),
            "receiving_thread_id": "r1", "receiver_disposition": "active",
        }}}
        plan = module.build_plan(catalog([conversation("t1", sha("c"), 3)]), ledger, states_from_ledger(ledger))
        result = results_for(plan, [{
            "source_key": key, "status": "applied", "receiving_thread_id": "r2",
        }])
        with self.assertRaisesRegex(module.LedgerError, "cannot replace"):
            module.commit_plan(plan, ledger, result)

    def test_skip_unchanged_persists_fresh_manual_archive_state(self):
        key = module.source_key("WORKSTATION-A", "t1")
        ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "original_thread_id": "t1", "conversation_sha256": sha("a"),
            "receiving_thread_id": "r1", "receiver_disposition": "active",
            "applied_state_version": module.APPLIED_STATE_VERSION,
        }}}
        states = {"t1": {"receiving_thread_id": "r1", "receiver_disposition": "archived"}}
        plan = module.build_plan(catalog([conversation("t1", sha("a"))]), ledger, states)
        self.assertEqual(plan["decisions"][0]["action"], "skip-unchanged")
        committed = module.commit_plan(plan, ledger, {"plan_sha256": plan["plan_sha256"], "results": []})
        self.assertEqual(committed["failed"], 0)
        self.assertEqual(committed["ledger"]["entries"][key]["receiver_disposition"], "archived")

    def test_legacy_index_without_messages_hash_still_plans_safely(self):
        item = conversation("t1", sha("a"))
        item.pop("messages_sha256")
        plan = module.build_plan(catalog([item]), {"format": module.LEDGER_FORMAT, "entries": {}}, {})
        self.assertEqual(plan["decisions"][0]["action"], "register-new")
        self.assertIsNone(plan["decisions"][0]["messages_sha256"])

    def test_filtered_receiving_catalog_is_accepted(self):
        bootstrap_id = module.bootstrap_identity("source-p1")
        receiving_catalog = sealed_catalog([{
                "kind": "conversation", "original_thread_id": "t1", "original_title": "task",
                "conversation_sha256": sha("a"), "messages_sha256": sha("b"),
                "message_count": 2, "attachment_count": 0, "project_id": "p1", "project_label": "P",
            }, {
                "kind": "project-bootstrap", "source_project_id": "source-p1",
                "original_thread_id": bootstrap_id,
                "conversation_sha256": module.bootstrap_sha256("WORKSTATION-A", "source-p1"),
                "messages_sha256": None, "message_count": 0, "attachment_count": 0,
                "project_id": "p1", "project_label": "P",
            }])
        plan = module.build_plan(receiving_catalog, {"format": module.LEDGER_FORMAT, "entries": {}}, {})
        self.assertEqual(len(plan["decisions"]), 2)
        self.assertEqual({row["original_thread_id"] for row in plan["decisions"]}, {"t1", bootstrap_id})

    def test_project_bootstrap_is_ledgered_once_and_manual_archive_is_preserved(self):
        source_project_id = "source-p1"
        identity = module.bootstrap_identity(source_project_id)
        digest = module.bootstrap_sha256("WORKSTATION-A", source_project_id)
        receiving_catalog = sealed_catalog([{
                "kind": "project-bootstrap", "source_project_id": source_project_id,
                "original_thread_id": identity, "original_title": None,
                "conversation_sha256": digest, "messages_sha256": None,
                "message_count": 0, "attachment_count": 0,
                "project_id": "p1", "project_label": "P",
            }])
        empty = {"format": module.LEDGER_FORMAT, "entries": {}}
        first = module.build_plan(receiving_catalog, empty, {})
        self.assertEqual(first["decisions"][0]["action"], "register-new")
        key = first["decisions"][0]["source_key"]
        committed = module.commit_plan(first, empty, results_for(first, [{
            "source_key": key, "status": "applied", "receiving_thread_id": "bootstrap-r1",
            "receiver_disposition": "active",
        }]))["ledger"]
        archived_state = {identity: {
            "receiving_thread_id": "bootstrap-r1", "receiver_disposition": "archived",
        }}
        second = module.build_plan(receiving_catalog, committed, archived_state)
        self.assertEqual(second["decisions"][0]["action"], "skip-unchanged")
        self.assertEqual(second["decisions"][0]["receiver_disposition"], "archived")
        committed_again = module.commit_plan(
            second, committed, {"plan_sha256": second["plan_sha256"], "results": []},
        )["ledger"]
        third = module.build_plan(receiving_catalog, committed_again, archived_state)
        self.assertEqual(third["decisions"][0]["action"], "skip-unchanged")
        self.assertEqual(third["decisions"][0]["receiving_thread_id"], "bootstrap-r1")

    def test_source_removal_never_deletes_receiver(self):
        key = module.source_key("WORKSTATION-A", "t1")
        ledger = {"format": module.LEDGER_FORMAT, "entries": {key: {
            "source_device": "A设备", "source_computer": "WORKSTATION-A",
            "original_thread_id": "t1", "conversation_sha256": sha("a"),
            "receiving_thread_id": "r1", "receiver_disposition": "archived",
        }}}
        plan = module.build_plan(catalog([]), ledger, states_from_ledger(ledger))
        self.assertEqual(plan["source_removed"][0]["action"], "retain-local-no-source-delete")


if __name__ == "__main__":
    unittest.main()
