"""``nous-sync`` command line."""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import socket
import sys

from . import __version__
from .client import Client, ServerError
from .config import AgentConfig, home


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nous-sync", description="Nous Sync local folder agent")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    pr = sub.add_parser("pair", help="pair this computer with your account using a one-time code")
    pr.add_argument("code")
    pr.add_argument("--server", required=True)
    pr.add_argument("--name", help="device name, e.g. 'Edit Suite iMac'")
    ad = sub.add_parser("add", help="watch a folder")
    ad.add_argument("path")
    ad.add_argument("--name")
    ad.add_argument("--ignore", action="append", default=[], help="extra ignore pattern (repeatable)")
    rm = sub.add_parser("remove", help="stop watching a folder and delete its index")
    rm.add_argument("path_or_id")
    sub.add_parser("list", help="list watched folders")
    sub.add_parser("status", help="show agent status")
    sub.add_parser("run", help="run the agent in the foreground")
    sub.add_parser("install", help="start the agent automatically at login")
    sub.add_parser("uninstall", help="remove the login item")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(sys.stderr),
                                  logging.FileHandler(home() / "agent.log", encoding="utf-8")])
    cfg = AgentConfig.load()

    if args.cmd == "pair":
        client = Client(args.server)
        try:
            res = client.pair(args.code, cfg.machine_id, socket.gethostname(),
                              f"{platform.system()} {platform.release()}", args.name)
        except ServerError as e:
            print(f"pairing failed: {e}", file=sys.stderr)
            return 1
        cfg.server, cfg.token = args.server, res["token"]
        cfg.device_id, cfg.device_name = res["device"]["id"], res["device"]["name"]
        cfg.save()
        print(f"Paired as '{cfg.device_name}'" + (" (re-paired; name and folders kept)" if res["repaired"] else ""))
        return 0
    if not cfg.token:
        print("not paired yet: nous-sync pair <CODE> --server <URL>", file=sys.stderr)
        return 1
    from .agent import Agent
    if args.cmd == "add":
        agent = Agent(cfg)
        try:
            conf = agent.add_source(args.path, args.name, args.ignore)
        except ServerError as e:
            print(f"could not add folder: {e}", file=sys.stderr)
            return 1
        print(f"Watching {conf.path} as '{conf.name}'. The running agent picks it up on restart "
              f"(or now, if you run `nous-sync run`).")
        _restart_service()
    elif args.cmd == "remove":
        conf = next((s for s in cfg.sources if args.path_or_id in (s.id, s.path, os.path.abspath(args.path_or_id))), None)
        if not conf:
            print("no such watched folder", file=sys.stderr)
            return 1
        agent = Agent(cfg)
        agent.client.remove_source(conf.id)
        agent.forget_source(conf.id)
        print(f"Stopped watching {conf.path}")
        _restart_service()
    elif args.cmd == "list":
        for s in cfg.sources:
            print(f"{s.id}  {s.name:<24} {s.path}{'  (paused)' if s.paused else ''}")
    elif args.cmd == "status":
        from .state import State
        st = State(home() / "state.sqlite3")
        print(json.dumps({"device": cfg.device_name, "server": cfg.server, "folders": len(cfg.sources),
                          "queued_events": st.pending(), "version": __version__}, indent=2))
    elif args.cmd == "run":
        Agent(cfg).run()
    elif args.cmd == "install":
        from .service import install
        print(install())
    elif args.cmd == "uninstall":
        from .service import uninstall
        print(uninstall())
    return 0


def _restart_service() -> None:
    try:
        from .service import restart
        restart()
    except Exception:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
