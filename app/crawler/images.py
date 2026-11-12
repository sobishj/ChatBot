"""Pictures on crawled pages: the text in them (a phone number on a banner, a price list, opening
hours on a poster) is read and added to the page text.

Reading: Tesseract OCR (the same OCR as scanned PDFs, with the configured OCR languages). When the
client's AI model can see pictures (LiteLLM ``supports_vision``), the model transcribes and briefly
describes the picture instead; OCR stays the fallback.

What a picture says is cached in PostgreSQL (``image_text_cache``) by the SHA-256 of its URL for
CACHE_DAYS, so re-crawls don't read it again. Icons, logos, SVG and GIF files are skipped.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.crawler.extract import PageImage
from app.db.models import Client, ImageTextCache

logger = logging.getLogger(__name__)

CACHE_DAYS = 60
MIN_BYTES = 3_000  # smaller files are icons, bullets and spacers
MAX_BYTES = 8_000_000
FETCH_TIMEOUT = 15.0  # seconds
MAX_LINE_CHARS = 800
MAX_PER_PAGE = 10
MAX_PER_CRAWL = 150  # new pictures read per crawl (cached ones don't count)
TIME_PER_PAGE = 60.0  # seconds: stop reading new pictures on a page after this
# Icons, vector graphics and logos: no facts, and a logo's description is long noise in search.
SKIP_RE = re.compile(r"\.(?:svg|ico|gif)(?:[?#]|$)|favicon|sprite|spacer|pixel|blank\.|loader|loading|placeholder|logo|brand-?mark", re.IGNORECASE)
LOGO_ALT_RE = re.compile(r"\blogo\b", re.IGNORECASE)
TYPES = {"image/jpeg": "jpg", "image/jpg": "jpg", "image/png": "png", "image/webp": "webp"}

# (picture bytes, content type) -> what the picture says ("" when nothing could be read).
ImageReader = Callable[[bytes, str], str]
# URL -> (bytes, content type), or None when the picture isn't usable (wrong type, too small/large, error).
ImageFetcher = Callable[[str], "tuple[bytes, str] | None"]


@dataclass
class ImageBudget:
    left: int = MAX_PER_CRAWL  # new pictures that may still be read in this crawl
    read: int = 0  # new pictures read so far


def candidate_images(images: list[PageImage], max_count: int = MAX_PER_PAGE) -> list[PageImage]:
    """Pictures worth reading: http(s), not icons, logos or vector graphics, at most ``max_count``."""
    seen: set[str] = set()
    out: list[PageImage] = []
    for img in images:
        if not img.url.lower().startswith(("http://", "https://")) or SKIP_RE.search(img.url) or LOGO_ALT_RE.search(img.alt) or img.url in seen:
            continue
        seen.add(img.url)
        out.append(img)
        if len(out) >= max_count:
            break
    return out


def plain_text(md: str) -> str:
    """The text as one plain line: no markdown headings, bold or bullets."""
    text = re.sub(r"^\s*#{1,6}\s*", "", md, flags=re.MULTILINE)
    text = re.sub(r"\*\*|__", "", text)
    text = re.sub(r"^\s*[*-]\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"\s+", " ", text).strip()


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


# ----------------------------------------------------------------------------- cache
def cached_text(db: Session, url: str) -> str | None:
    """The remembered text ("" = unreadable), or None when unknown or older than CACHE_DAYS."""
    row = db.get(ImageTextCache, url_hash(url))
    if row is None or row.created_at < datetime.now(UTC) - timedelta(days=CACHE_DAYS):
        return None
    return row.text


def remember(db: Session, url: str, text: str) -> None:
    now = datetime.now(UTC)
    stmt = insert(ImageTextCache).values(url_hash=url_hash(url), text=text, created_at=now)
    db.execute(stmt.on_conflict_do_update(index_elements=["url_hash"], set_={"text": text, "created_at": now}))
    db.commit()


def cleanup_cache(db: Session) -> int:
    """Delete expired picture texts (daily cleanup); returns the number removed."""
    cutoff = datetime.now(UTC) - timedelta(days=CACHE_DAYS)
    result = db.execute(delete(ImageTextCache).where(ImageTextCache.created_at < cutoff))
    db.commit()
    return result.rowcount or 0


# ----------------------------------------------------------------------------- reading
_warned_no_ocr = False


def ocr_picture(data: bytes, _content_type: str = "") -> str:
    """The text in a picture, by Tesseract OCR ("" when there is none or OCR isn't installed)."""
    global _warned_no_ocr
    from app.documents.readers import UnsupportedDocument, ocr_image, ocr_languages

    if shutil.which("tesseract") is None:
        if not _warned_no_ocr:
            _warned_no_ocr = True
            logger.warning("tesseract is not installed: pictures on web pages are not read")
        return ""
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as img:
            img.load()
            picture = img.convert("L")
        # Banner text is often small: Tesseract reads it better at a larger size.
        if max(picture.size) < 1200:
            picture = picture.resize((picture.width * 2, picture.height * 2))
        return ocr_image(picture, ocr_languages())
    except (UnsupportedDocument, OSError, ValueError) as exc:
        logger.info("Couldn't OCR a picture: %s", exc)
        return ""
    except Exception as exc:  # noqa: BLE001 - e.g. a tesseract timeout on a huge picture
        logger.info("Couldn't OCR a picture: %s", exc.__class__.__name__)
        return ""


def default_reader(db: Session, client: Client) -> ImageReader:
    """The client's (or default) AI model when it can see pictures, with OCR as fallback; else OCR."""
    from app.llm.client import LLMError, read_image, supports_vision
    from app.services.ai_models import resolve_chain

    chain = resolve_chain(db, client)
    cfg = chain[0] if chain else None
    if cfg is None or not supports_vision(cfg):
        return ocr_picture

    def read(data: bytes, content_type: str) -> str:
        try:
            text = read_image(cfg, data, content_type)
        except LLMError as exc:
            logger.info("Vision model couldn't read a picture (%s); using OCR", exc)
            text = ""
        return text or ocr_picture(data, content_type)

    return read


def read_one(url: str, fetch: ImageFetcher, reader: ImageReader) -> str:
    """What the picture says as one clean line; "" when it can't be read."""
    got = fetch(url)
    if got is None:
        return ""
    data, content_type = got
    try:
        text = reader(data, content_type)
    except Exception as exc:  # noqa: BLE001 - a picture never fails the crawl
        logger.info("Couldn't read picture %s: %s", url, exc.__class__.__name__)
        return ""
    return plain_text(text or "")[:MAX_LINE_CHARS]


def describe_images(
    db: Session,
    images: list[PageImage],
    budget: ImageBudget,
    fetch: ImageFetcher,
    reader: ImageReader,
    max_per_page: int = MAX_PER_PAGE,
    time_limit: float = TIME_PER_PAGE,
) -> str:
    """The "Pictures on this page" section for the page text, or "" when there is nothing to add.

    Pictures that weren't read (budget or time used up) still contribute their alt text.
    """
    deadline = time.monotonic() + time_limit
    lines: list[str] = []
    for img in candidate_images(images, max_per_page):
        text = cached_text(db, img.url)
        if text is None and budget.left > 0 and time.monotonic() < deadline:
            text = read_one(img.url, fetch, reader)
            budget.left -= 1
            budget.read += 1
            remember(db, img.url, text)
        line = ": ".join(part for part in (img.alt, text or "") if part)
        if line and line not in lines:
            lines.append(line)
    return "Pictures on this page:\n" + "\n".join(f"- {line}" for line in lines) if lines else ""
