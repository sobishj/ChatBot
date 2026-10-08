"""Developer / recovery command line. Everything here is also available in the admin UI.

Run inside the app container:
    docker compose exec app python cli.py crawl <client_id>
    docker compose exec app python cli.py index-docs <client_id>
    docker compose exec app python cli.py ask <client_id> "Where is the ASICS store?"
    docker compose exec app python cli.py stats <client_id> [--days 30]
    docker compose exec app python cli.py reset-admin-password <email>
"""

from __future__ import annotations

import argparse
import getpass
import json
import secrets
import sys
import uuid

from app.config import get_settings
from app.db.models import User
from app.db.session import session_scope
from app.db.startup import prepare_database
from app.logging_setup import configure_logging
from app.services.clients import get_by_slug


def _client(db, slug: str):  # type: ignore[no-untyped-def]
    client = get_by_slug(db, slug)
    if client is None:
        sys.exit(f"Unknown client: {slug}")
    return client


def _progress(percent: float, text: str) -> None:
    print(f"\r[{percent:5.1f}%] {text[:100]:<100}", end="", flush=True)


def cmd_crawl(args: argparse.Namespace) -> None:
    from app.crawler.service import crawl_client
    from app.embeddings.model import get_embedder

    with session_scope() as db:
        result = crawl_client(db, _client(db, args.client_id), get_embedder(), progress=_progress, log=lambda m: print(f"\n{m}"))
    print("\n" + json.dumps(result, indent=2, default=str))


def cmd_index_docs(args: argparse.Namespace) -> None:
    from app.documents.service import sync_documents
    from app.embeddings.model import get_embedder

    with session_scope() as db:
        result = sync_documents(db, _client(db, args.client_id), get_embedder(), force=args.force, progress=_progress, log=lambda m: print(f"\n{m}"))
    print("\n" + json.dumps(result, indent=2, default=str))


def cmd_ask(args: argparse.Namespace) -> None:
    from app.chat.service import answer_question

    with session_scope() as db:
        r = answer_question(db, _client(db, args.client_id), args.session or uuid.uuid4().hex, args.question, channel="cli")
    print(r.answer)
    print(f"\n— answered={r.answered} confidence={r.confidence:.3f} model={r.model_name} fallback={r.used_fallback} "
          f"{r.response_ms} ms tokens={r.input_tokens}/{r.output_tokens}")
    for s in r.sources:
        print(f"  source: {s.get('url') or s.get('title')}")
    if r.error:
        print(f"  error: {r.error}")


def cmd_stats(args: argparse.Namespace) -> None:
    from app.analytics.stats import question_stats

    with session_scope() as db:
        stats = question_stats(db, [_client(db, args.client_id).id], days=args.days)
    print(json.dumps(stats, indent=2, default=str, ensure_ascii=False))


def cmd_reset_admin_password(args: argparse.Namespace) -> None:
    from app.services.users import get_by_email, set_password

    password = args.password or getpass.getpass("New password (leave empty to generate one): ") or secrets.token_urlsafe(14)
    with session_scope() as db:
        user: User | None = get_by_email(db, args.email)
        if user is None:
            sys.exit(f"No user with email {args.email}")
        set_password(user, password)
        user.active = True
    print(f"Password updated for {args.email}." + ("" if args.password else f"\nNew password: {password}"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="cli.py", description="Website Assistant command line (developers/recovery).")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("crawl", help="Crawl a client's website now")
    p.add_argument("client_id")
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("index-docs", help="Index a client's documents (uploads + watched folder)")
    p.add_argument("client_id")
    p.add_argument("--force", action="store_true", help="Re-index unchanged files too")
    p.set_defaults(func=cmd_index_docs)

    p = sub.add_parser("ask", help="Ask a question as a visitor would")
    p.add_argument("client_id")
    p.add_argument("question")
    p.add_argument("--session", help="Session id (to test follow-up questions)")
    p.set_defaults(func=cmd_ask)

    p = sub.add_parser("stats", help="Show question statistics")
    p.add_argument("client_id")
    p.add_argument("--days", type=int, default=30)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("reset-admin-password", help="Set a new password for a user (and re-enable the account)")
    p.add_argument("email")
    p.add_argument("--password", help="New password (prompted if omitted)")
    p.set_defaults(func=cmd_reset_admin_password)

    args = parser.parse_args(argv)
    configure_logging(get_settings().log_level if args.command != "ask" else "WARNING")
    prepare_database()
    args.func(args)


if __name__ == "__main__":
    main()
