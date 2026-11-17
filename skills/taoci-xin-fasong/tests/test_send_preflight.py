"""Deterministic offline checks using synthetic sources in the project workspace."""

from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "send_preflight.py"
SPEC = importlib.util.spec_from_file_location("send_preflight", SCRIPT)
preflight = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(preflight)
ROSTER_HELPER = SCRIPT.parents[2] / "taoci-yu-ziwo-chenshu" / "scripts" / "workflow_io.py"
# Synthetic shape-only telephone fixture, never a real contact number.
SYNTHETIC_PHONE = "1" + "3" + "0" * 9
SUBJECT = "博士申请-示例机构-示例申请人"


def write_docx(path: Path, paragraphs: list[str]) -> None:
    # Minimal valid WordprocessingML fixture; no Word/LibreOffice/UI needed.
    namespace = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f'<w:p><w:r><w:t xml:space="preserve">{escape(p)}</w:t></w:r></w:p>' for p in paragraphs)
    xml = f'<w:document xmlns:w="{namespace}"><w:body>{body}</w:body></w:document>'
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", xml.encode("utf-8"))


def write_pdf(path: Path, paragraphs: list[str], pages: int = 2) -> None:
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))
    pdf = canvas.Canvas(str(path), invariant=1)
    for page in range(pages):
        pdf.setFont("STSong-Light", 12)
        if page == 0:
            for index, text in enumerate(paragraphs):
                pdf.drawString(50, 760 - 30 * index, text)
        pdf.showPage()
    pdf.save()


class OfflinePreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="preflight-fixtures-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.materials = self.root / "materials"
        self.materials.mkdir()
        self.work = self.root / "runs"
        self.work.mkdir()
        self.roster = self.root / "roster.xlsx"
        self.config_path = self.root / "project.json"
        self.records = [
            ("M001", "示例机构", "神经科", "示例导师", "advisor@example.invalid", "示例方向甲"),
            ("M002", "示例机构甲", "神经科", "同名导师", "advisor-a@example.invalid", "示例方向乙"),
            ("M003", "示例机构乙", "神经科", "同名导师", "advisor-b@example.invalid", "示例方向丙"),
        ]
        self.config = {
            "schema_version": 1,
            "project_root": str(self.root), "roster": str(self.roster),
            "materials_dir": str(self.materials), "work_dir": str(self.work),
            "roster_helper": str(ROSTER_HELPER.resolve()),
            "sender": "sender@example.invalid", "expected_subject": SUBJECT, "statement_pdf_pages": 2,
            "attachment_overrides": {
                "M002": "自我陈述-同名导师-示例机构甲.pdf",
                "M003": "自我陈述-同名导师-示例机构乙.pdf",
            },
        }
        self.write_config()
        self.write_roster()
        for record in self.records:
            self.write_materials(record)

    def write_config(self) -> None:
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")

    def write_roster(self) -> None:
        from openpyxl import Workbook

        wb = Workbook()
        contact = wb.active
        contact.title = "导师联系表"
        contact.append(["学院", "院系", "导师", "邮箱", "研究方向", "是否发送邮件", "回复状态"])
        verified = wb.create_sheet("方向与招生核验")
        verified.append(["编号", "学校", "院系", "导师", "官方及原始来源"])
        for advisor_id, school, department, name, email, direction in self.records:
            contact.append([school, department, name, email, direction, "已发送（历史原文）", "暂未回复"])
            verified.append([advisor_id, school, department, name, "合成离线测试来源"])
        wb.save(self.roster)
        wb.close()

    def letter_paragraphs(self, record: tuple = None) -> list[str]:
        record = record or self.records[0]
        name, direction = record[3], record[5]
        return [f"邮件标题：{SUBJECT}", "邮件内容：如下", f"{name}老师您好！",
                "学生申请2030博士。", f"得知您的研究方向为{direction}，学生十分感兴趣。",
                "附件是我的自我陈述。", "祝工作顺利！", "      学生：示例申请人",
                f"      （电话：{SYNTHETIC_PHONE}，微信同号）", "", ""]

    def statement_paragraphs(self, record: tuple = None) -> list[str]:
        record = record or self.records[0]
        return ["自我陈述", "个人经历：研究与学习。", f"科研兴趣：学生对{record[3]}老师的{record[5]}方向感兴趣。"]

    def write_materials(self, record: tuple) -> None:
        number, name = int(record[0][1:]), record[3]
        write_docx(self.materials / f"{number}-套磁信-{name}.docx", self.letter_paragraphs(record))
        paragraphs = self.statement_paragraphs(record)
        write_docx(self.materials / f"{number}-自我陈述-{name}.docx", paragraphs)
        filename = self.config["attachment_overrides"].get(record[0], f"自我陈述-{name}.pdf")
        write_pdf(self.materials / filename, paragraphs)

    def prepare(self, advisor_id: str = "M001", suffix: str = "case") -> Path:
        return preflight.prepare(self.config_path, advisor_id, self.work / suffix)

    def plan(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def test_prepare_and_check_preserves_sources_and_plain_text_scope(self) -> None:
        before = {p: preflight.fingerprint(p) for p in self.materials.iterdir()}
        before[self.roster] = preflight.fingerprint(self.roster)
        path = self.prepare()
        plan = self.plan(path)
        self.assertEqual(preflight.check(self.config_path, path), "offline_passed_needs_live_review")
        self.assertEqual(plan["status"], "prepared_offline_needs_live_review")
        self.assertEqual(plan["history"]["contact_status"], "已发送（历史原文）")
        self.assertEqual(plan["history"]["response_status"], "暂未回复")
        self.assertEqual(plan["to"], "advisor@example.invalid")
        self.assertEqual(len(plan["attachments"]), 1)
        self.assertEqual(plan["attachments"][0]["name"], "自我陈述-示例导师.pdf")
        self.assertTrue(plan["body_text"].startswith("示例导师老师您好！\n"))
        self.assertTrue(plan["body_text"].endswith(f"（电话：{SYNTHETIC_PHONE}，微信同号）"))
        self.assertNotIn("邮件标题", plan["body_text"])
        self.assertNotIn("邮件内容", plan["body_text"])
        self.assertEqual({p.name for p in path.parent.iterdir()}, {"plan.json", "body.txt"})
        self.assertFalse(plan["material_checks"]["layout_verified"])
        self.assertEqual(before, {p: preflight.fingerprint(p) for p in before})

    def test_prepare_and_check_do_not_resolve_or_connect_to_network(self) -> None:
        with mock.patch("socket.getaddrinfo", side_effect=AssertionError("Network DNS forbidden")) as dns, \
                mock.patch("socket.create_connection", side_effect=AssertionError("Network forbidden")) as connection, \
                mock.patch("socket.socket.connect", side_effect=AssertionError("Network forbidden")) as connect, \
                mock.patch("socket.socket.connect_ex", side_effect=AssertionError("Network forbidden")) as connect_ex:
            path = self.prepare()
            self.assertEqual(preflight.check(self.config_path, path), "offline_passed_needs_live_review")
            for operation in (dns, connection, connect, connect_ex):
                operation.assert_not_called()

    def test_same_name_synthetic_advisor_resolves_by_id_and_school(self) -> None:
        first = self.plan(self.prepare("M002", "first"))
        second = self.plan(self.prepare("M003", "second"))
        self.assertEqual((first["to"], first["school"]), ("advisor-a@example.invalid", "示例机构甲"))
        self.assertEqual((second["to"], second["school"]), ("advisor-b@example.invalid", "示例机构乙"))
        self.assertEqual(first["attachments"][0]["name"], "自我陈述-同名导师-示例机构甲.pdf")
        self.assertEqual(second["attachments"][0]["name"], "自我陈述-同名导师-示例机构乙.pdf")
        self.assertNotEqual(first["attachments"][0]["sha256"], second["attachments"][0]["sha256"])

    def test_missing_or_mixed_email_is_rejected(self) -> None:
        for email in ("", "a@example.invalid;b@example.invalid", "a@example.invalid b@example.invalid",
                      "邮箱：a@example.invalid（待核验）", "Name <a@example.invalid>", "a..b@example.invalid"):
            with self.subTest(email=email):
                self.records[0] = (*self.records[0][:4], email, self.records[0][5])
                self.write_roster()
                with self.assertRaisesRegex(preflight.PreflightError, "exactly one complete bare"):
                    self.prepare()

    def test_wrong_letter_advisor_is_rejected(self) -> None:
        paragraphs = self.letter_paragraphs()
        paragraphs[2] = "同名导师老师您好！"
        write_docx(self.materials / "1-套磁信-示例导师.docx", paragraphs)
        with self.assertRaisesRegex(preflight.PreflightError, "greeting for"):
            self.prepare()

    def test_missing_pdf_and_old_numeric_name_never_fall_back(self) -> None:
        pdf = self.materials / "自我陈述-示例导师.pdf"
        pdf.rename(self.materials / "1-自我陈述-示例导师.pdf")
        with self.assertRaises(FileNotFoundError):
            self.prepare()
        self.assertFalse((self.work / "case").exists())

    def test_docx_pdf_content_mismatch_is_rejected(self) -> None:
        paragraphs = self.statement_paragraphs()
        paragraphs[1] = "错误内容，与自述DOCX不一致。"
        write_pdf(self.materials / "自我陈述-示例导师.pdf", paragraphs)
        with self.assertRaisesRegex(preflight.PreflightError, "extracted text differ"):
            self.prepare()

    def test_wrong_statement_advisor_is_rejected_even_with_matching_pdf(self) -> None:
        paragraphs = self.statement_paragraphs()
        paragraphs[-1] = paragraphs[-1].replace("示例导师", "同名导师")
        write_docx(self.materials / "1-自我陈述-示例导师.docx", paragraphs)
        write_pdf(self.materials / "自我陈述-示例导师.pdf", paragraphs)
        with self.assertRaisesRegex(preflight.PreflightError, "selected advisor"):
            self.prepare()

    def test_wrong_pdf_page_count_is_rejected(self) -> None:
        write_pdf(self.materials / "自我陈述-示例导师.pdf", self.statement_paragraphs(), pages=3)
        with self.assertRaisesRegex(preflight.PreflightError, "2 pages"):
            self.prepare()

    def test_source_hash_change_is_rejected_even_when_visible_text_matches(self) -> None:
        path = self.prepare()
        source = self.materials / "1-套磁信-示例导师.docx"
        with ZipFile(source, "a", ZIP_DEFLATED) as archive:
            archive.writestr("new-metadata.txt", "Metadata-only change")
        with self.assertRaisesRegex(preflight.PreflightError, "Plan or source changed"):
            preflight.check(self.config_path, path)

    def test_config_hash_change_is_rejected(self) -> None:
        path = self.prepare()
        self.config_path.write_text(self.config_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        with self.assertRaisesRegex(preflight.PreflightError, "Plan or source changed"):
            preflight.check(self.config_path, path)

    def test_plan_to_body_subject_attachment_and_status_tampering_are_rejected(self) -> None:
        path = self.prepare()
        original = self.plan(path)
        for key, value in (("to", "other@example.invalid"), ("body_text", "Changed body"),
                           ("subject", "Changed subject"), ("attachments", []),
                           ("status", "sent_confirmed"), ("authorization", True)):
            with self.subTest(field=key):
                modified = {**original, key: value}
                path.write_text(json.dumps(modified, ensure_ascii=False), encoding="utf-8")
                with self.assertRaisesRegex(preflight.PreflightError, "Plan or source changed"):
                    preflight.check(self.config_path, path)

    def test_changed_plain_text_artifact_is_rejected(self) -> None:
        path = self.prepare()
        (path.parent / "body.txt").write_text("wrong", encoding="utf-8")
        with self.assertRaisesRegex(preflight.PreflightError, "body.txt changed"):
            preflight.check(self.config_path, path)

    def test_output_outside_workdir_and_existing_files_are_rejected(self) -> None:
        for output in (self.root / "outside", self.work / ".." / "outside", self.work):
            with self.subTest(output=output):
                with self.assertRaisesRegex(preflight.PreflightError, "inside work_dir"):
                    preflight.prepare(self.config_path, "M001", output)
        path = self.prepare()
        original = path.read_bytes()
        with self.assertRaisesRegex(preflight.PreflightError, "new or empty"):
            self.prepare()
        self.assertEqual(path.read_bytes(), original)

    def test_same_name_missing_or_swapped_override_is_rejected(self) -> None:
        del self.config["attachment_overrides"]["M002"]
        self.write_config()
        with self.assertRaisesRegex(preflight.PreflightError, "explicit school-specific"):
            self.prepare("M002")
        self.config["attachment_overrides"]["M002"] = "自我陈述-同名导师-示例机构乙.pdf"
        self.write_config()
        with self.assertRaisesRegex(preflight.PreflightError, "filename must match"):
            self.prepare("M002")

    def test_direction_mismatch_and_wrong_subject_are_rejected(self) -> None:
        paragraphs = self.letter_paragraphs()
        paragraphs[4] = "得知您的研究方向为完全不同方向，学生十分感兴趣。"
        write_docx(self.materials / "1-套磁信-示例导师.docx", paragraphs)
        with self.assertRaisesRegex(preflight.PreflightError, "research direction is absent"):
            self.prepare()
        paragraphs = self.letter_paragraphs()
        paragraphs[0] = "邮件标题：不正确主题"
        write_docx(self.materials / "1-套磁信-示例导师.docx", paragraphs)
        with self.assertRaisesRegex(preflight.PreflightError, "subject differs"):
            self.prepare()


if __name__ == "__main__":
    unittest.main(verbosity=2)
