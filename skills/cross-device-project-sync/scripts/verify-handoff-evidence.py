"""Check citation text and file evidence; semantic approval remains the controlling task's job."""

import argparse
import json
from pathlib import Path
import sys

from conversation_history import (directory_access, tree_attachment_records,
                                  tree_statistics, validate_tree)


SECTIONS = ("chronology", "decisions_and_rationale", "corrections_and_rejections",
            "current_state", "unresolved_work", "user_constraints")


def verify(root, report, expected_release_tag=None, expected_project_manifest=None,
           expected_project_root=None, expected_receiving_thread=None):
    names, read, digest = directory_access(root)
    metadata = validate_tree(names, read, digest)
    statistics = tree_statistics(names, read)
    attachment_records = tree_attachment_records(names, read)
    if report.get("thread_id") != metadata["thread_id"]:
        raise ValueError("Report identifies the wrong current task.")
    if report.get("project_manifest_sha256") != metadata.get("project_manifest_sha256"):
        raise ValueError("Report identifies the wrong project snapshot.")
    if expected_project_manifest and report.get("project_manifest_sha256") != expected_project_manifest.lower():
        raise ValueError("Report does not match the independently confirmed project manifest.")
    if report.get("conversation_cutoff") != metadata.get("cutoff_timestamp"):
        raise ValueError("Report identifies the wrong conversation cutoff.")
    if report.get("conversation_source_device") != metadata.get("conversation_source_device"):
        raise ValueError("Report identifies the wrong conversation source device.")
    if report.get("read_only") is not True:
        raise ValueError("Report must confirm read-only project inspection.")
    if (report.get("lineage_task_count") != statistics["task_count"]
            or report.get("lineage_message_count") != statistics["message_count"]
            or report.get("lineage_attachment_count") != statistics["attachment_count"]):
        raise ValueError("Report lineage counts do not match the verified history.")
    if expected_release_tag and report.get("release_tag") != expected_release_tag:
        raise ValueError("Report identifies the wrong Release tag.")
    if expected_project_root and Path(report.get("restored_project_root", "")).resolve() != Path(expected_project_root).resolve():
        raise ValueError("Report identifies the wrong restored project root.")
    if expected_receiving_thread and report.get("receiving_thread_id") != expected_receiving_thread:
        raise ValueError("Report identifies the wrong receiving task.")
    for section in SECTIONS:
        if not isinstance(report.get(section), str) or not report[section].strip():
            raise ValueError("Missing report section: " + section)
    evidence = report.get("files", [])
    checked = set()
    for item in evidence:
        path = item["path"]
        if path not in names or digest(path) != (item["size"], item["sha256"]):
            raise ValueError("Report file evidence mismatch: " + path)
        checked.add(path)
    transcripts = {n for n in names if n.endswith("FULL_TRANSCRIPT.jsonl")}
    handoffs = {n for n in names if n.endswith("DETAILED_HANDOFF.md")}
    attachment_paths = {item["path"] for item in attachment_records}
    if not (transcripts | handoffs | attachment_paths).issubset(checked):
        raise ValueError("Report must include file evidence for every inherited transcript, handoff, and attachment.")
    attachment_evidence = report.get("attachments")
    if not isinstance(attachment_evidence, list):
        raise ValueError("Report must include an attachments array, including an empty array when none exist.")
    expected_attachments = {item["path"]: item for item in attachment_records}
    reported_attachments = set()
    for item in attachment_evidence:
        path = item.get("path")
        source = expected_attachments.get(path)
        if not source or path in reported_attachments:
            raise ValueError("Attachment evidence is unknown or duplicated: " + str(path))
        if item.get("transcript") != source["transcript"] or item.get("sequence") != source["sequence"]:
            raise ValueError("Attachment evidence identifies the wrong source message: " + path)
        if item.get("inspection_status") != "inspected":
            raise ValueError("Every retained attachment must be inspected before continuation: " + path)
        if not isinstance(item.get("observation"), str) or len(item["observation"].strip()) < 12:
            raise ValueError("Attachment evidence requires a substantive observation: " + path)
        reported_attachments.add(path)
    if reported_attachments != attachment_paths:
        raise ValueError("Attachment evidence does not cover every retained attachment.")
    # Resolve all citations against exact source records, not merely valid sequence numbers.
    citations = report.get("citations", [])
    if not citations:
        raise ValueError("Report has no citations.")
    for item in citations:
        if item.get("transcript") not in transcripts:
            raise ValueError("Citation references an unknown transcript.")
        if not isinstance(item.get("sequence"), int) or isinstance(item["sequence"], bool):
            raise ValueError("Citation sequence must be an integer.")
        if not isinstance(item.get("quote"), str) or not item["quote"].strip():
            raise ValueError("Citation requires an exact quote.")
        if not isinstance(item.get("claim"), str) or len(item["claim"].strip()) < 8:
            raise ValueError("Citation requires the claim it supports.")
    for path in transcripts:
        # JSONL records are separated only by LF. ``str.splitlines()`` also
        # treats valid in-string Unicode separators (U+2028/U+2029) as record
        # boundaries and would corrupt otherwise valid transcripts.
        records = [json.loads(line.decode("utf-8")) for line in read(path).split(b"\n") if line.strip()]
        text_records = {r["sequence"]: r for r in records if any(
            p.get("type") == "text" and p.get("text", "").strip() for p in r["content"])}
        selected = [item for item in citations if item["transcript"] == path]
        covered = set()
        for item in selected:
            record = text_records.get(item["sequence"])
            source_texts = [p.get("text", "") for p in record["content"] if p.get("type") == "text"] if record else []
            matching = [text for text in source_texts if item["quote"] in text]
            if not matching:
                raise ValueError("Citation quote does not match source: " + path)
            quote_length = len(item["quote"].strip())
            if not any(quote_length >= min(12, len(text.strip())) for text in matching):
                raise ValueError("Citation quote is too short to provide meaningful evidence: " + path)
            covered.add(item["sequence"])
        if len(covered) < min(3, len(text_records)):
            raise ValueError("Insufficient citation coverage: " + path)
        roles = {r["role"] for r in text_records.values()}
        if {text_records[s]["role"] for s in covered} != roles:
            raise ValueError("Citation coverage omits a source role: " + path)
    return {"file_quote_and_attachment_checks_passed": True, "semantic_review_required": True,
            "continuation_complete": False, "transcript_count": len(transcripts),
            "attachment_count": len(attachment_records)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("handoff_directory")
    parser.add_argument("report_json")
    parser.add_argument("--expected-release-tag", required=True)
    parser.add_argument("--expected-project-manifest-sha256", required=True)
    parser.add_argument("--expected-project-root", required=True)
    parser.add_argument("--expected-receiving-thread-id", required=True)
    args = parser.parse_args()
    try:
        report = json.loads(Path(args.report_json).read_text(encoding="utf-8-sig"))
        expected_manifest = args.expected_project_manifest_sha256
        if expected_manifest and (len(expected_manifest) != 64 or any(c not in "0123456789abcdefABCDEF" for c in expected_manifest)):
            raise ValueError("Expected project manifest SHA-256 is invalid.")
        print(json.dumps(verify(Path(args.handoff_directory), report,
                                args.expected_release_tag, expected_manifest,
                                args.expected_project_root, args.expected_receiving_thread_id)))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("ERROR: " + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
