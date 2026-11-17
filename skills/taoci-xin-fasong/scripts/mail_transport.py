"""Serial, TLS-only configurable SMTP/IMAP transport with conservative delivery evidence.

The caller owns authorization, immutable MIME preparation, the global ledger lock,
and durable callbacks.  This module never reads credentials from disk.  A 250 is
SMTP acceptance, never a delivery/read receipt; an interrupted DATA is never retried.
Factories have the standard SMTP_SSL / IMAP4_SSL constructor signatures so tests
can inject in-memory clients without opening sockets.
"""
from __future__ import annotations

import base64
import hashlib
import imaplib
import re
import smtplib
import socket
import ssl
import time
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
from typing import Callable

SMTP_HOST, SMTP_PORT = "smtp.example.invalid", 465
IMAP_HOST, IMAP_PORT = "imap.example.invalid", 993
IMAP_ID_ARGUMENT = '("name" "PhD-Outreach" "version" "1.0" "vendor" "LocalProject")'
COMMON_SENT_NAMES = frozenset({
    "Sent", "Sent Messages", "Sent Items", "已发送", "已发送邮件", "已发送的邮件",
})
HEADER_FIELDS = "(BODY.PEEK[HEADER.FIELDS (TO SUBJECT FROM MESSAGE-ID)])"
_LIST = re.compile(rb'^\(([^)]*)\)\s+(NIL|"(?:[^"\\]|\\.)*")\s+(.+)$')
_MESSAGE_ID = re.compile(r"<[^<>\s@]+@[^<>\s@]+>\Z")


class TransportError(Exception):
    """A stable, non-sensitive error code (never a raw server response)."""

    def __init__(self, code: str, *, phase: str | None = None, smtp_code: int | None = None):
        # Callers supply fixed program constants, never server text or secrets.
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", code):
            raise ValueError("invalid_transport_error_code")
        if phase is not None and phase not in {
                "imap_connect", "imap_login", "imap_capability", "imap_id", "imap_list",
                "imap_select", "smtp_connect", "smtp_ehlo", "smtp_login"}:
            raise ValueError("invalid_transport_error_phase")
        self.code, self.phase = code, phase
        self.smtp_code = smtp_code if type(smtp_code) is int and 100 <= smtp_code <= 599 else None
        super().__init__(code)


def _address(value: str) -> str:
    if not isinstance(value, str) or any(c in value for c in "\r\n\x00"):
        raise TransportError("invalid_address")
    parsed = getaddresses([value])
    if len(parsed) != 1 or not parsed[0][1] or parsed[0][1] != value:
        raise TransportError("invalid_address")
    value.encode("ascii")
    if not re.fullmatch(r"[^\s<>(),;:\\]+@[^\s<>(),;:\\]+", value):
        raise TransportError("invalid_address")
    return value.casefold()


def _header_address(message, key: str) -> tuple[str, ...]:
    headers = message.get_all(key, [])
    if len(headers) != 1:
        return ()
    try:
        return tuple(_address(address) for _, address in getaddresses([str(headers[0])]))
    except (TransportError, ValueError, UnicodeError):
        return ()


def _headers(raw: bytes) -> dict:
    message = BytesParser(policy=policy.default).parsebytes(raw, headersonly=True)
    return {
        "sender": _header_address(message, "From"),
        "to": _header_address(message, "To"),
        "subject": str(message.get("Subject", "")),
        "message_id": str(message.get("Message-ID", "")).strip(),
        "unique_headers": all(len(message.get_all(h, [])) == 1
                              for h in ("From", "To", "Subject", "Message-ID")),
    }


def _fingerprint(raw: bytes) -> dict:
    """Compare decoded content, ignoring added transport headers/MIME boundaries."""
    if not isinstance(raw, bytes) or not raw:
        raise TransportError("invalid_message")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    result = _headers(raw)
    if (not result["unique_headers"] or len(result["sender"]) != 1
            or len(result["to"]) != 1 or not _MESSAGE_ID.fullmatch(result["message_id"])
            or any(c in result["subject"] for c in "\r\n\x00")
            or message.get_all("Cc") or message.get_all("Bcc") or message.defects):
        raise TransportError("invalid_message_headers")
    text_parts, pdf_parts = [], []
    for part in message.walk():
        if part.is_multipart():
            continue
        if part.defects:
            raise TransportError("invalid_message_part")
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            raise TransportError("invalid_message_part")
        if filename or part.get_content_disposition() == "attachment":
            if (not filename or not filename.casefold().endswith(".pdf")
                    or part.get_content_type() != "application/pdf"):
                raise TransportError("expected_unique_pdf")
            pdf_parts.append((filename, hashlib.sha256(payload).hexdigest()))
        elif part.get_content_type() in {"text/plain", "text/html"}:
            try:
                text = payload.decode(part.get_content_charset() or "us-ascii")
            except (UnicodeError, LookupError):
                raise TransportError("invalid_text_encoding") from None
            text_parts.append((part.get_content_type(), text.replace("\r\n", "\n").replace("\r", "\n")))
        else:
            raise TransportError("unexpected_message_part")
    if (len(pdf_parts) != 1 or len(text_parts) != 2
            or {kind for kind, _ in text_parts} != {"text/plain", "text/html"}):
        raise TransportError("expected_unique_pdf_and_alternative_body")
    result.update(text_parts=tuple(text_parts), pdf_parts=tuple(pdf_parts))
    return result


def _unquote(raw: bytes) -> bytes:
    if raw.startswith(b'"'):
        if not re.fullmatch(rb'"(?:[^"\\]|\\["\\])*"', raw):
            raise TransportError("invalid_mailbox_list")
        raw = raw[1:-1]
        raw = re.sub(rb'\\(["\\])', rb'\1', raw)
    elif any(c in raw for c in (b" ", b"\t", b'"', b"\\")):
        raise TransportError("invalid_mailbox_list")
    if not raw or any(c in raw for c in (b"\r", b"\n", b"\x00")):
        raise TransportError("invalid_mailbox_list")
    return raw


def _decode_utf7(raw: bytes) -> str:
    """IMAP modified UTF-7; no third-party codec and no mailbox interpolation."""
    try:
        value = raw.decode("ascii")
        output, index = [], 0
        while index < len(value):
            if value[index] != "&":
                if not 0x20 <= ord(value[index]) <= 0x7e:
                    raise ValueError
                output.append(value[index])
                index += 1
                continue
            end = value.index("-", index)
            token = value[index + 1:end]
            if not token:
                output.append("&")
            else:
                encoded = token.replace(",", "/")
                decoded = base64.b64decode(encoded + "=" * (-len(encoded) % 4), validate=True)
                output.append(decoded.decode("utf-16-be"))
            index = end + 1
        return "".join(output)
    except (ValueError, UnicodeError):
        raise TransportError("invalid_mailbox_encoding") from None


def _quote(raw: bytes | str) -> bytes:
    raw = raw.encode("ascii") if isinstance(raw, str) else raw
    if any(c in raw for c in (b"\r", b"\n", b"\x00")):
        raise TransportError("unsafe_imap_argument")
    return b'"' + raw.replace(b"\\", b"\\\\").replace(b'"', b'\\"') + b'"'


def _ok(response) -> bool:
    return bool(response and str(response[0]).upper() == "OK")


def _result(status: str, **values) -> dict:
    return {"status": status, **values}


class MailTransport:
    """One serial batch, one IMAP connection, and a lazily opened SMTP connection.

    send(): clear/duplicate, accepted_confirmed/accepted_unconfirmed, rejected,
    unknown, blocked, or copy_failed. reconcile() never sends or APPENDs; it returns
    missing/headers_found/copy_verified/copy_mismatch/blocked. copy_verified is
    evidence of matching Sent content, and does not invent a previous SMTP 250.
    """

    def __init__(self, sender: str, password: str, *, copy_mode: str = "verify",
                 sent_folder: str | None = None, connect_timeout: float = 30,
                 data_timeout: float = 600, smtp_factory=None, imap_factory=None,
                 smtp_host: str = SMTP_HOST, smtp_port: int = SMTP_PORT,
                 imap_host: str = IMAP_HOST, imap_port: int = IMAP_PORT):
        self.sender = _address(sender)
        for host in (smtp_host, imap_host):
            if not isinstance(host, str) or not re.fullmatch(r"[A-Za-z0-9.-]+", host):
                raise ValueError("invalid_transport_host")
        if any(type(port) is not int or not 1 <= port <= 65535 for port in (smtp_port, imap_port)):
            raise ValueError("invalid_transport_port")
        self.smtp_host, self.smtp_port = smtp_host, smtp_port
        self.imap_host, self.imap_port = imap_host, imap_port
        if copy_mode not in {"verify", "append_if_missing"}:
            raise ValueError("invalid_copy_mode")
        if connect_timeout <= 0 or data_timeout <= 0:
            raise ValueError("invalid_timeout")
        if sent_folder is not None and (not sent_folder or any(c in sent_folder for c in "\r\n\x00")):
            raise ValueError("invalid_sent_folder")
        # Public examples must never reach a live credential/server flow.
        if (smtp_factory is None or imap_factory is None) and any(
                value.casefold().endswith(".invalid")
                for value in (self.sender.rsplit("@", 1)[-1], smtp_host, imap_host)):
            raise ValueError("configure_sender_and_tls_endpoints_before_connecting")
        self._password = password
        self.copy_mode, self.sent_folder = copy_mode, sent_folder
        self.connect_timeout, self.data_timeout = connect_timeout, data_timeout
        self._smtp_factory = smtp_factory or smtplib.SMTP_SSL
        self._imap_factory = imap_factory or imaplib.IMAP4_SSL
        self._smtp = self._imap = None
        self._mailbox = self._mailbox_name = None
        self._closed = self._halted = False

    def __enter__(self):
        try:
            self._ensure_imap()
        except Exception:
            self.close()
            raise
        return self

    def __exit__(self, *exc):
        self.close()

    def close(self):
        self._drop_smtp()
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                try:
                    self._imap.shutdown()
                except Exception:
                    pass
        self._smtp = self._imap = None
        self._password = None
        self._closed = True

    def _ensure_imap(self):
        if self._closed:
            raise TransportError("transport_closed")
        if self._imap is None:
            phase = "imap_connect"
            failure_codes = {
                "imap_connect": "imap_connect_failed", "imap_login": "imap_login_failed",
                "imap_capability": "imap_capability_failed", "imap_list": "imap_list_failed",
            }
            try:
                self._imap = self._imap_factory(self.imap_host, self.imap_port,
                    ssl_context=ssl.create_default_context(), timeout=self.connect_timeout)
                phase = "imap_login"
                if not _ok(self._imap.login(self.sender, self._password)):
                    raise TransportError("imap_login_failed", phase=phase)
                phase = "imap_capability"
                self._identify_imap()
                phase = "imap_list"
                self._discover_sent()
            except TransportError as exc:
                failure = TransportError(exc.code, phase=exc.phase or phase, smtp_code=exc.smtp_code)
            except imaplib.IMAP4.abort:
                # abort subclasses error: do not call a lost session a login rejection.
                failure = TransportError("imap_connect_failed" if phase == "imap_connect"
                                         else "imap_connection_lost", phase=phase)
            except imaplib.IMAP4.error:
                failure = TransportError(failure_codes[phase], phase=phase)
            except Exception:
                failure = TransportError(failure_codes[phase], phase=phase)
            else:
                return
            self._drop_imap()
            # Raise outside the handler so the safe error does not retain a raw reply
            # in __context__, even when the caller inspects the exception object.
            raise failure from None

    def _identify_imap(self):
        """Advertise only this tool's fixed identity, only after login and CAPABILITY.

        NetEase requires an ID-capable client to identify itself. No platform,
        account, authorization code, or server ID response is recorded or returned.
        """
        try:
            response = self._imap.capability()
        except Exception:
            raise TransportError("imap_capability_failed", phase="imap_capability") from None
        if (not _ok(response) or not isinstance(response[1], list)
                or any(not isinstance(item, bytes) for item in response[1])):
            raise TransportError("imap_capability_failed", phase="imap_capability")
        capabilities = set(b" ".join(response[1]).upper().split())
        if b"ID" in capabilities:
            try:
                response = self._imap.xatom("ID", IMAP_ID_ARGUMENT)
            except Exception:
                raise TransportError("imap_id_failed", phase="imap_id") from None
            if not _ok(response):
                raise TransportError("imap_id_failed", phase="imap_id")

    def _drop_imap(self):
        if self._imap is not None:
            try:
                self._imap.logout()
            except Exception:
                try:
                    self._imap.shutdown()
                except Exception:
                    pass
        self._imap = self._mailbox = self._mailbox_name = None

    def _drop_smtp(self):
        if self._smtp is not None:
            try:
                self._smtp.quit()
            except Exception:
                try:
                    self._smtp.close()
                except Exception:
                    pass
        self._smtp = None

    def _ensure_smtp(self):
        if self._closed:
            raise TransportError("transport_closed")
        if self._smtp is None:
            phase = "smtp_connect"
            try:
                self._smtp = self._smtp_factory(self.smtp_host, self.smtp_port,
                    context=ssl.create_default_context(), timeout=self.connect_timeout)
                phase = "smtp_ehlo"
                if self._smtp.ehlo()[0] != 250:
                    raise TransportError("smtp_ehlo_failed", phase=phase)
                phase = "smtp_login"
                self._smtp.login(self.sender, self._password)
            except smtplib.SMTPAuthenticationError as exc:
                reply = exc.smtp_error
                permission_denied = (
                    isinstance(reply, bytes) and b"User has no permission" in reply
                    or isinstance(reply, str) and "User has no permission" in reply)
                failure = TransportError("smtp_permission_denied" if permission_denied
                                         else "smtp_login_failed", phase=phase, smtp_code=exc.smtp_code)
                del reply
            except smtplib.SMTPServerDisconnected:
                failure = TransportError("smtp_connect_failed" if phase == "smtp_connect"
                                         else "smtp_connection_lost", phase=phase)
            except TransportError as exc:
                failure = TransportError(exc.code, phase=exc.phase or phase, smtp_code=exc.smtp_code)
            except Exception:
                failure = TransportError({
                    "smtp_connect": "smtp_connect_failed", "smtp_ehlo": "smtp_ehlo_failed",
                    "smtp_login": "smtp_login_failed",
                }[phase], phase=phase)
            else:
                return
            self._drop_smtp()
            raise failure from None

    def _discover_sent(self):
        response = self._imap.list()
        if not _ok(response):
            raise TransportError("imap_list_failed")
        folders = []
        for row in response[1] or []:
            if row in (None, b"", b")"):
                continue
            literal = None
            if isinstance(row, tuple) and len(row) == 2:
                row, literal = row
            if not isinstance(row, bytes):
                raise TransportError("invalid_mailbox_list")
            match = _LIST.fullmatch(row)
            if not match:
                raise TransportError("invalid_mailbox_list")
            flags, _, name = match.groups()
            if literal is not None:
                if not re.fullmatch(rb'\{\d+\}', name) or int(name[1:-1]) != len(literal):
                    raise TransportError("invalid_mailbox_list")
                raw_name = literal
                if not raw_name or any(c in raw_name for c in (b"\r", b"\n", b"\x00")):
                    raise TransportError("invalid_mailbox_list")
            else:
                raw_name = _unquote(name)
            flag_set = {flag.lower() for flag in flags.split()}
            if b"\\noselect" in flag_set:
                continue
            folders.append((raw_name, _decode_utf7(raw_name), b"\\sent" in flag_set))
        if self.sent_folder is not None:
            candidates = [f for f in folders if f[1] == self.sent_folder
                          or f[0].decode("ascii") == self.sent_folder]
        else:
            candidates = [f for f in folders if f[2]]
            if not candidates:
                candidates = [f for f in folders if f[1] in COMMON_SENT_NAMES]
        # A duplicate LIST row is harmless, but different matching mailboxes are not.
        candidates = list({f[0]: f for f in candidates}.values())
        if len(candidates) != 1:
            raise TransportError("sent_folder_ambiguous" if candidates else "sent_folder_missing")
        self._mailbox, self._mailbox_name, _ = candidates[0]

    def _select(self):
        self._ensure_imap()
        if not _ok(self._imap.select(_quote(self._mailbox), readonly=True)):
            raise TransportError("sent_select_failed")

    def _search(self, *criteria) -> list[bytes]:
        response = self._imap.uid("SEARCH", None, *criteria)
        if not _ok(response) or not isinstance(response[1], list):
            raise TransportError("imap_search_failed")
        if any(not isinstance(item, bytes) for item in response[1]):
            raise TransportError("invalid_search_response")
        raw = b" ".join(response[1])
        if raw and not re.fullmatch(rb'\d+(?:\s+\d+)*', raw):
            raise TransportError("invalid_search_response")
        return list(dict.fromkeys(raw.split()))

    def _fetch(self, uid: bytes, query: str) -> bytes:
        response = self._imap.uid("FETCH", uid, query)
        if not _ok(response):
            raise TransportError("imap_fetch_failed")
        bodies = [r[1] for r in response[1] or []
                  if isinstance(r, tuple) and len(r) == 2 and isinstance(r[1], bytes)]
        if len(bodies) != 1:
            raise TransportError("invalid_fetch_response")
        return bodies[0]

    def probe(self) -> dict:
        """Authenticate both TLS channels without MAIL, RCPT, DATA, or APPEND."""
        try:
            self._select()
            self._ensure_smtp()
            return _result("ready", smtp_connected=True, imap_connected=True,
                           sent_folder=self._mailbox_name)
        except TransportError as exc:
            details = {"error_code": exc.code}
            if exc.phase is not None:
                details["phase"] = exc.phase
            if exc.smtp_code is not None:
                details["smtp_code"] = exc.smtp_code
            return _result("blocked", **details)
        except Exception:
            return _result("blocked", error_code="probe_failed")

    def duplicate(self, to: str, subject: str) -> dict:
        """Remote substring search is narrowed with exact decoded header matching."""
        try:
            to = _address(to)
            if not isinstance(subject, str) or any(c in subject for c in "\r\n\x00"):
                raise TransportError("invalid_subject")
            self._select()
            matches = []
            for uid in self._search("HEADER", "FROM", _quote(self.sender),
                                    "HEADER", "TO", _quote(to)):
                headers = _headers(self._fetch(uid, HEADER_FIELDS))
                if (headers["sender"] == (self.sender,) and headers["to"] == (to,)
                        and headers["subject"] == subject):
                    matches.append(uid.decode("ascii"))
            return _result("duplicate" if matches else "clear", match_count=len(matches),
                           sent_uid=matches[0] if len(matches) == 1 else None)
        except TransportError as exc:
            return _result("blocked", error_code=str(exc))
        except Exception:
            return _result("blocked", error_code="duplicate_check_failed")

    def reconcile(self, message_id: str, to: str, subject: str, *,
                  prior_status: str = "unknown", expected_message: bytes | None = None) -> dict:
        """Read-only reconciliation; it never sends, appends, or asserts a new 250."""
        base = {"message_id": message_id, "prior_status": prior_status,
                "smtp_accepted": None if prior_status == "unknown" else False}
        try:
            to = _address(to)
            if not isinstance(message_id, str) or not _MESSAGE_ID.fullmatch(message_id):
                raise TransportError("invalid_message_id")
            expected = _fingerprint(expected_message) if expected_message is not None else None
            if expected is not None and not self._matches(expected, message_id, to, subject):
                raise TransportError("expected_message_mismatch")
            self._select()
            candidates = []
            for uid in self._search("HEADER", "MESSAGE-ID", _quote(message_id)):
                headers = _headers(self._fetch(uid, HEADER_FIELDS))
                if headers["message_id"] == message_id:
                    candidates.append((uid, headers))
            if not candidates:
                return _result("missing", **base)
            if len(candidates) != 1:
                return _result("blocked", error_code="sent_copy_ambiguous", **base)
            uid, headers = candidates[0]
            evidence = {**base, "sent_uid": uid.decode("ascii")}
            if not self._matches(headers, message_id, to, subject):
                return _result("copy_mismatch", error_code="sent_headers_mismatch", **evidence)
            if expected is None:
                return _result("headers_found", **evidence)
            actual = _fingerprint(self._fetch(uid, "(BODY.PEEK[])"))
            if actual != expected:
                return _result("copy_mismatch", error_code="sent_content_mismatch", **evidence)
            return _result("copy_verified", content_verified=True, **evidence)
        except TransportError as exc:
            return _result("blocked", error_code=str(exc), **base)
        except Exception:
            return _result("blocked", error_code="reconcile_failed", **base)

    def _matches(self, headers: dict, message_id: str, to: str, subject: str) -> bool:
        return (headers["unique_headers"] and headers["sender"] == (self.sender,)
                and headers["to"] == (to,) and headers["subject"] == subject
                and headers["message_id"] == message_id)

    def send(self, message: bytes, to: str, subject: str, *, before_data: Callable,
             prior_status: str | None = None, allow_resend: bool = False,
             on_accepted: Callable | None = None) -> dict:
        """Send one immutable MIME message, recording an attempt before DATA.

        on_accepted is called immediately after the final SMTP 250, before any
        IMAP work. A callback failure retains smtp_accepted=True in the result.
        Failure/uncertainty halts this object's subsequent send calls.
        """
        if prior_status == "unknown" or self._halted:
            return _result("blocked", error_code="unknown_requires_reconcile" if prior_status == "unknown"
                           else "batch_stopped", smtp_accepted=None if prior_status == "unknown" else False)
        message_id = None
        try:
            to = _address(to)
            expected = _fingerprint(message)
            message_id = expected["message_id"]
            if not self._matches(expected, message_id, to, subject):
                raise TransportError("message_envelope_mismatch")
            if not callable(before_data) or (on_accepted is not None and not callable(on_accepted)):
                raise TransportError("invalid_persistence_callback")
            duplicate = self.duplicate(to, subject)
            if duplicate["status"] == "blocked":
                return {**duplicate, "message_id": message_id, "smtp_accepted": False}
            if duplicate["status"] == "duplicate" and not allow_resend:
                return {**duplicate, "message_id": message_id, "smtp_accepted": False}
            if duplicate["status"] == "duplicate":
                # An authorized resend is a new message. Reusing a Message-ID
                # could mistake the old Sent copy for this attempt's evidence.
                existing_id = self.reconcile(message_id, to, subject, prior_status="pre_send")
                if existing_id["status"] != "missing":
                    self._halted = True
                    reused = existing_id["status"] in {"headers_found", "copy_verified", "copy_mismatch"}
                    return _result("blocked", message_id=message_id, smtp_accepted=False,
                                   error_code="reused_message_id" if reused else
                                   existing_id.get("error_code", "resend_id_check_failed"),
                                   reason="new_bundle_required")
            self._ensure_smtp()
            for command, arg in ((self._smtp.mail, self.sender), (self._smtp.rcpt, to)):
                code, _ = command(arg)
                if not 200 <= code < 300:
                    self._halted = True
                    return _result("rejected", smtp_code=code, message_id=message_id,
                                   smtp_accepted=False, error_code="smtp_envelope_rejected")
            try:
                before_data({"message_id": message_id, "sender": self.sender, "to": to,
                             "subject": subject, "phase": "before_data",
                             "attempt_at": datetime.now(timezone.utc).isoformat()})
            except Exception:
                self._halted = True
                return _result("blocked", message_id=message_id, smtp_accepted=False,
                               error_code="attempt_persistence_failed")
            try:
                previous_timeout = self._smtp.sock.gettimeout()
                self._smtp.sock.settimeout(self.data_timeout)
            except Exception:
                self._halted = True
                return _result("blocked", message_id=message_id, smtp_accepted=False,
                               error_code="data_timeout_setup_failed")
            try:
                code, _ = self._smtp.data(message)
            except smtplib.SMTPDataError as exc:
                # smtplib.data raises this before transmitting the body if the
                # initial DATA command did not receive 354.
                self._halted = True
                return _result("rejected", smtp_code=exc.smtp_code, message_id=message_id,
                               smtp_accepted=False, error_code="smtp_data_rejected")
            except smtplib.SMTPResponseException as exc:
                self._halted = True
                rejected = isinstance(exc.smtp_code, int) and 400 <= exc.smtp_code < 600
                return _result("rejected" if rejected else "unknown", smtp_code=exc.smtp_code,
                               message_id=message_id, smtp_accepted=False if rejected else None,
                               error_code="smtp_data_rejected" if rejected else "smtp_data_outcome_unknown")
            except Exception:
                self._halted = True
                return _result("unknown", message_id=message_id, smtp_accepted=None,
                               error_code="smtp_data_outcome_unknown")
            finally:
                try:
                    self._smtp.sock.settimeout(previous_timeout)
                except Exception:
                    pass
            if code != 250:
                self._halted = True
                rejected = isinstance(code, int) and 400 <= code < 600
                return _result("rejected" if rejected else "unknown", smtp_code=code,
                               message_id=message_id, smtp_accepted=False if rejected else None,
                               error_code="smtp_data_rejected" if rejected else "smtp_data_outcome_unknown")
        except smtplib.SMTPResponseException as exc:
            self._halted = True
            return _result("rejected", message_id=message_id, smtp_accepted=False,
                           smtp_code=exc.smtp_code, error_code="smtp_envelope_rejected")
        except TransportError as exc:
            self._halted = True
            return _result("blocked", message_id=message_id, smtp_accepted=False, error_code=str(exc))
        except Exception:
            self._halted = True
            return _result("blocked", message_id=message_id, smtp_accepted=False,
                           error_code="smtp_before_data_failed")
        accepted = _result("smtp_accepted", smtp_accepted=True, smtp_code=250,
                           message_id=message_id,
                           accepted_at=datetime.now(timezone.utc).isoformat())
        if on_accepted is not None:
            try:
                on_accepted(dict(accepted))
            except Exception:
                self._halted = True
                return {**accepted, "status": "accepted_unconfirmed",
                        "error_code": "acceptance_persistence_failed"}
        verified = self.reconcile(message_id, to, subject, prior_status="smtp_accepted",
                                  expected_message=message)
        if verified["status"] == "copy_verified":
            return {**accepted, "status": "accepted_confirmed", "sent_uid": verified["sent_uid"],
                    "content_verified": True, "copy_appended": False}
        if verified["status"] != "missing" or self.copy_mode == "verify":
            self._halted = True
            return {**accepted, "status": "accepted_unconfirmed",
                    "error_code": verified.get("error_code", "sent_copy_missing")}
        # Only a confirmed 250 plus an exact Message-ID miss can authorize APPEND.
        try:
            response = self._imap.append(_quote(self._mailbox), "(\\Seen)",
                                        imaplib.Time2Internaldate(time.time()), message)
            if not _ok(response):
                raise TransportError("sent_append_failed")
        except Exception:
            self._halted = True
            return {**accepted, "status": "copy_failed", "error_code": "sent_append_failed"}
        verified = self.reconcile(message_id, to, subject, prior_status="smtp_accepted",
                                  expected_message=message)
        if verified["status"] == "copy_verified":
            return {**accepted, "status": "accepted_confirmed", "sent_uid": verified["sent_uid"],
                    "content_verified": True, "copy_appended": True}
        self._halted = True
        return {**accepted, "status": "copy_failed", "copy_appended": True,
                "error_code": verified.get("error_code", "appended_copy_unverified")}
