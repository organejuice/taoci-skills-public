#!/usr/bin/env python3
"""Configurable, lossless DOCX patching and reviewed three-file publication.

Research and visual assessment belong to the invoking Codex agent. This script
does not send email, modify the roster, or claim to verify paper assertions.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from zipfile import ZipFile

from lxml import etree as ET
import pdfplumber

from workflow_io import load_config, load_advisors, resolve_path, select_advisors, template_report, is_red_run

NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
W = "{" + NS["w"] + "}"
STORY = re.compile(r"word/(document|header\d*|footer\d*|footnotes|endnotes)\.xml$")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def normalize(text):
    return re.sub(r"\s+", "", text)


def own_nodes(paragraph, local):
    return [x for x in paragraph.iter(W + local)
            if next(x.iterancestors(W + "p"), None) is paragraph]


def ptext(paragraph):
    return "".join(x.text or "" for x in own_nodes(paragraph, "t"))


def docx_text(path):
    with ZipFile(path) as package:
        return "\n".join(ptext(p) for name in package.namelist() if STORY.fullmatch(name)
                         for p in ET.fromstring(package.read(name)).iter(W + "p"))


def pdf_text(path):
    with pdfplumber.open(path) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages), len(pdf.pages)


def assert_page_coverage(images, pages):
    numbers = []
    for path in images:
        match = re.fullmatch(r"page-(\d+)\.png", Path(path).name)
        if not match:
            raise ValueError("Unexpected page-image filename")
        numbers.append(int(match.group(1)))
    if len(numbers) != pages or sorted(numbers) != list(range(1, pages + 1)):
        raise ValueError("Visual review images do not cover the PDF's actual pages")


def render_word(docx, outdir, env):
    """Export read-only through installed Word, then rasterize every PDF page."""
    import pypdfium2
    if os.name != "nt":
        raise ValueError("The Word backend requires Windows and installed Microsoft Word")
    outdir.mkdir(parents=True, exist_ok=False)
    target = outdir / docx.with_suffix(".pdf").name
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        raise ValueError("Install or configure PowerShell before using the Word backend")
    helper = Path(__file__).with_name("word_export.ps1")
    proc = subprocess.run([str(powershell), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                           "-File", str(helper), "-InputPath", str(docx), "-PdfPath", str(target)],
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, env=env)
    if proc.returncode == 0 and target.is_file():
        pdf = pypdfium2.PdfDocument(str(target))
        try:
            for i in range(len(pdf)):
                page = pdf[i]
                bitmap = page.render(scale=1.7)
                bitmap.to_pil().save(outdir / f"page-{i + 1}.png")
                bitmap.close()
                page.close()
        finally:
            pdf.close()
    return proc


def replace_range(paragraph, start, end, replacement):
    """Change text nodes only, preserving runs, drawings and all run properties."""
    nodes = own_nodes(paragraph, "t")
    length = sum(len(x.text or "") for x in nodes)
    if not 0 <= start < end <= length:
        raise ValueError(f"Invalid text span {start}:{end}/{length}")
    offset = 0
    inserted = False
    for node in nodes:
        text = node.text or ""
        a, b = offset, offset + len(text)
        offset = b
        if b <= start or a >= end:
            continue
        prefix = text[:max(0, start - a)]
        suffix = text[max(0, end - a):] if end < b else ""
        node.text = prefix + (replacement if not inserted else "") + suffix
        node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        inserted = True
    if not inserted:
        raise ValueError("Editable span contained no text")


def red_spans(paragraph, styles, theme):
    segments, offset = [], 0
    for run in own_nodes(paragraph, "r"):
        text = "".join(n.text or "" for n in run.iter(W + "t")
                       if next(n.iterancestors(W + "p"), None) is paragraph)
        if is_red_run(run, styles, theme) and text:
            if segments and segments[-1][1] == offset:
                segments[-1] = (segments[-1][0], offset + len(text), segments[-1][2] + text)
            else:
                segments.append((offset, offset + len(text), text))
        offset += len(text)
    return segments


def patch_docx(source, destination, config, advisor, direction, interest, kind, black=False):
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError("Template is read-only")
    with ZipFile(source) as package:
        entries = {x.filename: package.read(x.filename) for x in package.infolist()}
        styles = ET.fromstring(entries["word/styles.xml"]) if "word/styles.xml" in entries else None
        theme = ET.fromstring(entries["word/theme/theme1.xml"]) if "word/theme/theme1.xml" in entries else None
        changes, modified = [], {}
        interests = 0
        for part, data in entries.items():
            if not STORY.fullmatch(part):
                continue
            root = ET.fromstring(data)
            part_changes = []
            for p in root.iter(W + "p"):
                text = ptext(p)
                edits = []
                opening_span = None
                if kind == "statement" and text.startswith("自我评价："):
                    start = len("自我评价：")
                    for opening in config.get("statement_opening_deletions", []):
                        if text[start:].startswith(opening):
                            end = start + len(opening)
                            if text[end:end + 1] in ("，", ",", "。"):
                                end += 1
                            opening_span = (start, end)
                            edits.append((start, end, "", "self_evaluation_opening_deletion"))
                            break
                if kind == "statement" and "科研兴趣：" in text:
                    interests += 1
                    start = text.index("科研兴趣：") + len("科研兴趣：")
                    edits.append((start, len(text), interest, "research_interest"))
                else:
                    for start, end, old in red_spans(p, styles, theme):
                        if opening_span and opening_span[0] <= start and end <= opening_span[1]:
                            # An exact configured deletion authorizes all text within its span.
                            continue
                        word = old.strip()
                        if not word:
                            continue
                        if word == config["baseline_advisor"]:
                            value, role = advisor["name"], "advisor_name"
                        elif word == config["baseline_direction"]:
                            value, role = direction, "direction"
                        else:
                            raise ValueError(f"Unmapped red field in {part}: {old!r}; inspect template drift")
                        value = old[:len(old) - len(old.lstrip())] + value + old[len(old.rstrip()):]
                        edits.append((start, end, value, role))
                for start, end, value, role in sorted(edits, reverse=True):
                    part_changes.append({"part": part, "paragraph": root.getroottree().getpath(p),
                                         "role": role, "old": text[start:end], "new": value})
                    replace_range(p, start, end, value)
                if black:
                    for run in own_nodes(p, "r"):
                        if is_red_run(run, styles, theme):
                            props = run.find(W + "rPr")
                            if props is None:
                                props = ET.Element(W + "rPr")
                                run.insert(0, props)
                            color = props.find(W + "color")
                            if color is None:
                                color = ET.SubElement(props, W + "color")
                            color.attrib.clear()
                            color.set(W + "val", "000000")
            if part_changes or black:
                modified[part] = ET.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
                changes.extend(part_changes)
        if kind == "statement" and interests != 1:
            raise ValueError(f"Expected exactly one research-interest paragraph, found {interests}")
        roles = {x["role"] for x in changes}
        required = {"advisor_name", "direction"} if kind == "letter" else {"research_interest"}
        if kind == "statement" and config.get("statement_opening_deletions"):
            required.add("self_evaluation_opening_deletion")
        if not required <= roles:
            raise ValueError(f"Missing expected editable slots: {required - roles}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with ZipFile(destination, "x") as target:
            for entry in package.infolist():
                target.writestr(entry, modified.get(entry.filename, entries[entry.filename]))
    with ZipFile(destination) as target:
        if set(target.namelist()) != set(entries):
            raise ValueError("OOXML package inventory changed")
        for part, data in entries.items():
            if part not in modified and target.read(part) != data:
                raise ValueError(f"Preserve-only package part changed: {part}")
    result_text = docx_text(destination)
    if advisor["name"] != config["baseline_advisor"] and config["baseline_advisor"] in result_text:
        raise ValueError("Baseline advisor name remains outside allowed slots; manual template review required")
    if kind == "statement" and any(opening in result_text for opening in config.get("statement_opening_deletions", [])):
        raise ValueError("Deleted self-evaluation opening remains")
    return changes


def source_hashes(config):
    return {str(resolve_path(config, k)): digest(resolve_path(config, k))
            for k in ("roster", "letter_template", "statement_docx", "statement_pdf")}


def assert_hashes(expected):
    for path, fingerprint in expected.items():
        if not Path(path).is_file() or digest(path) != fingerprint:
            raise ValueError(f"Source/artifact changed since review: {path}")


def validate_content(content, advisor):
    if content.get("advisor_id") != advisor["id"]:
        raise ValueError("Content advisor_id does not match selected advisor")
    for field in ("direction", "research_interest"):
        if not isinstance(content.get(field), str) or not content[field].strip():
            raise ValueError(f"Missing {field}")
        if re.search(r"XXX|\[待|待补充|待核实|turn\d+(?:search|view)|\{\{", content[field], re.I):
            raise ValueError(f"Unresolved placeholder/tool token in {field}")
    if advisor["name"] not in content["research_interest"]:
        raise ValueError("Research interest must name this advisor")
    if not content.get("identity_reviewed") or not content.get("claims_reviewed"):
        raise ValueError("Identity and claim evidence review are required")
    sources = content.get("sources", [])
    if not any(x.get("kind") == "official" for x in sources):
        raise ValueError("At least one checked official identity/direction source is required")
    papers = [x for x in sources if x.get("kind") == "paper"]
    if not papers and not content.get("paper_gap_note"):
        raise ValueError("Provide a verified paper or an explicit direction-only evidence gap note")
    for source in sources:
        for key in ("kind", "title", "url", "checked_at", "supports"):
            if not source.get(key):
                raise ValueError(f"Source lacks {key}")
        if not re.match(r"^https?://", source["url"]):
            raise ValueError("Source URL must be HTTP(S)")
        if source["kind"] == "paper":
            for key in ("authors", "affiliation_check", "published", "doi", "notice_check"):
                if not source.get(key):
                    raise ValueError(f"Paper source lacks {key}")


def build(config, content_path, staging_dir=None, black=False):
    content = read_json(content_path)
    advisors = select_advisors(load_advisors(config), ids=[content.get("advisor_id", "")])
    if len(advisors) != 1:
        raise ValueError("Build one explicitly selected advisor at a time")
    advisor = advisors[0]
    validate_content(content, advisor)
    report = template_report(config)
    # Use the actual documents, independently of the report's presentation schema.
    statement_source = resolve_path(config, "statement_docx")
    source_pdf_text, page_count = pdf_text(resolve_path(config, "statement_pdf"))
    if normalize(source_pdf_text) != normalize(docx_text(statement_source)):
        raise ValueError("Statement DOCX/PDF text differs; reconcile against the designated PDF first")
    run_id = datetime.now().strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:8]
    directory = Path(staging_dir) if staging_dir else resolve_path(config, "work_dir") / "runs" / run_id / advisor["id"]
    directory = directory.resolve()
    allowed = resolve_path(config, "work_dir").resolve()
    if not directory.is_relative_to(allowed):
        raise ValueError("Staging must remain in the project's workflow work_dir")
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("Staging directory must be new or empty")
    directory.mkdir(parents=True, exist_ok=True)
    prefix = f"{advisor['number']}-"
    letter = directory / f"{prefix}套磁信-{advisor['name']}.docx"
    statement = directory / f"{prefix}自我陈述-{advisor['name']}.docx"
    protected = source_hashes(config)
    changes = patch_docx(resolve_path(config, "letter_template"), letter, config, advisor,
                         content["direction"], content["research_interest"], "letter", black)
    changes += patch_docx(statement_source, statement, config, advisor, content["direction"],
                          content["research_interest"], "statement", black)
    assert_hashes(protected)
    manifest = {"schema_version": 1, "run_id": run_id, "created_at": now(), "status": "staged",
                "advisor": advisor, "source_hashes": protected, "content": content,
                "template_report": report, "changes": changes, "baseline_pages": page_count,
                "files": {"letter_docx": str(letter), "statement_docx": str(statement),
                          "statement_pdf": str(statement.with_suffix(".pdf"))},
                "build_hashes": {str(letter): digest(letter), str(statement): digest(statement)},
                "warnings": []}
    n = len(content["research_interest"])
    bounds = config.get("interest_length", {"target_min": 150, "target_max": 200})
    if not bounds["target_min"] <= n <= bounds["target_max"]:
        manifest["warnings"].append(f"Research interest length {n}; review against template capacity")
    write_json(directory / "manifest.json", manifest)
    return manifest


def render(config, manifest_path):
    manifest_path = Path(manifest_path)
    manifest = read_json(manifest_path)
    assert_hashes(manifest["source_hashes"])
    assert_hashes(manifest["build_hashes"])
    runtime = config.get("runtime", {})
    backend = runtime.get("render_backend", "word")
    if backend not in {"word", "external"}:
        raise ValueError("render_backend must be word or external")
    if backend == "external":
        renderer_setting = runtime.get("render_docx")
        if not renderer_setting:
            raise ValueError("The external backend requires a configured render_docx script")
        renderer = Path(renderer_setting).expanduser()
        python_setting = runtime.get("python") or sys.executable
        python = Path(shutil.which(python_setting) or python_setting).expanduser()
        if not renderer.is_file() or not python.is_file():
            raise ValueError("Configure existing Python and renderer paths for the external backend")
    rendered = []
    for key in ("letter_docx", "statement_docx"):
        docx = Path(manifest["files"][key])
        outdir = manifest_path.parent / "qa" / (key + "-" + uuid.uuid4().hex[:6])
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        # The packaged renderer uses installed native LibreOffice on Windows.
        office = runtime.get("office")
        if os.name == "nt" and office and Path(office).is_file():
            env["PATH"] = str(Path(office).parent) + os.pathsep + env.get("PATH", "")
        with tempfile.TemporaryDirectory(prefix="render-tmp-", dir=manifest_path.parent, ignore_cleanup_errors=True) as tempdir:
            env["TEMP"] = env["TMP"] = env["TMPDIR"] = tempdir
            if backend == "word":
                proc = render_word(docx, outdir, env)
            else:
                proc = subprocess.run([str(python), "-X", "utf8", str(renderer), str(docx), "--output_dir", str(outdir), "--emit_pdf"],
                                      capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, env=env)
        if Path(tempdir).exists():
            manifest["warnings"].append(f"Office diagnostic cache pending cleanup after file unlock: {tempdir}")
        images = sorted(outdir.glob("page-*.png"))
        pdfs = list(outdir.glob("*.pdf"))
        if proc.returncode or not images or len(pdfs) != 1:
            write_json(manifest_path.parent / "render-error.json", {"returncode": proc.returncode,
                       "stdout": proc.stdout[-6000:], "stderr": proc.stderr[-6000:]})
            raise ValueError("Selected renderer failed; see render-error.json; do not publish")
        extracted, pages = pdf_text(pdfs[0])
        assert_page_coverage(images, pages)
        if normalize(extracted) != normalize(docx_text(docx)):
            raise ValueError(f"Rendered PDF text mismatch: {docx.name}")
        if key == "statement_docx":
            if pages != manifest["baseline_pages"]:
                raise ValueError(f"Statement pagination drift: {pages} vs {manifest['baseline_pages']}; adjust content")
            shutil.copyfile(pdfs[0], manifest["files"]["statement_pdf"])
        rendered.append({"kind": key, "pdf": str(pdfs[0]), "pages": pages,
                         "pdf_sha256": digest(pdfs[0]), "images": {str(x): digest(x) for x in images}})
    manifest["rendered"] = rendered
    manifest["status"] = "awaiting_visual_review"
    manifest["render_hashes"] = {p: digest(p) for p in manifest["files"].values()}
    # A re-render invalidates any prior receipt and publication eligibility.
    manifest.pop("qa", None)
    (manifest_path.parent / "render-error.json").unlink(missing_ok=True)
    write_json(manifest_path, manifest)
    return {"status": manifest["status"], "rendered": rendered,
            "receipt_schema": {"reviewer": "Codex agent identifier", "reviewed_at": "ISO timestamp",
                               "passed": True, "images": {p: h for r in rendered for p, h in r["images"].items()},
                               "notes": "Describe observed layout, font, image and pagination checks"}}


def qa(config, manifest_path, receipt_path):
    manifest = read_json(manifest_path)
    receipt = read_json(receipt_path)
    if manifest.get("status") not in ("awaiting_visual_review", "qa_passed", "published"):
        raise ValueError("Render the current documents before QA")
    assert_hashes(manifest["source_hashes"])
    assert_hashes(manifest["render_hashes"])
    expected = {p: h for r in manifest["rendered"] for p, h in r["images"].items()}
    if not receipt.get("reviewer") or not receipt.get("reviewed_at") or receipt.get("passed") is not True:
        raise ValueError("A completed visual review receipt is required")
    if receipt.get("images") != expected or not receipt.get("notes"):
        raise ValueError("Receipt must cover every current page image and include review notes")
    assert_hashes(expected)
    if sorted(r["kind"] for r in manifest["rendered"]) != ["letter_docx", "statement_docx"]:
        raise ValueError("Both document renders must be present exactly once")
    for r in manifest["rendered"]:
        if digest(r["pdf"]) != r["pdf_sha256"]:
            raise ValueError("Render PDF changed after visual review")
        text, actual_pages = pdf_text(r["pdf"])
        if r["pages"] != actual_pages:
            raise ValueError("Recorded render page count differs from actual PDF")
        assert_page_coverage(r["images"], actual_pages)
        if normalize(text) != normalize(docx_text(manifest["files"][r["kind"]])):
            raise ValueError("DOCX/render PDF mismatch")
    text, pages = pdf_text(manifest["files"]["statement_pdf"])
    if normalize(text) != normalize(docx_text(manifest["files"]["statement_docx"])) or pages != manifest["baseline_pages"]:
        raise ValueError("Final statement PDF mismatch")
    manifest["qa"] = {"status": "passed", "checked_at": now(), "visual_receipt": receipt,
                      "file_hashes": {p: digest(p) for p in manifest["files"].values()}}
    manifest["status"] = "qa_passed"
    write_json(manifest_path, manifest)
    return manifest["qa"]


def publish(config, manifest_path):
    manifest_path = Path(manifest_path)
    manifest = read_json(manifest_path)
    if manifest.get("qa", {}).get("status") != "passed":
        raise ValueError("Publication requires completed content/render/visual QA")
    assert_hashes(manifest["source_hashes"])
    assert_hashes(manifest["qa"]["file_hashes"])
    # Recheck the evidence of visual QA; a JSON status alone is not sufficient.
    qa(config, manifest_path, _receipt_file(manifest_path, manifest["qa"]["visual_receipt"]))
    out = resolve_path(config, "output_dir").resolve()
    protected = {resolve_path(config, k).resolve() for k in ("letter_template", "statement_docx", "statement_pdf")}
    pairs = [(Path(p), out / Path(p).name) for p in manifest["files"].values()]
    for source, target in pairs:
        if target in protected:
            raise ValueError("Publication would overwrite an original baseline template")
        if target.exists() and digest(target) != digest(source):
            raise ValueError(f"Existing result differs; retain and review before updating: {target}")
    out.mkdir(parents=True, exist_ok=True)
    copied, skipped = [], []
    try:
        for source, target in pairs:
            if target.exists():
                skipped.append(str(target))
                continue
            with source.open("rb") as src, target.open("xb") as dst:
                shutil.copyfileobj(src, dst)
            copied.append(str(target))
            if digest(source) != digest(target):
                raise ValueError("Copy verification failed")
    except Exception:
        for path in copied:
            Path(path).unlink(missing_ok=True)
        raise
    manifest["status"] = "published"
    manifest["published_at"] = now()
    manifest["published_files"] = [str(t) for _, t in pairs]
    write_json(manifest_path, manifest)
    return {"status": "published", "copied": copied, "already_identical": skipped}


def _receipt_file(manifest_path, receipt):
    path = Path(manifest_path).parent / "visual-review.json"
    write_json(path, receipt)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("inspect")
    ls = sub.add_parser("list")
    ls.add_argument("--id", action="append", dest="ids")
    ls.add_argument("--name", action="append", dest="names")
    ls.add_argument("--school")
    ls.add_argument("--start", type=int)
    ls.add_argument("--end", type=int)
    ls.add_argument("--all", action="store_true")
    b = sub.add_parser("build")
    b.add_argument("--content", required=True)
    b.add_argument("--staging-dir")
    b.add_argument("--black", action="store_true")
    for command in ("render", "publish", "qa"):
        c = sub.add_parser(command)
        c.add_argument("--manifest", required=True)
        if command == "qa":
            c.add_argument("--receipt", required=True)
    args = parser.parse_args()
    config = load_config(args.config)
    if args.command == "inspect":
        result = template_report(config)
    elif args.command == "list":
        result = select_advisors(load_advisors(config), ids=["all"] if args.all else args.ids,
                                 names=args.names, school=args.school, start=args.start, end=args.end)
    elif args.command == "build":
        result = build(config, args.content, args.staging_dir, args.black)
    elif args.command == "render":
        result = render(config, args.manifest)
    elif args.command == "qa":
        result = qa(config, args.manifest, args.receipt)
    else:
        result = publish(config, args.manifest)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        main()
    except (ValueError, OSError, KeyError, subprocess.SubprocessError) as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2)
