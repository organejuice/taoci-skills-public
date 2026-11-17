"""Offline transport contracts. All sockets are prohibited, not merely unused."""
import importlib.util
import imaplib
import re
import smtplib
import socket
import ssl
import unittest
from email import policy
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
spec = importlib.util.spec_from_file_location("mail_transport_under_test", SCRIPTS / "mail_transport.py")
transport = importlib.util.module_from_spec(spec)
spec.loader.exec_module(transport)

SENDER = "sender@example.invalid"
TO = "advisor@example.invalid"
SUBJECT = "博士申请-示例机构-示例申请人"


def message(*, to=TO, sender=SENDER, subject=SUBJECT, mid="<one@example.invalid>",
            plain="老师您好！\n申请材料。\n", html="<p>老师您好！</p>\n",
            pdf=b"%PDF-1.4\nfixture\n", filename="自我陈述-教师.pdf"):
    msg = EmailMessage(policy=policy.SMTP)
    msg["From"], msg["To"], msg["Subject"], msg["Message-ID"] = sender, to, subject, mid
    msg.set_content(plain)
    msg.add_alternative(html, subtype="html")
    msg.add_attachment(pdf, maintype="application", subtype="pdf", filename=filename)
    return msg.as_bytes()


class FakeSocket:
    def __init__(self):
        self.timeout = 30
        self.timeouts = []

    def gettimeout(self):
        return self.timeout

    def settimeout(self, value):
        self.timeouts.append(value)
        self.timeout = value


class FakeServer:
    def __init__(self):
        self.messages = {}
        self.events = []
        self.imaps, self.smtps = [], []
        self.folders = [b'(\\HasNoChildren \\Sent) "/" "Sent Messages"']
        self.auto_copy = True
        self.data_error = None
        self.data_reply = 250
        self.mail_reply = 250
        self.rcpt_reply = 250
        self.append_reply = "OK"
        self.store_append = True
        self.imap_error = None
        self.search_response = None
        self.capabilities = [b"IMAP4rev1 ID"]
        self.id_reply = "OK"
        self.imap_login_error = None
        self.smtp_login_error = None

    def add(self, raw):
        uid = str(max([int(k) for k in self.messages] or [0]) + 1).encode()
        self.messages[uid] = raw
        return uid

    def smtp_factory(self, host, port, **kwargs):
        assert host == "smtp.example.invalid" and port == 465
        assert kwargs["timeout"] == 30
        assert kwargs["context"].check_hostname
        assert kwargs["context"].verify_mode == ssl.CERT_REQUIRED
        smtp = FakeSMTP(self)
        self.smtps.append(smtp)
        return smtp

    def imap_factory(self, host, port, **kwargs):
        assert host == "imap.example.invalid" and port == 993
        assert kwargs["timeout"] == 30
        assert kwargs["ssl_context"].check_hostname
        assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED
        imap = FakeIMAP(self)
        self.imaps.append(imap)
        return imap


class FakeSMTP:
    def __init__(self, server):
        self.server, self.sock = server, FakeSocket()
        self.data_calls = []
        self.quit_count = 0

    def ehlo(self):
        self.server.events.append("ehlo")
        return 250, b"OK"

    def login(self, username, password):
        self.server.events.append("smtp_login")
        if self.server.smtp_login_error is not None:
            raise self.server.smtp_login_error
        return 235, b"OK"

    def mail(self, sender):
        self.server.events.append("mail")
        return self.server.mail_reply, b"opaque server text"

    def rcpt(self, to):
        self.server.events.append("rcpt")
        return self.server.rcpt_reply, b"opaque server text"

    def data(self, raw):
        self.server.events.append("data")
        self.data_calls.append(raw)
        if self.server.data_error is not None:
            raise self.server.data_error
        if self.server.data_reply == 250 and self.server.auto_copy:
            self.server.add(b"Received: by test.invalid\r\n" + raw)
        return self.server.data_reply, b"queued"

    def quit(self):
        self.quit_count += 1
        return 221, b"bye"

    def close(self):
        pass


class FakeIMAP:
    def __init__(self, server):
        self.server = server
        self.selects, self.fetches, self.searches, self.appends = [], [], [], []
        self.logout_count = 0
        self.authenticated = False
        self.ids = []

    def login(self, username, password):
        self.server.events.append("imap_login")
        if self.server.imap_login_error is not None:
            raise self.server.imap_login_error
        self.authenticated = True
        return "OK", []

    def capability(self):
        assert self.authenticated, "CAPABILITY must be refreshed after login"
        self.server.events.append("capability")
        return "OK", self.server.capabilities

    def xatom(self, name, *args):
        assert self.authenticated
        self.server.events.append("id")
        self.ids.append((name, args))
        if isinstance(self.server.id_reply, Exception):
            raise self.server.id_reply
        return self.server.id_reply, [b"private server ID response never exposed"]

    def list(self):
        self.server.events.append("list")
        return "OK", self.server.folders

    def select(self, mailbox, readonly=False):
        self.server.events.append("select")
        self.selects.append((mailbox, readonly))
        return "OK", [str(len(self.server.messages)).encode()]

    def uid(self, operation, *args):
        if self.server.imap_error:
            raise self.server.imap_error
        if operation == "SEARCH":
            self.server.events.append("search")
            self.searches.append(args)
            # Deliberately broad: correctness must not trust substring matches.
            return self.server.search_response or ("OK", [b" ".join(self.server.messages)])
        assert operation == "FETCH"
        uid, query = args
        self.fetches.append((uid, query))
        assert "BODY.PEEK[" in query
        raw = self.server.messages[uid]
        if query == transport.HEADER_FIELDS:
            raw = re.split(rb"\r?\n\r?\n", raw, maxsplit=1)[0] + b"\r\n\r\n"
        else:
            assert query == "(BODY.PEEK[])"
            self.server.events.append("fetch_body")
        return "OK", [(uid + b" (BODY[] {" + str(len(raw)).encode() + b"}", raw), b")"]

    def append(self, mailbox, flags, date_time, raw):
        self.server.events.append("append")
        self.appends.append((mailbox, flags, date_time, raw))
        if isinstance(self.server.append_reply, Exception):
            raise self.server.append_reply
        if self.server.append_reply == "OK" and self.server.store_append:
            self.server.add(raw)
        return self.server.append_reply, [b"response intentionally ignored"]

    def logout(self):
        self.logout_count += 1
        return "BYE", []


class MailTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Reuse the real verified TLS context: loading the Windows trust store
        # repeatedly dominates these otherwise in-memory tests.
        cls.tls_context = ssl.create_default_context()

    def setUp(self):
        self.socket_patches = [patch("socket.create_connection", side_effect=AssertionError("real sockets forbidden")),
                               patch("socket.socket", side_effect=AssertionError("real sockets forbidden"))]
        for item in self.socket_patches:
            item.start()
            self.addCleanup(item.stop)
        tls_patch = patch.object(transport.ssl, "create_default_context", return_value=self.tls_context)
        tls_patch.start()
        self.addCleanup(tls_patch.stop)
        self.server = FakeServer()
        self.clients = []
        self.attempts, self.acceptances = [], []

    def tearDown(self):
        for client in self.clients:
            client.close()

    def client(self, **kwargs):
        obj = transport.MailTransport(SENDER, "non-secret-fixture", smtp_factory=self.server.smtp_factory,
                                     imap_factory=self.server.imap_factory, **kwargs)
        self.clients.append(obj)
        return obj

    def before(self, evidence):
        self.server.events.append("before_data")
        self.attempts.append(evidence)

    def accepted(self, evidence):
        self.server.events.append("on_accepted")
        self.acceptances.append(evidence)

    def send(self, client=None, raw=None, **kwargs):
        return (client or self.client()).send(raw or message(), TO, SUBJECT,
                    before_data=self.before, on_accepted=self.accepted, **kwargs)

    def test_public_placeholder_sender_and_endpoints_never_open_live_connections(self):
        with self.assertRaisesRegex(ValueError, "configure_sender_and_tls_endpoints"):
            transport.MailTransport(SENDER, "synthetic secret")
        with self.assertRaisesRegex(ValueError, "configure_sender_and_tls_endpoints"):
            transport.MailTransport("sender@configured.example.invalid", "synthetic secret")

    def test_explicit_tls_endpoints_are_used_by_both_factories(self):
        endpoints = []
        def smtp(host, port, **options):
            endpoints.append(("smtp", host, port))
            return FakeSMTP(self.server)
        def imap(host, port, **options):
            endpoints.append(("imap", host, port))
            return FakeIMAP(self.server)
        client = transport.MailTransport(SENDER, "synthetic secret", smtp_factory=smtp,
            imap_factory=imap, smtp_host="smtp-alt.example.invalid", smtp_port=2465,
            imap_host="imap-alt.example.invalid", imap_port=2993)
        self.addCleanup(client.close)
        self.send(client=client)
        self.assertEqual(endpoints, [("imap", "imap-alt.example.invalid", 2993),
                                     ("smtp", "smtp-alt.example.invalid", 2465)])

    def test_exact_to_sender_and_chinese_subject_filter(self):
        self.server.add(message(to="x" + TO))
        self.server.add(message(to=TO + ".invalid"))
        self.server.add(message(to=TO + ", other@example.invalid"))
        self.server.add(message(sender="other@example.invalid"))
        self.server.add(message(subject=SUBJECT + "修改"))
        client = self.client()
        self.assertEqual(client.duplicate(TO, SUBJECT)["status"], "clear")
        uid = self.server.add(message())
        result = client.duplicate(TO, SUBJECT)
        self.assertEqual((result["status"], result["match_count"], result["sent_uid"]),
                         ("duplicate", 1, uid.decode()))
        self.assertEqual(self.server.smtps, [])
        self.assertTrue(all(readonly for _, readonly in self.server.imaps[0].selects))
        self.assertTrue(all(query == transport.HEADER_FIELDS for _, query in self.server.imaps[0].fetches))
        self.assertTrue(all(all(not isinstance(a, str) or SUBJECT not in a for a in search)
                            for search in self.server.imaps[0].searches))

    def test_duplicate_blocks_send_unless_explicit_resend(self):
        self.server.add(message(mid="<old@example.invalid>"))
        client = self.client()
        self.assertEqual(self.send(client)["status"], "duplicate")
        self.assertEqual(self.attempts, [])
        self.assertEqual(self.server.smtps, [])
        result = self.send(client, allow_resend=True)
        self.assertEqual(result["status"], "accepted_confirmed")
        self.assertEqual(len(self.server.smtps[0].data_calls), 1)

    def test_authorized_resend_still_requires_fresh_message_id(self):
        self.server.add(message())
        result = self.send(allow_resend=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_code"], "reused_message_id")
        self.assertEqual(result["reason"], "new_bundle_required")
        self.assertEqual(self.server.smtps, [])
        self.assertNotIn("mail", self.server.events)
        self.assertNotIn("data", self.server.events)
        self.assertEqual(self.attempts, [])

    def test_success_callbacks_timeouts_and_received_header_tolerance(self):
        result = self.send()
        self.assertEqual(result["status"], "accepted_confirmed")
        self.assertTrue(result["smtp_accepted"])
        self.assertTrue(result["content_verified"])
        self.assertFalse(result["copy_appended"])
        self.assertEqual(self.acceptances[0]["status"], "smtp_accepted")
        self.assertEqual(self.attempts[0]["phase"], "before_data")
        events = self.server.events
        self.assertLess(events.index("before_data"), events.index("data"))
        self.assertLess(events.index("data"), events.index("on_accepted"))
        self.assertLess(events.index("on_accepted"), events.index("fetch_body"))
        self.assertEqual(self.server.smtps[0].sock.timeouts, [600, 30])
        self.assertNotIn("delivered", result)

    def test_serial_batch_reuses_both_connections(self):
        client = self.client()
        first = self.send(client)
        second_raw = message(to="second@example.invalid", mid="<two@example.invalid>")
        second = client.send(second_raw, "second@example.invalid", SUBJECT,
                             before_data=self.before, on_accepted=self.accepted)
        self.assertEqual([first["status"], second["status"]], ["accepted_confirmed"] * 2)
        self.assertEqual(len(self.server.smtps), 1)
        self.assertEqual(len(self.server.imaps), 1)
        client.close()
        self.assertEqual(self.server.smtps[0].quit_count, 1)
        self.assertEqual(self.server.imaps[0].logout_count, 1)

    def test_probe_never_sends(self):
        client = self.client()
        result = client.probe()
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["sent_folder"], "Sent Messages")
        self.assertEqual(self.server.smtps[0].data_calls, [])
        self.assertNotIn("mail", self.server.events)
        self.assertEqual(self.server.imaps[0].appends, [])

    def test_transport_error_public_metadata_is_safe_and_str_remains_code(self):
        error = transport.TransportError("smtp_login_failed", phase="smtp_login", smtp_code=550)
        self.assertEqual(str(error), "smtp_login_failed")
        self.assertEqual(error.args, ("smtp_login_failed",))
        self.assertEqual(vars(error), {"code": "smtp_login_failed", "phase": "smtp_login", "smtp_code": 550})
        for unsafe in ("private server reply", "smtp_login_failed\nsecret", "x" * 81):
            with self.subTest(unsafe_length=len(unsafe)):
                with self.assertRaises(ValueError):
                    transport.TransportError(unsafe)
        with self.assertRaises(ValueError):
            transport.TransportError("smtp_login_failed", phase="private server reply")
        for invalid_code in (True, "550 private reply", 99, 600, None):
            with self.subTest(code_type=type(invalid_code).__name__):
                self.assertIsNone(transport.TransportError("smtp_login_failed", smtp_code=invalid_code).smtp_code)

    def test_imap_login_failure_is_classified_and_retry_uses_new_connection(self):
        self.server.imap_login_error = imaplib.IMAP4.error("private-authorization-reply")
        client = self.client()
        for _ in range(2):
            self.assertEqual(client.probe(), {"status": "blocked", "error_code": "imap_login_failed", "phase": "imap_login"})
            self.assertIsNone(client._imap)
        self.assertEqual(len(self.server.imaps), 2)
        self.assertTrue(all(imap.logout_count == 1 for imap in self.server.imaps))
        self.assertEqual(self.server.smtps, [])
        self.server.imap_login_error = None
        self.assertEqual(client.probe()["status"], "ready")
        self.assertEqual(len(self.server.imaps), 3)
        self.assertEqual(self.server.events.count("imap_login"), 3)
        self.assertNotIn("mail", self.server.events)
        self.assertNotIn("data", self.server.events)
        self.assertTrue(all(not imap.appends for imap in self.server.imaps))

    def test_imap_abort_during_login_is_not_reported_as_login_rejection(self):
        self.server.imap_login_error = imaplib.IMAP4.abort("private-session-reply")
        client = self.client()
        self.assertEqual(client.probe(), {"status": "blocked", "error_code": "imap_connection_lost", "phase": "imap_login"})
        self.assertIsNone(client._imap)
        self.assertEqual(self.server.imaps[0].logout_count, 1)
        self.assertEqual(self.server.smtps, [])

    def test_imap_connect_failure_has_connect_phase(self):
        with patch.object(self.server, "imap_factory", side_effect=OSError("private-connect-reply")):
            client = self.client()
            self.assertEqual(client.probe(), {"status": "blocked", "error_code": "imap_connect_failed", "phase": "imap_connect"})
        self.assertIsNone(client._imap)
        self.assertEqual(self.server.smtps, [])

    def test_imap_setup_failures_report_capability_id_and_list_phases(self):
        for operation, code, phase in (
                ("capability", "imap_capability_failed", "imap_capability"),
                ("xatom", "imap_id_failed", "imap_id"),
                ("list", "imap_list_failed", "imap_list")):
            with self.subTest(operation=operation):
                self.server = FakeServer()
                with patch.object(FakeIMAP, operation, side_effect=imaplib.IMAP4.error("private-setup-reply")):
                    client = self.client()
                    self.assertEqual(client.probe(), {"status": "blocked", "error_code": code, "phase": phase})
                self.assertIsNone(client._imap)
                self.assertEqual(self.server.imaps[0].logout_count, 1)
                self.assertEqual(self.server.smtps, [])

    def test_smtp_auth_failure_keeps_numeric_code_and_does_not_cache_failed_session(self):
        self.server.smtp_login_error = smtplib.SMTPAuthenticationError(550, b"private-authorization-reply")
        client = self.client()
        for _ in range(2):
            self.assertEqual(client.probe(), {"status": "blocked", "error_code": "smtp_login_failed", "phase": "smtp_login", "smtp_code": 550})
            self.assertIsNone(client._smtp)
        self.assertEqual(len(self.server.smtps), 2)
        self.assertTrue(all(smtp.quit_count == 1 for smtp in self.server.smtps))
        self.server.smtp_login_error = None
        self.assertEqual(client.probe()["status"], "ready")
        self.assertEqual(len(self.server.smtps), 3)
        self.assertEqual(self.server.events.count("smtp_login"), 3)
        self.assertEqual(len(self.server.imaps), 1)
        for operation in ("mail", "rcpt", "data", "append"):
            self.assertNotIn(operation, self.server.events)

    def test_smtp_permission_reply_maps_only_to_safe_fixed_code(self):
        for reply, expected in (
                (b"User has no permission private-authorization-reply", "smtp_permission_denied"),
                ("User has no permission private-authorization-reply", "smtp_permission_denied"),
                (b"user has no permission private-authorization-reply", "smtp_login_failed"),
                (b"generic private-authorization-reply", "smtp_login_failed")):
            with self.subTest(reply_type=type(reply).__name__, expected=expected):
                self.server = FakeServer()
                self.server.smtp_login_error = smtplib.SMTPAuthenticationError(550, reply)
                client = self.client()
                result = client.probe()
                self.assertEqual(result, {"status": "blocked", "error_code": expected, "phase": "smtp_login", "smtp_code": 550})
                self.assertNotIn("private-authorization-reply", repr(result))
                self.assertIsNone(client._smtp)
                self.assertEqual(self.server.smtps[0].quit_count, 1)

    def test_smtp_connect_ehlo_and_disconnect_failures_remain_distinct(self):
        with patch.object(self.server, "smtp_factory", side_effect=OSError("private-connect-reply")):
            client = self.client()
            self.assertEqual(client.probe(), {"status": "blocked", "error_code": "smtp_connect_failed", "phase": "smtp_connect"})
            self.assertIsNone(client._smtp)
        for operation, failure, code, phase in (
                ("ehlo", OSError("private-ehlo-reply"), "smtp_ehlo_failed", "smtp_ehlo"),
                ("login", smtplib.SMTPServerDisconnected("private-session-reply"), "smtp_connection_lost", "smtp_login")):
            with self.subTest(operation=operation):
                self.server = FakeServer()
                with patch.object(FakeSMTP, operation, side_effect=failure):
                    client = self.client()
                    self.assertEqual(client.probe(), {"status": "blocked", "error_code": code, "phase": phase})
                self.assertIsNone(client._smtp)
                self.assertEqual(self.server.smtps[0].quit_count, 1)

    def test_setup_errors_do_not_retain_raw_exception_context(self):
        self.server.imap_login_error = imaplib.IMAP4.error("private-authorization-reply")
        with self.assertRaises(transport.TransportError) as captured:
            self.client()._ensure_imap()
        self.assertIsNone(captured.exception.__context__)
        self.assertNotIn("private-authorization-reply", repr(vars(captured.exception)))
        self.server = FakeServer()
        self.server.smtp_login_error = smtplib.SMTPAuthenticationError(550, b"User has no permission private-authorization-reply")
        with self.assertRaises(transport.TransportError) as captured:
            self.client()._ensure_smtp()
        self.assertIsNone(captured.exception.__context__)
        self.assertNotIn("private-authorization-reply", repr(vars(captured.exception)))

    def test_data_timeout_configurable(self):
        self.assertEqual(self.send(self.client(data_timeout=750))["status"], "accepted_confirmed")
        self.assertEqual(self.server.smtps[0].sock.timeouts, [750, 30])

    def test_smtp_envelope_refusal_never_reaches_data(self):
        for phase in ("mail_reply", "rcpt_reply"):
            with self.subTest(phase=phase):
                self.server = FakeServer()
                setattr(self.server, phase, 451)
                result = self.send()
                self.assertEqual(result["status"], "rejected")
                self.assertEqual(result["smtp_code"], 451)
                self.assertFalse(result["smtp_accepted"])
                self.assertEqual(self.server.smtps[0].data_calls, [])
        self.assertEqual(self.attempts, [])

    def test_explicit_data_refusal_is_rejected(self):
        for error in (smtplib.SMTPDataError(550, b"sensitive response"), None):
            with self.subTest(error=type(error).__name__):
                self.server = FakeServer()
                self.server.data_error, self.server.data_reply = error, 451
                client = self.client()
                result = self.send(client)
                self.assertEqual(result["status"], "rejected")
                self.assertFalse(result["smtp_accepted"])
                self.assertNotIn("sensitive", repr(result))
                self.assertEqual(self.send(client)["error_code"], "batch_stopped")

    def test_data_disconnect_and_timeout_are_unknown_and_never_retried(self):
        for error in (socket.timeout("private-token"), smtplib.SMTPServerDisconnected("private-token")):
            with self.subTest(error=type(error).__name__):
                self.server = FakeServer()
                self.server.data_error = error
                client = self.client()
                result = self.send(client)
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["smtp_accepted"])
                self.assertNotIn("private-token", repr(result))
                self.assertEqual(self.send(client)["error_code"], "batch_stopped")
                self.assertEqual(len(self.server.smtps[0].data_calls), 1)
                self.assertEqual(self.server.imaps[0].appends, [])

    def test_unknown_history_only_reconciles_and_does_not_invent_acceptance(self):
        client = self.client(copy_mode="append_if_missing")
        result = self.send(client, prior_status="unknown", allow_resend=True)
        self.assertEqual(result["error_code"], "unknown_requires_reconcile")
        self.assertEqual(self.server.smtps, [])
        raw = message()
        self.server.add(raw)
        result = client.reconcile("<one@example.invalid>", TO, SUBJECT, expected_message=raw)
        self.assertEqual(result["status"], "copy_verified")
        self.assertIsNone(result["smtp_accepted"])
        self.assertEqual(result["prior_status"], "unknown")
        self.assertEqual(self.server.imaps[0].appends, [])
        self.assertEqual(self.server.smtps, [])

    def test_headers_without_expected_mime_are_not_full_confirmation(self):
        self.server.add(message())
        result = self.client().reconcile("<one@example.invalid>", TO, SUBJECT)
        self.assertEqual(result["status"], "headers_found")
        self.assertIsNone(result["smtp_accepted"])
        self.assertTrue(all(query == transport.HEADER_FIELDS for _, query in self.server.imaps[0].fetches))

    def test_only_matching_message_id_candidate_body_is_fetched(self):
        unrelated = self.server.add(message(mid="<other@example.invalid>"))
        wanted = self.server.add(message())
        result = self.client().reconcile("<one@example.invalid>", TO, SUBJECT, expected_message=message())
        self.assertEqual(result["status"], "copy_verified")
        full = [uid for uid, query in self.server.imaps[0].fetches if query == "(BODY.PEEK[])"]
        self.assertEqual(full, [wanted])
        self.assertNotIn(unrelated, full)

    def test_wrong_headers_with_same_message_id_never_read_body(self):
        self.server.add(message(to="other@example.invalid"))
        result = self.client().reconcile("<one@example.invalid>", TO, SUBJECT, expected_message=message())
        self.assertEqual(result["status"], "copy_mismatch")
        self.assertTrue(all(query == transport.HEADER_FIELDS for _, query in self.server.imaps[0].fetches))

    def test_body_html_pdf_hash_and_pdf_filename_are_verified(self):
        for change in ({"plain": "different"}, {"html": "<p>different</p>"},
                       {"pdf": b"%PDF-1.4 altered"}, {"filename": "其他教师.pdf"}):
            with self.subTest(change=next(iter(change))):
                self.server = FakeServer()
                self.server.add(message(**change))
                result = self.client().reconcile("<one@example.invalid>", TO, SUBJECT, expected_message=message())
                self.assertEqual(result["status"], "copy_mismatch")

    def test_mime_boundaries_transfer_encoding_and_line_endings_can_change(self):
        raw = message()
        modified = raw.replace(b"\r\n", b"\n")
        self.server.add(modified)
        # The fake header splitter is CRLF-oriented, but the parser tolerates LF too.
        result = self.client().reconcile("<one@example.invalid>", TO, SUBJECT, expected_message=raw)
        self.assertEqual(result["status"], "copy_verified")

    def test_duplicate_message_id_is_ambiguous(self):
        self.server.add(message())
        self.server.add(message())
        result = self.client().reconcile("<one@example.invalid>", TO, SUBJECT, expected_message=message())
        self.assertEqual(result["error_code"], "sent_copy_ambiguous")
        self.assertEqual(result["status"], "blocked")

    def test_default_verify_does_not_append_missing_copy(self):
        self.server.auto_copy = False
        result = self.send()
        self.assertEqual(result["status"], "accepted_unconfirmed")
        self.assertTrue(result["smtp_accepted"])
        self.assertEqual(self.server.imaps[0].appends, [])

    def test_explicit_append_success_is_reverified(self):
        self.server.auto_copy = False
        result = self.send(self.client(copy_mode="append_if_missing"))
        self.assertEqual(result["status"], "accepted_confirmed")
        self.assertTrue(result["copy_appended"])
        self.assertEqual(len(self.server.imaps[0].appends), 1)
        self.assertTrue(result["smtp_accepted"])
        events = self.server.events
        self.assertLess(events.index("on_accepted"), events.index("append"))
        self.assertLess(events.index("append"), events.index("fetch_body"))

    def test_provider_copy_prevents_append(self):
        result = self.send(self.client(copy_mode="append_if_missing"))
        self.assertEqual(result["status"], "accepted_confirmed")
        self.assertFalse(result["copy_appended"])
        self.assertEqual(self.server.imaps[0].appends, [])

    def test_same_id_mismatch_prevents_append_even_with_resend_authorized(self):
        self.server.auto_copy = False
        self.server.add(message(pdf=b"wrong PDF"))
        result = self.send(self.client(copy_mode="append_if_missing"), allow_resend=True)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["error_code"], "reused_message_id")
        self.assertEqual(self.server.smtps, [])
        self.assertEqual(self.server.imaps[0].appends, [])

    def test_append_failure_never_hides_smtp_acceptance_or_retries(self):
        for reply in ("NO", socket.timeout("secret response")):
            with self.subTest(reply=type(reply).__name__):
                self.server = FakeServer()
                self.server.auto_copy, self.server.append_reply = False, reply
                client = self.client(copy_mode="append_if_missing")
                result = self.send(client)
                self.assertEqual(result["status"], "copy_failed")
                self.assertTrue(result["smtp_accepted"])
                self.assertEqual(len(self.server.smtps[0].data_calls), 1)
                self.assertEqual(self.send(client)["error_code"], "batch_stopped")
                self.assertNotIn("secret response", repr(result))

    def test_append_ok_without_verified_content_is_not_confirmation(self):
        self.server.auto_copy, self.server.store_append = False, False
        result = self.send(self.client(copy_mode="append_if_missing"))
        self.assertEqual(result["status"], "copy_failed")
        self.assertTrue(result["smtp_accepted"])

    def test_reconcile_missing_never_appends_or_connects_smtp(self):
        result = self.client(copy_mode="append_if_missing").reconcile(
            "<one@example.invalid>", TO, SUBJECT, expected_message=message())
        self.assertEqual(result["status"], "missing")
        self.assertEqual(self.server.imaps[0].appends, [])
        self.assertEqual(self.server.smtps, [])

    def test_attempt_callback_failure_never_sends_data(self):
        def failing(_):
            raise OSError("private path")
        client = self.client()
        result = client.send(message(), TO, SUBJECT, before_data=failing)
        self.assertEqual(result["error_code"], "attempt_persistence_failed")
        self.assertFalse(result["smtp_accepted"])
        self.assertEqual(self.server.smtps[0].data_calls, [])

    def test_acceptance_callback_failure_preserves_250_before_imap(self):
        def failing(_):
            raise OSError("private path")
        client = self.client()
        result = client.send(message(), TO, SUBJECT, before_data=self.before, on_accepted=failing)
        self.assertEqual(result["status"], "accepted_unconfirmed")
        self.assertTrue(result["smtp_accepted"])
        self.assertEqual(result["error_code"], "acceptance_persistence_failed")
        self.assertNotIn("fetch_body", self.server.events)
        self.assertEqual(self.send(client)["error_code"], "batch_stopped")

    def test_ambiguous_sent_folders_block_without_smtp(self):
        self.server.folders.append(b'(\\Sent) "/" "Another Sent"')
        result = self.client().duplicate(TO, SUBJECT)
        self.assertEqual(result["error_code"], "sent_folder_ambiguous")
        self.assertEqual(self.server.smtps, [])

    def test_fallback_names_are_exact_and_override_must_exist(self):
        self.server.folders = [b'(\\HasNoChildren) "/" "My Sent Archive"']
        self.assertEqual(self.client().duplicate(TO, SUBJECT)["error_code"], "sent_folder_missing")
        self.assertEqual(self.client(sent_folder="My Sent Archive").duplicate(TO, SUBJECT)["status"], "clear")
        self.assertEqual(self.client(sent_folder="absent").duplicate(TO, SUBJECT)["error_code"], "sent_folder_missing")

    def test_special_use_takes_precedence_over_name_fallback(self):
        self.server.folders = [b'(\\HasNoChildren) "/" "Sent"', b'(\\Sent) "/" "Actual"']
        client = self.client()
        self.assertEqual(client.duplicate(TO, SUBJECT)["status"], "clear")
        self.assertEqual(self.server.imaps[0].selects[0], (b'"Actual"', True))

    def test_modified_utf7_and_quoted_mailbox_names_are_safe(self):
        # 已发送 in IMAP modified UTF-7.
        self.server.folders = [b'(\\Sent) "/" "&XfJT0ZAB-"']
        client = self.client(sent_folder="已发送")
        self.assertEqual(client.duplicate(TO, SUBJECT)["status"], "clear")
        self.assertEqual(self.server.imaps[0].selects[0], (b'"&XfJT0ZAB-"', True))
        self.server = FakeServer()
        # Build a quoted mailbox without relying on shell/string escape behavior.
        self.server.folders = [b'(\\Sent) "/" ' + transport._quote(b'Sent "quoted" \\ path')]
        result = self.client(sent_folder='Sent "quoted" \\ path').duplicate(TO, SUBJECT)
        self.assertEqual(result["status"], "clear")
        self.assertEqual(self.server.imaps[0].selects[0][0], transport._quote(b'Sent "quoted" \\ path'))

    def test_literal_mailbox_name_is_supported(self):
        self.server.folders = [(b'(\\Sent) "/" {11}', b"Sent Folder")]
        self.assertEqual(self.client(sent_folder="Sent Folder").duplicate(TO, SUBJECT)["status"], "clear")

    def test_mailbox_injection_and_bad_encoding_are_blocked(self):
        for row in (b'(\\Sent) "/" "Sent\r\nINJECT"', b'(\\Sent) "/" "&invalid-"'):
            with self.subTest(row=row):
                self.server = FakeServer()
                self.server.folders = [row]
                self.assertEqual(self.client().duplicate(TO, SUBJECT)["status"], "blocked")
        with self.assertRaises(ValueError):
            self.client(sent_folder="Sent\r\nINJECT")

    def test_invalid_mime_and_envelope_mismatch_never_connect_smtp(self):
        for raw in (b"bad", message(to="other@example.invalid"), message(sender="other@example.invalid")):
            with self.subTest(raw_length=len(raw)):
                result = self.send(raw=raw)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(self.server.smtps, [])

    def test_extra_non_pdf_attachment_is_rejected(self):
        from email.parser import BytesParser
        msg = BytesParser(policy=policy.SMTP).parsebytes(message())
        msg.add_attachment(b"docx", maintype="application", subtype="octet-stream", filename="extra.docx")
        result = self.send(raw=msg.as_bytes())
        self.assertEqual(result["error_code"], "expected_unique_pdf")
        self.assertEqual(self.server.smtps, [])

    def test_imap_failure_is_sanitized_and_prevents_send(self):
        self.server.imap_error = OSError("authorization-code-and-entire-email")
        result = self.send()
        self.assertEqual(result["status"], "blocked")
        self.assertNotIn("authorization-code", repr(result))
        self.assertEqual(self.server.smtps, [])

    def test_context_manager_closes_and_clears_password(self):
        client = self.client()
        with client:
            self.assertEqual(client.duplicate(TO, SUBJECT)["status"], "clear")
        self.assertIsNone(client._password)
        self.assertEqual(client.duplicate(TO, SUBJECT)["error_code"], "transport_closed")

    def test_malformed_or_nonbytes_search_results_fail_closed(self):
        for payload in ([None], [b"1", None], [b"1:99"], [b"1 SEARCH extra"], "not-a-list"):
            with self.subTest(payload=repr(payload)):
                self.server = FakeServer()
                self.server.search_response = ("OK", payload)
                result = self.send()
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(self.server.smtps, [])

    def test_unexpected_final_data_responses_remain_unknown(self):
        for code in (150, 251, 354, 600):
            with self.subTest(code=code):
                self.server = FakeServer()
                self.server.data_reply = code
                result = self.send()
                self.assertEqual(result["status"], "unknown")
                self.assertIsNone(result["smtp_accepted"])
                self.assertEqual(self.server.imaps[0].appends, [])

    def test_accepted_unconfirmed_halts_batch(self):
        self.server.auto_copy = False
        client = self.client()
        self.assertEqual(self.send(client)["status"], "accepted_unconfirmed")
        self.assertEqual(self.send(client)["error_code"], "batch_stopped")
        self.assertEqual(len(self.server.smtps[0].data_calls), 1)

    def test_enter_failure_cleans_up_imap_connection(self):
        self.server.folders = []
        client = self.client()
        with self.assertRaisesRegex(transport.TransportError, "sent_folder_missing"):
            with client:
                self.fail("must not enter")
        self.assertEqual(self.server.imaps[0].logout_count, 1)
        self.assertIsNone(client._imap)
        self.assertIsNone(client._password)

    def test_plain_and_html_each_required_exactly_once(self):
        from email.parser import BytesParser
        for mode in ("plain_only", "duplicate_plain", "duplicate_html"):
            with self.subTest(mode=mode):
                msg = BytesParser(policy=policy.SMTP).parsebytes(message())
                alt = msg.get_payload()[0]
                if mode == "plain_only":
                    alt.set_payload([alt.get_payload()[0]])
                else:
                    extra = EmailMessage(policy=policy.SMTP)
                    extra.set_content("extra", subtype="html" if mode == "duplicate_html" else "plain")
                    alt.attach(extra)
                result = self.send(raw=msg.as_bytes())
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["error_code"], "expected_unique_pdf_and_alternative_body")
                self.assertEqual(self.server.smtps, [])

    def test_postlogin_id_precedes_list_and_select_once_per_connection(self):
        client = self.client()
        self.assertEqual(client.duplicate(TO, SUBJECT)["status"], "clear")
        self.assertEqual(client.duplicate(TO, SUBJECT)["status"], "clear")
        events = self.server.events
        self.assertLess(events.index("imap_login"), events.index("capability"))
        self.assertLess(events.index("capability"), events.index("id"))
        self.assertLess(events.index("id"), events.index("list"))
        self.assertLess(events.index("list"), events.index("select"))
        self.assertEqual(events.count("id"), 1)

    def test_missing_id_capability_does_not_send_unadvertised_extension(self):
        self.server.capabilities = [b"IMAP4rev1 X-IDLE IDLE"]
        self.assertEqual(self.client().duplicate(TO, SUBJECT)["status"], "clear")
        self.assertEqual(self.server.imaps[0].ids, [])
        self.assertNotIn("id", self.server.events)

    def test_id_rejection_or_exception_blocks_smtp_and_cleans_up(self):
        for reply in ("NO", OSError("private-authorization-code")):
            with self.subTest(reply=type(reply).__name__):
                self.server = FakeServer()
                self.server.id_reply = reply
                client = self.client()
                result = self.send(client)
                self.assertEqual(result["status"], "blocked")
                self.assertEqual(result["error_code"], "imap_id_failed")
                self.assertIsNone(client._imap)
                self.assertEqual(self.server.imaps[0].logout_count, 1)
                self.assertEqual(self.server.smtps, [])
                self.assertNotIn("list", self.server.events)
                self.assertNotIn("private-authorization-code", repr(result))

    def test_id_payload_is_fixed_tool_identity_without_private_information(self):
        self.server.capabilities = [b"imap4rev1 id"]
        result = self.client().duplicate(TO, SUBJECT)
        self.assertEqual(result["status"], "clear")
        self.assertEqual(self.server.imaps[0].ids,
                         [("ID", ('("name" "PhD-Outreach" "version" "1.0" "vendor" "LocalProject")',))])
        payload = repr(self.server.imaps[0].ids)
        for private in (SENDER, TO, "non-secret-fixture", "Windows", "OS", "网易", "support-email"):
            self.assertNotIn(private, payload)
        self.assertNotIn("private server ID response", repr(result))


if __name__ == "__main__":
    unittest.main(verbosity=2)
