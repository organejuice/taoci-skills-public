"""Protocol-default, optional client review, authorization and durable-state tests.

These fixtures do not claim that a human reviewed any real application email.
No credential vault or live service is used.
"""
import ctypes
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import mail_workflow as workflow
import mail_credentials as credentials


class FakeStore:
    def __init__(self):
        self.calls = 0

    def read(self, account):
        self.calls += 1
        return "TESTONLYCODE1234"


class FakeTransport:
    results = []
    calls = []
    instances = 0
    interrupt = False
    reconcile_status = "copy_verified"

    def __init__(self, sender, password, **options):
        type(self).instances += 1
        self.sender = sender

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def send(self, message, to, subject, *, before_data, on_accepted, allow_resend):
        type(self).calls.append(to)
        before_data({"phase": "before_data"})
        if type(self).interrupt:
            raise KeyboardInterrupt
        result = type(self).results.pop(0)
        if result["status"].startswith("accepted") or result["status"] == "copy_failed":
            on_accepted({"smtp_accepted": True, "smtp_code": 250})
        return result

    def reconcile(self, *args, **kwargs):
        return {"status": type(self).reconcile_status, "sent_uid": "7", "smtp_accepted": None}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix=".mail-workflow-test-", dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {"sender": "sender@example.invalid", "work_dir": str(self.root)}
        self.items = {}
        self.review_paths = []
        for number in (1, 4):
            advisor = f"M{number:03d}"
            directory = self.root / advisor
            directory.mkdir()
            path = directory / "bundle.json"
            path.write_text(json.dumps({"advisor": advisor}), encoding="utf-8")
            self.items[path] = {
                "bundle_path": path, "bundle": {"message_id": f"<{advisor}@example.invalid>",
                                                 "created_at": workflow.utc_now()},
                "plan": {"advisor": advisor, "sender": "sender@example.invalid",
                         "to": f"teacher{number}@example.invalid", "subject": "2030申请"},
                "eml_bytes": b"fixture MIME (transport itself is separately tested)",
            }
            self.review_paths.append(directory / "presentation-review.json")
        self.paths = list(self.items)
        self.auth = self.root / "authorization.json"
        self.write_auth()
        for path in self.paths:
            self.write_review(path)
        self.load = mock.patch.object(workflow.preflight, "load_config", return_value=(self.config, self.root / "config.json"))
        self.check = mock.patch.object(workflow.compose, "check_bundle", side_effect=lambda config, path: self.items[Path(path)])
        self.load.start()
        self.check.start()
        self.addCleanup(self.load.stop)
        self.addCleanup(self.check.stop)
        self.network = [mock.patch("socket.create_connection", side_effect=AssertionError("network forbidden")),
                        mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden"))]
        for patcher in self.network:
            patcher.start()
            self.addCleanup(patcher.stop)
        FakeTransport.results = []
        FakeTransport.calls = []
        FakeTransport.instances = 0
        FakeTransport.interrupt = False
        FakeTransport.reconcile_status = "copy_verified"
        self.store = FakeStore()

    def write_auth(self, **changes):
        value = {"action": "send", "sender": self.config["sender"],
                 "user_instruction": "Synthetic test instruction only", "instruction_reference": "test-fixture",
                 "allow_resend": False, "save_sent_copy": False,
                 "allowed": [{"advisor": i["plan"]["advisor"], "to": i["plan"]["to"],
                              "bundle_sha256": workflow.preflight.fingerprint(p)["sha256"]}
                             for p, i in self.items.items()]}
        value.update(changes)
        self.auth.write_text(json.dumps(value), encoding="utf-8")

    def write_review(self, bundle_path, review_path=None, **changes):
        item = self.items[bundle_path]
        value = {"advisor": item["plan"]["advisor"],
                 "bundle_sha256": workflow.preflight.fingerprint(bundle_path)["sha256"],
                 "eml_sha256": workflow.hashlib.sha256(item["eml_bytes"]).hexdigest(),
                 "reviewed_by": "SYNTHETIC TEST OBSERVER - NOT A REAL REVIEW",
                 "reviewed_at": "2000-01-01T09:00:00+08:00", "client": "示例邮件客户端",
                 "evidence": "Synthetic fixture only; no real message was opened or sent",
                 "checks": {key: True for key in workflow.PRESENTATION_CHECKS}}
        value.update(changes)
        target = review_path or bundle_path.parent / "presentation-review.json"
        target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return target

    def run_send(self, paths=None, **kwargs):
        # Existing visual-review regressions intentionally exercise opt-in mode.
        kwargs.setdefault("review_mode", "client")
        return workflow.send_bundles(self.root / "config.json", paths or self.paths, self.auth,
                                    store=self.store, transport_factory=FakeTransport, **kwargs)

    def assert_no_credentials_or_network(self):
        self.assertEqual(self.store.calls, 0)
        self.assertEqual(FakeTransport.instances, 0)

    def test_default_protocol_sends_without_client_reviews_or_visual_claims(self):
        for path in self.review_paths:
            path.unlink()
        FakeTransport.results = [{"status": "accepted_confirmed", "smtp_accepted": True}] * 2
        with mock.patch.object(workflow, "review_bundle", side_effect=AssertionError("UI reviews forbidden in protocol mode")), \
                mock.patch.object(workflow, "_review_paths", side_effect=AssertionError("UI paths forbidden in protocol mode")):
            results = workflow.send_bundles(self.root / "config.json", self.paths, self.auth,
                                            store=self.store, transport_factory=FakeTransport)
        self.assertEqual([result["validation_mode"] for result in results], ["protocol", "protocol"])
        self.assertEqual(FakeTransport.calls, ["teacher1@example.invalid", "teacher4@example.invalid"])
        events = workflow.read_events(self.config)
        self.assertTrue(all(event["validation_mode"] == "protocol" for event in events))
        self.assertTrue(all(event["presentation_review"] is None for event in events))
        self.assertTrue(all(not path.exists() for path in self.review_paths))
        for event in events:
            self.assertFalse(set(event) & {"checks", "client", "body_visible", "formatting_correct"})

    def test_protocol_does_not_read_or_rewrite_existing_stale_review(self):
        self.review_paths[0].write_text("not even JSON", encoding="utf-8")
        FakeTransport.results = [{"status": "accepted_confirmed", "smtp_accepted": True}]
        self.run_send([self.paths[0]], review_mode="protocol")
        self.assertEqual(self.review_paths[0].read_text(encoding="utf-8"), "not even JSON")
        self.assertIsNone(workflow.read_events(self.config)[-1]["presentation_review"])

    def test_protocol_rejects_explicit_reviews_and_unknown_modes(self):
        with self.assertRaisesRegex(workflow.WorkflowError, "require review_mode=client"):
            self.run_send(review_mode="protocol", reviews=self.review_paths)
        with self.assertRaisesRegex(workflow.WorkflowError, "protocol or client"):
            self.run_send(review_mode="skip")
        self.assert_no_credentials_or_network()

    def test_protocol_keeps_authorization_scope_and_append_checks_before_credentials(self):
        self.auth.unlink()
        with self.assertRaises(OSError):
            self.run_send(review_mode="protocol")
        self.write_auth(user_instruction="")
        with self.assertRaises(workflow.WorkflowError):
            self.run_send(review_mode="protocol")
        self.write_auth(allowed=[])
        with self.assertRaises(workflow.WorkflowError):
            self.run_send(review_mode="protocol")
        self.write_auth()
        auth = json.loads(self.auth.read_text())
        auth["allowed"][0]["to"] = "wrong@example.invalid"
        self.auth.write_text(json.dumps(auth))
        with self.assertRaises(workflow.WorkflowError):
            self.run_send(review_mode="protocol")
        self.write_auth()
        with self.assertRaises(workflow.WorkflowError):
            self.run_send(review_mode="protocol", copy_mode="append_if_missing")
        self.paths[0].write_text("a different bundle")
        with self.assertRaises(workflow.WorkflowError):
            self.run_send(review_mode="protocol")
        self.assert_no_credentials_or_network()

    def test_protocol_still_requires_source_and_wire_check_before_credentials(self):
        with mock.patch.object(workflow.compose, "check_bundle", side_effect=ValueError("source or MIME changed")):
            with self.assertRaisesRegex(ValueError, "source or MIME changed"):
                self.run_send(review_mode="protocol")
        self.assert_no_credentials_or_network()

    def test_protocol_source_change_before_data_prevents_data_marker(self):
        def changing_send(session, message, to, subject, *, before_data, **kwargs):
            with mock.patch.object(workflow.compose, "check_bundle", side_effect=ValueError("source changed before DATA")):
                before_data({"phase": "before_data"})
            self.fail("A changed source must prevent DATA")

        with mock.patch.object(FakeTransport, "send", new=changing_send):
            with self.assertRaisesRegex(ValueError, "source changed before DATA"):
                self.run_send([self.paths[0]], review_mode="protocol")
        self.assertEqual([event["status"] for event in workflow.read_events(self.config)],
                         ["attempt_started", "failed_before_data"])

    def test_protocol_authorization_change_before_data_prevents_data_marker(self):
        def changing_send(session, message, to, subject, *, before_data, **kwargs):
            self.write_auth(user_instruction="")
            before_data({"phase": "before_data"})
            self.fail("Changed authority must prevent DATA")

        with mock.patch.object(FakeTransport, "send", new=changing_send):
            with self.assertRaises(workflow.WorkflowError):
                self.run_send([self.paths[0]], review_mode="protocol")
        self.assertEqual([event["status"] for event in workflow.read_events(self.config)],
                         ["attempt_started", "failed_before_data"])

    def test_protocol_acceptance_and_unknown_state_keep_original_stop_rules(self):
        FakeTransport.results = [{"status": "accepted_unconfirmed", "smtp_accepted": True}]
        self.run_send([self.paths[0]], review_mode="protocol")
        result = self.run_send([self.paths[0]], review_mode="protocol")
        self.assertEqual((result[0]["status"], result[0]["validation_mode"]), ("duplicate", "protocol"))
        self.assertEqual(len(FakeTransport.calls), 1)
        FakeTransport.results = [{"status": "unknown", "smtp_accepted": None}]
        self.run_send([self.paths[1]], review_mode="protocol")
        self.write_auth(allow_resend=True)
        credential_reads = self.store.calls
        with self.assertRaises(workflow.WorkflowError):
            self.run_send([self.paths[1]], review_mode="protocol")
        self.assertEqual(len(FakeTransport.calls), 2)
        self.assertEqual(self.store.calls, credential_reads)

    def test_protocol_resend_still_requires_fresh_message_id_and_bundle(self):
        FakeTransport.results = [{"status": "accepted_unconfirmed", "smtp_accepted": True}]
        self.run_send([self.paths[0]], review_mode="protocol")
        self.write_auth(allow_resend=True)
        with self.assertRaises(workflow.WorkflowError):
            self.run_send([self.paths[0]], review_mode="protocol")
        self.items[self.paths[0]]["bundle"]["message_id"] = "<fresh-protocol-M001@example.invalid>"
        self.paths[0].write_text('{"advisor":"M001","generation":2}')
        self.write_auth(allow_resend=True)
        FakeTransport.results = [{"status": "accepted_confirmed", "smtp_accepted": True}]
        self.run_send([self.paths[0]], review_mode="protocol")
        self.assertEqual(len(FakeTransport.calls), 2)

    def test_protocol_reconcile_preserves_mode_without_client_claims(self):
        FakeTransport.results = [{"status": "unknown", "smtp_accepted": None}]
        self.run_send([self.paths[0]], review_mode="protocol")
        with mock.patch.object(workflow, "review_bundle", side_effect=AssertionError("No client review in read-only reconcile")):
            result = workflow.reconcile_bundle(self.root / "config.json", self.paths[0],
                                               store=self.store, transport_factory=FakeTransport)
        self.assertEqual((result["status"], result["validation_mode"]), ("unknown_copy_verified", "protocol"))
        event = workflow.read_events(self.config)[-1]
        self.assertEqual(event["validation_mode"], "protocol")
        self.assertIsNone(event["presentation_review"])
        self.assertFalse(result["smtp_accepted"])

    def test_legacy_reconcile_retains_review_without_inventing_validation_mode(self):
        FakeTransport.results = [{"status": "unknown", "smtp_accepted": None}]
        self.run_send([self.paths[0]])
        ledger = workflow._state_dir(self.config) / "attempts.jsonl"
        legacy = workflow.read_events(self.config)
        for event in legacy:
            event.pop("validation_mode")
        ledger.write_text("".join(json.dumps(event) + "\n" for event in legacy), encoding="utf-8")
        for path in self.review_paths:
            path.unlink()
        result = workflow.reconcile_bundle(self.root / "config.json", self.paths[0],
                                           store=self.store, transport_factory=FakeTransport)
        event = workflow.read_events(self.config)[-1]
        self.assertEqual(event["presentation_review"], legacy[-1]["presentation_review"])
        self.assertNotIn("validation_mode", result)
        self.assertNotIn("validation_mode", event)

    def test_send_cli_default_protocol_works_without_review_files(self):
        for path in self.review_paths:
            path.unlink()
        FakeTransport.results = [{"status": "accepted_confirmed", "smtp_accepted": True}]
        output = io.StringIO()
        with mock.patch.object(workflow.credentials, "WindowsCredentialStore", return_value=self.store), \
                mock.patch.object(workflow.transport, "MailTransport", new=FakeTransport), \
                contextlib.redirect_stdout(output):
            code = workflow.main(["send", "--bundles", str(self.paths[0]), "--authorization", str(self.auth)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())[0]["validation_mode"], "protocol")
        self.assertTrue(all(not path.exists() for path in self.review_paths))

    def test_authorization_does_not_replace_missing_real_client_review(self):
        path = self.review_paths[1]
        path.unlink()
        with self.assertRaises(OSError):
            self.run_send()
        self.assertFalse(path.exists())  # Validation never fabricates a review.
        self.assert_no_credentials_or_network()

    def test_every_visual_check_must_be_literal_true(self):
        for key in workflow.PRESENTATION_CHECKS:
            for value in (False, 1, "true", None):
                with self.subTest(check=key, value=value):
                    checks = {name: True for name in workflow.PRESENTATION_CHECKS}
                    checks[key] = value
                    self.write_review(self.paths[0], checks=checks)
                    with self.assertRaises(workflow.WorkflowError):
                        self.run_send()
                    self.assert_no_credentials_or_network()
        self.write_review(self.paths[0], checks={})
        with self.assertRaises(workflow.WorkflowError):
            self.run_send()
        self.assert_no_credentials_or_network()

    def test_review_requires_observer_client_evidence_and_iso_time(self):
        for key in ("reviewed_by", "client", "evidence", "reviewed_at"):
            with self.subTest(field=key):
                self.write_review(self.paths[0], **{key: "  "})
                with self.assertRaises(workflow.WorkflowError):
                    self.run_send()
        for stamp in ("not a date", "2000-01-01", "2000-01-01T09:00:00"):
            self.write_review(self.paths[0], reviewed_at=stamp)
            with self.assertRaises(workflow.WorkflowError):
                self.run_send()
        self.assert_no_credentials_or_network()

    def test_review_binds_both_bundle_and_exact_eml_hash(self):
        for field in ("bundle_sha256", "eml_sha256"):
            with self.subTest(field=field):
                self.write_review(self.paths[0], **{field: "0" * 64})
                with self.assertRaises(workflow.WorkflowError):
                    self.run_send()
                self.assert_no_credentials_or_network()
        self.write_review(self.paths[0])
        self.items[self.paths[0]]["eml_bytes"] += b" changed source fixture"
        with self.assertRaises(workflow.WorkflowError):
            self.run_send()
        self.assert_no_credentials_or_network()

    def test_new_authorization_cannot_cover_stale_bundle_review(self):
        self.paths[0].write_text('{"advisor":"M001","generation":2}')
        self.write_auth()  # Even fresh explicit authority cannot infer display.
        with self.assertRaises(workflow.WorkflowError):
            self.run_send()
        self.assert_no_credentials_or_network()

    def test_review_path_must_stay_inside_work_dir(self):
        with tempfile.TemporaryDirectory(prefix=".mail-review-outside-", dir=Path.cwd()) as directory:
            outside = self.write_review(self.paths[0], Path(directory) / "review.json")
            with self.assertRaises(workflow.WorkflowError):
                self.run_send(reviews=[outside, self.review_paths[1]])
        self.assert_no_credentials_or_network()

    def test_explicit_reviews_map_by_advisor_and_record_optional_evidence(self):
        paths = [self.write_review(
            bundle, self.root / f"{item['plan']['advisor']}-client.json",
            test_target="sender@example.invalid", test_message_id="<self-only-draft@example.invalid>",
            evidence_provenance={"display": "Synthetic draft fixture with matching body/HTML/PDF",
                                 "recipient": "Final production To checked against source bundle, separately from draft To"},
        ) for bundle, item in self.items.items()]
        self.review_paths[0].unlink()
        self.review_paths[1].unlink()
        before = [path.read_bytes() for path in paths]
        review = workflow.review_bundle(self.config, self.items[self.paths[0]], paths[0])
        self.assertEqual(review["test_target"], "sender@example.invalid")
        self.assertEqual(review["review_sha256"], workflow.preflight.fingerprint(paths[0])["sha256"])
        FakeTransport.results = [{"status": "accepted_confirmed", "smtp_accepted": True}] * 2
        self.run_send(reviews=list(reversed(paths)))
        self.assertEqual(FakeTransport.calls, ["teacher1@example.invalid", "teacher4@example.invalid"])
        self.assertEqual([path.read_bytes() for path in paths], before)

    def test_explicit_review_scope_rejects_missing_duplicate_or_wrong_advisor(self):
        for paths in ([self.review_paths[0]], [self.review_paths[0]] * 2, []):
            with self.assertRaises(workflow.WorkflowError):
                self.run_send(reviews=paths)
        self.write_review(self.paths[0], advisor="M999")
        with self.assertRaises(workflow.WorkflowError):
            self.run_send()
        self.assert_no_credentials_or_network()

    def test_review_does_not_replace_sending_authorization(self):
        self.write_auth(user_instruction="")
        with self.assertRaises(workflow.WorkflowError):
            self.run_send()
        self.assert_no_credentials_or_network()

    def test_review_changed_before_credentials_is_rejected(self):
        count = 0

        def check(config, path):
            nonlocal count
            count += 1
            if count == len(self.paths) + 1:
                self.write_review(self.paths[0], evidence="Changed after initial validation")
            return self.items[Path(path)]

        with mock.patch.object(workflow.compose, "check_bundle", side_effect=check):
            with self.assertRaises(workflow.WorkflowError):
                self.run_send()
        self.assert_no_credentials_or_network()

    def test_review_changed_after_first_message_blocks_second_message(self):
        original_send = FakeTransport.send

        def changing_send(session, *args, **kwargs):
            result = original_send(session, *args, **kwargs)
            self.write_review(self.paths[1], evidence="Changed after the first message")
            return result

        FakeTransport.results = [{"status": "accepted_confirmed", "smtp_accepted": True}] * 2
        with mock.patch.object(FakeTransport, "send", new=changing_send):
            with self.assertRaises(workflow.WorkflowError):
                self.run_send()
        self.assertEqual(FakeTransport.calls, ["teacher1@example.invalid"])
        self.assertEqual(workflow.read_events(self.config)[-1]["status"], "accepted_confirmed")

    def test_review_changed_before_data_does_not_record_data_started(self):
        def changing_send(session, message, to, subject, *, before_data, **kwargs):
            self.write_review(self.paths[0], evidence="Changed immediately before DATA")
            before_data({"phase": "before_data"})
            self.fail("A changed review must prevent DATA")

        with mock.patch.object(FakeTransport, "send", new=changing_send):
            with self.assertRaises(workflow.WorkflowError):
                self.run_send([self.paths[0]])
        self.assertEqual([event["status"] for event in workflow.read_events(self.config)],
                         ["attempt_started", "failed_before_data"])

    def test_send_cli_passes_explicit_review_paths(self):
        with mock.patch.object(workflow, "send_bundles", return_value=[{"status": "accepted_confirmed"}]) as send:
            with contextlib.redirect_stdout(io.StringIO()):
                code = workflow.main(["send", "--bundles", str(self.paths[0]),
                                      "--authorization", str(self.auth),
                                      "--review-mode", "client",
                                      "--reviews", str(self.review_paths[0])])
        self.assertEqual(code, 0)
        self.assertEqual(send.call_args.kwargs["reviews"], [self.review_paths[0]])
        self.assertEqual(send.call_args.kwargs["review_mode"], "client")

    def test_offline_check_and_reconcile_do_not_require_review(self):
        FakeTransport.results = [{"status": "unknown", "smtp_accepted": None}]
        self.run_send([self.paths[0]])
        for path in self.review_paths:
            path.unlink()
        with mock.patch.object(workflow, "review_bundle", side_effect=AssertionError("Read-only commands must not need reviews")):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(workflow.main(["check", "--bundle", str(self.paths[0])]), 0)
            result = workflow.reconcile_bundle(self.root / "config.json", self.paths[0],
                                                store=self.store, transport_factory=FakeTransport)
        self.assertEqual(result["status"], "unknown_copy_verified")
        self.assertEqual(len(FakeTransport.calls), 1)
        self.assertIsNotNone(workflow.read_events(self.config)[-1]["presentation_review"])

    def test_missing_or_wrong_scope_blocks_before_credentials_and_network(self):
        self.auth.unlink()
        with self.assertRaises(OSError):
            self.run_send()
        self.write_auth(allowed=[])
        with self.assertRaises(workflow.WorkflowError):
            self.run_send()
        self.assertEqual(self.store.calls, 0)
        self.assertEqual(FakeTransport.instances, 0)

    def test_exact_bundle_hash_and_recipient_are_required(self):
        auth = json.loads(self.auth.read_text())
        auth["allowed"][0]["to"] = "wrong@example.invalid"
        self.auth.write_text(json.dumps(auth))
        with self.assertRaises(workflow.WorkflowError):
            self.run_send()
        self.write_auth()
        self.paths[0].write_text("tampered bundle")
        with self.assertRaises(workflow.WorkflowError):
            self.run_send()
        self.assertEqual(self.store.calls, 0)

    def test_append_mode_needs_explicit_scope(self):
        with self.assertRaises(workflow.WorkflowError):
            self.run_send(copy_mode="append_if_missing")
        self.assertEqual(FakeTransport.instances, 0)

    def test_one_connection_batch_and_durable_acceptance_per_message(self):
        FakeTransport.results = [{"status": "accepted_confirmed", "smtp_accepted": True}] * 2
        results = self.run_send()
        self.assertEqual(len(results), 2)
        self.assertEqual(FakeTransport.instances, 1)
        events = workflow.read_events(self.config)
        self.assertEqual([e["status"] for e in events], ["attempt_started", "data_started", "smtp_accepted", "accepted_confirmed"] * 2)
        text = (workflow._state_dir(self.config) / "attempts.jsonl").read_text()
        self.assertNotIn("TESTONLYCODE1234", text)
        self.assertEqual(events[0]["presentation_review"], {
            "path": str(self.review_paths[0].resolve()),
            "sha256": workflow.preflight.fingerprint(self.review_paths[0])["sha256"],
        })
        self.assertTrue(all(event["validation_mode"] == "client" for event in events))
        self.assertTrue(all(result["validation_mode"] == "client" for result in results))
        self.assertFalse((workflow._state_dir(self.config) / "send.lock").exists())

    def test_unknown_halts_batch_and_blocks_even_resend_flag(self):
        FakeTransport.results = [{"status": "unknown", "smtp_accepted": None}]
        self.assertEqual(len(self.run_send()), 1)
        self.assertEqual(len(FakeTransport.calls), 1)
        self.write_auth(allow_resend=True)
        with self.assertRaises(workflow.WorkflowError):
            self.run_send()
        self.assertEqual(len(FakeTransport.calls), 1)

    def test_interrupt_after_data_marker_is_durably_unknown(self):
        FakeTransport.interrupt = True
        with self.assertRaises(KeyboardInterrupt):
            self.run_send()
        self.assertEqual(workflow.read_events(self.config)[-1]["status"], "unknown")
        self.assertFalse((workflow._state_dir(self.config) / "send.lock").exists())

    def test_accepted_missing_copy_blocks_default_resend(self):
        FakeTransport.results = [{"status": "accepted_unconfirmed", "smtp_accepted": True}]
        self.run_send()
        result = self.run_send([self.paths[0]])
        self.assertEqual(result[0]["status"], "duplicate")
        self.assertEqual(len(FakeTransport.calls), 1)

    def test_confirmed_rejection_can_be_retried_under_same_scope(self):
        FakeTransport.results = [{"status": "rejected", "smtp_accepted": False}]
        self.run_send([self.paths[0]])
        FakeTransport.results = [{"status": "accepted_confirmed", "smtp_accepted": True}]
        self.run_send([self.paths[0]])
        self.assertEqual(len(FakeTransport.calls), 2)

    def test_resend_requires_fresh_identity_even_if_sent_copy_was_missing(self):
        FakeTransport.results = [{"status": "accepted_unconfirmed", "smtp_accepted": True}]
        self.run_send([self.paths[0]])
        self.write_auth(allow_resend=True)
        with self.assertRaises(workflow.WorkflowError):
            self.run_send([self.paths[0]])
        self.assertEqual(len(FakeTransport.calls), 1)
        self.items[self.paths[0]]["bundle"]["message_id"] = "<fresh-M001@example.invalid>"
        self.paths[0].write_text('{"advisor":"M001","generation":2}')
        self.write_auth(allow_resend=True)
        self.write_review(self.paths[0])
        FakeTransport.results = [{"status": "accepted_confirmed", "smtp_accepted": True}]
        self.run_send([self.paths[0]])
        self.assertEqual(len(FakeTransport.calls), 2)

    def test_existing_lock_and_malformed_ledger_fail_closed(self):
        directory = workflow._state_dir(self.config)
        directory.mkdir()
        (directory / "send.lock").write_text("existing process lock")
        with self.assertRaises(workflow.WorkflowError):
            self.run_send()
        self.assertEqual((directory / "send.lock").read_text(), "existing process lock")
        (directory / "send.lock").unlink()
        (directory / "attempts.jsonl").write_text('{"partial"')
        with self.assertRaises(workflow.WorkflowError):
            self.run_send()
        self.assertEqual(self.store.calls, 0)

    def test_duplicate_skips_and_continues_to_next_distinct_teacher(self):
        FakeTransport.results = [{"status": "duplicate"}, {"status": "accepted_confirmed", "smtp_accepted": True}]
        self.assertEqual(len(self.run_send()), 2)

    def test_unknown_copy_reconciliation_never_invents_smtp_acceptance(self):
        FakeTransport.results = [{"status": "unknown", "smtp_accepted": None}]
        self.run_send([self.paths[0]])
        result = workflow.reconcile_bundle(self.root / "config.json", self.paths[0], store=self.store, transport_factory=FakeTransport)
        self.assertEqual(result["status"], "unknown_copy_verified")
        self.assertFalse(result["smtp_accepted"])
        with self.assertRaises(workflow.WorkflowError):
            self.run_send([self.paths[0]])
        self.assertEqual(len(FakeTransport.calls), 1)


class CredentialTests(unittest.TestCase):
    def test_target_is_project_scoped_and_injection_rejected(self):
        self.assertEqual(credentials.target_name("sender@example.invalid"), "Codex/Outreach-Public/sender@example.invalid")
        for bad in ("missing-at-sign", "sender@example.invalid\r\nX: y", "another user@example.invalid"):
            with self.assertRaises(credentials.CredentialError):
                credentials.target_name(bad)

    def test_hidden_prompt_refuses_pipe_and_does_not_read_vault(self):
        vault = mock.Mock()
        with mock.patch("sys.stdin.isatty", return_value=False):
            with self.assertRaises(credentials.CredentialError):
                credentials.prompt_and_store("sender@example.invalid", store=vault)
        vault.read.assert_not_called()
        vault.write.assert_not_called()

    def test_code_format_and_existing_target_are_not_silently_overwritten(self):
        vault = credentials.WindowsCredentialStore.__new__(credentials.WindowsCredentialStore)
        vault.read = mock.Mock(return_value="existing-test-code")
        vault.api = mock.Mock()
        with self.assertRaises(credentials.CredentialError):
            vault.write("sender@example.invalid", "short")
        with self.assertRaises(credentials.CredentialError):
            vault.write("sender@example.invalid", "TESTCODE12345678")
        vault.api.CredWriteW.assert_not_called()

    def test_native_write_blob_and_local_machine_persistence(self):
        vault = credentials.WindowsCredentialStore.__new__(credentials.WindowsCredentialStore)
        vault.read = mock.Mock(return_value=None)
        captured = {}

        def write(pointer, flags):
            row = ctypes.cast(pointer, ctypes.POINTER(credentials.CREDENTIALW)).contents
            captured.update(target=row.TargetName, username=row.UserName, persist=row.Persist,
                            type=row.Type, blob=ctypes.string_at(row.CredentialBlob, row.CredentialBlobSize))
            return True

        vault.api = mock.Mock()
        vault.api.CredWriteW.side_effect = write
        vault.write("sender@example.invalid", "TESTCODE12345678")
        self.assertEqual(captured["blob"].decode("utf-16-le"), "TESTCODE12345678")
        self.assertEqual((captured["type"], captured["persist"]), (1, 2))
        self.assertEqual(captured["username"], "sender@example.invalid")


class ProbeCliTests(unittest.TestCase):
    def setUp(self):
        self.config = {"sender": "sender@example.invalid", "work_dir": str(Path.cwd())}
        self.session = mock.MagicMock()
        for patcher in (
            mock.patch.object(workflow.preflight, "load_config", return_value=(self.config, Path("config.json"))),
            mock.patch.object(workflow, "_secret", return_value="SYNTHETIC-CODE-DO-NOT-LOG"),
            mock.patch.object(workflow.transport, "MailTransport", return_value=self.session),
            mock.patch("socket.create_connection", side_effect=AssertionError("network forbidden")),
            mock.patch("socket.getaddrinfo", side_effect=AssertionError("DNS forbidden")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            status = workflow.main(argv)
        return status, stdout.getvalue(), stderr.getvalue()

    def test_blocked_probe_has_nonzero_exit_and_closes_without_enter(self):
        self.session.probe.return_value = {"status": "blocked", "error_code": "imap_login_failed", "phase": "imap_login"}
        code, stdout, stderr = self.run_cli(["probe"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["error_code"], "imap_login_failed")
        self.session.__enter__.assert_not_called()
        self.session.close.assert_called_once()
        self.assertEqual(stderr, "")

    def test_ready_probe_exits_zero_and_closes(self):
        self.session.probe.return_value = {"status": "ready"}
        code, stdout, _ = self.run_cli(["probe"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout)["status"], "ready")
        self.session.close.assert_called_once()

    def test_transport_error_retains_stable_code_and_phase(self):
        self.session.probe.side_effect = workflow.transport.TransportError("smtp_permission_denied", phase="smtp_login", smtp_code=550)
        code, stdout, stderr = self.run_cli(["probe"])
        self.assertEqual(code, 2)
        self.assertIn('"error_code": "smtp_permission_denied"', stderr)
        self.assertIn('"smtp_code": 550', stderr)
        self.assertIn('"phase": "smtp_login"', stderr)
        self.assertNotIn("SYNTHETIC-CODE", stdout + stderr)
        self.session.close.assert_called_once()

    def test_unexpected_auth_exception_does_not_print_server_text(self):
        self.session.probe.side_effect = workflow.smtplib.SMTPAuthenticationError(550, b"SYNTHETIC-CODE-DO-NOT-LOG")
        code, stdout, stderr = self.run_cli(["probe"])
        self.assertEqual(code, 2)
        self.assertIn("550", stderr)
        self.assertNotIn("SYNTHETIC-CODE", stdout + stderr)
        self.session.close.assert_called_once()

    def test_duplicate_enter_failure_retains_error_code(self):
        self.session.__enter__.side_effect = workflow.transport.TransportError("imap_login_failed", phase="imap_login")
        with mock.patch.object(workflow.compose, "check_bundle", return_value={"plan": {"to": "advisor@example.invalid", "subject": "申请"}}):
            code, _, stderr = self.run_cli(["duplicates", "--bundle", "fixture.json"])
        self.assertEqual(code, 2)
        self.assertIn("imap_login_failed", stderr)

    def test_blocked_duplicate_exits_nonzero(self):
        self.session.__enter__.return_value = self.session
        self.session.duplicate.return_value = {"status": "blocked", "error_code": "sent_select_failed"}
        with mock.patch.object(workflow.compose, "check_bundle", return_value={"plan": {"to": "advisor@example.invalid", "subject": "申请"}}):
            code, stdout, _ = self.run_cli(["duplicates", "--bundle", "fixture.json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stdout)["error_code"], "sent_select_failed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
