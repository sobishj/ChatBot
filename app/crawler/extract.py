"""HTML → (title, clean text, links).

Two extraction modes (per client, Website tab → crawl settings):

* ``auto`` (default): structural cleaning. Remove page chrome (header, nav, footer,
  aside, scripts, forms, cookie banners, page-builder header/footer templates) and keep
  ALL remaining text with line breaks between blocks. High recall: keeps small facts
  like "Located on: Second Floor" or opening hours that article extractors drop.
* ``main_content``: trafilatura's article extraction. Best for blog/news-style sites.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

from lxml import etree
from lxml import html as lxml_html

# Elements that never contain page content.
_DROP_XPATH = (
    "//script|//style|//noscript|//template|//svg|//iframe|//form|//button|//select|//textarea"
    "|//header|//nav|//footer|//aside"
    "|//*[@role='navigation' or @role='banner' or @role='contentinfo' or @role='dialog' or @aria-hidden='true']"
    # Page builders (Elementor, Divi, ...) mark their global header/footer templates.
    "|//*[@data-elementor-type='header' or @data-elementor-type='footer' or @data-elementor-type='popup']"
    "|//*[contains(concat(' ', normalize-space(@class), ' '), ' screen-reader-text ')]"
    "|//*[contains(@class, 'cookie') or contains(@id, 'cookie')]"
)

_BLOCK_TAGS = {
    "p", "div", "section", "article", "main", "li", "ul", "ol", "dl", "dt", "dd", "table", "tr", "td", "th",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "pre", "address", "figure", "figcaption", "br", "hr",
}
_SKIP_LINES = {"skip to main content", "skip to content", "menu", "close", "search"}
# Call-to-action link labels ("Know More »", "View Store ›", "Read more →") carry no facts
# and interleave with real content (e.g. between every movie title).
_CTA_RE = re.compile(
    r"^(?:read|know|learn|view|see|show|find out|discover|explore)(?: \w+){0,2}\s*[»›→>]*$|^.{0,30}\s[»›→]$",
    re.IGNORECASE,
)


def _is_noise(line: str) -> bool:
    lowered = line.lower()
    if lowered in _SKIP_LINES:
        return True
    return len(line.split()) <= 4 and bool(_CTA_RE.match(line)) and not any(ch.isdigit() for ch in line)
MAX_TEXT_CHARS = 200_000


@dataclass
class Extracted:
    title: str
    text: str
    links: list[str] = field(default_factory=list)


def parse(html: str) -> lxml_html.HtmlElement | None:
    try:
        return lxml_html.fromstring(html)
    except (etree.ParserError, ValueError):
        return None


def extract_title(doc: lxml_html.HtmlElement) -> str:
    for xpath in ("//meta[@property='og:title']/@content", "//title/text()", "//h1//text()"):
        values = [v.strip() for v in doc.xpath(xpath) if v and v.strip()]
        if values:
            return re.sub(r"\s+", " ", values[0])[:300]
    return ""


def extract_links(doc: lxml_html.HtmlElement, base_url: str) -> list[str]:
    base = doc.xpath("//base/@href")
    base_url = urljoin(base_url, base[0]) if base else base_url
    links: list[str] = []
    for href in doc.xpath("//a/@href"):
        href = (href or "").strip()
        if href and not href.startswith(("#", "mailto:", "tel:", "javascript:")):
            links.append(urljoin(base_url, href))
    return links


def _structural_text(doc: lxml_html.HtmlElement) -> str:
    for el in doc.xpath(_DROP_XPATH):
        if el.getparent() is not None:
            el.drop_tree()
    body = doc.find("body")
    root = body if body is not None else doc
    for el in root.iter():
        if isinstance(el.tag, str) and el.tag.lower() in _BLOCK_TAGS:
            el.tail = "\n" + (el.tail or "")
            if el.tag.lower() in ("td", "th"):
                el.tail = " | " + el.tail.lstrip("\n")
    lines: list[str] = []
    for raw in root.text_content().splitlines():
        line = re.sub(r"\s+", " ", raw).strip(" |")
        if line and not _is_noise(line):
            lines.append(line)
    return "\n".join(lines)


def _main_content_text(html: str, url: str) -> str:
    import trafilatura

    return trafilatura.extract(html, url=url, include_tables=True, include_comments=False, favor_recall=True) or ""


def extract(html: str, url: str, mode: str = "auto") -> Extracted:
    """Extract title, text and outgoing links from an HTML page."""
    doc = parse(html)
    if doc is None:
        return Extracted(title="", text="")
    title = extract_title(doc)
    links = extract_links(doc, url)
    text = _main_content_text(html, url) if mode == "main_content" else _structural_text(doc)
    return Extracted(title=title, text=text[:MAX_TEXT_CHARS], links=links)
