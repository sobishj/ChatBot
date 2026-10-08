"""Polite same-site crawler.

1. Read robots.txt (respect Disallow and Crawl-delay; collect Sitemap: entries).
2. Try sitemaps (robots.txt entries, /sitemap.xml, /sitemap_index.xml, /wp-sitemap.xml).
   If they list pages, crawl those. Otherwise follow internal links from the start URL.
3. Fetch pages one by one with a delay, extract clean text, report each page via a callback.

The crawler knows nothing about the database; :mod:`app.crawler.service` stores results.
"""

from __future__ import annotations

import gzip
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from fnmatch import fnmatch
from urllib.parse import parse_qsl, urldefrag, urlencode, urlparse, urlunparse
from urllib.robotparser import RobotFileParser

import httpx
from defusedxml import ElementTree as SafeET

from app import __version__
from app.crawler.extract import Extracted, extract

logger = logging.getLogger(__name__)

USER_AGENT = f"WebsiteAssistantBot/{__version__} (+website assistant crawler)"
SITEMAP_PATHS = ("/sitemap.xml", "/sitemap_index.xml", "/wp-sitemap.xml")
MAX_PAGE_BYTES = 5_000_000
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


@dataclass
class FetchedPage:
    url: str
    title: str
    text: str


@dataclass
class CrawlResult:
    pages: int = 0  # successfully extracted pages
    failed: int = 0
    seen_urls: set[str] = field(default_factory=set)  # every URL fetched successfully
    gone_urls: set[str] = field(default_factory=set)  # 404/410: page removed from the site
    hit_limit: bool = False
    used_sitemap: bool = False
    errors: list[str] = field(default_factory=list)


class RobotsBlocked(Exception):
    """robots.txt could not be read (server error) — crawling would be impolite."""


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
    def __init__(self, config: CrawlConfig, client: httpx.Client | None = None, sleep: Callable[[float], None] = time.sleep) -> None:
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

    # ----------------------------------------------------------------- helpers
    def same_site(self, url: str) -> bool:
        p = urlparse(url)
        return p.scheme in ("http", "https") and site_key(p.hostname or "") == self.site

    def allowed(self, url: str) -> bool:
        return (
            self.same_site(url)
            and not urlparse(url).path.lower().endswith(SKIP_EXTENSIONS)
            and self.robots.can_fetch(USER_AGENT, url)
            and matches_patterns(url, self.config.include_patterns, self.config.exclude_patterns)
        )

    def _wait(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if self._last_request and elapsed < self.delay:
            self._sleep(self.delay - elapsed)
        self._last_request = time.monotonic()

    def _get(self, url: str) -> httpx.Response:
        self._wait()
        with self.client.stream("GET", url) as resp:
            body = b""
            for chunk in resp.iter_bytes():
                body += chunk
                if len(body) > MAX_PAGE_BYTES:
                    break
            return httpx.Response(resp.status_code, headers=resp.headers, content=body, request=resp.request)

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

    # ----------------------------------------------------------------- crawl
    def fetch_page(self, url: str) -> tuple[int, str | None, Extracted | None]:
        """Return (status, final_url, extracted) for one URL; extracted is None for non-HTML/errors."""
        resp = self._get(url)
        final_url = normalize_url(str(resp.url))
        if resp.status_code != 200:
            return resp.status_code, final_url, None
        if "html" not in resp.headers.get("content-type", "").lower():
            return resp.status_code, final_url, None
        return resp.status_code, final_url, extract(resp.text, final_url, self.config.extraction)

    def crawl(
        self,
        on_page: Callable[[FetchedPage], None],
        on_progress: Callable[[int, int, str], None] = lambda _d, _t, _u: None,
        should_stop: Callable[[], bool] = lambda: False,
        log: Callable[[str], None] = lambda _m: None,
    ) -> CrawlResult:
        result = CrawlResult()
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
                on_page(FetchedPage(url=final_url, title=extracted.title, text=extracted.text))
            if follow_links:
                for link in extracted.links:
                    link = normalize_url(link)
                    if link not in queued and self.allowed(link):
                        queued.add(link)
                        queue.append(link)
            total = min(limit, len(queued))
            on_progress(result.pages + result.failed, total, final_url)
        return result
