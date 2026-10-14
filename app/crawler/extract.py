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


# What an icon next to a detail means (Font Awesome, Elementor, Material, Bootstrap icon names).
_ICON_LABELS = (
    (re.compile(r"phone|mobile|whatsapp|\btel\b|call", re.I), "Phone"),
    (re.compile(r"envelope|\bmail|email|inbox", re.I), "Email"),
    (re.compile(r"clock|\btime\b|hours|schedule", re.I), "Opening hours"),
    (re.compile(r"map-marker|map-pin|location|\baddress\b|place", re.I), "Address"),
)
_LABEL_WORDS = re.compile(r"\b(?:phone|tel|mobile|call|email|e-mail|mail|hours|timings?|open|address|located)\b", re.I)


def _decode_cf_email(hexed: str) -> str | None:
    """Cloudflare "email protection" hides addresses as XOR-ed hex; browsers decode it with JavaScript."""
    try:
        key = int(hexed[:2], 16)
        return "".join(chr(int(hexed[i : i + 2], 16) ^ key) for i in range(2, len(hexed), 2))
    except ValueError:
        return None


def _set_text(el: lxml_html.HtmlElement, text: str) -> None:
    for child in list(el):
        el.remove(child)
    el.text = text


def _prefix(el: lxml_html.HtmlElement, label: str) -> None:
    """Put "Label: " before an element's text, unless it already says what it is."""
    if _LABEL_WORDS.search(el.text_content() or ""):
        return
    # Attach the label to the first real text (not the element's start), so "Phone:" and the
    # number stay on one line even when the markup has line breaks between icon and value.
    for node in el.iter():
        if not isinstance(node.tag, str):
            continue
        if node.text and node.text.strip():
            node.text = f"{label}: " + node.text.lstrip()
            return
        if node is not el and node.tail and node.tail.strip():
            node.tail = f" {label}: " + node.tail.lstrip()
            return
    el.text = f"{label}: " + (el.text or "")


def _annotate(doc: lxml_html.HtmlElement) -> None:
    """Make implicit details explicit before the text is extracted, so the assistant knows what each one is:
    decode protected emails, and label phone/email links and details shown only with an icon."""
    for el in doc.xpath("//*[@data-cfemail]"):
        email = _decode_cf_email(el.get("data-cfemail", ""))
        if email:
            _set_text(el, email)
    for a in doc.xpath("//a[@href]"):
        href = a.get("href", "").strip()
        if "/cdn-cgi/l/email-protection#" in href:
            email = _decode_cf_email(href.rsplit("#", 1)[1])
            if email:
                _set_text(a, email)
                _prefix(a, "Email")
        elif href.lower().startswith("tel:") and (a.text_content() or "").strip():
            _prefix(a, "Phone")
        elif href.lower().startswith("mailto:") and (a.text_content() or "").strip():
            _prefix(a, "Email")
    # A detail shown with only an icon (e.g. a phone icon before "4055877"): label the item it belongs to.
    for icon in doc.xpath("//svg[@class]|//i[@class]|//span[contains(@class, 'icon') and @class]"):
        names = " ".join(filter(None, [icon.get("class", ""), icon.get("data-icon", ""), icon.get("aria-label", "")]))
        label = next((text for pattern, text in _ICON_LABELS if pattern.search(names)), None)
        if label is None:
            continue
        item = icon.getparent()
        for _ in range(3):  # the list item / paragraph that holds the icon and its text
            if item is None or (item.tag in ("li", "p", "dd", "td") or len((item.text_content() or "").strip()) > 0):
                break
            item = item.getparent()
        if item is None:
            continue
        text = re.sub(r"\s+", " ", item.text_content() or "").strip()
        if text and len(text) <= 120 and not text.lower().startswith(label.lower()):
            _prefix(item, label)


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
    _annotate(doc)
    if mode == "main_content":
        text = _main_content_text(lxml_html.tostring(doc, encoding="unicode"), url)
    else:
        text = _structural_text(doc)
    return Extracted(title=title, text=text[:MAX_TEXT_CHARS], links=links)
