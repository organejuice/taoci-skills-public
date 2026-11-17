"""Synthetic, deterministic, network-disabled HTML/MIME safety tests."""

from __future__ import annotations

from email import policy
from email.parser import BytesParser
import hashlib
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
from zipfile import ZIP_DEFLATED, ZipFile

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("mail_compose", ROOT / "scripts" / "mail_compose.py")
compose = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(compose)
FIXTURE_SPEC = importlib.util.spec_from_file_location("compose_fixtures", Path(__file__).with_name("test_send_preflight.py"))
fixtures = importlib.util.module_from_spec(FIXTURE_SPEC)
FIXTURE_SPEC.loader.exec_module(fixtures)


class HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paragraphs = []
        self.paragraph_styles = []
        self.span_styles = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self.active = []
            self.paragraph_styles.append(dict(attrs).get("style", ""))
        if tag == "span" and self.active is not None:
            self.span_styles.append(dict(attrs).get("style", ""))
        if tag == "br" and self.active is not None:
            self.active.append("\n")

    def handle_endtag(self, tag):
        if tag == "p":
            self.paragraphs.append("".join(self.active))
            self.active = None

    def handle_data(self, data):
        if self.active is not None:
            self.active.append(data)


class ComposeTests(unittest.TestCase):
    write_config = fixtures.OfflinePreflightTests.write_config
    write_roster = fixtures.OfflinePreflightTests.write_roster
    letter_paragraphs = fixtures.OfflinePreflightTests.letter_paragraphs
    statement_paragraphs = fixtures.OfflinePreflightTests.statement_paragraphs
    write_materials = fixtures.OfflinePreflightTests.write_materials

    def setUp(self):
        fixtures.OfflinePreflightTests.setUp(self)
        self.network = []
        for target in ("socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyname_ex", "socket.create_connection", "socket.socket.connect", "socket.socket.connect_ex"):
            patch = mock.patch(target, side_effect=AssertionError("Offline composer must never use the network"))
            self.network.append(patch.start())
            self.addCleanup(patch.stop)

    def tearDown(self):
        for operation in self.network:
            operation.assert_not_called()

    def prepare(self, advisor="M001", suffix="bundle"):
        return compose.prepare_bundle(self.config_path, advisor, self.work / suffix)

    def check(self, path):
        return compose.check_bundle(self.config_path, path)

    def manifest(self, path):
        return json.loads(path.read_text(encoding="utf-8"))

    def write_manifest(self, path, manifest):
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    def letter_path(self):
        return self.materials / "1-套磁信-示例导师.docx"

    def modify_xml(self, transform):
        path = self.letter_path()
        with ZipFile(path) as archive:
            members = {name: archive.read(name) for name in archive.namelist()}
        members["word/document.xml"] = transform(members["word/document.xml"].decode("utf-8")).encode("utf-8")
        with ZipFile(path, "w", ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)

    def test_source_bound_bundle_exactly_one_personal_pdf_and_chinese_filename(self):
        before = {p: compose.preflight.fingerprint(p) for p in self.materials.iterdir()}
        path = self.prepare()
        result = self.check(path)
        self.assertEqual(result["status"], "offline_passed_needs_live_review")
        self.assertEqual(result["sender"], "sender@example.invalid")
        self.assertEqual(result["recipient"], "advisor@example.invalid")
        manifest = result["bundle"]
        self.assertEqual(set(p.name for p in path.parent.iterdir()), {"plan.json", "body.txt", "body.html", "message.eml", "bundle.json"})
        parsed = BytesParser(policy=policy.SMTP).parsebytes(result["eml_bytes"])
        self.assertEqual(str(parsed["Message-ID"]), manifest["message_id"])
        self.assertEqual(len(list(parsed.iter_attachments())), 1)
        pdf = next(parsed.iter_attachments())
        self.assertEqual(pdf.get_filename(), "自我陈述-示例导师.pdf")
        self.assertEqual(pdf.get_content_type(), "application/pdf")
        self.assertEqual(pdf.get_payload(decode=True), (self.materials / "自我陈述-示例导师.pdf").read_bytes())
        self.assertEqual(manifest["outputs"]["message_eml"]["sha256"], hashlib.sha256(result["eml_bytes"]).hexdigest())
        self.assertEqual(manifest["schema_version"], 2)
        self.assertIn("mime_wire", manifest["generators"])
        self.assertEqual(result["wire_validation"]["filename"], "自我陈述-示例导师.pdf")
        self.assertEqual(result["wire_validation"]["payloads"]["pdf"], {"bytes": len(result["attachment_bytes"]), "sha256": hashlib.sha256(result["attachment_bytes"]).hexdigest()})
        self.assertTrue(all(1 <= length <= 70 for length in result["wire_validation"]["boundary_lengths"].values()))
        self.assertNotIn(b"boundary*", result["eml_bytes"])
        self.assertEqual(before, {p: compose.preflight.fingerprint(p) for p in before})

    def test_html_escape_no_remote_resources_no_template_labels_and_plain_text_scope(self):
        paragraphs = self.letter_paragraphs()
        paragraphs[3] = '学生申请2030博士。<script>alert("x")</script> & <img src="https://remote.invalid/pixel">'
        fixtures.write_docx(self.letter_path(), paragraphs)
        path = self.prepare()
        result = self.check(path)
        html = result["html_text"]
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&amp;", html)
        self.assertNotIn("<script", html)
        self.assertNotIn("<img", html)
        self.assertNotIn("邮件标题", html)
        self.assertNotIn("邮件内容", html)
        self.assertIn("default-src 'none'", html)
        parser = HTMLText()
        parser.feed(html)
        self.assertEqual(parser.paragraphs, [p.lstrip() if i >= len(paragraphs[2:9]) - 2 else p for i, p in enumerate(paragraphs[2:9])])
        self.assertEqual((path.parent / "body.txt").read_text(encoding="utf-8"), result["plan"]["body_text"] + "\n")

    def test_run_order_blue_bold_red_editing_mark_and_current_source_font(self):
        self.modify_xml(lambda xml: xml.replace("<w:r><w:t xml:space=\"preserve\">学生申请2030博士。</w:t></w:r>",
            '<w:r><w:rPr><w:color w:val="0000FF"/><w:b/><w:sz w:val="24"/><w:rFonts w:eastAsia="宋体"/></w:rPr><w:t>学生</w:t></w:r>'
            '<w:r><w:rPr><w:color w:val="FF0000"/></w:rPr><w:t>申请</w:t></w:r>'
            '<w:r><w:t>2030博士。</w:t></w:r>'))
        result = self.check(self.prepare())
        html = result["html_text"]
        self.assertIn("color:#0000FF", html)
        self.assertIn("font-weight:bold", html)
        self.assertIn("font-size:12pt", html)
        self.assertIn("宋体", html)
        self.assertNotIn("color:#FF0000", html)
        self.assertIn('color:#000000">申请</span>', html)
        parser = HTMLText()
        parser.feed(html)
        self.assertEqual(parser.paragraphs[1], "学生申请2030博士。")

    def test_paragraph_indent_spacing_signature_right_alignment_and_plain_spaces(self):
        self.modify_xml(lambda xml: xml.replace('<w:p><w:r><w:t xml:space="preserve">学生申请2030博士。',
            '<w:p><w:pPr><w:ind w:firstLineChars="200"/><w:spacing w:line="360" w:lineRule="auto"/></w:pPr><w:r><w:t xml:space="preserve">学生申请2030博士。'))
        result = self.check(self.prepare())
        self.assertIn("text-indent:2em", result["html_text"])
        self.assertIn("line-height:1.5", result["html_text"])
        self.assertEqual(result["html_text"].count("text-align:right"), 2)
        self.assertNotIn("      学生", result["html_text"])
        self.assertIn("      学生：示例申请人", result["plan"]["body_text"])

    def test_email_greeting_starts_flush_body_keeps_source_indent_and_no_outer_padding(self):
        def with_indents(xml):
            for text in ("示例导师老师您好！", "学生申请2030博士。"):
                old = f'<w:p><w:r><w:t xml:space="preserve">{text}'
                new = ('<w:p><w:pPr><w:ind w:firstLineChars="200"/>'
                       '<w:spacing w:line="360" w:lineRule="auto"/></w:pPr>'
                       f'<w:r><w:t xml:space="preserve">{text}')
                self.assertIn(old, xml)
                xml = xml.replace(old, new)
            return xml
        self.modify_xml(with_indents)
        result = self.check(self.prepare())
        parser = HTMLText()
        parser.feed(result["html_text"])
        def css(style):
            return dict(declaration.split(":", 1) for declaration in style.split(";") if declaration)
        self.assertEqual(parser.paragraphs[0], "示例导师老师您好！")
        self.assertEqual(css(parser.paragraph_styles[0])["text-indent"], "0")
        self.assertEqual(css(parser.paragraph_styles[1])["text-indent"], "2em")
        self.assertEqual(css(parser.paragraph_styles[1])["line-height"], "1.5")
        self.assertIn('<body style="margin:0;', result["html_text"])
        self.assertNotIn("margin:24px", result["html_text"])

    def test_fixed_bytes_id_and_read_only_recheck(self):
        path = self.prepare()
        before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in path.parent.iterdir()}
        first, second = self.check(path), self.check(path)
        self.assertEqual(first["eml_bytes"], second["eml_bytes"])
        self.assertEqual(first["message_id"], second["message_id"])
        self.assertEqual(before, {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in before})
        other = self.check(self.prepare(suffix="another"))
        self.assertNotEqual(first["message_id"], other["message_id"])

    def test_same_name_school_attachment_resolution(self):
        for advisor, school, address in (("M002", "示例机构甲", "advisor-a@example.invalid"), ("M003", "示例机构乙", "advisor-b@example.invalid")):
            result = self.check(self.prepare(advisor, advisor))
            self.assertEqual(result["recipient"], address)
            self.assertEqual(result["bundle"]["attachments"][0]["name"], f"自我陈述-同名导师-{school}.pdf")

    def test_source_metadata_and_attachment_tampering(self):
        path = self.prepare()
        source = self.letter_path()
        original = source.read_bytes()
        with ZipFile(source, "a", ZIP_DEFLATED) as archive:
            archive.writestr("metadata.txt", "source changed")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.check(path)
        source.write_bytes(original)
        pdf = self.materials / "自我陈述-示例导师.pdf"
        pdf.write_bytes(pdf.read_bytes() + b"\n%changed\n")
        with self.assertRaisesRegex(ValueError, "source changed"):
            self.check(path)

    def test_artifact_hash_and_manifest_tampering(self):
        path = self.prepare()
        for filename in ("body.html", "message.eml", "body.txt", "plan.json"):
            target = path.parent / filename
            original = target.read_bytes()
            with self.subTest(filename=filename):
                target.write_bytes(original + b"\n")
                if filename == "plan.json":
                    # JSON whitespace is hashed even though its parsed plan is equal.
                    expected = "manifest"
                elif filename == "body.txt":
                    expected = "body.txt changed"
                else:
                    expected = "manifest"
                with self.assertRaisesRegex(ValueError, expected):
                    self.check(path)
                target.write_bytes(original)
        original = self.manifest(path)
        for key, value in (("status", "sent_confirmed"), ("from", "wrong@example.invalid"), ("to", "a@example.invalid,b@example.invalid"), ("cc", ["extra@example.invalid"]), ("bcc", ["extra@example.invalid"]), ("attachments", []), ("message_id", "<injected@example.invalid>"), ("unexpected", True)):
            with self.subTest(key=key):
                self.write_manifest(path, {**original, key: value})
                with self.assertRaisesRegex(ValueError, "manifest"):
                    self.check(path)
        self.write_manifest(path, original)

    def test_rehashed_html_eml_and_header_injection_still_fail_source_binding(self):
        path = self.prepare()
        original_manifest = self.manifest(path)
        for filename, addition, expected in (("body.html", b"<script>x</script>", "HTML differs"), ("message.eml", b"x", "MIME bytes differ")):
            target = path.parent / filename
            original = target.read_bytes()
            target.write_bytes(original + addition)
            manifest = self.manifest(path)
            key = "body_html" if filename == "body.html" else "message_eml"
            manifest["outputs"][key] = compose.preflight.fingerprint(target)
            self.write_manifest(path, manifest)
            with self.assertRaisesRegex(ValueError, expected):
                self.check(path)
            target.write_bytes(original)
            self.write_manifest(path, original_manifest)
        target = path.parent / "message.eml"
        target.write_bytes(b"Bcc: extra@example.invalid\r\n" + target.read_bytes())
        manifest = self.manifest(path)
        manifest["outputs"]["message_eml"] = compose.preflight.fingerprint(target)
        self.write_manifest(path, manifest)
        with self.assertRaisesRegex(ValueError, "MIME bytes differ"):
            self.check(path)

    def test_generator_hash_change_requires_new_bundle(self):
        path = self.prepare()
        real = compose._generators
        def changed(config):
            result = real(config)
            result["mail_compose"]["sha256"] = "0" * 64
            return result
        with mock.patch.object(compose, "_generators", side_effect=changed):
            with self.assertRaisesRegex(ValueError, "generators changed"):
                self.check(path)

    def test_manifest_byte_changes_and_output_path_redirection_are_rejected(self):
        path = self.prepare()
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        with self.assertRaisesRegex(ValueError, "serialization changed"):
            self.check(path)
        path.write_bytes(original)
        manifest = self.manifest(path)
        manifest["outputs"]["message_eml"]["path"] = str(self.root / "outside.eml")
        self.write_manifest(path, manifest)
        with self.assertRaisesRegex(ValueError, "manifest"):
            self.check(path)

    def test_new_empty_output_only_boundary_and_no_overwrite(self):
        for target in (self.work, self.root / "outside", self.work / ".." / "outside"):
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "inside work_dir"):
                compose.prepare_bundle(self.config_path, "M001", target)
        target = self.work / "empty"
        target.mkdir()
        path = compose.prepare_bundle(self.config_path, "M001", target)
        original = path.read_bytes()
        with self.assertRaisesRegex(ValueError, "new or empty"):
            compose.prepare_bundle(self.config_path, "M001", target)
        self.assertEqual(path.read_bytes(), original)
        outside = self.root / "bundle.json"
        outside.write_bytes(original)
        with self.assertRaisesRegex(ValueError, "inside work_dir"):
            self.check(outside)

    def test_objects_fields_revisions_hyperlinks_and_tables_fail_closed(self):
        source = self.letter_path()
        original = source.read_bytes()
        for element in ("<w:drawing/>", "<w:fldChar w:fldCharType=\"begin\"/>", "<w:instrText>DATE</w:instrText>", "<w:object/>", "<w:ins/>", "<w:del/>", "<w:rPrChange/>", "<w:hyperlink/>", "<w:tbl/>", "<w:numPr/>"):
            with self.subTest(element=element):
                source.write_bytes(original)
                self.modify_xml(lambda xml: xml.replace("</w:body>", f"{element}</w:body>"))
                with self.assertRaisesRegex(ValueError, "Unsupported Word"):
                    self.prepare(suffix="unsupported")
                # Failed preparations retain only their ordinary offline plan;
                # subsequent cases use a different empty output child.
                for file in (self.work / "unsupported").glob("*"):
                    file.unlink()
        source.write_bytes(original)

    def test_run_break_tab_order_and_plain_text_identity(self):
        self.modify_xml(lambda xml: xml.replace("学生申请2030博士。</w:t>", "学生</w:t><w:tab/><w:t>申请</w:t><w:br/><w:t>2030博士。</w:t>"))
        result = self.check(self.prepare())
        self.assertIn("学生\t申请\n2030博士。", result["plan"]["body_text"])
        self.assertIn("学生\t申请\n2030博士。", result["html_text"])

    def test_duplicate_json_keys_are_rejected(self):
        path = self.prepare()
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace('"schema_version": 2,', '"schema_version": 2, "schema_version": 2,'), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Duplicate JSON key"):
            self.check(path)

    def test_wire_validator_is_mandatory_during_prepare_and_actual_bundle_check(self):
        validate = compose.mime_wire.validate_wire
        with mock.patch.object(compose.mime_wire, "validate_wire", wraps=validate) as called:
            path = self.prepare()
            self.assertGreaterEqual(called.call_count, 2)
            called.reset_mock()
            self.check(path)
            self.assertGreaterEqual(called.call_count, 2)
        with mock.patch.object(compose.mime_wire, "validate_wire", side_effect=compose.mime_wire.WireError("wire rejected")):
            with self.assertRaisesRegex(ValueError, "wire rejected"):
                self.check(path)
            with self.assertRaisesRegex(ValueError, "wire rejected"):
                self.prepare(suffix="rejected")
        self.assertFalse((self.work / "rejected" / "message.eml").exists())

    def test_old_bundle_version_and_missing_wire_generator_require_preparation(self):
        path = self.prepare()
        original = self.manifest(path)
        self.write_manifest(path, {**original, "schema_version": 1})
        with self.assertRaisesRegex(ValueError, "Obsolete MIME bundle version"):
            self.check(path)
        self.write_manifest(path, original)
        legacy = self.manifest(path)
        del legacy["generators"]["mime_wire"]
        self.write_manifest(path, legacy)
        with self.assertRaisesRegex(ValueError, "generators changed"):
            self.check(path)

    def test_valid_base64_payload_and_filename_tampering_fail_independent_source_hashes(self):
        import base64
        result = self.check(self.prepare())
        raw = result["eml_bytes"]
        parsed = BytesParser(policy=policy.SMTP).parsebytes(raw)
        plain = parsed.get_payload()[0].get_payload()[0].get_payload(decode=True)
        def encoded(data):
            payload = base64.b64encode(data)
            return b"\r\n".join(payload[i:i + 76] for i in range(0, len(payload), 76))
        changed = plain.replace("博士".encode(), "硕士".encode(), 1)
        altered = raw.replace(encoded(plain), encoded(changed), 1)
        self.assertNotEqual(compose.mime_wire.validate_wire(altered)["payloads"]["plain"], result["wire_validation"]["payloads"]["plain"])
        with self.assertRaisesRegex(ValueError, "Wire decoded payloads differ"):
            compose._verify_mime(altered, result["plan"], result["html_text"], result["attachment_bytes"], result["bundle"])



if __name__ == "__main__":
    unittest.main(verbosity=2)
