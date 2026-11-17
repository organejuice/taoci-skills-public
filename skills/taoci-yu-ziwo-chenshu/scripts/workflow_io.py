"""Read-only inputs for the configurable doctoral outreach workflow.

The workbook and baseline templates are source assets, never output targets.
This module performs no writes and never changes contact or reply statuses.
"""

from __future__ import annotations

import colorsys
import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable
from zipfile import ZipFile

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
NS = {"w": W, "a": A}
WP, WR = f"{{{W}}}p", f"{{{W}}}r"
PATH_KEYS = {
    "project_root", "roster", "output_dir", "letter_template",
    "statement_pdf", "statement_docx", "work_dir",
}
STORY = re.compile(r"word/(?:document|header[^/]*|footer[^/]*|footnotes|endnotes)\.xml\Z")


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load project settings without creating directories or installing packages."""
    target = Path(path) if path else Path(__file__).resolve().parents[1] / "references" / "project.json"
    target = target.expanduser().resolve()
    with target.open("r", encoding="utf-8-sig") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("Project configuration must be a JSON object")
    missing = sorted(PATH_KEYS.difference(config))
    if missing:
        raise ValueError(f"Missing project path settings: {', '.join(missing)}")
    config["_config_dir"] = str(target.parent)
    return config


def resolve_path(config: dict[str, Any], key: str) -> Path:
    """Resolve absolute settings, or paths relative to the project root."""
    if key not in PATH_KEYS:
        raise KeyError(f"Unknown project path key: {key}")
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Project path {key!r} must be a nonempty string")
    target = Path(os.path.expandvars(value)).expanduser()
    if not target.is_absolute():
        if key == "project_root":
            target = Path(config.get("_config_dir", Path.cwd())) / target
        else:
            target = resolve_path(config, "project_root") / target
    return target.resolve()


def _cell(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return value.strip() if isinstance(value, str) else value


def _label(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _key(*values: Any) -> tuple[str, ...]:
    return tuple(re.sub(r"\s+", "", _label(v)) for v in values)


def _sheet_records(workbook: Any, name: str, required: Iterable[str]) -> list[tuple[int, dict[str, Any]]]:
    if name not in workbook.sheetnames:
        raise ValueError(f"Required worksheet is missing: {name}")
    rows = iter(workbook[name].iter_rows(values_only=True))
    try:
        header = [_label(v) for v in next(rows)]
    except StopIteration:
        raise ValueError(f"Worksheet is empty: {name}") from None
    missing = set(required).difference(header)
    if missing:
        raise ValueError(f"Worksheet {name} is missing columns: {', '.join(sorted(missing))}")
    populated = [h for h in header if h]
    if len(populated) != len(set(populated)):
        raise ValueError(f"Worksheet {name} has duplicate column labels")
    result = []
    for row_number, values in enumerate(rows, 2):
        if not any(v is not None and _label(v) for v in values):
            continue
        result.append((row_number, {h: _cell(v) for h, v in zip(header, values) if h}))
    return result


def load_advisors(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Join verified advisors to contact history by school, department and name.

    An unmatched contact row is explicit. It is never interpreted as not sent.
    Duplicate identities or IDs are rejected rather than silently merged.
    """
    import openpyxl

    workbook = openpyxl.load_workbook(resolve_path(config, "roster"), read_only=True, data_only=True)
    try:
        contact_rows = _sheet_records(
            workbook, "导师联系表", ("学院", "院系", "导师", "邮箱", "研究方向", "是否发送邮件", "回复状态")
        )
        verified_rows = _sheet_records(
            workbook, "方向与招生核验", ("编号", "学校", "院系", "导师", "官方及原始来源")
        )
        contacts: dict[tuple[str, ...], list[tuple[int, dict[str, Any]]]] = defaultdict(list)
        for row_number, record in contact_rows:
            contacts[_key(record.get("学院"), record.get("院系"), record.get("导师"))].append((row_number, record))
        result = []
        seen_numbers: set[int] = set()
        seen_identities: set[tuple[str, ...]] = set()
        for row_number, record in verified_rows:
            advisor_id = _label(record.get("编号")).upper()
            if not re.fullmatch(r"M[0-9]+", advisor_id) or int(advisor_id[1:]) <= 0:
                raise ValueError(f"Invalid advisor ID at 方向与招生核验 row {row_number}: {advisor_id!r}")
            number = int(advisor_id[1:])
            if number in seen_numbers:
                raise ValueError(f"Duplicate numeric advisor ID: {advisor_id}")
            seen_numbers.add(number)
            school, department, name = (_label(record.get(k)) for k in ("学校", "院系", "导师"))
            if not school or not name:
                raise ValueError(f"Missing school or name at 方向与招生核验 row {row_number}")
            identity = _key(school, department, name)
            if identity in seen_identities:
                raise ValueError(f"Duplicate advisor identity: {school} / {department} / {name}")
            seen_identities.add(identity)
            matching = contacts.get(identity, [])
            if len(matching) > 1:
                raise ValueError(f"Ambiguous contact history for {advisor_id}: {school} / {department} / {name}")
            contact_row, contact = matching[0] if matching else (None, {})
            result.append({
                "id": advisor_id,
                "number": number,
                "school": school,
                "department": department,
                "name": name,
                "email": _label(contact.get("邮箱")),
                "direction": _label(contact.get("研究方向")),
                "official_sources": _label(record.get("官方及原始来源")),
                "contact_status": contact.get("是否发送邮件"),
                "response_status": contact.get("回复状态"),
                "contact_match_status": "exact" if matching else "unmatched",
                "contact_row": contact_row,
                "roster_row": row_number,
                "verification": record,
            })
        return result
    finally:
        workbook.close()


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (str, int)):
        return [value]
    return list(value)


def _number(value: Any) -> int:
    token = str(value).strip()
    if token[:1].upper() == "M":
        token = token[1:]
    if not re.fullmatch(r"[0-9]+", token) or int(token) <= 0:
        raise ValueError(f"Expected a positive advisor number or M ID: {value!r}")
    return int(token)


def select_advisors(
    advisors: list[dict[str, Any]], ids: Any = None, names: Any = None,
    school: str | None = None, start: Any = None, end: Any = None,
) -> list[dict[str, Any]]:
    """Select an explicit scope; no arguments never means all 272 advisors.

    Selectors combine by intersection. IDs may include one literal ``all``;
    names require a unique identity after an optional exact school filter.
    Workbook order and original output numbers are retained.
    """
    id_values, name_values = _list(ids), _list(names)
    if not id_values and not name_values and not school and start is None and end is None:
        raise ValueError("Specify advisor IDs, names, school or a numeric range; use ids=['all'] for the whole table")
    selected = list(advisors)
    if school:
        selected = [a for a in selected if _key(a["school"]) == _key(school)]
        if not selected:
            raise ValueError(f"No advisors found for school: {school}")
    if id_values:
        all_tokens = [v for v in id_values if str(v).strip().lower() == "all"]
        if all_tokens and len(id_values) != 1:
            raise ValueError("The all selector cannot be combined with individual IDs")
        if not all_tokens:
            wanted = {_number(v) for v in id_values}
            known = {a["number"] for a in advisors}
            unknown = wanted.difference(known)
            if unknown:
                raise ValueError(f"Unknown advisor numbers: {', '.join(map(str, sorted(unknown)))}")
            selected = [a for a in selected if a["number"] in wanted]
    if name_values:
        wanted_numbers = set()
        for name in name_values:
            matches = [a for a in selected if _key(a["name"]) == _key(name)]
            if not matches:
                raise ValueError(f"No advisor found in the selected scope with name: {name}")
            if len(matches) > 1:
                choices = "; ".join(f"{a['id']} {a['school']} / {a['department']}" for a in matches)
                raise ValueError(f"Ambiguous advisor name {name!r}; specify an M ID: {choices}")
            wanted_numbers.add(matches[0]["number"])
        selected = [a for a in selected if a["number"] in wanted_numbers]
    low = _number(start) if start is not None else 1
    high = _number(end) if end is not None else max((a["number"] for a in advisors), default=0)
    if low > high:
        raise ValueError("Advisor range start must not exceed end")
    if start is not None or end is not None:
        selected = [a for a in selected if low <= a["number"] <= high]
    if not selected:
        raise ValueError("No advisors match the combined selection")
    return selected


def _parse(data: bytes) -> etree._Element:
    return etree.fromstring(data, parser=etree.XMLParser(resolve_entities=False, no_network=True))


def docx_story_parts(path: str | Path) -> dict[str, etree._Element]:
    """Read document/header/footer/note stories, including nested text boxes."""
    with ZipFile(path) as archive:
        return {
            name: _parse(archive.read(name))
            for name in archive.namelist() if STORY.fullmatch(name)
        }


def _own_paragraph(node: etree._Element, paragraph: etree._Element) -> bool:
    return next(node.iterancestors(WP), None) is paragraph


def paragraph_text(paragraph: etree._Element) -> str:
    """Read visible run text without counting nested textbox paragraphs twice."""
    pieces = []
    for node in paragraph.iter():
        if not _own_paragraph(node, paragraph):
            continue
        if node.tag == f"{{{W}}}t":
            pieces.append(node.text or "")
        elif node.tag == f"{{{W}}}tab":
            pieces.append("\t")
        elif node.tag in (f"{{{W}}}br", f"{{{W}}}cr"):
            pieces.append("\n")
    return "".join(pieces)


def _style_color(styles: etree._Element | None, style_id: str | None) -> etree._Element | None:
    if styles is None or not style_id:
        return None
    indexed = {s.get(f"{{{W}}}styleId"): s for s in styles.findall("w:style", NS)}
    visited = set()
    while style_id and style_id not in visited:
        visited.add(style_id)
        style = indexed.get(style_id)
        if style is None:
            break
        color = style.find("w:rPr/w:color", NS)
        if color is not None:
            return color
        base = style.find("w:basedOn", NS)
        style_id = base.get(f"{{{W}}}val") if base is not None else None
    return None


def _theme_rgb(color: etree._Element, theme: etree._Element | None) -> str | None:
    if theme is None:
        return None
    token = color.get(f"{{{W}}}themeColor")
    aliases = {"dark1": "dk1", "light1": "lt1", "dark2": "dk2", "light2": "lt2", "hyperlink": "hlink", "followedHyperlink": "folHlink"}
    slot = aliases.get(token, token)
    if not slot:
        return None
    scheme = theme.find("a:themeElements/a:clrScheme", NS)
    if scheme is None:
        return None
    entry = scheme.find(f"{{{A}}}{slot}")
    if entry is None or not len(entry):
        return None
    child = entry[0]
    rgb = child.get("val") if child.tag == f"{{{A}}}srgbClr" else child.get("lastClr")
    if not rgb or not re.fullmatch(r"[0-9a-fA-F]{6}", rgb):
        return None
    channels = [int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    hue, lightness, saturation = colorsys.rgb_to_hls(*channels)
    for attribute in ("themeShade", "themeTint"):
        value = color.get(f"{{{W}}}{attribute}")
        if value and re.fullmatch(r"[0-9a-fA-F]{2}", value):
            fraction = int(value, 16) / 255
            lightness = lightness * fraction if attribute == "themeShade" else lightness + (1 - lightness) * fraction
    return "".join(f"{round(c * 255):02X}" for c in colorsys.hls_to_rgb(hue, lightness, saturation))


def effective_run_color(
    run: etree._Element, styles: etree._Element | None = None, theme: etree._Element | None = None,
) -> str | None:
    """Resolve direct color, character style, paragraph style and doc defaults."""
    color = run.find("w:rPr/w:color", NS)
    if color is None:
        run_style = run.find("w:rPr/w:rStyle", NS)
        color = _style_color(styles, run_style.get(f"{{{W}}}val") if run_style is not None else None)
    if color is None:
        paragraph = next(run.iterancestors(WP), None)
        p_style = paragraph.find("w:pPr/w:pStyle", NS) if paragraph is not None else None
        p_style_id = p_style.get(f"{{{W}}}val") if p_style is not None else None
        if not p_style_id and styles is not None:
            default = styles.xpath("./w:style[@w:type='paragraph' and (@w:default='1' or @w:default='true')]", namespaces=NS)
            p_style_id = default[0].get(f"{{{W}}}styleId") if default else None
        color = _style_color(styles, p_style_id)
    if color is None and styles is not None:
        color = styles.find("w:docDefaults/w:rPrDefault/w:rPr/w:color", NS)
    if color is None:
        return None
    if color.get(f"{{{W}}}themeColor"):
        themed = _theme_rgb(color, theme)
        if themed:
            return themed
    rgb = color.get(f"{{{W}}}val", "").upper()
    return rgb if re.fullmatch(r"[0-9A-F]{6}", rgb) else None


def _red_rgb(rgb: str | None) -> bool:
    if not rgb:
        return False
    red, green, blue = (int(rgb[i:i + 2], 16) for i in (0, 2, 4))
    # Include dark reds and near-red template variants; reject neutral colors.
    return red >= 96 and red >= 1.4 * green and red >= 1.4 * blue and red - max(green, blue) >= 48


def is_red_run(
    run: etree._Element, styles: etree._Element | None = None, theme: etree._Element | None = None,
) -> bool:
    return _red_rgb(effective_run_color(run, styles, theme))


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _docx_report(path: Path) -> dict[str, Any]:
    stories = docx_story_parts(path)
    with ZipFile(path) as archive:
        styles = _parse(archive.read("word/styles.xml")) if "word/styles.xml" in archive.namelist() else None
        theme = _parse(archive.read("word/theme/theme1.xml")) if "word/theme/theme1.xml" in archive.namelist() else None
        image_count = len([n for n in archive.namelist() if n.startswith("word/media/") and not n.endswith("/")])
    regions, scientific_interest, main_text = [], [], []
    for part, root in stories.items():
        for paragraph_index, paragraph in enumerate(root.iter(WP)):
            text = paragraph_text(paragraph)
            if part == "word/document.xml":
                main_text.append(text)
            if text.lstrip().startswith("科研兴趣"):
                scientific_interest.append({"part": part, "paragraph_index": paragraph_index, "text": text})
            own_runs = [r for r in paragraph.iter(WR) if _own_paragraph(r, paragraph)]
            cursor = 0
            for run_index, run in enumerate(own_runs):
                run_text = "".join(n.text or "" for n in run.iter(f"{{{W}}}t") if _own_paragraph(n, paragraph))
                position = text.find(run_text, cursor) if run_text else cursor
                position = max(position, cursor)
                if is_red_run(run, styles, theme) and run_text:
                    regions.append({
                        "part": part, "paragraph_index": paragraph_index, "run_index": run_index,
                        "text": run_text, "color": effective_run_color(run, styles, theme),
                        "context": text[max(0, position - 35):position + len(run_text) + 45],
                    })
                cursor = position + len(run_text)
    return {"red_regions": regions, "scientific_interest": scientific_interest, "main_text": "\n".join(main_text), "image_count": image_count}


def template_report(config: dict[str, Any]) -> dict[str, Any]:
    """Inspect immutable templates; text equality does not certify layout fidelity."""
    import pdfplumber

    source_keys = ("roster", "letter_template", "statement_docx", "statement_pdf")
    files = {}
    for key in source_keys:
        source = resolve_path(config, key)
        if not source.is_file():
            raise FileNotFoundError(f"Required input {key}: {source}")
        files[key] = {"path": str(source), "sha256": _hash(source), "bytes": source.stat().st_size}
    letter = _docx_report(resolve_path(config, "letter_template"))
    statement = _docx_report(resolve_path(config, "statement_docx"))
    with pdfplumber.open(resolve_path(config, "statement_pdf")) as pdf:
        pdf_text = "\n".join(page.extract_text() or "" for page in pdf.pages)
        page_count = len(pdf.pages)
        page_sizes = [[float(page.width), float(page.height)] for page in pdf.pages]
    normalize = lambda value: re.sub(r"\s+", "", value)
    doc_text, pdf_text = normalize(statement["main_text"]), normalize(pdf_text)
    equal = bool(doc_text) and doc_text == pdf_text
    return {
        "files": files,
        "red_regions": {"letter": letter["red_regions"], "statement": statement["red_regions"]},
        "scientific_interest": statement["scientific_interest"],
        "statement_docx_pdf_text_equal": equal,
        "statement_text_similarity": SequenceMatcher(None, doc_text, pdf_text, autojunk=False).ratio(),
        "statement_pdf_pages": page_count,
        "statement_pdf_page_sizes": page_sizes,
        "statement_docx_image_count": statement["image_count"],
        "layout_verified": False,
        "layout_note": "Text equality and image counts do not verify image placement, fonts, page breaks or visual fidelity; render and inspect separately.",
    }
