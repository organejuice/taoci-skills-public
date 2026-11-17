"""Project-scoped offline MIME preparation and explicitly authorized SMTP/IMAP sending.

prepare/check never connect. auth set uses the user's own hidden terminal input.
probe/duplicates/reconcile are read-only mailbox operations. send defaults to
protocol validation of source-bound MIME and requires an actual user instruction.
Optional client mode additionally requires a real-client presentation review
bound to each exact bundle and EML hash. Protocol validation is not a visual review.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import imaplib
import os
import re
import smtplib
import ssl
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.dont_write_bytecode = True
import send_preflight as preflight
import mail_compose as compose
import mail_credentials as credentials
import mail_transport as transport


class WorkflowError(ValueError):
    pass


PRESENTATION_CHECKS = (
    "body_visible", "greeting_direction_correct", "formatting_correct",
    "recipient_correct", "subject_correct", "attachment_name_correct",
    "attachment_content_correct",
)


def _transport_options(config):
    """Only explicit TLS endpoint options are forwarded from local configuration."""
    options = config.get("transport", {})
    keys = {"smtp_host", "smtp_port", "imap_host", "imap_port"}
    if not isinstance(options, dict) or set(options).difference(keys):
        raise WorkflowError("Invalid transport configuration")
    return dict(options)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def _review_path(config, path):
    target = Path(path).expanduser().resolve(strict=True)
    if not target.is_relative_to(Path(config["work_dir"]).resolve()):
        raise WorkflowError("The presentation review must be inside work_dir")
    if not target.is_file():
        raise WorkflowError("The presentation review must be a file")
    return target


def review_bundle(config, item, review_path=None):
    """Validate a recorded real-client observation; never create or infer one.

    A self-only draft may supply display evidence when its body/HTML/PDF match,
    but recipient_correct still describes the final production bundle. Optional
    test_message_id, test_target and evidence_provenance record that distinction.
    This declaration does not grant sending authority.
    """
    path = _review_path(config, review_path or Path(item["bundle_path"]).parent / "presentation-review.json")
    fingerprint = preflight.fingerprint(path)
    review = preflight._json(path)
    if preflight.fingerprint(path) != fingerprint:
        raise WorkflowError("Presentation review changed while being read")
    if review.get("advisor") != item["plan"]["advisor"]:
        raise WorkflowError("Presentation review advisor does not match the bundle")
    if review.get("bundle_sha256") != preflight.fingerprint(item["bundle_path"])["sha256"]:
        raise WorkflowError("Presentation review does not cover the exact bundle; perform a new real-client review")
    if review.get("eml_sha256") != hashlib.sha256(item["eml_bytes"]).hexdigest():
        raise WorkflowError("Presentation review does not cover the exact EML; perform a new real-client review")
    for key in ("reviewed_by", "reviewed_at", "client", "evidence"):
        if not isinstance(review.get(key), str) or not review[key].strip():
            raise WorkflowError(f"Presentation review requires a nonempty {key}")
    try:
        stamp = datetime.fromisoformat(review["reviewed_at"])
    except ValueError as exc:
        raise WorkflowError("Presentation reviewed_at must be an ISO 8601 time with a timezone") from exc
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise WorkflowError("Presentation reviewed_at must be an ISO 8601 time with a timezone")
    checks = review.get("checks")
    if not isinstance(checks, dict) or any(checks.get(key) is not True for key in PRESENTATION_CHECKS):
        raise WorkflowError("Every presentation check requires an explicit true from a real-client observer")
    return {**review, "review_path": str(path), "review_sha256": fingerprint["sha256"]}


def _review_paths(config, checked, reviews):
    if reviews is None:
        return {item["plan"]["advisor"]: Path(item["bundle_path"]).parent / "presentation-review.json"
                for item in checked}
    indexed = {}
    # Explicit files are mapped by their recorded advisor, never by list order.
    for candidate in reviews:
        path = _review_path(config, candidate)
        advisor = preflight._json(path).get("advisor")
        if not isinstance(advisor, str) or not advisor or advisor in indexed:
            raise WorkflowError("Explicit presentation reviews require distinct advisor IDs")
        indexed[advisor] = path
    if set(indexed) != {item["plan"]["advisor"] for item in checked}:
        raise WorkflowError("Explicit presentation reviews must cover exactly the selected advisors")
    return indexed


def _state_dir(config):
    root = Path(config["work_dir"]).resolve()
    target = (root / "transport-state").resolve()
    if not target.is_relative_to(root):
        raise WorkflowError("Transport state resolves outside work_dir")
    return target


@contextmanager
def state_lock(config):
    directory = _state_dir(config)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "send.lock"
    token = str(uuid.uuid4())
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise WorkflowError("Another send/reconcile run holds send.lock; inspect its process and ledger before removing a stale lock") from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "token": token, "created_utc": utc_now()}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        yield
    finally:
        if path.exists() and preflight._json(path).get("token") == token:
            path.unlink()


def read_events(config):
    path = _state_dir(config) / "attempts.jsonl"
    if not path.exists():
        return []
    events = []
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip() or not line.endswith("\n"):
                raise WorkflowError(f"Ledger has an empty or partial record at line {number}; reconcile before continuing")
            try:
                event = json.loads(line)
            except ValueError as exc:
                raise WorkflowError(f"Ledger record {number} is incomplete; reconcile before continuing") from exc
            if not isinstance(event, dict) or not all(event.get(k) for k in ("attempt", "status", "sender", "to", "subject")):
                raise WorkflowError(f"Ledger record {number} is invalid")
            events.append(event)
    return events


def append_event(config, common, status, result=None):
    # No password, MIME body or unrelated mailbox content is admitted to logs.
    evidence = {k: v for k, v in (result or {}).items() if k in {
        "error_code", "smtp_code", "smtp_response", "smtp_accepted", "sent_uid",
        "folder", "copy_verified", "copy_appended", "duplicate_uids", "reason", "phase",
    }}
    event = {**common, "status": status, "at_utc": utc_now(), "evidence": evidence}
    path = _state_dir(config) / "attempts.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return event


def prior_state(events, plan):
    relevant = [e for e in events if (e["sender"].casefold(), e["to"].casefold(), e["subject"]) ==
                (plan["sender"].casefold(), plan["to"].casefold(), plan["subject"])]
    grouped = {}
    for event in relevant:
        grouped.setdefault(event["attempt"], []).append(event)
    unresolved = {"data_started", "unknown", "unknown_copy_verified"}
    accepted = {"smtp_accepted", "accepted_confirmed", "accepted_unconfirmed", "copy_failed"}
    for history in grouped.values():
        last = history[-1]["status"]
        had_accepted = any(e["status"] in accepted for e in history)
        if last in unresolved and not had_accepted:
            return "unknown"
        if last == "attempt_started":
            # A crash could have occurred before the durable DATA marker.
            return "incomplete_before_data"
    if any(e["status"] in accepted for e in relevant):
        return "accepted"
    return "clear"


def validate_authorization(config, auth_path, checked, copy_mode):
    path = Path(auth_path).resolve(strict=True)
    if not path.is_relative_to(Path(config["work_dir"]).resolve()):
        raise WorkflowError("The authorization record must be inside work_dir")
    auth = preflight._json(path)
    if auth.get("action") != "send" or auth.get("sender") != config["sender"]:
        raise WorkflowError("Authorization action/sender does not match")
    if not all(isinstance(auth.get(k), str) and auth[k].strip() for k in ("user_instruction", "instruction_reference")):
        raise WorkflowError("Record the actual user sending instruction and its chat reference; preparation is not sending authorization")
    if type(auth.get("allow_resend", False)) is not bool or type(auth.get("save_sent_copy", False)) is not bool:
        raise WorkflowError("Authorization flags must be booleans")
    if copy_mode == "append_if_missing" and auth.get("save_sent_copy") is not True:
        raise WorkflowError("Appending a Sent copy is not included in this authorization record")
    allowed = auth.get("allowed")
    if not isinstance(allowed, list) or not allowed:
        raise WorkflowError("Authorization has no explicit reviewed bundles")
    indexed = {}
    for row in allowed:
        if not isinstance(row, dict) or not all(isinstance(row.get(k), str) and row[k] for k in ("advisor", "to", "bundle_sha256")):
            raise WorkflowError("Invalid authorization recipient/bundle entry")
        key = row["advisor"]
        if key in indexed:
            raise WorkflowError("Authorization includes a duplicate advisor")
        indexed[key] = row
    for item in checked:
        plan = item["plan"]
        row = indexed.get(plan["advisor"])
        digest = preflight.fingerprint(item["bundle_path"])["sha256"]
        if row is None or row["to"] != plan["to"] or row["bundle_sha256"] != digest:
            raise WorkflowError("Sending authorization does not cover the exact recipient and reviewed bundle")
    return auth, preflight.fingerprint(path)


def _secret(config, store=None):
    secret = (store or credentials.WindowsCredentialStore()).read(config["sender"])
    if secret is None:
        raise credentials.CredentialError("No project authorization code is stored. Run auth set in your own local terminal first")
    if not isinstance(secret, str) or not secret:
        raise credentials.CredentialError("The project credential is empty")
    return secret


def _common(item, authorization=None, attempt=None, presentation_review=None, validation_mode=None):
    plan = item["plan"]
    common = {
        "attempt": attempt or str(uuid.uuid4()), "advisor": plan["advisor"],
        "sender": plan["sender"], "to": plan["to"], "subject": plan["subject"],
        "message_id": item["bundle"]["message_id"],
        "bundle": preflight.fingerprint(item["bundle_path"]),
        "authorization": authorization,
        "presentation_review": presentation_review,
    }
    if validation_mode is not None:
        common["validation_mode"] = validation_mode
    return common


def send_bundles(config_path, bundle_paths, auth_path, *, copy_mode="verify", sent_folder=None,
                 review_mode="protocol", reviews=None, store=None, transport_factory=None):
    config, _ = preflight.load_config(config_path)
    if copy_mode not in {"verify", "append_if_missing"}:
        raise WorkflowError("Unknown Sent copy mode")
    if review_mode not in {"protocol", "client"}:
        raise WorkflowError("Review mode must be protocol or client")
    if review_mode != "client" and reviews is not None:
        raise WorkflowError("Explicit presentation reviews require review_mode=client (--review-mode client)")
    checked = [compose.check_bundle(config_path, path) for path in bundle_paths]
    if not checked or len({i["plan"]["advisor"] for i in checked}) != len(checked):
        raise WorkflowError("Select a nonempty batch of distinct explicit M IDs")
    # Protocol mode never inspects or fabricates a client-observation record.
    # Source-bound MIME and authorization checks remain mandatory in both modes.
    review_paths = _review_paths(config, checked, reviews) if review_mode == "client" else {}
    reviewed = {item["plan"]["advisor"]: review_bundle(config, item, review_paths[item["plan"]["advisor"]])
                for item in checked} if review_mode == "client" else {}
    auth, auth_fingerprint = validate_authorization(config, auth_path, checked, copy_mode)

    def recheck(old_item):
        item = compose.check_bundle(config_path, old_item["bundle_path"])
        advisor = item["plan"]["advisor"]
        if review_mode == "client":
            review = review_bundle(config, item, review_paths[advisor])
            if review != reviewed[advisor]:
                raise WorkflowError("Presentation review changed during the batch; perform a new real-client review")
        validate_authorization(config, auth_path, [item], copy_mode)
        if preflight.fingerprint(auth_path) != auth_fingerprint:
            raise WorkflowError("Authorization record changed during the batch")
        return item

    results = []
    factory = transport_factory or transport.MailTransport
    with state_lock(config):
        events = read_events(config)
        pending = []
        # Scope and durable-state failures block before credentials or network.
        for item in checked:
            state = prior_state(events, item["plan"])
            if state in {"unknown", "incomplete_before_data"}:
                raise WorkflowError("An unfinished/unknown attempt exists; reconcile it before sending")
            if state == "accepted" and auth.get("allow_resend", False):
                if any(event.get("message_id") == item["bundle"]["message_id"]
                       and event["status"] in {"smtp_accepted", "accepted_confirmed", "accepted_unconfirmed", "copy_failed"}
                       for event in events):
                    raise WorkflowError("A resend requires a newly prepared bundle and Message-ID, even when the previous Sent copy is missing")
            if state == "accepted" and not auth.get("allow_resend", False):
                results.append({"advisor": item["plan"]["advisor"], "status": "duplicate",
                                "smtp_accepted": True, "reason": "local_smtp_acceptance_already_recorded",
                                "validation_mode": review_mode})
            else:
                pending.append(item)
        if not pending:
            return results
        # Revalidate the whole batch before acquiring credentials or connecting.
        pending = [recheck(item) for item in pending]
        secret = _secret(config, store)
        with factory(config["sender"], secret, copy_mode=copy_mode, sent_folder=sent_folder, **_transport_options(config)) as session:
            for old_item in pending:
                # Re-read immediately before the per-message operation.
                item = recheck(old_item)
                review = reviewed.get(item["plan"]["advisor"])
                common = _common(item, auth_fingerprint, presentation_review={
                    "path": review["review_path"], "sha256": review["review_sha256"],
                } if review is not None else None, validation_mode=review_mode)
                append_event(config, common, "attempt_started")
                data_started = False
                smtp_accepted = False

                def before_data(evidence):
                    nonlocal data_started
                    current = recheck(item)
                    if current["eml_bytes"] != item["eml_bytes"]:
                        raise WorkflowError("EML changed before DATA; prepare and verify again")
                    append_event(config, common, "data_started", evidence)
                    data_started = True

                def on_accepted(evidence):
                    nonlocal smtp_accepted
                    # If the disk write fails, data_started remains a durable
                    # stop marker, and this process must not attempt a resend.
                    smtp_accepted = True
                    append_event(config, common, "smtp_accepted", evidence)

                try:
                    result = session.send(
                        item["eml_bytes"], item["plan"]["to"], item["plan"]["subject"],
                        before_data=before_data, on_accepted=on_accepted,
                        allow_resend=auth.get("allow_resend", False),
                    )
                except BaseException:
                    status = "accepted_unconfirmed" if smtp_accepted else "unknown" if data_started else "failed_before_data"
                    append_event(config, common, status, {"smtp_accepted": smtp_accepted, "reason": "operation_interrupted"})
                    raise
                status = result.get("status")
                if status not in {"accepted_confirmed", "accepted_unconfirmed", "rejected", "unknown", "blocked", "copy_failed", "duplicate"}:
                    status = "accepted_unconfirmed" if smtp_accepted else "unknown" if data_started else "failed_before_data"
                    result = {"status": status, "error_code": "unexpected_transport_result", "smtp_accepted": smtp_accepted}
                append_event(config, common, status, result)
                results.append({"advisor": item["plan"]["advisor"], **result, "validation_mode": review_mode})
                if status not in {"accepted_confirmed", "duplicate"}:
                    break  # No automatic retry, concurrency or rate-limit bypass.
    return results


def reconcile_bundle(config_path, bundle_path, *, sent_folder=None, store=None, transport_factory=None):
    config, _ = preflight.load_config(config_path)
    item = compose.check_bundle(config_path, bundle_path)
    with state_lock(config):
        events = read_events(config)
        matching = [e for e in events if e.get("message_id") == item["bundle"]["message_id"]]
        if not matching:
            raise WorkflowError("No recorded attempt exists for this bundle; reconciliation is not a send command")
        accepted = any(e["status"] in {"smtp_accepted", "accepted_confirmed", "accepted_unconfirmed", "copy_failed"} for e in matching)
        with (transport_factory or transport.MailTransport)(config["sender"], _secret(config, store),
                copy_mode="verify", sent_folder=sent_folder, **_transport_options(config)) as session:
            result = session.reconcile(item["bundle"]["message_id"], item["plan"]["to"],
                                       item["plan"]["subject"], prior_status="smtp_accepted" if accepted else "unknown",
                                       expected_message=item["eml_bytes"])
        status = "accepted_confirmed" if accepted and result.get("status") in {"copy_verified", "accepted_confirmed"} else "accepted_unconfirmed" if accepted else "unknown_copy_verified" if result.get("status") == "copy_verified" else "unknown"
        common = _common(item, matching[-1].get("authorization"), matching[-1]["attempt"],
                         matching[-1].get("presentation_review"), matching[-1].get("validation_mode"))
        append_event(config, common, status, result)
        return {**result, "status": status, "smtp_accepted": accepted,
                **({"validation_mode": common["validation_mode"]} if "validation_mode" in common else {})}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=preflight.DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare", help="Offline: create HTML/EML bundles for explicit M IDs")
    prepare.add_argument("--ids", nargs="+", required=True)
    prepare.add_argument("--out", type=Path, required=True, help="New/empty batch directory within work_dir")
    check = sub.add_parser("check", help="Offline: revalidate every source, HTML, MIME and attachment")
    check.add_argument("--bundle", type=Path, required=True)
    auth = sub.add_parser("auth", help="Only this project's Windows credential target")
    auth.add_argument("action", choices=["status", "set", "remove"])
    auth.add_argument("--replace", action="store_true")
    for command in ("probe", "duplicates", "reconcile", "send"):
        child = sub.add_parser(command)
        child.add_argument("--sent-folder", help="Exact existing Sent folder name if automatic discovery is ambiguous")
        if command in {"duplicates", "reconcile"}:
            child.add_argument("--bundle", type=Path, required=True)
        if command == "send":
            child.add_argument("--bundles", nargs="+", type=Path, required=True)
            child.add_argument("--review-mode", choices=["protocol", "client"], default="protocol",
                               help="Default: protocol checks without UI; client additionally requires actual-client review records")
            child.add_argument("--reviews", nargs="+", type=Path,
                               help="Client mode only: review JSON files mapped by advisor; default in client mode: each bundle's presentation-review.json")
            child.add_argument("--authorization", type=Path, required=True)
            child.add_argument("--copy-mode", choices=["verify", "append_if_missing"], default="verify")
    args = parser.parse_args(argv)
    try:
        config, _ = preflight.load_config(args.config)
        if args.command == "prepare":
            if len(args.ids) != len(set(args.ids)):
                raise WorkflowError("Duplicate M IDs in the batch")
            output = preflight._output(config, args.out)
            result = [str(compose.prepare_bundle(args.config, advisor, output / advisor)) for advisor in args.ids]
        elif args.command == "check":
            item = compose.check_bundle(args.config, args.bundle)
            result = {"status": "offline_bundle_verified", "advisor": item["plan"]["advisor"], "message_id": item["bundle"]["message_id"]}
        elif args.command == "auth":
            vault = credentials.WindowsCredentialStore()
            if args.action == "set":
                credentials.prompt_and_store(config["sender"], replace=args.replace, store=vault)
                result = {"credential_stored": True, "target": credentials.target_name(config["sender"])}
            elif args.action == "remove":
                result = {"removed": vault.delete(config["sender"])}
            else:
                result = {"credential_present": vault.read(config["sender"]) is not None, "target": credentials.target_name(config["sender"])}
        elif args.command == "send":
            result = send_bundles(args.config, args.bundles, args.authorization, copy_mode=args.copy_mode,
                                  sent_folder=args.sent_folder, review_mode=args.review_mode, reviews=args.reviews)
        elif args.command == "reconcile":
            result = reconcile_bundle(args.config, args.bundle, sent_folder=args.sent_folder)
        elif args.command == "probe":
            # probe owns its connection/error result; __enter__ would connect
            # first and bypass that classification on an IMAP login failure.
            session = transport.MailTransport(config["sender"], _secret(config), sent_folder=args.sent_folder, **_transport_options(config))
            try:
                result = session.probe()
            finally:
                session.close()
        else:
            item = compose.check_bundle(args.config, args.bundle)
            with transport.MailTransport(config["sender"], _secret(config), sent_folder=args.sent_folder, **_transport_options(config)) as session:
                result = session.duplicate(item["plan"]["to"], item["plan"]["subject"])
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if args.command == "send" and any(r.get("status") not in {"accepted_confirmed", "duplicate"} for r in result):
            return 3
        if args.command == "probe" and result.get("status") != "ready":
            return 2
        if args.command == "duplicates" and result.get("status") == "blocked":
            return 2
        return 0
    except KeyboardInterrupt:
        print("Stopped. Check the durable ledger before any resend.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Authentication exceptions can contain arbitrary server text. Report
        # only their class/code; local validation errors contain no credentials.
        if isinstance(exc, transport.TransportError):
            code = getattr(exc, "code", str(exc))
            if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,100}", code):
                code = "transport_failed"
            info = {"status": "blocked", "error_code": code}
            phase = getattr(exc, "phase", None)
            if isinstance(phase, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,100}", phase):
                info["phase"] = phase
            smtp_code = getattr(exc, "smtp_code", None)
            if isinstance(smtp_code, int):
                info["smtp_code"] = smtp_code
            detail = json.dumps(info, ensure_ascii=False)
        elif isinstance(exc, (smtplib.SMTPException, imaplib.IMAP4.error, ssl.SSLError)):
            detail = f"{type(exc).__name__}; smtp_code={getattr(exc, 'smtp_code', 'unavailable')}"
        elif isinstance(exc, (ValueError, OSError)):
            detail = str(exc)
        else:
            detail = f"{type(exc).__name__}; code={getattr(exc, 'smtp_code', 'unavailable')}"
        print(f"mail_workflow_failed: {detail}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
