"""Behavioural tests for local drafting and publication safeguards.

All data are synthetic and all outputs live in TemporaryDirectory.  The PDF
extractor is mocked only in publication tests, which test review/hash binding
rather than the external Office converter or page layout.
"""
from __future__ import annotations

import base64
import copy
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from zipfile import ZipFile

from docx import Document
from docx.shared import Pt, RGBColor
from lxml import etree as ET
from reportlab.pdfgen import canvas

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import workflow as wf


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a9uoAAAAASUVORK5CYII="
)


def save_pdf(path: Path, pages: int = 1):
    document = canvas.Canvas(str(path))
    for page in range(pages):
        document.drawString(72, 740, "Synthetic layout fixture page {}".format(page + 1))
        document.showPage()
    document.save()


def red(paragraph, text, color="FF0000", size=12):
    run = paragraph.add_run(text)
    run.font.name = "Arial"
    run.font.size = Pt(size)
    run.font.bold = size == 15
    run.font.color.rgb = RGBColor.from_string(color)
    return run


def package(path):
    with ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def run_properties(data):
    tree = ET.fromstring(data)
    return [ET.tostring(r.find(wf.W + "rPr")) if r.find(wf.W + "rPr") is not None else None
            for r in tree.iter(wf.W + "r")]


class WorkflowFixture(unittest.TestCase):
    def setUp(self):
        # The Windows app-container TEMP can allow mkdtemp but deny children;
        # keep fixtures under the explicitly writable project instead.
        self.temp_parent = Path(__file__).resolve().parents[2] / "tmp-tests"
        self.temp_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="taoci-behaviour-", dir=self.temp_parent)
        self.addCleanup(self.cleanup_fixture)
        self.root = Path(self.temp.name)
        self.templates = self.root / "templates"
        self.templates.mkdir()
        self.work = self.root / "work"
        self.work.mkdir()
        self.output = self.root / "deliveries"
        self.output.mkdir()
        self.letter = self.templates / "1-套磁信-模板导师.docx"
        self.statement = self.templates / "1-自我陈述-模板导师.docx"
        self.source_pdf = self.templates / "1-自我陈述-模板导师.pdf"
        self.roster = self.root / "synthetic-roster.xlsx"
        self.roster.write_bytes(b"synthetic roster; load_advisors is isolated in generator tests")
        self.make_letter(self.letter)
        self.make_statement(self.statement)
        save_pdf(self.source_pdf, 4)
        self.advisor = {
            "id": "M002", "number": 2, "name": "示例导师", "school": "示例机构",
            "department": "示例院系", "email": "advisor@example.invalid",
            "direction": "测试方向", "official_sources": "https://example.invalid/fixture",
            "contact_status": "未知", "response_status": "未知", "roster_row": 3,
            "verification": {},
        }
        self.config = {
            "project_root": str(self.root), "work_dir": str(self.work),
            "output_dir": str(self.output), "roster": str(self.roster),
            "letter_template": str(self.letter), "statement_docx": str(self.statement),
            "statement_pdf": str(self.source_pdf), "baseline_advisor": "模板导师",
            "baseline_direction": "示例研究方向",
            "statement_opening_deletions": [
                "示例待删除开头，引用模板导师的探索",
                "示例待删除开头，引用示例字段的探索",
            ],
            "interest_length": {"target_min": 1, "target_max": 500},
        }
        self.protected_before = wf.source_hashes(self.config)
        self.pdf_fixtures = {}

    def cleanup_fixture(self):
        self.temp.cleanup()
        try:
            self.temp_parent.rmdir()
        except OSError:
            pass

    @staticmethod
    def make_letter(path, unknown=False):
        document = Document()
        greeting = document.add_paragraph()
        red(greeting, "模板", size=12)
        red(greeting, "导师", color="EE0000", size=15)
        greeting.add_run("老师您好！")
        direction = document.add_paragraph("研究方向为")
        red(direction, "示例研究", size=12)
        red(direction, "方向", color="EE0000", size=15)
        direction.add_run("，希望继续探索。")
        document.add_paragraph("固定学历与成果，不应随导师改变。")
        table = document.add_table(rows=1, cols=1)
        table.cell(0, 0).text = "固定表格事实"
        document.sections[0].header.paragraphs[0].text = "固定页眉"
        document.sections[0].footer.paragraphs[0].text = "固定页脚"
        document.add_picture(io.BytesIO(PNG))
        if unknown:
            red(document.add_paragraph(), "未知红色占位")
        document.save(path)

    @staticmethod
    def make_statement(path, marker="模板导师", separator="，", marker_red=True,
                       opening_prefix="示例待删除开头，引用", unknown=False):
        document = Document()
        document.add_paragraph("固定个人经历与成果。")
        evaluation = document.add_paragraph("自我评价：")
        evaluation.runs[0].bold = True
        evaluation.add_run(opening_prefix)
        if marker_red:
            red(evaluation, marker[:1], size=12)
            red(evaluation, marker[1:], color="EE0000", size=15)
        else:
            evaluation.add_run(marker)
        evaluation.add_run("的探索" + separator + "保留后续示例文字。")
        if unknown:
            red(evaluation, "未知红色占位")
        interest = document.add_paragraph()
        interest.add_run("科研兴趣：").bold = True
        interest.add_run("学生对模板导师老师的既有方向感兴趣。")
        document.add_picture(io.BytesIO(PNG))
        document.save(path)

    def content(self):
        return {
            "advisor_id": self.advisor["id"], "direction": "核验后的研究方向",
            "research_interest": "学生对{}老师已核验的研究方向感兴趣，希望结合自身真实背景继续探索。".format(self.advisor["name"]),
            "identity_reviewed": True, "claims_reviewed": True,
            "paper_gap_note": "Synthetic unit-test fixture makes no claim about a real paper.",
            "sources": [{"kind": "official", "title": "Synthetic official source",
                         "url": "https://example.invalid/fixture", "checked_at": "2000-01-01",
                         "supports": "Synthetic identity and direction only"}],
        }

    def stage(self):
        content_path = self.work / "content.json"
        wf.write_json(content_path, self.content())
        with mock.patch.object(wf, "load_advisors", return_value=[self.advisor]), \
                mock.patch.object(wf, "template_report", return_value={"fixture": True}), \
                mock.patch.object(wf, "pdf_text", return_value=(wf.docx_text(self.statement), 4)):
            manifest = wf.build(self.config, content_path, self.work / "stage")
        self.manifest_path = Path(manifest["files"]["letter_docx"]).parent / "manifest.json"
        return manifest

    def reviewable(self):
        manifest = self.stage()
        staging = self.manifest_path.parent
        rendered = []
        for key, pages in (("letter_docx", 1), ("statement_docx", 4)):
            pdf = staging / "qa" / key / "render.pdf"
            pdf.parent.mkdir(parents=True)
            save_pdf(pdf, pages)
            images = {}
            for page in range(1, pages + 1):
                image = pdf.parent / "page-{}.png".format(page)
                image.write_bytes(PNG)
                images[str(image)] = wf.digest(image)
            rendered.append({"kind": key, "pdf": str(pdf), "pages": pages,
                             "pdf_sha256": wf.digest(pdf), "images": images})
            self.pdf_fixtures[str(pdf.resolve())] = (wf.docx_text(manifest["files"][key]), pages)
            if key == "statement_docx":
                final_pdf = Path(manifest["files"]["statement_pdf"])
                final_pdf.write_bytes(pdf.read_bytes())
                self.pdf_fixtures[str(final_pdf.resolve())] = self.pdf_fixtures[str(pdf.resolve())]
        manifest["rendered"] = rendered
        manifest["status"] = "awaiting_visual_review"
        manifest["render_hashes"] = {path: wf.digest(path) for path in manifest["files"].values()}
        wf.write_json(self.manifest_path, manifest)
        self.receipt = {
            "reviewer": "synthetic-test-reviewer", "reviewed_at": "2000-01-01T00:00:00Z",
            "passed": True, "images": {p: h for item in rendered for p, h in item["images"].items()},
            "notes": "Synthetic receipt tests binding only; real visual inspection is a separate acceptance check.",
        }
        self.receipt_path = staging / "receipt.json"
        wf.write_json(self.receipt_path, self.receipt)
        return manifest

    def fixture_pdf_text(self, path):
        return self.pdf_fixtures[str(Path(path).resolve())]

    def accept_review(self):
        with mock.patch.object(wf, "pdf_text", side_effect=self.fixture_pdf_text):
            return wf.qa(self.config, self.manifest_path, self.receipt_path)

    def publish(self):
        with mock.patch.object(wf, "pdf_text", side_effect=self.fixture_pdf_text):
            return wf.publish(self.config, self.manifest_path)

    def assert_sources_unchanged(self):
        self.assertEqual(self.protected_before, wf.source_hashes(self.config))


class PatchingTests(WorkflowFixture):
    def test_cross_run_replacement_preserves_properties_media_and_other_parts(self):
        destination = self.work / "patched-letter.docx"
        before = package(self.letter)
        wf.patch_docx(self.letter, destination, self.config, self.advisor,
                      "核验后的研究方向", "unused", "letter")
        after = package(destination)
        self.assertEqual(set(before), set(after))
        self.assertEqual(run_properties(before["word/document.xml"]), run_properties(after["word/document.xml"]))
        for name in before:
            if name != "word/document.xml":
                self.assertEqual(before[name], after[name], name)
        text = wf.docx_text(destination)
        self.assertIn("示例导师老师您好！", text)
        self.assertIn("研究方向为核验后的研究方向，希望继续探索。", text)
        self.assertIn("固定学历与成果，不应随导师改变。", text)
        self.assertIn("固定表格事实", text)
        self.assertNotIn("模板导师", text)
        self.assert_sources_unchanged()

    def test_statement_opening_deletion_preserves_following_text_and_package(self):
        destination = self.work / "patched-statement.docx"
        before = package(self.statement)
        changes = wf.patch_docx(self.statement, destination, self.config, self.advisor,
                               "核验方向", self.content()["research_interest"], "statement")
        text = wf.docx_text(destination)
        self.assertNotIn("示例待删除开头", text)
        self.assertNotIn("引用示例字段的探索", text)
        self.assertNotIn("引用示例导师的探索", text)
        self.assertNotIn("模板导师", text)
        self.assertEqual("自我评价：保留后续示例文字。",
                         Document(destination).paragraphs[1].text)
        self.assertIn(self.content()["research_interest"], text)
        self.assertIn("固定个人经历与成果。", text)
        self.assertEqual({item["role"] for item in changes},
                         {"self_evaluation_opening_deletion", "research_interest"})
        deletion = next(item for item in changes if item["role"] == "self_evaluation_opening_deletion")
        self.assertEqual(self.config["statement_opening_deletions"][0] + "，", deletion["old"])
        self.assertEqual("", deletion["new"])
        after = package(destination)
        self.assertEqual(set(before), set(after))
        self.assertEqual(run_properties(before["word/document.xml"]),
                         run_properties(after["word/document.xml"]))
        for name in before:
            if name != "word/document.xml":
                self.assertEqual(before[name], after[name], name)
        self.assert_sources_unchanged()

    def test_already_repaired_opening_is_deleted_with_its_separator(self):
        for index, (separator, marker_red) in enumerate((("，", True), (",", True), ("。", True), ("，", False))):
            with self.subTest(separator=separator, marker_red=marker_red):
                source = self.work / "repaired-{}.docx".format(index)
                self.make_statement(source, marker="示例字段", separator=separator, marker_red=marker_red)
                fingerprint = wf.digest(source)
                destination = self.work / "deleted-{}.docx".format(index)
                wf.patch_docx(source, destination, self.config, self.advisor,
                              "核验方向", self.content()["research_interest"], "statement")
                self.assertEqual("自我评价：保留后续示例文字。",
                                 Document(destination).paragraphs[1].text)
                self.assertNotIn("引用示例字段的探索", wf.docx_text(destination))
                self.assertEqual(fingerprint, wf.digest(source))
        self.assert_sources_unchanged()

    def test_statement_unknown_red_after_deletion_is_still_rejected(self):
        source = self.work / "statement-unknown.docx"
        self.make_statement(source, unknown=True)
        destination = self.work / "should-not-exist.docx"
        with self.assertRaisesRegex(ValueError, "Unmapped red field"):
            wf.patch_docx(source, destination, self.config, self.advisor,
                          "核验方向", self.content()["research_interest"], "statement")
        self.assertFalse(destination.exists())
        self.assert_sources_unchanged()

    def test_empty_deletion_configuration_preserves_evaluation_opening(self):
        source = self.work / "no-deletion.docx"
        self.make_statement(source, marker="示例字段", marker_red=False)
        self.config["statement_opening_deletions"] = []
        original_evaluation = Document(source).paragraphs[1].text
        destination = self.work / "opening-retained.docx"
        changes = wf.patch_docx(source, destination, self.config, self.advisor,
                                "核验方向", self.content()["research_interest"], "statement")
        self.assertEqual(original_evaluation, Document(destination).paragraphs[1].text)
        self.assertEqual({"research_interest"}, {change["role"] for change in changes})
        self.assert_sources_unchanged()

    def test_changed_opening_is_not_silently_deleted(self):
        source = self.work / "changed-opening.docx"
        self.make_statement(source, opening_prefix="变更后的示例开头，引用")
        destination = self.work / "should-not-exist.docx"
        with self.assertRaisesRegex(ValueError, "Missing expected editable slots"):
            wf.patch_docx(source, destination, self.config, self.advisor,
                          "核验方向", self.content()["research_interest"], "statement")
        self.assertFalse(destination.exists())
        self.assert_sources_unchanged()

    def test_unmapped_red_field_fails_without_output(self):
        source = self.work / "unknown.docx"
        self.make_letter(source, unknown=True)
        destination = self.work / "should-not-exist.docx"
        with self.assertRaisesRegex(ValueError, "Unmapped red field"):
            wf.patch_docx(source, destination, self.config, self.advisor,
                          "核验方向", "unused", "letter")
        self.assertFalse(destination.exists())

    def test_source_and_destination_cannot_be_same(self):
        with self.assertRaisesRegex(ValueError, "read-only"):
            wf.patch_docx(self.letter, self.letter, self.config, self.advisor,
                          "核验方向", "unused", "letter")
        self.assert_sources_unchanged()


class EvidenceTests(WorkflowFixture):
    def test_wrong_interest_name_is_rejected(self):
        content = self.content()
        content["research_interest"] = "学生对另一位导师的研究感兴趣。"
        with self.assertRaisesRegex(ValueError, "must name this advisor"):
            wf.validate_content(content, self.advisor)

    def test_missing_identity_or_claim_review_is_rejected(self):
        for field in ("identity_reviewed", "claims_reviewed"):
            with self.subTest(field=field):
                content = self.content()
                content[field] = False
                with self.assertRaisesRegex(ValueError, "evidence review"):
                    wf.validate_content(content, self.advisor)

    def test_missing_official_source_is_rejected(self):
        content = self.content()
        content["sources"] = []
        with self.assertRaisesRegex(ValueError, "official"):
            wf.validate_content(content, self.advisor)

    def test_missing_paper_without_explicit_gap_is_rejected(self):
        content = self.content()
        content.pop("paper_gap_note")
        with self.assertRaisesRegex(ValueError, "verified paper"):
            wf.validate_content(content, self.advisor)

    def test_paper_metadata_must_include_identity_and_notice_checks(self):
        for omitted in ("authors", "affiliation_check", "published", "doi", "notice_check"):
            with self.subTest(omitted=omitted):
                content = self.content()
                paper = {"kind": "paper", "title": "Synthetic paper, not a real citation",
                         "url": "https://example.invalid/paper", "checked_at": "2000-01-01",
                         "supports": "Synthetic schema coverage", "authors": "Fixture Author",
                         "affiliation_check": "Synthetic institution", "published": "2000",
                         "doi": "synthetic-test-value", "notice_check": "Synthetic test only"}
                paper.pop(omitted)
                content["sources"].append(paper)
                with self.assertRaisesRegex(ValueError, omitted):
                    wf.validate_content(content, self.advisor)

    def test_valid_direction_only_evidence_is_accepted_with_explicit_gap(self):
        wf.validate_content(self.content(), self.advisor)


class ReviewBindingTests(WorkflowFixture):
    def test_manifest_cannot_omit_a_page_from_review_coverage(self):
        manifest = self.reviewable()
        # A receipt must cover the PDF's actual pages, not merely a shortened
        # image list that was copied into both the manifest and the receipt.
        omitted = next(iter(manifest["rendered"][1]["images"]))
        manifest["rendered"][1]["images"].pop(omitted)
        self.receipt["images"].pop(omitted)
        wf.write_json(self.manifest_path, manifest)
        wf.write_json(self.receipt_path, self.receipt)
        with self.assertRaises(ValueError):
            self.accept_review()

    def test_receipt_must_cover_every_current_page(self):
        self.reviewable()
        self.assertEqual(5, len(self.receipt["images"]))
        self.receipt["images"].pop(next(iter(self.receipt["images"])))
        wf.write_json(self.receipt_path, self.receipt)
        with self.assertRaisesRegex(ValueError, "every current page"):
            self.accept_review()

    def test_receipt_rejects_substituted_image_fingerprint(self):
        self.reviewable()
        first = next(iter(self.receipt["images"]))
        self.receipt["images"][first] = "0" * 64
        wf.write_json(self.receipt_path, self.receipt)
        with self.assertRaisesRegex(ValueError, "every current page"):
            self.accept_review()

    def test_unchanged_receipt_rejects_mutated_image_file(self):
        self.reviewable()
        first = Path(next(iter(self.receipt["images"])))
        first.write_bytes(first.read_bytes() + b"tampered-after-review")
        with self.assertRaisesRegex(ValueError, "changed since review"):
            self.accept_review()

    def test_current_render_pdf_hash_is_also_bound(self):
        manifest = self.reviewable()
        rendered_pdf = Path(manifest["rendered"][0]["pdf"])
        rendered_pdf.write_bytes(rendered_pdf.read_bytes() + b"tampered-render")
        with self.assertRaisesRegex(ValueError, "Render PDF changed"):
            self.accept_review()

    def test_document_change_invalidates_review(self):
        manifest = self.reviewable()
        statement = Path(manifest["files"]["statement_docx"])
        statement.write_bytes(statement.read_bytes() + b"tampered-document")
        with self.assertRaisesRegex(ValueError, "changed since review"):
            self.accept_review()

    def test_complete_receipt_accepts_and_binds_all_final_files(self):
        manifest = self.reviewable()
        result = self.accept_review()
        self.assertEqual("passed", result["status"])
        self.assertEqual(self.receipt, result["visual_receipt"])
        self.assertEqual({p: wf.digest(p) for p in manifest["files"].values()}, result["file_hashes"])
        self.assert_sources_unchanged()


class PublicationTests(WorkflowFixture):
    def test_publish_without_qa_is_rejected(self):
        self.stage()
        with self.assertRaisesRegex(ValueError, "requires completed"):
            self.publish()
        self.assertEqual([], list(self.output.iterdir()))

    def test_cannot_publish_over_original_template_names(self):
        self.advisor.update(id="M001", number=1, name="模板导师")
        self.config["output_dir"] = str(self.templates)
        self.reviewable()
        self.accept_review()
        with self.assertRaisesRegex(ValueError, "original baseline template"):
            self.publish()
        self.assert_sources_unchanged()

    def test_conflicting_existing_result_is_not_overwritten_or_partially_published(self):
        manifest = self.reviewable()
        self.accept_review()
        target = self.output / Path(manifest["files"]["letter_docx"]).name
        target.write_bytes(b"retained previous result")
        fingerprint = wf.digest(target)
        with self.assertRaisesRegex(ValueError, "Existing result differs"):
            self.publish()
        self.assertEqual(fingerprint, wf.digest(target))
        self.assertEqual([target], list(self.output.iterdir()))
        self.assert_sources_unchanged()

    def test_repeat_publish_skips_identical_and_repairs_one_missing_file(self):
        manifest = self.reviewable()
        self.accept_review()
        first = self.publish()
        self.assertEqual(3, len(first["copied"]))
        self.assertEqual([], first["already_identical"])
        for path in manifest["files"].values():
            self.assertEqual(wf.digest(path), wf.digest(self.output / Path(path).name))
        second = self.publish()
        self.assertEqual([], second["copied"])
        self.assertEqual(3, len(second["already_identical"]))
        missing = self.output / Path(manifest["files"]["statement_docx"]).name
        missing.unlink()
        third = self.publish()
        self.assertEqual([str(missing)], third["copied"])
        self.assertEqual(2, len(third["already_identical"]))
        self.assertEqual(3, len(list(self.output.iterdir())))
        self.assert_sources_unchanged()


if __name__ == "__main__":
    unittest.main(verbosity=2)
