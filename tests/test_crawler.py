"""Crawler tests against an in-memory fake website (httpx.MockTransport)."""

from __future__ import annotations

import httpx
import pytest

from app.crawler.crawler import CrawlConfig, Crawler, FetchedPage, RobotsBlocked, matches_patterns, normalize_url
from app.crawler.extract import extract

SITE = "https://www.shop.test"


def page(title: str, body: str, links: list[str] | None = None) -> str:
    nav = "".join(f'<a href="{href}">{href}</a>' for href in (links or []))
    return (
        f"<html><head><title>{title}</title></head><body>"
        f'<header><nav><a href="/">Home</a> Menu Shop Dine</nav></header>'
        f'<div data-elementor-type="header">GLOBAL HEADER</div>'
        f"<main><h1>{title}</h1>{body}<div>{nav}</div></main>"
        f"<footer>Copyright footer text</footer><script>var x = 1;</script></body></html>"
    )


def make_site(pages: dict[str, tuple[int, str, str]], robots: str = "User-agent: *\nDisallow: /private/\n", sitemap: str | None = None):
    """pages: path -> (status, content-type, body)."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path + (f"?{request.url.query.decode()}" if request.url.query else "")
        seen.append(path)
        if path == "/robots.txt":
            return httpx.Response(200, text=robots)
        if path == "/sitemap.xml" and sitemap is not None:
            return httpx.Response(200, text=sitemap, headers={"content-type": "application/xml"})
        if path in pages:
            status, ctype, body = pages[path]
            return httpx.Response(status, text=body, headers={"content-type": ctype})
        return httpx.Response(404, text="not found", headers={"content-type": "text/html"})

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True), seen


def crawl(client: httpx.Client, **cfg) -> tuple[list[FetchedPage], object]:
    pages: list[FetchedPage] = []
    crawler = Crawler(CrawlConfig(start_url=SITE + "/", delay_seconds=0, **cfg), client=client, sleep=lambda s: None)
    result = crawler.crawl(pages.append)
    return pages, result


def test_extract_removes_chrome_and_keeps_small_facts() -> None:
    html = page("ASICS - Mall", "<p>Sports shoes.</p><div><span>Located on</span><span>Second Floor</span></div><div>10:00 AM to 10:00 PM</div>")
    out = extract(html, SITE + "/shop/asics/")
    assert out.title == "ASICS - Mall"
    assert "Second Floor" in out.text and "10:00 AM to 10:00 PM" in out.text
    for junk in ("GLOBAL HEADER", "Copyright footer", "var x", "Menu Shop Dine"):
        assert junk not in out.text


def test_link_following_same_site_only_and_robots() -> None:
    site = {
        "/": (200, "text/html", page("Home", "<p>Welcome</p>", ["/a/", "/private/secret/", "https://other.test/x", "/file.pdf", "/b/#frag"])),
        "/a/": (200, "text/html", page("A", "<p>Page A</p>", ["/"])),
        "/b/": (200, "text/html", page("B", "<p>Page B</p>")),
        "/private/secret/": (200, "text/html", page("Secret", "<p>no</p>")),
    }
    client, seen = make_site(site)
    pages, result = crawl(client)
    urls = sorted(p.url for p in pages)
    assert urls == [SITE + "/", SITE + "/a/", SITE + "/b/"]
    assert "/private/secret/" not in seen  # robots.txt respected
    assert not any("other.test" in s for s in seen)
    assert not result.used_sitemap


def test_sitemap_is_used_first() -> None:
    sitemap = f"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>{SITE}/x/</loc></url><url><loc>{SITE}/y/</loc></url></urlset>"""
    site = {
        "/": (200, "text/html", page("Home", "<p>Home</p>", ["/not-in-sitemap/"])),
        "/x/": (200, "text/html", page("X", "<p>X</p>")),
        "/y/": (200, "text/html", page("Y", "<p>Y</p>")),
        "/not-in-sitemap/": (200, "text/html", page("N", "<p>N</p>")),
    }
    client, _ = make_site(site, sitemap=sitemap)
    pages, result = crawl(client)
    assert result.used_sitemap
    assert sorted(p.url for p in pages) == [SITE + "/", SITE + "/x/", SITE + "/y/"]


def test_max_pages_and_gone_pages() -> None:
    sitemap = f"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>{SITE}/gone/</loc></url><url><loc>{SITE}/p1/</loc></url><url><loc>{SITE}/p2/</loc></url></urlset>"""
    site = {"/": (200, "text/html", page("Home", "<p>H</p>")), "/p1/": (200, "text/html", page("1", "<p>1</p>")), "/p2/": (200, "text/html", page("2", "<p>2</p>"))}
    client, _ = make_site(site, sitemap=sitemap)
    _, result = crawl(client)
    assert SITE + "/gone/" in result.gone_urls
    client, _ = make_site(site, sitemap=sitemap)
    pages, result = crawl(client, max_pages=2)
    assert result.hit_limit and len(pages) <= 2


def test_include_exclude_patterns() -> None:
    assert matches_patterns("https://s.test/shop/asics/", ["/shop/*"], [])
    assert not matches_patterns("https://s.test/dine/x/", ["/shop/*"], [])
    assert not matches_patterns("https://s.test/shop/x/?print=1", [], ["*?print=*"])


def test_normalize_url_strips_tracking_and_fragment() -> None:
    assert normalize_url("HTTPS://WWW.Shop.test/a?utm_source=x&id=2#top") == "https://www.shop.test/a?id=2"


def test_robots_server_error_stops_crawl() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RobotsBlocked):
        Crawler(CrawlConfig(start_url=SITE, delay_seconds=0), client=client).crawl(lambda p: None)


def test_gzip_encoded_responses() -> None:
    """Real servers send gzip; the size-capped fetch must not decode twice."""
    import gzip as _gzip

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            body, ctype = b"User-agent: *" + bytes([10]) + b"Disallow:" + bytes([10]), "text/plain"
        elif request.url.path == "/":
            body, ctype = page("Home", "<p>Gzipped welcome</p>").encode(), "text/html; charset=UTF-8"
        else:
            return httpx.Response(404)
        return httpx.Response(200, content=_gzip.compress(body), headers={"content-type": ctype, "content-encoding": "gzip"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    pages, result = crawl(client)
    assert result.failed == 0 and pages and "Gzipped welcome" in pages[0].text


def test_extract_drops_call_to_action_links() -> None:
    html = page("Movies", "<div>Zootopia 2</div><div><a>Know More »</a></div><div>Dheeram</div><div><a>View Store ›</a></div><div>Read more</div><div>View Website</div><div>Located on</div><div>Second Floor</div>")
    text = extract(html, SITE + "/movies/").text
    assert text.splitlines() == ["Movies", "Zootopia 2", "Dheeram", "Located on", "Second Floor"]
