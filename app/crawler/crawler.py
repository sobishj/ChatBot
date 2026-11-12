"""Polite same-site crawler.

1. Read robots.txt (respect Disallow and Crawl-delay; collect Sitemap: entries).
2. Try sitemaps (robots.txt entries, /sitemap.xml, /sitemap_index.xml, /wp-sitemap.xml).
   If they list pages, crawl those. Otherwise follow internal links from the start URL.
3. Fetch pages one by one with a delay, extract clean text, report each page via a callback.

Reading "everything" (crawl settings, all on by default):

* ``render_js``: pages that arrive nearly empty (JavaScript-built sites) are opened in a local
  headless browser and read as visitors see them (:mod:`app.crawler.render`).
* ``read_documents``: PDF, Word and Excel files linked from the site are read with the document
  readers (OCR for scanned PDFs) and indexed like pages, with their URL as the source.
* The start page keeps its site header and footer, where contact details often are.
* Pictures are reported with each page; :mod:`app.crawler.service` reads them (``read_images``).

The crawler knows nothing about the database; :mod:`app.crawler.service` stores results.
"""

from __future__ import annotations

import gzip
import logging
import re
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urldefrag, urlencode, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from defusedxml import ElementTree as SafeET

from app import __version__
from app.crawler.extract import Extracted, PageImage, extract
from app.crawler.render import BrowserUnavailable, PageRenderer, needs_rendering

logger = logging.getLogger(__name__)

USER_AGENT = f"WebsiteAssistantBot/{__version__} (+website assistant crawler)"
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")
MAX_PAGE_BYTES = 5_000_000
MAX_DOCUMENT_BYTES = 20_000_000
# Files linked from the site that are read like uploaded documents (crawl setting read_documents).
DOC_EXTENSIONS = (".pdf", ".docx", ".xlsx")
DOC_TYPES = {
    "application/pdf": ".pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}
MAX_SITEMAPS = 200
SKIP_EXTENSIONS = (
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".css", ".js", ".json", ".xml", ".zip",
    ".rar", ".gz", ".mp4", ".mp3", ".avi", ".mov", ".webm", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".woff", ".woff2", ".ttf", ".eot", ".txt", ".csv",
)
TRACKING_PARAMS = ("utm_", "fbclid", "gclid", "mc_cid", "mc_eid", "_ga")


@dataclass
class CrawlConfig:
    start_url: str
    max_pages: int = 500
    delay_seconds: float = 1.0
    include_patterns: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)
    extraction: str = "auto"  # auto | main_content
    render_js: str = "auto"  # auto | always | never: open JavaScript-built pages in a headless browser
    read_documents: bool = True  # read PDF, Word and Excel files linked from the site


@dataclass
class FetchedPage:
    url: str
    title: str
    text: str
    images: list[PageImage] = field(default_factory=list)


@dataclass
class CrawlResult:
    pages: int = 0  # successfully extracted pages
    failed: int = 0
    seen_urls: set[str] = field(default_factory=set)  # every URL fetched successfully
    gone_urls: set[str] = field(default_factory=set)  # 404/410: page removed from the site
    hit_limit: bool = False
    used_sitemap: bool = False
    errors: list[str] = field(default_factory=list)
    rendered: int = 0  # pages read with JavaScript (headless browser)
    documents: int = 0  # linked PDF/Word/Excel files read


class RobotsBlocked(Exception):
    """robots.txt could not be read (server error) — crawling would be impolite."""


class DocumentError(Exception):
    """A linked document couldn't be read; the message is shown in the crawl's errors."""


def is_document_url(url: str) -> bool:
    """A linked PDF/Word/Excel file that the crawl reads (when read_documents is on)."""
    return urlparse(url).path.lower().endswith(DOC_EXTENSIONS)


def document_title(url: str, ext: str) -> str:
    """Title from the file name: no extension, ``_``/``-`` turned into spaces."""
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or f"document{ext}"
    stem = name[: -len(ext)] if name.lower().endswith(ext) else name
    return re.sub(r"[_-]+", " ", stem).strip() or name


def site_key(host: str) -> str:
    """Host without a leading 'www.' — www and bare domain count as the same site."""
    return host.lower().removeprefix("www.")


def normalize_url(url: str) -> str:
    """Drop fragments and tracking parameters, lower-case the host, keep a stable form."""
    url, _ = urldefrag(url.strip())
    p = urlparse(url)
    query = urlencode([(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith(TRACKING_PARAMS)])
    path = p.path or "/"
    return urlunparse((p.scheme.lower(), (p.netloc or "").lower(), path, "", query, ""))


def matches_patterns(url: str, include: list[str], exclude: list[str]) -> bool:
    """Wildcard patterns match the URL path (+query), e.g. ``/blog/*`` or ``*?print=*``."""
    p = urlparse(url)
    target = p.path + (f"?{p.query}" if p.query else "")
    if include and not any(fnmatch(target, pat) or fnmatch(url, pat) for pat in include):
        return False
    return not any(fnmatch(target, pat) or fnmatch(url, pat) for pat in exclude)


class Crawler:
    def __init__(
        self,
        config: CrawlConfig,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        renderer: PageRenderer | None = None,
    ) -> None:
        self.config = config
        start = urlparse(config.start_url)
        if start.scheme not in ("http", "https") or not start.hostname:
            raise ValueError(f"Invalid start URL: {config.start_url}")
        self.site = site_key(start.hostname)
        self.origin = f"{start.scheme}://{start.netloc}"
        self.client = client or httpx.Client(
            follow_redirects=True, timeout=httpx.Timeout(20.0), headers={"User-Agent": USER_AGENT}
        )
        self._sleep = sleep
        self.robots = RobotFileParser()
        self.robots_sitemaps: list[str] = []
        self.delay = config.delay_seconds
        self._last_request = 0.0
        self.start_url = normalize_url(config.start_url)
        self.renderer = renderer  # created on first use (one browser for the whole crawl)
        self._render_off = False  # the browser isn't available: plain HTML for the rest of the crawl
        self._result = CrawlResult()
        self._log: Callable[[str], None] = lambda _m: None
        self._should_stop: Callable[[], bool] = lambda: False

    # ----------------------------------------------------------------- helpers
    def same_site(self, url: str) -> bool:
        p = urlparse(url)
        return p.scheme in ("http", "https") and site_key(p.hostname or "") == self.site

    def allowed(self, url: str) -> bool:
        path = urlparse(url).path.lower()
        return (
            self.same_site(url)
            and (not path.endswith(SKIP_EXTENSIONS) or (self.config.read_documents and is_document_url(url)))
            and self.robots.can_fetch(USER_AGENT, url)
            and matches_patterns(url, self.config.include_patterns, self.config.exclude_patterns)
        )

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < self.delay:
            self._sleep(self.delay - elapsed)
        self._last_request = time.monotonic()

    def _get(self, url: str, max_bytes: int = MAX_PAGE_BYTES) -> httpx.Response:
        """GET with the politeness delay; the body is cut just after ``max_bytes``."""
        self._wait()
        with self.client.stream("GET", url) as resp:
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > max_bytes:
                    break
            # iter_bytes() already decompressed the body: drop the encoding headers so the
            # rebuilt response isn't decoded a second time.
            headers = [(k, v) for k, v in resp.headers.multi_items() if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")]
            return httpx.Response(resp.status_code, headers=headers, content=body, request=resp.request)

    # ----------------------------------------------------------------- robots + sitemaps
    def load_robots(self) -> None:
        url = self.origin + "/robots.txt"
        try:
            resp = self._get(url)
        except httpx.HTTPError as exc:
            logger.warning("robots.txt unreachable (%s); assuming allowed", exc)
            self.robots.parse([])
            return
        if resp.status_code >= 500:
            raise RobotsBlocked(f"robots.txt returned HTTP {resp.status_code}; not crawling to stay polite.")
        if resp.status_code >= 400:
            self.robots.parse([])  # no robots.txt: everything allowed
            return
        lines = resp.text.splitlines()
        self.robots.parse(lines)
        self.robots_sitemaps = [ln.split(":", 1)[1].strip() for ln in lines if ln.lower().startswith("sitemap:")]
        crawl_delay = self.robots.crawl_delay(USER_AGENT)
        if crawl_delay:
            self.delay = max(self.delay, float(crawl_delay))

    def sitemap_urls(self, log: Callable[[str], None] = lambda _m: None) -> list[str]:
        """Collect page URLs from sitemaps (index files are followed, .gz supported)."""
        queue = deque(dict.fromkeys(self.robots_sitemaps + [self.origin + p for p in SITEMAP_PATHS]))
        visited: set[str] = set()
        pages: list[str] = []
        while queue and len(visited) < MAX_SITEMAPS:
            sm = queue.popleft()
            key = normalize_url(sm)
            if key in visited:
                continue
            visited.add(key)
            try:
                resp = self._get(sm)
            except httpx.HTTPError:
                continue
            final = normalize_url(str(resp.url))
            if resp.status_code != 200:
                continue
            if final != key and final in visited:
                continue  # e.g. /sitemap.xml redirecting to an index already read
            visited.add(final)
            content = resp.content
            if sm.endswith(".gz") or content[:2] == b"\x1f\x8b":
                try:
                    content = gzip.decompress(content)
                except OSError:
                    continue
            try:
                root = SafeET.fromstring(content)
            except Exception:  # noqa: BLE001 - not XML (e.g. HTML 404 page)
                continue
            tag = root.tag.split("}")[-1]
            locs = [el.text.strip() for el in root.iter() if el.tag.split("}")[-1] == "loc" and el.text]
            if tag == "sitemapindex":
                queue.extend(locs)
            elif tag == "urlset":
                pages.extend(locs)
                log(f"Sitemap {sm}: {len(locs)} URLs")
        return list(dict.fromkeys(pages))

    # ----------------------------------------------------------------- pictures
    def fetch_image(self, url: str) -> tuple[bytes, str] | None:
        """A picture's bytes and type: JPEG/PNG/WebP of 3 KB to 8 MB, else None."""
        from app.crawler import images

        try:
            with self.client.stream("GET", url, timeout=images.FETCH_TIMEOUT) as resp:
                content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
                declared = int(resp.headers.get("content-length") or 0) if (resp.headers.get("content-length") or "").isdigit() else 0
                if resp.status_code != 200 or content_type not in images.TYPES or declared > images.MAX_BYTES:
                    return None
                body = b""
                for chunk in resp.iter_bytes():
                    body += chunk
                    if len(body) > images.MAX_BYTES:
                        return None
        except httpx.HTTPError:
            return None
        if len(body) < images.MIN_BYTES:
            return None
        return body, content_type

    # ----------------------------------------------------------------- JavaScript pages
    def _render(self, url: str, final_url: str, plain: Extracted, keep_chrome: bool) -> Extracted:
        """Open the page in the headless browser; use the result when it has more text of its own."""
        if self.renderer is None:
            self.renderer = PageRenderer(USER_AGENT)
        self._wait()  # the browser loads the page from the site too
        try:
            rendered = self.renderer.render(final_url)
        except BrowserUnavailable as exc:
            self._render_off = True
            message = f"JavaScript-built pages are read without running their scripts: {exc}"
            logger.warning(message)
            self._log(message)
            return plain
        except Exception as exc:  # noqa: BLE001 - one page failing in the browser never fails the crawl
            self._result.errors.append(f"{url}: couldn't open in the browser ({str(exc).strip()[:200] or exc.__class__.__name__})")
            return plain
        full = extract(rendered.html, final_url, self.config.extraction, keep_chrome=keep_chrome)
        if full.body_chars <= plain.body_chars:
            return plain
        self._result.rendered += 1
        full.links = list(dict.fromkeys([*plain.links, *full.links]))
        full.images = [*rendered.images, *full.images]
        return full

    def close(self) -> None:
        """Close the browser, if one was started."""
        if self.renderer is not None:
            self.renderer.close()

    # ----------------------------------------------------------------- documents
    def _read_document(self, resp: httpx.Response, url: str, ext: str) -> Extracted:
        """A PDF, Word or Excel file linked from the site, read like an uploaded document."""
        from app.documents.readers import EmptyDocument, UnsupportedDocument, read_document

        if len(resp.content) > MAX_DOCUMENT_BYTES:
            raise DocumentError(f"file is larger than {MAX_DOCUMENT_BYTES // 1_000_000} MB")
        with tempfile.TemporaryDirectory(prefix="wa-crawl-") as tmp:
            path = Path(tmp) / f"document{ext}"
            path.write_bytes(resp.content)
            try:
                text = read_document(path, should_stop=self._should_stop)
            except EmptyDocument as exc:
                raise DocumentError("no text found" + (": the pages are pictures (a scanned PDF)" if ext == ".pdf" and "scan" in str(exc) else "")) from exc
            except UnsupportedDocument as exc:
                raise DocumentError(str(exc)) from exc
        return Extracted(title=document_title(url, ext), text=text, body_chars=len(text))

    # ----------------------------------------------------------------- crawl
    def fetch_page(self, url: str) -> tuple[int, str | None, Extracted | None]:
        """Return (status, final_url, extracted) for one URL; extracted is None for non-HTML/errors.

        Linked documents (read_documents) come back as an Extracted too; DocumentError when unreadable.
        """
        wants_document = self.config.read_documents and is_document_url(url)
        resp = self._get(url, MAX_DOCUMENT_BYTES if wants_document else MAX_PAGE_BYTES)
        final_url = normalize_url(str(resp.url))
        if resp.status_code != 200 or not self.same_site(final_url):  # off-site redirects are skipped unread
            return resp.status_code, final_url, None
        content_type = resp.headers.get("content-type", "").split(";")[0].strip().lower()
        if self.config.read_documents and "html" not in content_type:
            ext = DOC_TYPES.get(content_type) or next((e for e in DOC_EXTENSIONS if urlparse(final_url).path.lower().endswith(e)), None)
            if ext:
                extracted = self._read_document(resp, final_url, ext)
                self._result.documents += 1
                return resp.status_code, final_url, extracted
        if "html" not in content_type:
            return resp.status_code, final_url, None
        html = resp.text
        keep_chrome = self.start_url in (url, final_url)  # the start page keeps its header and footer
        extracted = extract(html, final_url, self.config.extraction, keep_chrome=keep_chrome)
        mode = self.config.render_js
        if mode != "never" and not self._render_off and (mode == "always" or needs_rendering(html, extracted)):
            extracted = self._render(url, final_url, extracted, keep_chrome)
        return resp.status_code, final_url, extracted

    def crawl(
        self,
        on_page: Callable[[FetchedPage], None],
        on_progress: Callable[[int, int, str], None] = lambda _d, _t, _u: None,
        should_stop: Callable[[], bool] = lambda: False,
        log: Callable[[str], None] = lambda _m: None,
    ) -> CrawlResult:
        self._result = CrawlResult()
        self._log = log
        self._should_stop = should_stop
        try:
            return self._crawl(self._result, on_page, on_progress, should_stop, log)
        finally:
            self.close()

    def _crawl(
        self,
        result: CrawlResult,
        on_page: Callable[[FetchedPage], None],
        on_progress: Callable[[int, int, str], None],
        should_stop: Callable[[], bool],
        log: Callable[[str], None],
    ) -> CrawlResult:
        from app.documents.readers import ReadCancelled

        self.load_robots()
        start = normalize_url(self.config.start_url)
        seeds = [u for u in (normalize_url(u) for u in self.sitemap_urls(log)) if self.allowed(u)]
        follow_links = not seeds
        result.used_sitemap = bool(seeds)
        log(f"Found {len(seeds)} pages in sitemaps" if seeds else "No usable sitemap; following links from the start page")
        queue: deque[str] = deque([start, *seeds] if self.allowed(start) else seeds)
        queued: set[str] = set(queue)
        limit = self.config.max_pages

        while queue:
            if should_stop():
                break
            if result.pages + result.failed >= limit:
                result.hit_limit = True
                break
            url = queue.popleft()
            if url in result.seen_urls:
                continue  # already fetched as the target of a redirect
            try:
                status, final_url, extracted = self.fetch_page(url)
            except httpx.HTTPError as exc:
                result.failed += 1
                result.errors.append(f"{url}: {exc.__class__.__name__}")
                continue
            except DocumentError as exc:
                result.failed += 1
                result.seen_urls.add(url)  # still on the site: keep it from looking removed
                result.errors.append(f"{url}: {exc}")
                continue
            except ReadCancelled:
                break
            if status in (404, 410):
                result.gone_urls.add(url)
                continue
            if extracted is None or final_url is None:
                if status != 200:
                    result.failed += 1
                    result.errors.append(f"{url}: HTTP {status}")
                continue
            if not self.same_site(final_url):
                continue  # redirected off-site
            already_done = final_url != url and final_url in result.seen_urls
            result.seen_urls.update({url, final_url})
            if already_done:
                continue
            if extracted.text.strip():
                result.pages += 1
                on_page(FetchedPage(url=final_url, title=extracted.title, text=extracted.text, images=extracted.images))
            # Follow links when there is no sitemap; linked documents are picked up either way.
            for link in extracted.links:
                link = normalize_url(link)
                if (follow_links or (self.config.read_documents and is_document_url(link))) and link not in queued and self.allowed(link):
                    queued.add(link)
                    queue.append(link)
            total = min(limit, len(queued))
            on_progress(result.pages + result.failed, total, final_url)
        return result
