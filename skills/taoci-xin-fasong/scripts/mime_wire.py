"""Independent wire checks for this project's deliberately narrow MIME format.

This is not a general mail parser. In particular, boundary extensions, preambles,
epilogues, non-base64 leaves and other legitimate MIME formats are out of scope.
RFC 2231 UTF-8 filename parameters (including continuations) remain supported.
No email package parser is used here; callers must also perform semantic checks.
"""

from __future__ import annotations

import base64
from datetime import datetime
import hashlib
import html
import re
from urllib.parse import unquote_to_bytes


class WireError(ValueError):
    """The bytes are not this project's safe, source-bound wire format."""


CRLF = b"\r\n"
TOKEN = r"[!#$%&'*+.^_`|~0-9A-Za-z-]+"
ADDRESS = r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?"


def _fail(message: str) -> None:
    raise WireError(message)


def _headers(entity: bytes) -> tuple[dict[str, str], bytes]:
    head, separator, body = entity.partition(CRLF + CRLF)
    if not separator or not head:
        _fail("Missing CRLF MIME header/body separator")
    headers: dict[str, str] = {}
    current = None
    for line in head.split(CRLF):
        if any(c < 32 and c != 9 or c > 126 for c in line):
            _fail("Headers must be printable ASCII with legal folding")
        if line.startswith((b" ", b"\t")):
            if current is None or not line.strip():
                _fail("Orphan or empty folded header")
            headers[current] += " " + line.decode("ascii").strip()
            continue
        match = re.fullmatch(rb"([A-Za-z0-9-]+):(?: ([\x20-\x7e]*))?", line)
        if not match:
            _fail("Malformed MIME header line")
        current = match[1].decode("ascii").lower()
        if current in headers:
            _fail("Duplicate MIME header: " + current)
        headers[current] = (match[2] or b"").decode("ascii").strip()
    return {name: value.strip() for name, value in headers.items()}, body


def _parameters(value: str) -> tuple[str, dict[str, tuple[str, bool]]]:
    """Parse only ASCII MIME tokens/quoted strings, retaining quotedness."""
    main, _, rest = value.partition(";")
    if not re.fullmatch(TOKEN + r"(?:/" + TOKEN + r")?", main):
        _fail("Invalid MIME media type or disposition")
    params = {}
    while rest:
        match = re.match(r"\s*(" + TOKEN + r")\s*=\s*(?:\"((?:[^\"\\]|\\[\x20-\x7e])*)\"|(" + TOKEN + r"))\s*(?:;|$)", rest)
        if not match:
            _fail("Invalid MIME parameter syntax")
        name = match[1].lower()
        if name in params:
            _fail("Duplicate MIME parameter: " + name)
        quoted = match[2] is not None
        decoded = re.sub(r"\\(.)", r"\1", match[2]) if quoted else match[3]
        # Escapes in boundary values can differ between client parsers.
        if name == "boundary" and (not quoted or "\\" in match[2]):
            _fail("Boundary must be an ordinary unescaped quoted parameter")
        params[name] = (decoded, quoted)
        rest = rest[match.end():]
    if value.endswith(";"):
        _fail("Empty trailing MIME parameter")
    return main.lower(), params


def _keys(headers: dict[str, str], expected: set[str]) -> None:
    if set(headers) != expected:
        _fail("Unexpected or missing MIME headers: " + ", ".join(sorted(set(headers) ^ expected)))


def _boundary(headers: dict[str, str], subtype: str, used: list[str]) -> str:
    media, params = _parameters(headers["content-type"])
    if any(name.startswith("boundary*") for name in params):
        _fail("Extended boundary parameters are forbidden in this project")
    if media != "multipart/" + subtype or set(params) != {"boundary"}:
        _fail("Expected multipart/" + subtype + " with one ordinary boundary")
    boundary = params["boundary"][0]
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,70}", boundary):
        _fail("Boundary must contain 1..70 safe ASCII characters")
    if boundary in used or any(boundary.startswith(old) or old.startswith(boundary) for old in used):
        _fail("Multipart boundaries must be distinct and non-overlapping")
    used.append(boundary)
    return boundary


def _multipart(body: bytes, boundary: str) -> list[bytes]:
    marker = b"--" + boundary.encode("ascii")
    lines = body.split(CRLF)
    if not lines or lines[0] != marker:
        _fail("Multipart has a missing opening boundary or a preamble")
    parts, start, closed = [], 0, False
    for index, line in enumerate(lines):
        if not line.startswith(marker):
            continue
        if line not in (marker, marker + b"--"):
            _fail("Malformed boundary delimiter line")
        if closed:
            _fail("Boundary after closing delimiter")
        if index:
            part = CRLF.join(lines[start + 1:index])
            if not part:
                _fail("Empty MIME part")
            parts.append(part)
        if line == marker + b"--":
            if lines[index + 1:] != [b""]:
                _fail("Multipart epilogue or missing final CRLF")
            closed = True
        start = index
    if not closed:
        _fail("Missing closing boundary delimiter")
    return parts


def _decode(headers: dict[str, str], body: bytes) -> bytes:
    if headers["content-transfer-encoding"] != "base64":
        _fail("Every leaf must use base64 Content-Transfer-Encoding")
    lines = body.split(CRLF)
    if not lines or lines[-1] != b"" or not lines[:-1]:
        _fail("Base64 body must have complete CRLF lines")
    lines.pop()
    if any(not re.fullmatch(rb"[A-Za-z0-9+/]+={0,2}", line) or len(line) > 76 or len(line) % 4 for line in lines):
        _fail("Invalid base64 wire line")
    if any(len(line) != 76 or b"=" in line for line in lines[:-1]):
        _fail("Noncanonical base64 line wrapping or padding")
    encoded = b"".join(lines)
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise WireError("Invalid base64 payload") from exc
    if not decoded or base64.b64encode(decoded) != encoded:
        _fail("Empty or noncanonical base64 payload")
    return decoded


def _filename(value: str) -> str:
    disposition, params = _parameters(value)
    if disposition != "attachment":
        _fail("PDF must have attachment Content-Disposition")
    names = set(params)
    if names == {"filename"}:
        filename = params["filename"][0]
        if filename.startswith("=?"):
            _fail("RFC 2047 is not a filename parameter encoding")
    else:
        if names == {"filename*"}:
            encoded = params["filename*"][0]
        else:
            if not names or any(not re.fullmatch(r"filename\*(?:0|[1-9][0-9]*)\*", name) for name in names):
                _fail("Unsupported or mixed PDF filename parameters")
            ordered = sorted(names, key=lambda name: int(name[9:-1]))
            if ordered != [f"filename*{index}*" for index in range(len(ordered))]:
                _fail("Noncontiguous RFC 2231 filename continuations")
            encoded = "".join(params[name][0] for name in ordered)
        if not encoded.lower().startswith("utf-8''"):
            _fail("PDF filename must use RFC 2231 UTF-8 without language")
        encoded = encoded[7:]
        if re.search(r"%(?![0-9A-Fa-f]{2})", encoded):
            _fail("Invalid percent escape in PDF filename")
        try:
            filename = unquote_to_bytes(encoded).decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise WireError("Invalid UTF-8 PDF filename") from exc
    if not filename or not filename.endswith(".pdf") or any(c in filename for c in "\\/\r\n\x00") or any(ord(c) < 32 for c in filename):
        _fail("Unsafe or non-PDF attachment filename")
    return filename


def _payload(data: bytes) -> dict:
    return {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def validate_wire(raw: bytes) -> dict:
    """Reject unsafe project MIME bytes, returning decoded payload sizes/hashes.

    The caller still binds headers, text, filename and PDF bytes to checked
    source files. This function independently checks raw framing and decoding.
    """
    if not isinstance(raw, bytes) or not raw:
        _fail("Wire input must be nonempty bytes")
    if re.search(rb"(?<!\r)\n|\r(?!\n)", raw) or not raw.endswith(CRLF):
        _fail("Wire lines must use complete CRLF endings")
    if any(len(line) > 998 for line in raw.split(CRLF)):
        _fail("Wire line exceeds 998 octets")
    if b"\x00" in raw:
        _fail("NUL is forbidden on the wire")
    top, body = _headers(raw)
    _keys(top, {"from", "to", "subject", "message-id", "date", "mime-version", "content-type"})
    for key in ("from", "to"):
        if not re.fullmatch(ADDRESS, top[key]):
            _fail("Expected one bare ASCII address in " + key)
    if not top["subject"] or not re.fullmatch(r"<[^<>\s@]+@[^<>\s@]+>", top["message-id"]):
        _fail("Missing subject or invalid Message-ID")
    try:
        stamp = datetime.strptime(top["date"], "%a, %d %b %Y %H:%M:%S %z")
    except ValueError as exc:
        raise WireError("Invalid project Date header") from exc
    if stamp.strftime("%a, %d %b %Y %H:%M:%S %z") != top["date"]:
        _fail("Noncanonical Date header")
    if top["mime-version"] != "1.0":
        _fail("Expected MIME-Version 1.0")
    used: list[str] = []
    outer = _boundary(top, "mixed", used)
    parts = _multipart(body, outer)
    if len(parts) != 2:
        _fail("Mixed must contain exactly alternative body and PDF")
    alternative, alt_body = _headers(parts[0])
    _keys(alternative, {"content-type"})
    inner = _boundary(alternative, "alternative", used)
    alternatives = _multipart(alt_body, inner)
    if len(alternatives) != 2:
        _fail("Alternative must contain exactly plain then HTML")
    payloads = {}
    for index, kind in enumerate(("plain", "html")):
        headers, content = _headers(alternatives[index])
        expected = {"content-type", "content-transfer-encoding"} | ({"mime-version"} if kind == "html" else set())
        _keys(headers, expected)
        if "mime-version" in headers and headers["mime-version"] != "1.0":
            _fail("Invalid leaf MIME-Version")
        media, params = _parameters(headers["content-type"])
        if media != "text/" + kind or set(params) != {"charset"} or params["charset"][0].lower() != "utf-8":
            _fail("Expected UTF-8 text/" + kind)
        decoded = _decode(headers, content)
        try:
            text = decoded.decode("utf-8", errors="strict")
        except UnicodeError as exc:
            raise WireError("Invalid UTF-8 body") from exc
        visible = text
        if kind == "html":
            visible = re.sub(r"(?is)<!--.*?-->|<(head|script|style)\b[^>]*>.*?</\1\s*>", "", text)
            visible = html.unescape(re.sub(r"(?s)<[^>]*>", "", visible))
            if not re.search(r"(?i)<html\b", text) or not re.search(r"(?i)<body\b", text):
                _fail("HTML body must be a complete project document")
        if not visible.strip() or "\x00" in text:
            _fail("Empty or invalid " + kind + " body")
        payloads[kind] = _payload(decoded)
    pdf_headers, pdf_body = _headers(parts[1])
    _keys(pdf_headers, {"content-type", "content-transfer-encoding", "content-disposition", "mime-version"})
    if pdf_headers["content-type"] != "application/pdf" or pdf_headers["mime-version"] != "1.0":
        _fail("Expected one application/pdf attachment")
    filename = _filename(pdf_headers["content-disposition"])
    pdf = _decode(pdf_headers, pdf_body)
    if not pdf.startswith(b"%PDF-") or not pdf.rstrip().endswith(b"%%EOF"):
        _fail("PDF payload is not a complete PDF file")
    payloads["pdf"] = _payload(pdf)
    return {"schema_version": 1, "boundaries": {"mixed": outer, "alternative": inner},
            "boundary_lengths": {"mixed": len(outer), "alternative": len(inner)},
            "payloads": payloads, "filename": filename}
