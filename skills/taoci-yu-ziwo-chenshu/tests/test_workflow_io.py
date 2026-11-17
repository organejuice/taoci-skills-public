"""Deterministic checks for roster identity, source preservation and red fields."""

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from lxml import etree
import openpyxl

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from workflow_io import (
    A, W, NS, docx_story_parts, effective_run_color, is_red_run,
    load_advisors, load_config, paragraph_text, resolve_path, select_advisors, template_report,
)


def temporary_workspace():
    # Windows desktop sandbox TEMP can permit mkdir yet deny file creation.
    # Keep every test file under this writable project, and delete it on exit.
    return tempfile.TemporaryDirectory(prefix="workflow-io-test-", dir=Path(__file__).resolve().parent)


def xml(body):
    return etree.fromstring(body.encode("utf-8"))


class RosterTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_workspace()
        self.root = Path(self.temp.name)
        self.book = self.root / "roster.xlsx"
        wb = openpyxl.Workbook()
        contact = wb.active
        contact.title = "导师联系表"
        contact.append(["学院", "院系", "导师", "邮箱", "研究方向", "是否发送邮件", "回复状态"])
        contact.append(["示例机构甲", "Lab A", "示例导师", "advisor-a@example.invalid", "Memory", "是（历史记录）", "已读未回（历史记录）"])
        contact.append(["示例机构乙", "Lab B", "示例导师", "advisor-b@example.invalid", "Protein", "待核对", "待核对"])
        contact.append(["示例机构丙", "Lab C", "另一示例导师", "advisor-c@example.invalid", "Neurons", "否", "未回复"])
        verified = wb.create_sheet("方向与招生核验")
        verified.append(["编号", "学校", "院系", "导师", "官方及原始来源"])
        verified.append(["M017", "示例机构乙", "Lab B", "示例导师", "https://example.invalid/b"])
        verified.append(["M002", "示例机构甲", "Lab A", "示例导师", "https://example.invalid/a"])
        verified.append(["M040", "示例机构丙", "Lab C", "另一示例导师", "https://example.invalid/c"])
        wb.save(self.book)
        wb.close()
        self.config = {"project_root": str(self.root), "roster": str(self.book)}

    def tearDown(self):
        self.temp.cleanup()

    def test_join_uses_full_identity_not_row_or_name(self):
        before = self.book.read_bytes()
        advisors = load_advisors(self.config)
        self.assertEqual([a["number"] for a in advisors], [17, 2, 40])
        self.assertEqual(advisors[0]["email"], "advisor-b@example.invalid")
        self.assertEqual(advisors[1]["contact_status"], "是（历史记录）")
        self.assertEqual(advisors[1]["response_status"], "已读未回（历史记录）")
        self.assertEqual(self.book.read_bytes(), before)

    def test_scope_required_and_duplicate_name_is_actionable(self):
        advisors = load_advisors(self.config)
        with self.assertRaisesRegex(ValueError, "Specify"):
            select_advisors(advisors)
        with self.assertRaisesRegex(ValueError, "M017.*M002"):
            select_advisors(advisors, names=["示例导师"])
        self.assertEqual(select_advisors(advisors, names="示例导师", school="示例机构甲")[0]["id"], "M002")

    def test_ids_ranges_and_all_do_not_renumber(self):
        advisors = load_advisors(self.config)
        self.assertEqual([a["number"] for a in select_advisors(advisors, ids=["M002", "17"])], [17, 2])
        self.assertEqual(select_advisors(advisors, start="M010", end=20)[0]["number"], 17)
        self.assertEqual(len(select_advisors(advisors, ids=["all"])), 3)
        with self.assertRaisesRegex(ValueError, "Unknown"):
            select_advisors(advisors, ids=["M999"])
        with self.assertRaises(ValueError):
            select_advisors(advisors, ids=["all", "M002"])
        with self.assertRaises(ValueError):
            select_advisors(advisors, start=20, end=10)

    def test_missing_contact_not_interpreted_as_unsent(self):
        wb = openpyxl.load_workbook(self.book)
        wb["导师联系表"].delete_rows(2)
        wb.save(self.book)
        wb.close()
        advisors = load_advisors(self.config)
        self.assertEqual(advisors[1]["contact_match_status"], "unmatched")
        self.assertIsNone(advisors[1]["contact_status"])

    def test_numeric_id_alias_duplicates_rejected(self):
        wb = openpyxl.load_workbook(self.book)
        wb["方向与招生核验"].append(["M2", "Other", "Other", "Other", "https://example.invalid/d"])
        wb.save(self.book)
        wb.close()
        with self.assertRaisesRegex(ValueError, "Duplicate numeric"):
            load_advisors(self.config)

    def test_config_relative_paths_use_project_root(self):
        config_file = self.root / "project.json"
        paths = {k: k for k in ("roster", "output_dir", "letter_template", "statement_pdf", "statement_docx", "work_dir")}
        config_file.write_text(json.dumps({"project_root": str(self.root), **paths}), encoding="utf-8")
        config = load_config(config_file)
        self.assertEqual(resolve_path(config, "output_dir"), self.root / "output_dir")
        with self.assertRaises(KeyError):
            resolve_path(config, "runtime")


class RedFieldTests(unittest.TestCase):
    def test_direct_variants_and_black_override(self):
        styles = xml(f'<w:styles xmlns:w="{W}"><w:style w:type="character" w:styleId="red"><w:rPr><w:color w:val="FF0000"/></w:rPr></w:style></w:styles>')
        for value in ("FF0000", "EE0000", "800000"):
            run = xml(f'<w:r xmlns:w="{W}"><w:rPr><w:color w:val="{value}"/></w:rPr><w:t>Name</w:t></w:r>')
            self.assertTrue(is_red_run(run), value)
        for value in ("000000", "auto", "FFFFFF"):
            run = xml(f'<w:r xmlns:w="{W}"><w:rPr><w:rStyle w:val="red"/><w:color w:val="{value}"/></w:rPr><w:t>Name</w:t></w:r>')
            self.assertFalse(is_red_run(run, styles), value)

    def test_character_and_paragraph_style_inheritance(self):
        styles = xml(f'''<w:styles xmlns:w="{W}">
          <w:style w:type="character" w:styleId="base"><w:rPr><w:color w:val="EE0000"/></w:rPr></w:style>
          <w:style w:type="character" w:styleId="child"><w:basedOn w:val="base"/></w:style>
          <w:style w:type="paragraph" w:styleId="P"><w:rPr><w:color w:val="FF0000"/></w:rPr></w:style>
        </w:styles>''')
        run = xml(f'<w:r xmlns:w="{W}"><w:rPr><w:rStyle w:val="child"/></w:rPr><w:t>Name</w:t></w:r>')
        self.assertTrue(is_red_run(run, styles))
        paragraph = xml(f'<w:p xmlns:w="{W}"><w:pPr><w:pStyle w:val="P"/></w:pPr><w:r><w:t>Name</w:t></w:r></w:p>')
        self.assertTrue(is_red_run(paragraph.find("w:r", NS), styles))

    def test_theme_color_resolves_and_tint_remains_red(self):
        theme = xml(f'<a:theme xmlns:a="{A}"><a:themeElements><a:clrScheme name="Test"><a:accent2><a:srgbClr val="FF0000"/></a:accent2></a:clrScheme></a:themeElements></a:theme>')
        run = xml(f'<w:r xmlns:w="{W}"><w:rPr><w:color w:val="000000" w:themeColor="accent2" w:themeTint="33"/></w:rPr><w:t>Name</w:t></w:r>')
        self.assertTrue(is_red_run(run, theme=theme))
        self.assertEqual(effective_run_color(run, theme=theme), "FF3333")

    def test_defaults_and_style_cycles(self):
        styles = xml(f'''<w:styles xmlns:w="{W}">
          <w:docDefaults><w:rPrDefault><w:rPr><w:color w:val="FF0000"/></w:rPr></w:rPrDefault></w:docDefaults>
          <w:style w:styleId="a"><w:basedOn w:val="b"/></w:style><w:style w:styleId="b"><w:basedOn w:val="a"/></w:style>
        </w:styles>''')
        run = xml(f'<w:r xmlns:w="{W}"><w:rPr><w:rStyle w:val="a"/></w:rPr><w:t>Name</w:t></w:r>')
        self.assertTrue(is_red_run(run, styles))

    def test_nested_textbox_paragraphs_not_double_counted(self):
        paragraph = xml(f'<w:p xmlns:w="{W}"><w:r><w:t>Outside</w:t><w:drawing><w:txbxContent><w:p><w:r><w:t>Inside</w:t></w:r></w:p></w:txbxContent></w:drawing></w:r><w:r><w:tab/><w:t>End</w:t></w:r></w:p>')
        self.assertEqual(paragraph_text(paragraph), "Outside\tEnd")
        self.assertEqual(paragraph_text(paragraph.find(".//w:txbxContent/w:p", NS)), "Inside")

    def test_story_parts_include_notes_and_exclude_styles_comments(self):
        with temporary_workspace() as temp:
            path = Path(temp) / "test.docx"
            part = f'<w:document xmlns:w="{W}"><w:p><w:r><w:t>Text</w:t></w:r></w:p></w:document>'
            with ZipFile(path, "w") as archive:
                for name in ("word/document.xml", "word/header1.xml", "word/footer1.xml", "word/footnotes.xml", "word/endnotes.xml", "word/styles.xml", "word/comments.xml"):
                    archive.writestr(name, part)
            self.assertEqual(set(docx_story_parts(path)), {"word/document.xml", "word/header1.xml", "word/footer1.xml", "word/footnotes.xml", "word/endnotes.xml"})


class TemplateReportTests(unittest.TestCase):
    def test_text_equality_never_claims_visual_verification(self):
        from docx import Document
        from docx.shared import RGBColor
        from reportlab.pdfgen.canvas import Canvas

        with temporary_workspace() as temp:
            root = Path(temp)
            source = root / "statement.docx"
            doc = Document()
            p = doc.add_paragraph()
            p.add_run("Advisor").font.color.rgb = RGBColor(255, 0, 0)
            doc.save(source)
            pdf_path = root / "statement.pdf"
            canvas = Canvas(str(pdf_path))
            canvas.drawString(60, 750, "Advisor")
            canvas.save()
            roster = root / "roster.xlsx"
            roster.write_bytes(b"immutable roster fixture")
            config = {
                "project_root": str(root), "roster": str(roster),
                "letter_template": str(source), "statement_docx": str(source), "statement_pdf": str(pdf_path),
            }
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (source, pdf_path, roster)}
            report = template_report(config)
            self.assertTrue(report["statement_docx_pdf_text_equal"])
            self.assertEqual(report["statement_pdf_pages"], 1)
            self.assertFalse(report["layout_verified"])
            self.assertEqual(report["red_regions"]["statement"][0]["text"], "Advisor")
            after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before}
            self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
