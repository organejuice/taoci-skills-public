"""Offline, source-bound HTML and MIME bundles for one explicit advisor.

No network, mailbox, credentials or sending operations live here. SMTP and IMAP
must reuse the exact checked ``eml_bytes`` rather than serialize another message.
Unsupported Word content fails closed and must be resolved in the source first.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import format_datetime
import hashlib
import html
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any
from zipfile import ZipFile

sys.dont_write_bytecode = True
_SPEC = importlib.util.spec_from_file_location("compose_preflight", Path(__file__).with_name("send_preflight.py"))
preflight = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preflight)
_WIRE_SPEC = importlib.util.spec_from_file_location("compose_mime_wire", Path(__file__).with_name("mime_wire.py"))
mime_wire = importlib.util.module_from_spec(_WIRE_SPEC)
_WIRE_SPEC.loader.exec_module(mime_wire)
ComposeError = preflight.PreflightError
DEFAULT_CONFIG = preflight.DEFAULT_CONFIG
W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
STATUS = "prepared_offline_needs_live_review"
OUTPUT_NAMES = {"plan_json": "plan.json", "body_txt": "body.txt", "body_html": "body.html", "message_eml": "message.eml"}


def _tag(name: str) -> str:
    return f"{{{W}}}{name}"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _generators(config: dict[str, Any]) -> dict[str, Any]:
    return {key: preflight.fingerprint(path) for key, path in {
        "mail_compose": __file__, "send_preflight": preflight.__file__,
        "mime_wire": mime_wire.__file__,
        "workflow_io": config["roster_helper"],
    }.items()}


def _parse_xml(data: bytes) -> Any:
    from lxml import etree
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    root = etree.fromstring(data, parser=parser)
    if root.getroottree().docinfo.doctype:
        raise ComposeError("DOCX XML declarations with a DTD are unsupported")
    return root


def _val(node: Any, name: str = "val") -> str | None:
    return node.get(_tag(name)) if node is not None else None


def _style_chain(styles: Any, style_id: str | None) -> list[Any]:
    indexed = {_val(s, "styleId"): s for s in styles.findall("w:style", NS)} if styles is not None else {}
    chain, seen = [], set()
    while style_id:
        if style_id in seen:
            raise ComposeError("Cyclic Word style inheritance")
        seen.add(style_id)
        style = indexed.get(style_id)
        if style is None:
            raise ComposeError(f"Missing referenced Word style: {style_id}")
        chain.append(style)
        style_id = _val(style.find("w:basedOn", NS))
    return list(reversed(chain))


def _properties(run: Any, paragraph: Any, styles: Any) -> list[Any]:
    """Resolve inherited fonts/size/bold, then direct formatting, without guessing."""
    result = []
    if styles is not None:
        result.append(styles.find("w:docDefaults/w:rPrDefault/w:rPr", NS))
    paragraph_style = _val(paragraph.find("w:pPr/w:pStyle", NS))
    if not paragraph_style and styles is not None:
        defaults = styles.xpath("./w:style[@w:type='paragraph' and (@w:default='1' or @w:default='true')]", namespaces=NS)
        paragraph_style = _val(defaults[0], "styleId") if defaults else None
    result.extend(style.find("w:rPr", NS) for style in _style_chain(styles, paragraph_style))
    result.append(paragraph.find("w:pPr/w:rPr", NS))
    result.extend(style.find("w:rPr", NS) for style in _style_chain(styles, _val(run.find("w:rPr/w:rStyle", NS))))
    result.append(run.find("w:rPr", NS))
    return [p for p in result if p is not None]


def _property(props: list[Any], name: str) -> Any:
    return next((node for prop in reversed(props) if (node := prop.find(f"w:{name}", NS)) is not None), None)


def _number(value: str | None, label: str) -> float:
    if value is None or not re.fullmatch(r"-?\d+(?:\.\d+)?", value):
        raise ComposeError(f"Unsupported numeric Word property: {label}")
    number = float(value)
    if not -100000 <= number <= 100000:
        raise ComposeError(f"Out-of-range Word property: {label}")
    return number


def _on(node: Any) -> bool:
    return node is not None and _val(node) not in ("0", "false", "off")


def _run_css(run: Any, paragraph: Any, styles: Any, theme: Any, helper: Any) -> str:
    props = _properties(run, paragraph, styles)
    css = []
    color = helper.effective_run_color(run, styles, theme)
    # Helper ignores paragraph-mark rPr; use it only as a fallback when a run
    # has no other resolved color. Never transfer red editing marks to email.
    if color is None:
        rgb = _val(_property(props, "color"))
        color = rgb.upper() if rgb and re.fullmatch(r"[0-9a-fA-F]{6}", rgb) else None
    is_red = helper.is_red_run(run, styles, theme)
    if color and (is_red or helper._red_rgb(color)):
        color = "000000"
    if color:
        css.append(f"color:#{color}")
    size = _val(_property(props, "sz"))
    if size is not None:
        points = _number(size, "font size") / 2
        if not 1 <= points <= 200:
            raise ComposeError("Unsupported Word font size")
        css.append(f"font-size:{points:g}pt")
    fonts = _property(props, "rFonts")
    if fonts is not None:
        families = list(dict.fromkeys(value for name in ("ascii", "hAnsi", "eastAsia", "cs") if (value := _val(fonts, name))))
        if families:
            # Font names are source data, not executable CSS.
            if any(not re.fullmatch(r"[\w .()-]{1,100}", value, flags=re.UNICODE) for value in families):
                raise ComposeError("Unsafe or unsupported Word font family")
            css.append("font-family:" + ",".join(f"'{value}'" for value in families))
    if _property(props, "b") is not None:
        css.append("font-weight:" + ("bold" if _on(_property(props, "b")) else "normal"))
    if _property(props, "i") is not None:
        css.append("font-style:" + ("italic" if _on(_property(props, "i")) else "normal"))
    underline = _val(_property(props, "u"))
    if underline and underline != "none":
        css.append("text-decoration:underline")
    if _on(_property(props, "vanish")) or _on(_property(props, "webHidden")):
        raise ComposeError("Hidden Word text requires explicit source review")
    if _on(_property(props, "strike")) or _on(_property(props, "dstrike")):
        raise ComposeError("Struck-through Word text requires explicit source review")
    return ";".join(css)


def _paragraph_css(paragraph: Any, signature: bool, greeting: bool = False) -> str:
    css = ["margin:0", "white-space:pre-wrap", "overflow-wrap:anywhere"]
    if signature:
        return ";".join(css + ["text-align:right", "text-indent:0"])
    # Email salutations begin at the left edge; subsequent paragraphs retain
    # their source first-line indentation rather than inheriting this override.
    if greeting:
        css.append("text-indent:0")
    prop = paragraph.find("w:pPr", NS)
    if prop is None:
        return ";".join(css)
    alignment = _val(prop.find("w:jc", NS))
    if alignment:
        alignments = {"left": "left", "start": "left", "right": "right", "end": "right", "center": "center", "both": "justify", "distribute": "justify"}
        if alignment not in alignments:
            raise ComposeError(f"Unsupported Word paragraph alignment: {alignment}")
        css.append(f"text-align:{alignments[alignment]}")
    indent = prop.find("w:ind", NS)
    if indent is not None:
        for twips, chars, name in (("left", "leftChars", "margin-left"), ("right", "rightChars", "margin-right"), ("firstLine", "firstLineChars", "text-indent"), ("hanging", "hangingChars", "text-indent")):
            if greeting and twips in ("firstLine", "hanging"):
                continue
            value, character = _val(indent, twips), _val(indent, chars)
            sign = -1 if twips == "hanging" else 1
            if character is not None:
                css.append(f"{name}:{sign * _number(character, chars) / 100:g}em")
            elif value is not None:
                css.append(f"{name}:{sign * _number(value, twips) / 20:g}pt")
    spacing = prop.find("w:spacing", NS)
    if spacing is not None:
        for word, css_name in (("before", "margin-top"), ("after", "margin-bottom")):
            if (value := _val(spacing, word)) is not None:
                css.append(f"{css_name}:{_number(value, word) / 20:g}pt")
        if (value := _val(spacing, "line")) is not None:
            line = _number(value, "line spacing")
            rule = _val(spacing, "lineRule") or "auto"
            if rule == "auto":
                css.append(f"line-height:{line / 240:g}")
            elif rule in ("exact", "atLeast"):
                css.append(f"line-height:{line / 20:g}pt")
            else:
                raise ComposeError("Unsupported Word line spacing")
    return ";".join(css)


def _assert_safe_document(root: Any) -> None:
    """Reject content that the deliberately narrow renderer cannot represent."""
    prohibited = {"fldChar", "fldSimple", "instrText", "delInstrText", "drawing", "pict", "object", "sym", "footnoteReference", "endnoteReference", "commentReference", "ins", "del", "moveFrom", "moveTo", "altChunk", "sdt", "hyperlink", "tbl", "customXml", "subDoc", "numPr"}
    for node in root.iter():
        local = node.tag.split("}")[-1] if isinstance(node.tag, str) else ""
        if local in prohibited or local.endswith("Change") or local.startswith(("moveFrom", "moveTo")):
            raise ComposeError(f"Unsupported Word object, field or revision: {local}")
    body = root.find("w:body", NS)
    if body is None:
        raise ComposeError("Missing Word document body")
    for node in body:
        if node.tag not in {_tag("p"), _tag("sectPr")}:
            raise ComposeError("Unsupported Word body structure")
    for paragraph in body.findall("w:p", NS):
        for node in paragraph:
            if node.tag not in {_tag(x) for x in ("pPr", "r", "bookmarkStart", "bookmarkEnd", "proofErr")}:
                raise ComposeError(f"Unsupported Word paragraph content: {node.tag}")
        for run in paragraph.findall("w:r", NS):
            for node in run:
                if node.tag not in {_tag(x) for x in ("rPr", "t", "tab", "br", "cr")}:
                    raise ComposeError(f"Unsupported Word run content: {node.tag}")
                if node.tag == _tag("br") and _val(node, "type") not in (None, "textWrapping"):
                    raise ComposeError("Word page/column breaks require explicit source review")


def render_html(plan: dict[str, Any], config: dict[str, Any]) -> str:
    """Render source run order; compare all visible text to the checked plan."""
    helper = preflight._helper(Path(config["roster_helper"]))
    for name in ("effective_run_color", "is_red_run", "_red_rgb"):
        if not hasattr(helper, name):
            raise ComposeError(f"Roster helper does not provide {name}")
    with ZipFile(plan["sources"]["letter_docx"]["path"]) as archive:
        root = _parse_xml(archive.read("word/document.xml"))
        styles = _parse_xml(archive.read("word/styles.xml")) if "word/styles.xml" in archive.namelist() else None
        theme = _parse_xml(archive.read("word/theme/theme1.xml")) if "word/theme/theme1.xml" in archive.namelist() else None
    _assert_safe_document(root)
    paragraphs = root.find("w:body", NS).findall("w:p", NS)
    texts = [helper.paragraph_text(p) for p in paragraphs]
    subject, source_body = preflight._letter(texts, plan["name"], plan["subject"])
    if subject != plan["subject"] or source_body != plan["body_text"]:
        raise ComposeError("OOXML paragraph text differs from plan body")
    start = texts.index(next(t for t in texts if t.strip() == f"{plan['name']}老师您好！"))
    # A paragraph may itself contain a line break; select by the telephone end.
    end = next(i for i in range(start + 1, len(texts)) if re.fullmatch(r"[（(]?电话[：:]\s*(?:\+?86[- ]?)?1[3-9][0-9]{9}[^\r\n]*", texts[i].strip()))
    selected = paragraphs[start:end + 1]
    rendered, reconstructed = [], []
    sender_name = plan["subject"].rsplit("-", 1)[-1]
    for index, paragraph in enumerate(selected):
        text = helper.paragraph_text(paragraph)
        signature = index == len(selected) - 1 or (sender_name in text and re.match(r"\s*(?:姓名|学生|申请人)[：:]", text))
        leading = len(text) - len(text.lstrip()) if signature else 0
        spans, pieces = [], []
        for run in paragraph.findall("w:r", NS):
            pieces.append("".join((n.text or "") if n.tag == _tag("t") else "\t" if n.tag == _tag("tab") else "\n" for n in run if n.tag != _tag("rPr")))
            run_text = pieces[-1]
            skip = min(leading, len(run_text))
            leading -= skip
            run_text = run_text[skip:]
            if run_text:
                css = _run_css(run, paragraph, styles, theme, helper)
                spans.append(f'<span style="{html.escape(css, quote=True)}">{html.escape(run_text, quote=True)}</span>')
        if "".join(pieces) != text:
            raise ComposeError("OOXML run order/text differs from paragraph text")
        reconstructed.append(text.rstrip())
        css = _paragraph_css(paragraph, bool(signature), greeting=index == 0)
        rendered.append(f'<p style="{html.escape(css, quote=True)}">{"".join(spans) or "<br>"}</p>')
    if "\n".join(reconstructed) != plan["body_text"]:
        raise ComposeError("OOXML run text differs from plan body")
    # Explicit ordinary foreground/background prevents red editing colors and
    # dark-mode screenshots from becoming source-of-truth formatting guesses.
    return ('<!doctype html>\n<html lang="zh-CN"><head><meta charset="utf-8">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">'
            '<title>邮件正文预览</title></head><body style="margin:0;color:#000000;background-color:#FFFFFF">\n'
            + "\n".join(rendered) + "\n</body></html>\n")


def _identity(plan: dict[str, Any], html_text: str, output: Path, created_at: str) -> tuple[str, str, str]:
    try:
        stamp = datetime.fromisoformat(created_at)
    except (ValueError, TypeError) as exc:
        raise ComposeError("Invalid bundle creation timestamp") from exc
    if stamp.tzinfo != timezone.utc or stamp.isoformat(timespec="seconds") != created_at:
        raise ComposeError("Bundle timestamp must be canonical UTC seconds")
    token = _sha(_canonical({"plan": plan, "html": html_text, "output": str(output), "created_at": created_at}))
    domain = plan["sender"].split("@", 1)[1]
    return f"<{token}@{domain}>", format_datetime(stamp), token


def _make_eml(plan: dict[str, Any], html_text: str, attachment: bytes, message_id: str, date: str, token: str) -> bytes:
    preflight.single_address(plan["sender"], "sender")
    preflight.single_address(plan["to"], "recipient")
    if plan["cc"] or plan["bcc"] or len(plan["attachments"]) != 1 or "\r" in plan["subject"] or "\n" in plan["subject"]:
        raise ComposeError("MIME requires one recipient, no Cc/Bcc and exactly one attachment")
    message = EmailMessage(policy=policy.SMTP)
    message["From"], message["To"] = plan["sender"], plan["to"]
    message["Subject"], message["Message-ID"], message["Date"] = plan["subject"], message_id, date
    message.set_content(plan["body_text"], subtype="plain", charset="utf-8", cte="base64")
    message.add_alternative(html_text, subtype="html", charset="utf-8", cte="base64")
    # 128 bits from the source-bound identity, with distinct domain prefixes.
    # Short ordinary ASCII parameters survive SMTP folding without RFC 2231
    # boundary continuations; the old inner 76-character boundary was illegal.
    message.set_boundary("a_" + _sha(("alternative:" + token).encode("ascii"))[:32])
    message.add_attachment(attachment, maintype="application", subtype="pdf", filename=plan["attachments"][0]["name"], cte="base64")
    message.set_boundary("m_" + _sha(("mixed:" + token).encode("ascii"))[:32])
    return message.as_bytes(policy=policy.SMTP)


def _manifest(plan: dict[str, Any], config: dict[str, Any], output: Path, created_at: str, html_text: str) -> tuple[dict[str, Any], bytes, bytes]:
    attachment = Path(plan["attachments"][0]["path"]).read_bytes()
    if _sha(attachment) != plan["attachments"][0]["sha256"] or len(attachment) != plan["attachments"][0]["bytes"]:
        raise ComposeError("Attachment changed during MIME preparation")
    message_id, date, token = _identity(plan, html_text, output, created_at)
    eml = _make_eml(plan, html_text, attachment, message_id, date, token)
    manifest = {"schema_version": 2, "status": STATUS, "advisor": plan["advisor"], "created_at": created_at,
                "message_id": message_id, "date": date, "from": plan["sender"], "to": plan["to"],
                "subject": plan["subject"], "cc": [], "bcc": [], "attachments": plan["attachments"],
                "sources": plan["sources"], "generators": _generators(config), "outputs": {}}
    _verify_mime(eml, plan, html_text, attachment, manifest)
    return manifest, eml, attachment


def prepare_bundle(config_path: str | Path, advisor_id: str, out: str | Path) -> Path:
    """Write one new/empty offline bundle, never overwrite existing files."""
    config, _ = preflight.load_config(config_path)
    output = preflight._output(config, out)
    generators = _generators(config)
    plan_path = preflight.prepare(config_path, advisor_id, output)
    plan = preflight._json(plan_path)
    html_text = render_html(plan, config)
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    manifest, eml, _ = _manifest(plan, config, output, created_at, html_text)
    if generators != manifest["generators"]:
        raise ComposeError("Generator changed while preparing; prepare again")
    # Inputs are checked again before publishing the derived artifacts.
    preflight.check(config_path, plan_path)
    for name, contents in (("body.html", html_text.encode("utf-8")), ("message.eml", eml)):
        with (output / name).open("xb") as stream:
            stream.write(contents)
    manifest["outputs"] = {key: preflight.fingerprint(output / name) for key, name in OUTPUT_NAMES.items()}
    target = output / "bundle.json"
    with target.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    check_bundle(config_path, target)
    return target


def _verify_mime(eml: bytes, plan: dict[str, Any], html_text: str, attachment: bytes, manifest: dict[str, Any]) -> dict[str, Any]:
    # This check reads raw CRLF headers/delimiters and base64 independently of
    # Python's email parser, which accepted the original broken boundary wire.
    wire = mime_wire.validate_wire(eml)
    expected_payloads = {"plain": (plan["body_text"] + "\n").replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"),
                         "html": html_text.replace("\r\n", "\n").replace("\n", "\r\n").encode("utf-8"),
                         "pdf": attachment}
    if wire["filename"] != plan["attachments"][0]["name"] or wire["payloads"] != {
            key: {"bytes": len(value), "sha256": _sha(value)} for key, value in expected_payloads.items()}:
        raise ComposeError("Wire decoded payloads differ from checked sources")
    parsed = BytesParser(policy=policy.SMTP).parsebytes(eml)
    if any(part.defects for part in parsed.walk()):
        raise ComposeError("Malformed MIME message")
    expected_headers = {"From": plan["sender"], "To": plan["to"], "Subject": plan["subject"], "Message-ID": manifest["message_id"], "Date": manifest["date"]}
    for key, expected in expected_headers.items():
        values = parsed.get_all(key, [])
        if len(values) != 1 or str(values[0]) != expected:
            raise ComposeError(f"MIME header differs: {key}")
    if sorted(key.lower() for key in parsed.keys()) != sorted(key.lower() for key in (*expected_headers, "MIME-Version", "Content-Type")):
        raise ComposeError("Unexpected MIME headers or extra recipients")
    if parsed.get_content_type() != "multipart/mixed" or len(parsed.get_payload()) != 2:
        raise ComposeError("MIME must contain one alternative body and one PDF")
    alternative, pdf = parsed.get_payload()
    if alternative.get_content_type() != "multipart/alternative" or len(alternative.get_payload()) != 2:
        raise ComposeError("MIME must contain plain and HTML alternatives")
    plain, rich = alternative.get_payload()
    if (plain.get_content_type(), rich.get_content_type()) != ("text/plain", "text/html"):
        raise ComposeError("Unexpected MIME alternative types")
    # MIME uses CRLF; compare decoded textual content without hiding other edits.
    if plain.get_content().replace("\r\n", "\n") != plan["body_text"] + "\n" or rich.get_content().replace("\r\n", "\n") != html_text:
        raise ComposeError("MIME alternative content differs")
    attachments = list(parsed.iter_attachments())
    if len(attachments) != 1 or attachments[0] is not pdf or pdf.get_content_type() != "application/pdf" or pdf.get_content_disposition() != "attachment":
        raise ComposeError("MIME must contain exactly one application/pdf attachment")
    if pdf.get_filename() != plan["attachments"][0]["name"] or pdf.get_payload(decode=True) != attachment:
        raise ComposeError("MIME attachment name/content differs")
    return wire


def check_bundle(config_path: str | Path, bundle_path: str | Path) -> dict[str, Any]:
    """Read-only current-source, manifest, HTML and exact MIME validation."""
    config, _ = preflight.load_config(config_path)
    target = Path(bundle_path).expanduser().resolve(strict=True)
    output = target.parent
    work = Path(config["work_dir"]).resolve()
    if target.name != "bundle.json" or output == work or not output.is_relative_to(work):
        raise ComposeError("Bundle must be bundle.json in a child directory inside work_dir")
    for name in (*OUTPUT_NAMES.values(), "bundle.json"):
        path = output / name
        if path.is_symlink() or path.resolve(strict=True).parent != output:
            raise ComposeError("Bundle artifacts must be local files, never redirected paths")
    original = {name: (output / name).read_bytes() for name in (*OUTPUT_NAMES.values(), "bundle.json")}
    manifest = preflight._json(target)
    if manifest.get("schema_version") != 2:
        raise ComposeError("Obsolete MIME bundle version; prepare again")
    preflight.check(config_path, output / "plan.json")
    plan = preflight._json(output / "plan.json")
    html_text = render_html(plan, config)
    expected, eml, attachment = _manifest(plan, config, output, manifest.get("created_at"), html_text)
    expected["outputs"] = {key: preflight.fingerprint(output / name) for key, name in OUTPUT_NAMES.items()}
    if manifest != expected:
        fields = sorted(key for key in set(manifest) | set(expected) if manifest.get(key) != expected.get(key))
        raise ComposeError(f"Bundle manifest or generators changed: {', '.join(fields)}; prepare again")
    if original["bundle.json"] != (json.dumps(expected, ensure_ascii=False, indent=2) + "\n").encode("utf-8"):
        raise ComposeError("Bundle manifest serialization changed; prepare again")
    if original["body.html"] != html_text.encode("utf-8"):
        raise ComposeError("HTML differs from current source formatting")
    if original["message.eml"] != eml:
        raise ComposeError("MIME bytes differ from the source-bound bundle")
    wire = _verify_mime(original["message.eml"], plan, html_text, attachment, manifest)
    preflight.check(config_path, output / "plan.json")
    if any((output / name).read_bytes() != contents for name, contents in original.items()) or expected["generators"] != _generators(config):
        raise ComposeError("Bundle or generator changed during verification")
    return {"status": "offline_passed_needs_live_review", "plan": plan, "bundle": manifest, "eml_bytes": original["message.eml"],
            "html_text": html_text, "attachment_bytes": attachment, "message_id": manifest["message_id"],
            "wire_validation": wire,
            "sender": plan["sender"], "recipient": plan["to"], "bundle_path": target,
            "plan_path": output / "plan.json", "eml_path": output / "message.eml", "html_path": output / "body.html"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    prepare = sub.add_parser("prepare")
    prepare.add_argument("--id", required=True)
    prepare.add_argument("--out", type=Path, required=True)
    check = sub.add_parser("check")
    check.add_argument("--bundle", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            print(prepare_bundle(args.config, args.id, args.out))
        else:
            result = check_bundle(args.config, args.bundle)
            print(json.dumps({"status": result["status"], "message_id": result["message_id"], "sha256": _sha(result["eml_bytes"])}, ensure_ascii=False))
        return 0
    except (OSError, ValueError, KeyError) as exc:
        print(f"offline_compose_failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
