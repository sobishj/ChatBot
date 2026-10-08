"""Extract plain text from supported document types."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".xlsx", ".txt", ".md"}
MAX_TEXT_CHARS = 2_000_000


class UnsupportedDocument(ValueError):
    pass


class EmptyDocument(ValueError):
    pass


def is_supported(path: Path | str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def read_pdf(path: Path) -> str:
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception as exc:  # noqa: BLE001
            raise UnsupportedDocument("The PDF is password-protected.") from exc
    parts = []
    for number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()
        if text:
            parts.append(f"[Page {number}]\n{text}")
    return "\n\n".join(parts)


def read_docx(path: Path) -> str:
    import docx

    document = docx.Document(str(path))
    parts: list[str] = [p.text for p in document.paragraphs if p.text.strip()]
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells]
            # Merged cells repeat their text; keep each value once per row.
            unique = list(dict.fromkeys(c for c in cells if c))
            if unique:
                parts.append(" | ".join(unique))
    return "\n".join(parts)


def read_xlsx(path: Path) -> str:
    """Each row becomes 'Header: value | Header: value' so facts stay self-describing."""
    from openpyxl import load_workbook

    workbook = load_workbook(str(path), read_only=True, data_only=True)
    parts: list[str] = []
    try:
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(values_only=True)
            header: list[str] | None = None
            lines: list[str] = []
            for row in rows:
                values = ["" if v is None else str(v).strip() for v in row]
                if not any(values):
                    continue
                if header is None:
                    header = values
                    continue
                pairs = [f"{header[i] or f'Column {i + 1}'}: {v}" if i < len(header) else v for i, v in enumerate(values) if v]
                lines.append(" | ".join(pairs))
            if header is not None and not lines:  # single-row sheet
                lines.append(" | ".join(v for v in header if v))
            if lines:
                parts.append(f"[Sheet: {sheet.title}]\n" + "\n".join(lines))
    finally:
        workbook.close()
    return "\n\n".join(parts)


def read_text(path: Path) -> str:
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):  # UTF-16 only when it has a byte-order mark
        return raw.decode("utf-16")
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


READERS = {".pdf": read_pdf, ".docx": read_docx, ".xlsx": read_xlsx, ".txt": read_text, ".md": read_text}


def read_document(path: Path) -> str:
    """Return the document's text. Raises UnsupportedDocument / EmptyDocument with a user-facing message."""
    reader = READERS.get(path.suffix.lower())
    if reader is None:
        raise UnsupportedDocument(f"Unsupported file type: {path.suffix or 'none'}")
    try:
        text = reader(path)
    except (UnsupportedDocument, EmptyDocument):
        raise
    except Exception as exc:  # noqa: BLE001 - corrupt files raise all kinds of errors
        raise UnsupportedDocument(f"Could not read the file ({exc.__class__.__name__}). Is it corrupt?") from exc
    text = text.strip()
    if not text:
        hint = " It may be a scanned PDF (images only); OCR is not supported yet." if path.suffix.lower() == ".pdf" else ""
        raise EmptyDocument("No text found in the document." + hint)
    return text[:MAX_TEXT_CHARS]
