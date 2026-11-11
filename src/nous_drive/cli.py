"""``nous-drive`` command line: migrate, accounts/keys, run the API, worker and MCP server."""

from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nous-drive", description="Nous Drive Connectors and Index MCP")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("migrate", help="apply database migrations")
    a = sub.add_parser("create-account", help="create an account and print its first admin key")
    a.add_argument("name")
    a.add_argument("--email")
    a.add_argument("--org-id")
    k = sub.add_parser("create-key", help="issue a key (admin key required)")
    k.add_argument("--admin-key", required=True)
    k.add_argument("--kind", choices=["session", "admin"], default="session")
    k.add_argument("name")
    pc = sub.add_parser("pairing-code", help="create a device pairing code")
    pc.add_argument("--admin-key", required=True)
    pc.add_argument("--name")
    s = sub.add_parser("serve", help="run the HTTP API (web app, agent ingest, webhooks)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8080)
    w = sub.add_parser("worker", help="run the indexer worker and scheduler")
    w.add_argument("--concurrency", type=int)
    w.add_argument("--no-scheduler", action="store_true")
    sub.add_parser("drain", help="run queued jobs until the queue is empty, then exit")
    sub.add_parser("tick", help="run one scheduler pass")
    m = sub.add_parser("mcp", help="run the Index MCP server (stdio by default)")
    m.add_argument("--http", action="store_true", help="serve streamable HTTP instead of stdio")
    m.add_argument("--host", default="127.0.0.1")
    m.add_argument("--port", type=int, default=8090)
    sub.add_parser("purge", help="apply retention now")
    args = p.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stderr)

    if args.cmd == "migrate":
        from .db import migrate
        print(json.dumps({"applied": migrate()}))
    elif args.cmd == "create-account":
        from .accounts import create_account
        acct, key = create_account(args.name, args.email, args.org_id)
        print(json.dumps({"account_id": str(acct["id"]), "admin_key": key}, indent=2))
    elif args.cmd == "create-key":
        from . import db
        from .accounts import issue_key, resolve_key
        ident = resolve_key(args.admin_key, ("admin",))
        with db.scoped(ident["account_id"]) as conn:
            print(issue_key(conn, ident["account_id"], args.kind, args.name))
    elif args.cmd == "pairing-code":
        from . import db
        from .accounts import create_pairing_code, resolve_key
        ident = resolve_key(args.admin_key, ("admin",))
        with db.scoped(ident["account_id"]) as conn:
            print(create_pairing_code(conn, ident["account_id"], args.name))
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("nous_drive.api.app:app", host=args.host, port=args.port, proxy_headers=True)
    elif args.cmd == "worker":
        from .worker import Worker
        Worker(args.concurrency, with_scheduler=not args.no_scheduler).run()
    elif args.cmd == "drain":
        from .worker import drain
        print(json.dumps({"jobs": drain()}))
    elif args.cmd == "tick":
        from .scheduler import tick
        print(json.dumps(tick(), default=str))
    elif args.cmd == "mcp":
        from .mcp.server import main as mcp_main
        mcp_main(http=args.http, host=args.host, port=args.port)
    elif args.cmd == "purge":
        from .retention import purge
        print(json.dumps(purge()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
