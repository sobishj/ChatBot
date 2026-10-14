"""A client's essential pages (opening hours, contact, about, location), always given to the assistant.

Vague questions like "is it open now?" or "how do I reach you?" don't always match these pages
in search, so the assistant gets them every time, found automatically by page title or URL.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

ESSENTIAL_PATTERN = r"timing|opening|hours|contact|about|location|find[ -]us|direction|reach[ -]us|visit[ -]us"
MAX_PAGES = 4
MAX_CHARS_PER_PAGE = 1200
MAX_TOTAL_CHARS = 3500


def essential_pages(db: Session, client_id: int) -> str:
    """Opening-hours pages first, then contact, then about/location; trimmed to a small total."""
    rows = db.execute(
        text(
            "SELECT title, url, content FROM pages WHERE client_id = :cid AND content <> '' "
            "AND (coalesce(title, '') ~* :pat OR url ~* :pat) "
            "ORDER BY CASE WHEN coalesce(title, '') || ' ' || url ~* 'timing|opening|hours' THEN 0 "
            "WHEN coalesce(title, '') || ' ' || url ~* 'contact|reach|find' THEN 1 ELSE 2 END, length(content) LIMIT :n"
        ),
        {"cid": client_id, "pat": ESSENTIAL_PATTERN, "n": MAX_PAGES},
    ).all()
    blocks: list[str] = []
    used = 0
    for title, url, content in rows:
        body = content.strip()[:MAX_CHARS_PER_PAGE]
        block = f"From \"{title or url}\":\n{body}"
        if used + len(block) > MAX_TOTAL_CHARS and blocks:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


def key_information(db: Session, client_id: int, key_facts: str) -> str:
    """The admin's own key information plus the essential pages."""
    return "\n\n".join(part for part in (key_facts.strip(), essential_pages(db, client_id)) if part)


MAX_DIRECTORY_CHARS = 6000


def page_directory(db: Session, client_id: int) -> str:
    """Names of all the client's pages (e.g. every shop), for "which shops sell …?" questions.

    A shop's page often doesn't say what it sells ("the latest gear to take on sports"), so
    search alone misses it; with the full list the assistant can pick the matching ones.
    """
    titles = [t.strip() for t in db.execute(text("SELECT title FROM pages WHERE client_id = :cid AND title <> ''"), {"cid": client_id}).scalars() if t]
    if len(titles) < 3:
        return ""
    # Drop the site name most titles end with ("NIKE - Lulu Malls" -> "NIKE").
    suffixes: dict[str, int] = {}
    for title in titles:
        for sep in (" - ", " | ", " – "):
            if sep in title:
                suffixes[sep + title.rsplit(sep, 1)[1]] = suffixes.get(sep + title.rsplit(sep, 1)[1], 0) + 1
    common = max(suffixes, key=lambda k: suffixes[k]) if suffixes else ""
    if common and suffixes[common] < len(titles) / 2:
        common = ""
    names = sorted({t[: -len(common)] if common and t.endswith(common) else t for t in titles}, key=str.lower)
    listing = ", ".join(names)
    return listing if len(listing) <= MAX_DIRECTORY_CHARS else listing[:MAX_DIRECTORY_CHARS].rsplit(", ", 1)[0] + ", …"
