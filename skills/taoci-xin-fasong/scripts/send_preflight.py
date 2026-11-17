"""Read-only, offline checks for this project's outreach email materials.

Only prepare writes files: plan.json and body.txt in a new/empty work directory.
This module has no network, UI, credential, upload, draft or send implementation.
The text artifact is for review; it does not preserve Word rich text or certify a
live recipient, duplicate-send check, permission, upload, delivery or layout.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

# Do not create bytecode next to the reused helper in managed .agents/skills.
sys.dont_write_bytecode = True

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "references" / "project.json"
STATUS = "prepared_offline_needs_live_review"
REQUIRED = {
    "project_root", "roster", "materials_dir", "work_dir", "roster_helper",
    "sender", "expected_subject", "attachment_overrides",
}
ATOM = r"[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+"
LABEL = r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
ADDRESS = re.compile(rf"{ATOM}(?:\.{ATOM})*@(?:{LABEL}\.)+[A-Za-z]{{2,63}}\Z")


class PreflightError(ValueError):
    """A required offline check failed; never proceed to sending from this result."""


def _json(path: Path) -> dict[str, Any]:
    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise PreflightError(f"Duplicate JSON key: {key}")
            result[key] = value
        return result

    with path.open(encoding="utf-8-sig") as stream:
        result = json.load(stream, object_pairs_hook=unique)
    if not isinstance(result, dict):
        raise PreflightError(f"Expected a JSON object: {path}")
    return result


def load_config(path: str | Path = DEFAULT_CONFIG) -> tuple[dict[str, Any], Path]:
    target = Path(path).expanduser().resolve(strict=True)
    config = _json(target)
    missing = REQUIRED.difference(config)
    if missing:
        raise PreflightError(f"Missing configuration fields: {', '.join(sorted(missing))}")
    for key in REQUIRED.difference({"attachment_overrides"}):
        if not isinstance(config[key], str) or not config[key].strip():
            raise PreflightError(f"Configuration {key} must be a nonempty string")
    if not isinstance(config["attachment_overrides"], dict):
        raise PreflightError("attachment_overrides must be an object")
    root = Path(config["project_root"]).expanduser()
    if not root.is_absolute():
        root = target.parent / root
    config["project_root"] = str(root.resolve(strict=True))
    for key in ("roster", "materials_dir", "work_dir", "roster_helper"):
        value = Path(config[key]).expanduser()
        config[key] = str((value if value.is_absolute() else root / value).resolve())
    single_address(config["sender"], "sender")
    return config, target


def single_address(value: Any, label: str = "email") -> str:
    # Full-field validation deliberately rejects notes, display names, multiple
    # addresses and embedded whitespace instead of searching for one substring.
    if not isinstance(value, str) or len(value) > 254 or not ADDRESS.fullmatch(value):
        raise PreflightError(f"{label} must contain exactly one complete bare email address")
    if len(value.split("@", 1)[0]) > 64:
        raise PreflightError(f"{label} local part exceeds 64 characters")
    return value


def _helper(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("outreach_roster_io", path)
    if spec is None or spec.loader is None:
        raise PreflightError(f"Cannot load the configured roster helper: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name in ("load_advisors", "docx_story_parts", "paragraph_text", "WP"):
        if not hasattr(module, name):
            raise PreflightError(f"Roster helper does not provide {name}")
    return module


def fingerprint(path: str | Path) -> dict[str, Any]:
    target = Path(path).resolve(strict=True)
    if not target.is_file():
        raise PreflightError(f"Required source is not a file: {target}")
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return {"path": str(target), "bytes": target.stat().st_size, "sha256": digest.hexdigest()}


def normalized(value: str) -> str:
    """Only remove whitespace; do not hide punctuation or content differences."""
    return re.sub(r"\s+", "", value)


def _component(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or re.search(r'[\\/:*?"<>|\x00-\x1f]', value):
        raise PreflightError(f"Unsafe or missing filename component: {label}")
    return value


def _paragraphs(helper: Any, path: Path) -> list[str]:
    parts = helper.docx_story_parts(path)
    if "word/document.xml" not in parts:
        raise PreflightError(f"DOCX main document is missing: {path}")
    return [helper.paragraph_text(p) for p in parts["word/document.xml"].iter(helper.WP)]


def _letter(paragraphs: list[str], name: str, expected_subject: str) -> tuple[str, str]:
    if not paragraphs or not paragraphs[0].strip().startswith("邮件标题："):
        raise PreflightError("The first letter paragraph must start with 邮件标题：")
    subject = paragraphs[0].strip().removeprefix("邮件标题：").strip()
    if subject != expected_subject or "\n" in subject or "\r" in subject:
        raise PreflightError("Letter subject differs from expected_subject")
    greeting = f"{name}老师您好！"
    starts = [i for i, text in enumerate(paragraphs) if text.strip() == greeting]
    if len(starts) != 1:
        raise PreflightError(f"Letter must contain exactly one greeting for {name}")
    start = starts[0]
    endings = [i for i in range(start + 1, len(paragraphs)) if re.fullmatch(
        r"[（(]?电话[：:]\s*(?:\+?86[- ]?)?1[3-9][0-9]{9}[^\r\n]*", paragraphs[i].strip()
    )]
    if len(endings) != 1:
        raise PreflightError("Letter must contain exactly one signature telephone line")
    end = endings[0]
    if any(p.strip() for p in paragraphs[end + 1:]):
        raise PreflightError("Unexpected nonempty content after the signature telephone line")
    body = "\n".join(p.rstrip() for p in paragraphs[start:end + 1])
    if "邮件标题" in body or "邮件内容" in body:
        raise PreflightError("Letter body contains an email template label")
    greetings = re.findall(r"([^\s，。！？：:]+)老师您好[！!]", body)
    if greetings != [name]:
        raise PreflightError("Letter body includes a different or duplicate advisor greeting")
    sender_name = expected_subject.rsplit("-", 1)[-1]
    if not any("学生" in p and sender_name in p for p in paragraphs[start + 1:end]):
        raise PreflightError("Student name signature is missing before the telephone line")
    return subject, body


def build_plan(config_path: str | Path, advisor_id: str) -> dict[str, Any]:
    """Read source materials, validate one explicit M ID, and return a plan."""
    if not re.fullmatch(r"M[0-9]{3}", advisor_id) or int(advisor_id[1:]) <= 0:
        raise PreflightError("Select one exact positive Mxxx advisor ID")
    config, config_file = load_config(config_path)
    initial = {key: fingerprint(path) for key, path in {
        "config": config_file, "roster": config["roster"], "roster_helper": config["roster_helper"],
    }.items()}
    helper = _helper(Path(config["roster_helper"]))
    advisors = helper.load_advisors(config)
    matches = [a for a in advisors if a["id"] == advisor_id]
    if len(matches) != 1:
        raise PreflightError(f"Advisor ID must resolve to one unique record: {advisor_id}")
    advisor = matches[0]
    name = _component(advisor["name"], "advisor name")
    school = _component(advisor["school"], "advisor school")
    to = single_address(advisor.get("email"), f"{advisor_id} email")
    if advisor.get("contact_match_status") != "exact":
        raise PreflightError("Advisor has no exact joined contact history record")
    materials = Path(config["materials_dir"]).resolve(strict=True)
    number = advisor["number"]
    overrides = config["attachment_overrides"]
    same_name = [a for a in advisors if a["name"] == name]
    if len(same_name) > 1 and advisor_id not in overrides:
        raise PreflightError("A same-name advisor requires an explicit school-specific attachment override")
    pdf_name = overrides.get(advisor_id, f"自我陈述-{name}.pdf")
    _component(pdf_name, "attachment override")
    required_pdf_name = f"自我陈述-{name}-{school}.pdf" if len(same_name) > 1 else f"自我陈述-{name}.pdf"
    if pdf_name != required_pdf_name:
        raise PreflightError("Attachment filename must match the advisor and, for duplicate names, school")
    paths = {
        "letter_docx": materials / f"{number}-套磁信-{name}.docx",
        "statement_docx": materials / f"{number}-自我陈述-{name}.docx",
        "statement_pdf": materials / pdf_name,
    }
    # Exact current names only: there is intentionally no glob or old-prefix fallback.
    sources = {**initial, **{key: fingerprint(path) for key, path in paths.items()}}
    if any(not Path(source["path"]).is_relative_to(materials) for key, source in sources.items() if key in paths):
        raise PreflightError("A material path resolves outside materials_dir")
    letter = _paragraphs(helper, paths["letter_docx"])
    statement = _paragraphs(helper, paths["statement_docx"])
    subject, body = _letter(letter, name, config["expected_subject"])
    interests = [p.strip() for p in statement if p.lstrip().startswith("科研兴趣")]
    if len(interests) != 1 or f"{name}老师" not in interests[0]:
        raise PreflightError("Statement scientific-interest paragraph must identify the selected advisor")
    for other in {a["name"] for a in advisors}.difference({name}):
        if f"{other}老师" in body or f"{other}老师" in interests[0]:
            raise PreflightError(f"Material mentions a different advisor: {other}")
    import pdfplumber

    with pdfplumber.open(paths["statement_pdf"]) as pdf:
        page_count = len(pdf.pages)
        pdf_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    expected_pages = config.get("statement_pdf_pages", 1)
    if type(expected_pages) is not int or expected_pages <= 0:
        raise PreflightError("statement_pdf_pages must be a positive integer")
    if page_count != expected_pages:
        raise PreflightError(f"Statement PDF must have {expected_pages} pages; found {page_count}")
    doc_text = normalized("\n".join(statement))
    if not doc_text or doc_text != normalized(pdf_text):
        raise PreflightError("Statement DOCX and PDF extracted text differ after whitespace normalization")
    direction_match = re.search(r"研究方向为(.+?)[，,]学生", body)
    direction = direction_match.group(1) if direction_match else None
    warnings = [
        "Offline preparation only; live sender/recipient, authorization and duplicate-send review remain required.",
        "body.txt is plain text for comparison; rich text formatting requires live review.",
        "PDF text equality does not verify layout or image CV content; images are not OCRed.",
        "History is copied from the roster and does not establish current sent-mail status.",
    ]
    direction_compared = False
    if direction:
        strip_quotes = lambda s: normalized(s).translate(str.maketrans("", "", '“”「」『』\"'))
        if strip_quotes(direction) not in strip_quotes(interests[0]):
            raise PreflightError("Letter research direction is absent from the statement scientific-interest paragraph")
        direction_compared = True
    else:
        warnings.append("Letter research direction was not mechanically isolated; compare the complete source texts manually.")
    # Rehash after all reads so a changed input cannot silently yield a mixed plan.
    if any(fingerprint(source["path"]) != source for source in sources.values()):
        raise PreflightError("A source changed while preparing; prepare again after edits finish")
    return {
        "schema_version": 1,
        "status": STATUS,
        "advisor": advisor_id,
        "name": name,
        "school": school,
        "department": advisor["department"],
        "to": to,
        "cc": [],
        "bcc": [],
        "sender": config["sender"],
        "subject": subject,
        "body_text": body,
        "sources": sources,
        "attachments": [{"name": pdf_name, **sources["statement_pdf"]}],
        "history": {key: advisor.get(key) for key in (
            "contact_status", "response_status", "contact_match_status", "contact_row",
        )},
        "material_checks": {
            "pdf_pages": page_count,
            "statement_docx_pdf_text_equal": True,
            "statement_interest": interests[0],
            "body_direction": direction,
            "direction_compared_to_statement": direction_compared,
            "layout_verified": False,
            "image_cv_verified": False,
        },
        "warnings": warnings,
    }


def _output(config: dict[str, Any], path: str | Path) -> Path:
    work = Path(config["work_dir"]).resolve()
    output = Path(path).expanduser().resolve()
    if output == work or not output.is_relative_to(work):
        raise PreflightError("Output must be a new or empty child directory inside work_dir")
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise PreflightError("Output directory must be new or empty; existing files are never overwritten")
    return output


def prepare(config_path: str | Path, advisor_id: str, out: str | Path) -> Path:
    config, _ = load_config(config_path)
    output = _output(config, out)
    plan = build_plan(config_path, advisor_id)
    _output(config, output)  # Recheck immediately before the only write operation.
    output.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    try:
        for name, contents in (
            ("plan.json", json.dumps(plan, ensure_ascii=False, indent=2) + "\n"),
            ("body.txt", plan["body_text"] + "\n"),
        ):
            target = output / name
            with target.open("x", encoding="utf-8", newline="\n") as stream:
                created.append(target)
                stream.write(contents)
    except OSError:
        for target in created:
            target.unlink(missing_ok=True)
        raise
    return output / "plan.json"


def check(config_path: str | Path, plan_path: str | Path) -> str:
    """Recompute every plan field against current sources; perform no writes."""
    config, _ = load_config(config_path)
    target = Path(plan_path).expanduser().resolve(strict=True)
    if target.name != "plan.json" or not target.is_relative_to(Path(config["work_dir"]).resolve()):
        raise PreflightError("Plan must be plan.json inside configured work_dir")
    plan = _json(target)
    advisor_id = plan.get("advisor")
    if not isinstance(advisor_id, str):
        raise PreflightError("Plan advisor ID is missing or invalid")
    expected = build_plan(config_path, advisor_id)
    if plan != expected:
        fields = sorted(key for key in set(plan) | set(expected) if plan.get(key) != expected.get(key))
        raise PreflightError(f"Plan or source changed: {', '.join(fields)}; prepare again and review")
    if (target.parent / "body.txt").read_text(encoding="utf-8") != expected["body_text"] + "\n":
        raise PreflightError("body.txt changed; prepare again and review")
    return "offline_passed_needs_live_review"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG, help="Project JSON configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)
    make = subparsers.add_parser("prepare", help="Validate one advisor and write plan.json + body.txt only")
    make.add_argument("--id", required=True, help="One exact Mxxx advisor ID")
    make.add_argument("--out", type=Path, required=True, help="New/empty child directory of configured work_dir")
    verify = subparsers.add_parser("check", help="Recompute source hashes and compare the complete offline plan")
    verify.add_argument("--plan", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = prepare(args.config, args.id, args.out) if args.command == "prepare" else check(args.config, args.plan)
        print(result)
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"offline_preflight_failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
