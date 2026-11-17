"""Raw-byte MIME regression checks; sockets/DNS are disabled for every test."""

from __future__ import annotations

import base64
from email import policy
from email.parser import BytesParser
import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("test_wire_validator", ROOT / "scripts" / "mime_wire.py")
wire = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wire)

PLAIN = "示例导师老师您好！\r\n学生申请博士。\r\n".encode()
HTML = '<!doctype html><html><head><title>预览</title></head><body><p>示例导师老师您好！</p></body></html>\r\n'.encode()
PDF = b"%PDF-1.7\n%independent wire fixture\n%%EOF\n"
OUTER, INNER = "m_0123456789abcdef0123456789abcdef", "a_abcdef0123456789abcdef0123456789"


def leaf(media, payload, *, disposition=None, version=False, cte="base64"):
    headers = [b"Content-Type: " + media.encode(), b"Content-Transfer-Encoding: " + cte.encode()]
    if disposition:
        headers.append(b"Content-Disposition: " + disposition.encode())
    if version:
        headers.append(b"MIME-Version: 1.0")
    encoded = base64.b64encode(payload)
    return b"\r\n".join(headers) + b"\r\n\r\n" + b"\r\n".join(encoded[i:i + 76] for i in range(0, len(encoded), 76)) + b"\r\n"


def multipart(media, boundary, parts):
    marker = b"--" + boundary.encode()
    return (b'Content-Type: multipart/' + media.encode() + b'; boundary="' + boundary.encode() + b'"\r\n\r\n'
            + b"".join(marker + b"\r\n" + part + b"\r\n" for part in parts) + marker + b"--\r\n")


def message(plain=PLAIN, rich=HTML, pdf=PDF, filename="attachment; filename*=utf-8''%E8%87%AA%E6%88%91%E9%99%88%E8%BF%B0-%E7%A4%BA%E4%BE%8B%E5%AF%BC%E5%B8%88.pdf", outer=OUTER, inner=INNER):
    alternative = multipart("alternative", inner, [leaf('text/plain; charset="utf-8"', plain), leaf('text/html; charset="utf-8"', rich, version=True)])
    mixed = multipart("mixed", outer, [alternative, leaf("application/pdf", pdf, disposition=filename, version=True)])
    return (b"From: sender@example.invalid\r\nTo: advisor@example.invalid\r\nSubject: fixed project subject\r\n"
            b"Message-ID: <fixed@example.invalid>\r\nDate: Sat, 01 Jan 2000 00:00:00 +0000\r\nMIME-Version: 1.0\r\n" + mixed)


class WireTests(unittest.TestCase):
    def setUp(self):
        self.network = []
        for target in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex", "socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex"):
            patch = mock.patch(target, side_effect=AssertionError("Wire regression may never use network/DNS"))
            self.network.append(patch.start())
            self.addCleanup(patch.stop)

    def tearDown(self):
        for operation in self.network:
            operation.assert_not_called()

    def test_independent_raw_fixture_structure_chinese_name_and_all_decoded_hashes(self):
        result = wire.validate_wire(message())
        self.assertEqual(result["filename"], "自我陈述-示例导师.pdf")
        self.assertEqual(result["boundaries"], {"mixed": OUTER, "alternative": INNER})
        self.assertEqual(result["payloads"], {name: {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                                              for name, data in (("plain", PLAIN), ("html", HTML), ("pdf", PDF))})
        parsed = BytesParser(policy=policy.SMTP).parsebytes(message())
        self.assertEqual(next(parsed.iter_attachments()).get_filename(), result["filename"])

    def test_rfc2231_filename_continuations_are_valid_and_decode_chinese(self):
        raw = message(filename="attachment;\r\n filename*0*=utf-8''%E8%87%AA%E6%88%91;\r\n filename*1*=%E9%99%88%E8%BF%B0-%E7%A4%BA%E4%BE%8B%E5%AF%BC%E5%B8%88.pdf")
        self.assertEqual(wire.validate_wire(raw)["filename"], "自我陈述-示例导师.pdf")
        self.assertEqual(next(BytesParser(policy=policy.SMTP).parsebytes(raw).iter_attachments()).get_filename(), "自我陈述-示例导师.pdf")


    def test_boundary_lengths_collision_extensions_quoting_and_escapes(self):
        self.assertEqual(wire.validate_wire(message(outer="m" * 70))["boundary_lengths"]["mixed"], 70)
        self.assertEqual(wire.validate_wire(message(outer="m"))["boundary_lengths"]["mixed"], 1)
        raw = message()
        cases = {
            "long inner": message(inner="a" * 71),
            "long outer": message(outer="m" * 71),
            "empty": raw.replace(OUTER.encode(), b""),
            "collision": message(inner=OUTER),
            "prefix": message(inner=OUTER + "x"),
            "unquoted": raw.replace(('boundary="' + OUTER + '"').encode(), ("boundary=" + OUTER).encode()),
            "extended": raw.replace(('boundary="' + OUTER + '"').encode(), ("boundary*=us-ascii''" + OUTER).encode()),
            "continuation": raw.replace(('boundary="' + OUTER + '"').encode(), ("boundary*0*=us-ascii''" + OUTER[:10] + ";\r\n boundary*1*=" + OUTER[10:]).encode()),
            "escaped": raw.replace(('boundary="' + OUTER + '"').encode(), ('boundary="\\' + OUTER + '"').encode()),
            "duplicate": raw.replace(('boundary="' + OUTER + '"').encode(), ('boundary="' + OUTER + '"; boundary="x"').encode()),
        }
        for name, changed in cases.items():
            with self.subTest(name=name), self.assertRaises(wire.WireError):
                wire.validate_wire(changed)

    def test_boundary_open_close_extra_parts_wrong_hierarchy_and_framing(self):
        raw = message()
        marker = b"--" + OUTER.encode()
        cases = {
            "missing open": raw.replace(marker + b"\r\n", b"", 1),
            "missing close": raw.replace(marker + b"--\r\n", b""),
            "inner missing close": raw.replace(b"--" + INNER.encode() + b"--\r\n", b""),
            "suffix": raw.replace(marker + b"\r\n", marker + b" \r\n", 1),
            "inner suffix": raw.replace(b"--" + INNER.encode() + b"\r\n", b"--" + INNER.encode() + b"oops\r\n", 1),
            "preamble": raw.replace(marker + b"\r\n", b"preamble\r\n" + marker + b"\r\n", 1),
            "epilogue": raw + b"extra\r\n",
            "LF only": raw.replace(b"\r\n", b"\n"),
            "bare CR": raw.replace(b"To: ", b"To:\r", 1),
            "no final CRLF": raw[:-2],
            "duplicate closing": raw + marker + b"--\r\n",
            "wrong top": raw.replace(b"multipart/mixed", b"multipart/alternative", 1),
            "wrong nested": raw.replace(b"multipart/alternative", b"multipart/mixed", 1),
            "extra pdf": raw.replace(marker + b"--\r\n", marker + b"\r\n" + leaf("application/pdf", PDF, disposition='attachment; filename="extra.pdf"', version=True) + b"\r\n" + marker + b"--\r\n"),
            "wrong text order": raw.replace(b"text/plain", b"text/TEMP").replace(b"text/html", b"text/plain").replace(b"text/TEMP", b"text/html"),
        }
        for name, changed in cases.items():
            with self.subTest(name=name), self.assertRaises(wire.WireError):
                wire.validate_wire(changed)

    def test_header_duplicates_injection_extra_disposition_line_length_and_cte(self):
        raw = message()
        cases = [b"Bcc: third@example.invalid\r\n" + raw, b"To: other@example.invalid\r\n" + raw,
                 raw.replace(b"Content-Type: text/plain;", b"Content-Type: text/plain; charset=ascii;"),
                 raw.replace(b"Content-Transfer-Encoding: base64", b"Content-Transfer-Encoding: base64\r\ncontent-transfer-encoding: base64", 1),
                 raw.replace(b"Content-Transfer-Encoding: base64", b"Content-Disposition: attachment\r\nContent-Transfer-Encoding: base64", 1),
                 raw.replace(b"To: advisor@example.invalid", b"To: advisor@example.invalid,other@example.invalid"),
                 raw.replace(b"Subject: fixed project subject", b"Subject: " + b"x" * 999),
                 raw.replace(b"Subject: fixed project subject", b"Subject: x\x00y"),
                 raw.replace(b"Content-Transfer-Encoding: base64", b"Content-Transfer-Encoding: 8bit", 1),
                 raw.replace(b"Date: Sat, 01 Jan 2000", b"Date: Sun, 01 Jan 2000"),
                 raw.replace(b'MIME-Version: 1.0', b'MIME-Version: 2.0', 1),
                 raw.replace(b'charset="utf-8"', b'charset="gbk"', 1)]
        for index, changed in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(wire.WireError):
                wire.validate_wire(changed)

    def test_empty_invalid_bodies_base64_pdf_and_filename_fail_closed(self):
        cases = [message(plain=b""), message(plain=b" \r\n"), message(rich=b""),
                 message(rich=b"<html><head><title>invisible</title></head><body> </body></html>"),
                 message(rich=b"visible text but no html"), message(plain=b"\xff\xfe\r\n"),
                 message(pdf=b"not PDF"), message(pdf=b"%PDF-1.7\ntruncated"),
                 message(filename='attachment; filename="../escape.pdf"'),
                 message(filename="attachment; filename*=utf-8''invalid%ZZ.pdf"),
                 message(filename="attachment; filename*=utf-8''%FF.pdf"),
                 message(filename="attachment; filename*0*=utf-8''a; filename*2*=b.pdf"),
                 message(filename="attachment; filename*=utf-8''a.pdf; filename=b.pdf"),
                 message(filename='inline; filename="a.pdf"'),
                 message().replace(base64.b64encode(PLAIN), b"!" + base64.b64encode(PLAIN)[1:]),
                 message().replace(base64.b64encode(PLAIN), base64.b64encode(PLAIN)[:-1]),
                 message().replace(base64.b64encode(PLAIN), base64.b64encode(PLAIN) + b"AAAA")]
        for index, changed in enumerate(cases):
            with self.subTest(case=index), self.assertRaises(wire.WireError):
                wire.validate_wire(changed)

    def test_valid_payload_changes_are_observable_hashes_not_silent_acceptance(self):
        first = wire.validate_wire(message())
        changed = wire.validate_wire(message(plain=PLAIN.replace("博士".encode(), "硕士".encode())))
        self.assertNotEqual(first["payloads"]["plain"]["sha256"], changed["payloads"]["plain"]["sha256"])
        self.assertEqual(first["payloads"]["pdf"], changed["payloads"]["pdf"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
