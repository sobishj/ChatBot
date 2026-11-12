"""JavaScript-built pages, opened in a local headless browser (Playwright + Chromium).

React, Vue, Angular, … apps send an almost empty ``<div id="root">``: the text only appears
after their scripts run. Such pages are opened in Chromium so the crawler reads what visitors see.

One browser is started (lazily) for the whole crawl, each page gets a new tab, and everything is
closed at the end of the crawl. When Playwright or Chromium isn't installed, or the browser
can't start, this is logged once and the crawl reads the plain HTML instead: it never fails
the crawl.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.crawler.extract import Extracted, PageImage

logger = logging.getLogger(__name__)

NAVIGATION_TIMEOUT_MS = 25_000
SETTLE_MS = 800  # after scrolling to the end, for pictures and sections that load on scroll
MIN_OWN_TEXT_CHARS = 300
MAX_IMAGES = 40
# The empty mount point of a single-page app: <div id="root"></div>, <div id="app"></div>, …
APP_SHELL_RE = re.compile(r"""<div[^>]*\bid=["'](?:root|app|__next|__nuxt|svelte|main-app)["'][^>]*>\s*</div>""", re.IGNORECASE)

# Runs in the page: loaded pictures big enough to be content (not icons), plus CSS background
# pictures of large blocks (banners, hero images).
COLLECT_IMAGES = r"""(() => {
  const out = [];
  for (const img of Array.from(document.images)) {
    const src = img.currentSrc || img.src;
    if (src && img.naturalWidth >= 100 && img.naturalHeight >= 60) out.push({ url: src, alt: (img.alt || "").trim() });
  }
  for (const el of Array.from(document.querySelectorAll("body *"))) {
    if (el.offsetWidth < 300 || el.offsetHeight < 150) continue;
    const m = /url\(["']?([^"')]+)["']?\)/.exec(getComputedStyle(el).backgroundImage || "");
    if (m) out.push({ url: new URL(m[1], location.href).href, alt: "" });
  }
  return out;
})()"""

# Runs in the page: apps often lay out <span>s and <div>s as blocks with CSS, which the HTML alone
# doesn't show ("Project Reports" + "Know your business" would read "Project ReportsKnow your …").
# A space before and after every element the browser shows as a block keeps the words apart
# (also "<strong>Title</strong><p>Text</p>": inline text followed by a block).
# It also removes initials badges ("SJ" in a circle next to "Soyson Joseph Thonakkara"): read as
# text they become part of the name.
SEPARATE_BLOCKS = r"""(() => {
  for (const el of Array.from(document.querySelectorAll("body *"))) {
    if (el.children.length === 0 && /^[A-Z]{1,3}$/.test((el.textContent || "").trim())) {
      const s = getComputedStyle(el);
      const round = parseFloat(s.borderTopLeftRadius) >= Math.min(el.offsetWidth, el.offsetHeight) / 3;
      const square = el.offsetWidth > 0 && Math.abs(el.offsetWidth - el.offsetHeight) <= 8;
      if ((round && square) || /avatar|initial|monogram/i.test(String(el.className || "") + " " + String((el.parentElement || {}).className || ""))) {
        el.remove();
        continue;
      }
    }
  }
  for (const el of Array.from(document.querySelectorAll("body *"))) {
    const d = getComputedStyle(el).display;
    if (d && d !== "inline" && d !== "contents" && d !== "none" && el.parentNode && !/^(SCRIPT|STYLE|TEMPLATE)$/.test(el.tagName)) {
      el.before(document.createTextNode(" "));
      el.after(document.createTextNode(" "));
    }
  }
})()"""


@dataclass
class Rendered:
    html: str
    images: list[PageImage] = field(default_factory=list)


class BrowserUnavailable(Exception):
    """Playwright or Chromium isn't installed, or the browser couldn't start."""


def needs_rendering(html: str, extracted: Extracted) -> bool:
    """True when the plain HTML hardly has any text of its own, or is an app shell filled by JavaScript."""
    return extracted.body_chars < MIN_OWN_TEXT_CHARS or bool(APP_SHELL_RE.search(html))


def _sync_playwright() -> Callable[[], Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable("Playwright is not installed") from exc
    return sync_playwright


def _is_timeout(exc: Exception) -> bool:
    return "timeout" in exc.__class__.__name__.lower() or "timeout" in str(exc).lower()


class PageRenderer:
    """One headless Chromium for a whole crawl; ``render`` opens each page in a new tab.

    Playwright's sync API is bound to the thread that started it: create, use and close the
    renderer in the crawl's thread (the worker runs each job in one thread).
    """

    def __init__(self, user_agent: str | None = None) -> None:
        self.user_agent = user_agent
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self.unavailable: str | None = None  # why the browser can't be used (then render() raises)

    def _start(self) -> None:
        if self._context is not None:
            return
        if self.unavailable:
            raise BrowserUnavailable(self.unavailable)
        try:
            self._playwright = _sync_playwright()().start()
            # Chromium's sandbox needs privileges a container doesn't have; /dev/shm is small in Docker.
            self._browser = self._playwright.chromium.launch(headless=True, args=["--disable-dev-shm-usage"])
            self._context = self._browser.new_context(user_agent=self.user_agent) if self.user_agent else self._browser.new_context()
        except BrowserUnavailable as exc:
            self.unavailable = str(exc)
            self.close()
            raise
        except Exception as exc:  # noqa: BLE001 - missing browser binaries, missing system libraries, ...
            self.unavailable = f"the browser could not start ({str(exc).strip().splitlines()[0][:200] if str(exc).strip() else exc.__class__.__name__})"
            self.close()
            raise BrowserUnavailable(self.unavailable) from exc

    def render(self, url: str) -> Rendered:
        """Open the page, let its scripts run, and return its HTML and the pictures it shows."""
        self._start()
        page = self._context.new_page()
        try:
            try:
                page.goto(url, wait_until="networkidle", timeout=NAVIGATION_TIMEOUT_MS)
            except Exception as exc:  # noqa: BLE001
                # Pages that keep a connection open never go idle: read what has loaded by now.
                if not _is_timeout(exc):
                    raise
            # Scroll to the end so pictures and sections that load on scroll appear, then give them a moment.
            page.evaluate("window.scrollTo(0, document.body ? document.body.scrollHeight : 0)")
            page.wait_for_timeout(SETTLE_MS)
            images: list[PageImage] = []
            try:
                seen: set[str] = set()
                for item in page.evaluate(COLLECT_IMAGES) or []:
                    src = str(item.get("url") or "")
                    if src.lower().startswith(("http://", "https://")) and src not in seen:
                        seen.add(src)
                        images.append(PageImage(url=src, alt=str(item.get("alt") or "")))
                images = images[:MAX_IMAGES]
            except Exception:  # noqa: BLE001 - pictures are a bonus
                images = []
            try:
                page.evaluate(SEPARATE_BLOCKS)
            except Exception:  # noqa: BLE001
                pass
            return Rendered(html=page.content(), images=images)
        finally:
            try:
                page.close()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        """Close the browser (safe to call more than once)."""
        for obj, method in ((self._context, "close"), (self._browser, "close"), (self._playwright, "stop")):
            if obj is not None:
                try:
                    getattr(obj, method)()
                except Exception:  # noqa: BLE001 - already gone
                    pass
        self._context = self._browser = self._playwright = None
