"""Reading "everything" on a website (ported from worker/test/read_everything.test.ts).

Header/footer on the start page, pictures (read once, cached in PostgreSQL), linked
PDF/Word/Excel files, and JavaScript-built pages (Playwright is mocked: no real browser).
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.crawler import render
from app.crawler.crawler import CrawlConfig, Crawler, FetchedPage, document_title, is_document_url
from app.crawler.extract import PageImage, extract
from app.crawler.images import ImageBudget, candidate_images, cleanup_cache, describe_images, plain_text, remember, url_hash
from app.crawler.render import COLLECT_IMAGES, SEPARATE_BLOCKS, BrowserUnavailable, PageRenderer, Rendered, needs_rendering
from app.documents.readers import has_text
from app.services.clients import ClientError, crawl_settings, validate_crawl_settings

SITE = "https://www.shop.test"
BANNER = b"BANNER: Call 0484 4061110 for a free consultation. " * 80  # > 3 KB, like a real picture

HTML = (
    "<html><head><title>Home</title></head><body>"
    "<header><img src='/logo.png' alt='Logo'><nav><img src='/nav-icon.png'></nav></header>"
    "<main><h1>Welcome</h1><p>We help businesses.</p>"
    "<img src='/banner.jpg' alt='Office'><img data-src='/lazy.webp' src='data:image/gif;base64,R0lGOD'>"
    "<img srcset='/small.jpg 320w, /large.jpg 1280w'><img src='/banner.jpg'></main>"
    "<footer>Phone: 0484 4061110 · mail@taxlead.in</footer></body></html>"
)


# ----------------------------------------------------------------------------- extraction
def test_keeps_the_header_and_footer_only_when_asked() -> None:
    assert "mail@taxlead.in" not in extract(HTML, SITE + "/").text
    assert "Phone: 0484 4061110 · mail@taxlead.in" in extract(HTML, SITE + "/", keep_chrome=True).text


def test_collects_content_pictures_lazy_srcset_deduplicated_not_from_navigation() -> None:
    assert extract(HTML, SITE + "/").images == [
        PageImage(SITE + "/banner.jpg", "Office"),
        PageImage(SITE + "/lazy.webp", ""),
        PageImage(SITE + "/large.jpg", ""),
    ]
    with_chrome = [i.url for i in extract(HTML, SITE + "/", keep_chrome=True).images]
    assert SITE + "/logo.png" in with_chrome and SITE + "/nav-icon.png" not in with_chrome


def test_keep_chrome_still_drops_navigation_and_popups() -> None:
    html = (
        "<body><header>Call 0484 4061110<nav>Home Shop Dine</nav></header>"
        "<div data-elementor-type='popup'>Subscribe now</div><div data-elementor-type='footer'>Open 9 AM to 6 PM</div></body>"
    )
    text = extract(html, SITE + "/", keep_chrome=True).text
    assert "Call 0484 4061110" in text and "Open 9 AM to 6 PM" in text
    assert "Home Shop Dine" not in text and "Subscribe now" not in text


def test_body_chars_leaves_out_the_meta_description() -> None:
    shell = "<html><head><meta name='description' content='A long description of the company.'></head><body><div id='root'></div></body></html>"
    out = extract(shell, SITE + "/")
    assert out.body_chars == 0 and "A long description" in out.text


def test_skips_icons_and_vector_graphics() -> None:
    images = [
        PageImage(SITE + "/a.svg"), PageImage(SITE + "/favicon-32.png"), PageImage(SITE + "/anim.gif"),
        PageImage("data:image/png;base64,xx"), PageImage(SITE + "/photo.jpg"), PageImage(SITE + "/photo.jpg"),
    ]
    assert [i.url for i in candidate_images(images, 10)] == [SITE + "/photo.jpg"]
    # Logos say nothing useful and their descriptions are long: skipped by file name or alt text.
    assert candidate_images([PageImage(SITE + "/assets/logo-A2Br.webp", "Taxlead"), PageImage(SITE + "/x.png", "Company logo")], 10) == []
    assert len(candidate_images([PageImage(f"{SITE}/{i}.jpg") for i in range(20)])) == 10  # at most 10 per page


def test_treats_a_pdf_of_page_markers_only_as_having_no_text() -> None:
    assert not has_text("[Page 1]\n\n\n[Page 2]\n\n[Page 3]")
    assert has_text("[Page 1]\nFees from Rs 999")


def test_turns_a_markdown_description_into_one_plain_line() -> None:
    assert plain_text('### Image Overview\nA poster. The text reads **"Call 0484 4061110"**.\n* **Colours:** teal') == (
        'Image Overview A poster. The text reads "Call 0484 4061110". Colours: teal'
    )


def test_recognises_javascript_built_pages() -> None:
    shell = (
        "<html><head><title>App</title><meta name='description' content='A long description of the company that is all a crawler sees.'>"
        '</head><body><div id="root"></div><script type=module src=/main.js></script></body></html>'
    )
    assert needs_rendering(shell, extract(shell, SITE + "/"))
    real = f"<html><body><main><p>{'Real content. ' * 40}</p></main></body></html>"
    assert not needs_rendering(real, extract(real, SITE + "/"))
    # An empty app shell is rendered even when the rest of the page has plenty of text.
    shell_with_text = f"<html><body><div id='app'></div><noscript>x</noscript><p>{'Footer text. ' * 40}</p></body></html>"
    assert needs_rendering(shell_with_text, extract(shell_with_text, SITE + "/"))


def test_linked_documents_and_their_titles() -> None:
    assert is_document_url(SITE + "/files/Price_List-2026.PDF") and not is_document_url(SITE + "/files/report.doc")
    assert document_title(SITE + "/files/Price_List-2026.pdf", ".pdf") == "Price List 2026"
    assert document_title(SITE + "/files/brochure%20final.docx", ".docx") == "brochure final"


def test_crawl_settings_validation() -> None:
    assert validate_crawl_settings({"render_js": "never", "read_images": 0, "read_documents": True}) == {
        "render_js": "never", "read_images": False, "read_documents": True,
    }
    with pytest.raises(ClientError, match="JavaScript rendering must be auto, always or never."):
        validate_crawl_settings({"render_js": "sometimes"})


def test_crawl_settings_defaults() -> None:
    from types import SimpleNamespace

    settings = crawl_settings(SimpleNamespace(crawl_settings={}))  # type: ignore[arg-type]
    assert (settings["render_js"], settings["read_images"], settings["read_documents"]) == ("auto", True, True)


# ----------------------------------------------------------------------------- Playwright (mocked)
class FakePage:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.closed = False

    def goto(self, url: str, wait_until: str, timeout: int) -> None:
        self.browser.calls.append(("goto", url, wait_until, timeout))
        if self.browser.goto_error:
            raise self.browser.goto_error

    def evaluate(self, script: str) -> Any:
        self.browser.scripts.append(script)
        if script == COLLECT_IMAGES:
            return [
                {"url": SITE + "/hero.jpg", "alt": "Hero"},
                {"url": SITE + "/hero.jpg", "alt": "Hero"},
                {"url": "data:image/png;base64,xx", "alt": ""},
            ]
        return None

    def wait_for_timeout(self, ms: int) -> None:
        self.browser.calls.append(("wait", ms))

    def content(self) -> str:
        return self.browser.html

    def close(self) -> None:
        self.closed = True


class FakeBrowser:
    def __init__(self, html: str = "", launch_error: Exception | None = None, goto_error: Exception | None = None) -> None:
        self.html = html
        self.launch_error = launch_error
        self.goto_error = goto_error
        self.calls: list[tuple[Any, ...]] = []
        self.scripts: list[str] = []
        self.pages: list[FakePage] = []
        self.launches = 0
        self.closed: list[str] = []

    # sync_playwright() -> .start() -> playwright
    def __call__(self) -> FakeBrowser:
        return self

    def start(self) -> FakeBrowser:
        return self

    @property
    def chromium(self) -> FakeBrowser:
        return self

    def launch(self, headless: bool, args: list[str]) -> FakeBrowser:
        self.launches += 1
        if self.launch_error:
            raise self.launch_error
        return self

    def new_context(self, **_kwargs: Any) -> FakeBrowser:
        return self

    def new_page(self) -> FakePage:
        page = FakePage(self)
        self.pages.append(page)
        return page

    def close(self) -> None:
        self.closed.append("close")

    def stop(self) -> None:
        self.closed.append("stop")


def use_browser(monkeypatch: pytest.MonkeyPatch, browser: FakeBrowser) -> FakeBrowser:
    monkeypatch.setattr(render, "_sync_playwright", lambda: browser)
    return browser


def test_renderer_runs_the_reference_steps(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = use_browser(monkeypatch, FakeBrowser(html="<html><body><p>Rendered</p></body></html>"))
    renderer = PageRenderer()
    first = renderer.render(SITE + "/a")
    renderer.render(SITE + "/b")
    assert first == Rendered(html="<html><body><p>Rendered</p></body></html>", images=[PageImage(SITE + "/hero.jpg", "Hero")])
    assert browser.launches == 1  # one browser for the whole crawl, a new page per URL
    assert ("goto", SITE + "/a", "networkidle", 25_000) in browser.calls and ("wait", 800) in browser.calls
    assert browser.scripts[1:3] == [COLLECT_IMAGES, SEPARATE_BLOCKS]  # after scrolling to the end
    assert all(p.closed for p in browser.pages) and len(browser.pages) == 2
    renderer.close()
    assert browser.closed == ["close", "close", "stop"]


def test_renderer_reads_what_has_loaded_after_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class TimeoutError(Exception):  # noqa: A001 - named like Playwright's
        pass

    use_browser(monkeypatch, FakeBrowser(html="<p>Partly loaded</p>", goto_error=TimeoutError("Timeout 25000ms exceeded")))
    assert PageRenderer().render(SITE + "/").html == "<p>Partly loaded</p>"


def test_renderer_unavailable_when_the_browser_cannot_start(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = use_browser(monkeypatch, FakeBrowser(launch_error=RuntimeError("Executable doesn't exist at /ms-playwright/chromium")))
    renderer = PageRenderer()
    with pytest.raises(BrowserUnavailable, match="could not start"):
        renderer.render(SITE + "/")
    with pytest.raises(BrowserUnavailable):
        renderer.render(SITE + "/")
    assert browser.launches == 1  # not retried for every page


# ----------------------------------------------------------------------------- crawler + rendering
SHELL = (
    "<html><head><title>App</title></head><body><div id='root'></div>"
    "<a href='/about/'>About</a><script src='/main.js'></script></body></html>"
)
RENDERED = f"<html><head><title>App</title></head><body><main><h1>Taxlead</h1><p>{'GST filing and audits. ' * 20}</p><a href='/services/'>Services</a></main></body></html>"


def site_client(pages: dict[str, tuple[str, bytes | str]], seen: list[str] | None = None) -> httpx.Client:
    """path -> (content type, body)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\n")
        if request.url.path in pages:
            ctype, body = pages[request.url.path]
            return httpx.Response(200, content=body if isinstance(body, bytes) else body.encode(), headers={"content-type": ctype})
        return httpx.Response(404, text="not found", headers={"content-type": "text/html"})

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def run_crawl(client: httpx.Client, **cfg: Any) -> tuple[list[FetchedPage], Any, Crawler]:
    pages: list[FetchedPage] = []
    logs: list[str] = []
    crawler = Crawler(CrawlConfig(start_url=SITE + "/", delay_seconds=0, **cfg), client=client, sleep=lambda s: None)
    result = crawler.crawl(pages.append, log=logs.append)
    result.logs = logs
    return pages, result, crawler


def test_crawl_renders_javascript_built_pages_and_merges_links(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = use_browser(monkeypatch, FakeBrowser(html=RENDERED))
    site = {"/": ("text/html", SHELL), "/about/": ("text/html", RENDERED), "/services/": ("text/html", RENDERED)}
    pages, result, _ = run_crawl(site_client(site))
    home = next(p for p in pages if p.url == SITE + "/")
    assert "GST filing and audits." in home.text
    assert {p.url for p in pages} == {SITE + "/", SITE + "/about/", SITE + "/services/"}  # links of both versions
    assert home.images[0] == PageImage(SITE + "/hero.jpg", "Hero")
    assert result.rendered == 1 and browser.launches == 1
    assert "close" in browser.closed  # browser closed at the end of the crawl


def test_crawl_keeps_the_plain_html_when_rendering_adds_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    use_browser(monkeypatch, FakeBrowser(html="<html><body></body></html>"))
    pages, result, _ = run_crawl(site_client({"/": ("text/html", "<html><body><p>Short page</p></body></html>")}))
    assert pages[0].text == "Short page" and result.rendered == 0


def test_render_js_never_and_always(monkeypatch: pytest.MonkeyPatch) -> None:
    browser = use_browser(monkeypatch, FakeBrowser(html=RENDERED))
    run_crawl(site_client({"/": ("text/html", SHELL)}), render_js="never")
    assert browser.launches == 0
    real = f"<html><body><main><p>{'Plain content. ' * 25}</p></main></body></html>"  # enough text: "auto" wouldn't render it
    assert not needs_rendering(real, extract(real, SITE + "/"))
    _, result, _ = run_crawl(site_client({"/": ("text/html", real)}), render_js="always")
    assert browser.launches == 1 and result.rendered == 1


def test_crawl_without_a_browser_reads_plain_html_and_says_so_once() -> None:
    # The autouse fixture makes Playwright unavailable.
    site = {"/": ("text/html", SHELL.replace("<div id='root'></div>", "<div id='root'></div><p>Hello</p>")), "/about/": ("text/html", SHELL)}
    pages, result, _ = run_crawl(site_client(site))
    assert [p.url for p in pages] == [SITE + "/", SITE + "/about/"]  # both read as plain HTML
    assert sum("without running their scripts" in m for m in result.logs) == 1  # said once, not per page
    assert result.failed == 0 and result.rendered == 0


def test_a_page_failing_in_the_browser_is_noted_not_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    use_browser(monkeypatch, FakeBrowser(goto_error=RuntimeError("net::ERR_ABORTED")))
    site = {"/": ("text/html", SHELL.replace("<div id='root'></div>", "<div id='root'></div><p>Hello</p>"))}
    pages, result, _ = run_crawl(site_client(site))
    assert pages[0].text.startswith("Hello") or "Hello" in pages[0].text
    assert any("couldn't open in the browser" in e for e in result.errors)


# ----------------------------------------------------------------------------- linked documents
def make_docx(text: str) -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph(text)
    buf = io.BytesIO()
    document.save(buf)
    return buf.getvalue()


DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_linked_documents_are_read_even_with_a_sitemap() -> None:
    sitemap = f'<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{SITE}/services/</loc></url></urlset>'
    site = {
        "/": ("text/html", "<html><body><main><p>Home</p></main></body></html>"),
        "/sitemap.xml": ("application/xml", sitemap),
        "/services/": ("text/html", "<html><body><main><p>Services</p><a href='/files/Fee_List.docx'>Fees</a><a href='/other/'>x</a></main></body></html>"),
        "/files/Fee_List.docx": (DOCX_TYPE, make_docx("GST filing from Rs 999 a month.")),
    }
    seen: list[str] = []
    pages, result, _ = run_crawl(site_client(site, seen))
    doc = next(p for p in pages if p.url == SITE + "/files/Fee_List.docx")
    assert doc.title == "Fee List" and "GST filing from Rs 999 a month." in doc.text
    assert result.documents == 1 and "/other/" not in seen  # sitemap crawl: only documents are taken from links


def test_unreadable_documents_are_skipped_with_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.documents import readers

    def scanned(path: Any, progress: Any = None, should_stop: Any = None) -> str:
        return "[Page 1]\n\n[Page 2]"  # pages are pictures and OCR found nothing

    monkeypatch.setattr(readers, "read_pdf", scanned)
    monkeypatch.setitem(readers.READERS, ".pdf", scanned)
    site = {
        "/": ("text/html", "<html><body><main><p>Home</p><a href='/scan.pdf'>Scan</a><a href='/big.pdf'>Big</a></main></body></html>"),
        "/scan.pdf": ("application/pdf", b"%PDF-1.7 scanned"),
        "/big.pdf": ("application/pdf", b"%PDF" + b"0" * 20_000_001),
    }
    pages, result, _ = run_crawl(site_client(site))
    assert [p.url for p in pages] == [SITE + "/"]
    assert any(e.startswith(SITE + "/scan.pdf: no text found") for e in result.errors)
    assert any("/big.pdf: file is larger than 20 MB" in e for e in result.errors)


def test_documents_can_be_switched_off() -> None:
    seen: list[str] = []
    site = {"/": ("text/html", "<html><body><main><p>Home</p><a href='/a.docx'>A</a></main></body></html>"), "/a.docx": (DOCX_TYPE, make_docx("A"))}
    pages, result, _ = run_crawl(site_client(site, seen), read_documents=False)
    assert "/a.docx" not in seen and result.documents == 0


# ----------------------------------------------------------------------------- pictures + cache (Postgres)
def serve(seen: list[str]) -> httpx.Client:
    return site_client(
        {
            "/": ("text/html", "<html><head><title>Home</title></head><body><main><h1>Home</h1><p>Tax and business services in Kochi.</p>"
                  "<img src='/banner.png' alt='Offer'><a href='/brochure.docx'>Brochure</a></main>"
                  "<footer>Email: mail@taxlead.in</footer></body></html>"),
            "/banner.png": ("image/png", BANNER),
            "/brochure.docx": (DOCX_TYPE, make_docx("Our fees: GST filing from Rs 999 a month.")),
        },
        seen,
    )


def fake_reader(data: bytes, content_type: str) -> str:
    return data[:60].decode()


@pytest.mark.integration
def test_crawl_adds_pictures_footer_and_linked_documents_and_caches_pictures(db: Session, embedder, make_client) -> None:
    from app.crawler.service import crawl_client
    from app.db.models import Page

    client = make_client("shop", "Shop", SITE)
    seen: list[str] = []

    def crawl() -> dict[str, Any]:
        crawler = Crawler(CrawlConfig(start_url=SITE + "/", delay_seconds=0), client=serve(seen), sleep=lambda s: None)
        return crawl_client(db, client, embedder, crawler=crawler, image_reader=fake_reader)

    first = crawl()
    pages = {p.url: p for p in db.scalars(select(Page).where(Page.client_id == client.id))}
    home = pages[SITE + "/"]
    assert "Email: mail@taxlead.in" in home.content  # the start page keeps its footer
    assert "Pictures on this page:\n- Offer: BANNER: Call 0484 4061110" in home.content
    brochure = pages[SITE + "/brochure.docx"]
    assert brochure.title == "brochure" and "GST filing from Rs 999 a month." in brochure.content
    assert first["images_read"] == 1 and first["documents"] == 1 and first["rendered"] == 0

    # A re-crawl uses the remembered picture text: the picture isn't downloaded again.
    before = seen.count("/banner.png")
    second = crawl()
    assert seen.count("/banner.png") == before and second["images_read"] == 0
    assert second["unchanged"] == 2


@pytest.mark.integration
def test_pictures_and_documents_can_be_switched_off(db: Session, embedder, make_client) -> None:
    from app.crawler.service import crawl_client
    from app.db.models import Page

    client = make_client("shop", "Shop", SITE)
    client.crawl_settings = {**client.crawl_settings, "read_images": False, "read_documents": False}
    db.commit()
    seen: list[str] = []
    crawler = Crawler(CrawlConfig(start_url=SITE + "/", delay_seconds=0, read_documents=False), client=serve(seen), sleep=lambda s: None)
    crawl_client(db, client, embedder, crawler=crawler, image_reader=fake_reader)
    pages = list(db.scalars(select(Page).where(Page.client_id == client.id)))
    assert [p.url for p in pages] == [SITE + "/"]
    assert "Pictures on this page" not in pages[0].content
    assert "/banner.png" not in seen and "/brochure.docx" not in seen


@pytest.mark.integration
def test_picture_cache_expires_after_60_days(db: Session) -> None:
    from app.db.models import ImageTextCache

    url = SITE + "/old.jpg"
    remember(db, url, "Old text")
    db.get(ImageTextCache, url_hash(url)).created_at = datetime.now(UTC) - timedelta(days=61)
    db.commit()
    reads: list[str] = []

    def reader(data: bytes, ctype: str) -> str:
        reads.append(ctype)
        return "New text"

    section = describe_images(db, [PageImage(url, "Sign")], ImageBudget(), lambda u: (b"x" * 4000, "image/jpeg"), reader)
    assert section == "Pictures on this page:\n- Sign: New text" and reads == ["image/jpeg"]  # expired: read again

    db.get(ImageTextCache, url_hash(url)).created_at = datetime.now(UTC) - timedelta(days=61)
    remember(db, SITE + "/fresh.jpg", "Fresh")
    db.commit()
    assert cleanup_cache(db) == 1
    assert db.get(ImageTextCache, url_hash(SITE + "/fresh.jpg")) is not None


@pytest.mark.integration
def test_picture_budget_and_alt_text_fallback(db: Session) -> None:
    budget = ImageBudget(left=1)
    images = [PageImage(SITE + "/one.jpg", "One"), PageImage(SITE + "/two.jpg", "Two")]
    section = describe_images(db, images, budget, lambda u: (b"x" * 4000, "image/png"), lambda d, t: "**Text** in it")
    assert section == "Pictures on this page:\n- One: Text in it\n- Two"  # the second one wasn't read: alt text only
    assert budget.left == 0 and budget.read == 1
