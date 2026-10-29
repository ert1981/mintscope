"""MintScope command line.

    mintscope                  start the local app (UI on http://127.0.0.1:8765) and open the browser
    mintscope local [--port N] [--no-browser]
    mintscope serve [--host 0.0.0.0] [--port 8000]       hosted API (x402 pricing, featured, manifests)
    mintscope admin featured list|add|edit|remove|reanalyze|sponsor|unsponsor ...
    mintscope admin submissions list|approve|reject ...
    mintscope version
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import threading
import webbrowser


def _free_port(start: int) -> int:
    for port in range(start, start + 50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.3)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                continue  # something already answers there (possibly bound on 0.0.0.0)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise SystemExit("No free local port found.")


def _local_env() -> None:
    import keystore
    os.environ["MINTSCOPE_LOCAL"] = "1"
    os.environ.setdefault("MINTSCOPE_MAX_LIVE_CALLS_PER_DAY", "5000")
    d = keystore.config_dir()
    d.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MINTSCOPE_DB_PATH", str(d / "mintscope.db"))


def cmd_local(a) -> None:
    _local_env()
    import uvicorn
    from app import create_app
    from config import get_settings
    port = _free_port(a.port)
    url = f"http://127.0.0.1:{port}/"
    print(f"MintScope local app: {url}  (Ctrl+C to stop)", flush=True)
    if not a.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    # Bound to 127.0.0.1 only; access log off so no request details end up in a terminal log.
    uvicorn.run(create_app(get_settings()), host="127.0.0.1", port=port, access_log=False, log_level="warning")


def cmd_serve(a) -> None:
    import uvicorn
    from app import create_app
    from config import get_settings
    uvicorn.run(create_app(get_settings()), host=a.host, port=a.port, proxy_headers=True)


def _service():
    from config import get_settings
    import featured
    s = get_settings()
    return featured.FeaturedService(s.db_path, sponsored_enabled=s.sponsored_enabled)


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def cmd_admin(a) -> None:
    import featured
    svc = _service()
    try:
        if a.area == "featured":
            if a.action == "list":
                _print([{k: i[k] for k in ("id", "name", "chain", "contract_address", "active", "stats")}
                        | {"score": i["risk"]["score"], "sponsored": i["sponsored"]} for i in svc.list(admin=True)])
            elif a.action == "add":
                i = svc.add(contract=a.contract, chain=a.chain, link=a.link, name=a.name, image_url=a.image,
                            mint_url=a.mint_url, note=a.note)
                _print({"added": i["id"], "name": i["name"], "score": i["risk"]["score"]})
            elif a.action == "edit":
                fields = {k: v for k, v in {"name": a.name, "image_url": a.image, "mint_url": a.mint_url,
                                            "note": a.note}.items() if v is not None}
                if a.active is not None:
                    fields["active"] = a.active == "yes"
                _print(svc.edit(a.id, **fields))
            elif a.action == "remove":
                svc.remove(a.id)
                _print({"removed": a.id})
            elif a.action == "reanalyze":
                i = svc.reanalyze(a.id)
                _print({"id": i["id"], "score": i["risk"]["score"]})
            elif a.action == "sponsor":
                _print(svc.sponsor(a.id, a.plan, a.start)["sponsored"])
            elif a.action == "unsponsor":
                svc.unsponsor(a.id)
                _print({"unsponsored": a.id})
        else:
            if a.action == "list":
                _print(svc.submissions(None if a.status == "all" else a.status))
            elif a.action == "approve":
                _print(svc.review(a.id, True, a.image, a.note))
            elif a.action == "reject":
                _print(svc.review(a.id, False))
    except featured.FeaturedError as exc:
        raise SystemExit(f"Error: {exc}")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="mintscope", description="MintScope AI: NFT risk analysis")
    sub = ap.add_subparsers(dest="cmd")
    lp = sub.add_parser("local", help="local app with web UI (default)")
    lp.add_argument("--port", type=int, default=8765)
    lp.add_argument("--no-browser", action="store_true")
    sp = sub.add_parser("serve", help="hosted API")
    sp.add_argument("--host", default="0.0.0.0")
    sp.add_argument("--port", type=int, default=8000)
    sub.add_parser("version")
    ad = sub.add_parser("admin", help="manage featured collections and submissions (local database)")
    asub = ad.add_subparsers(dest="area", required=True)
    fe = asub.add_parser("featured")
    fsub = fe.add_subparsers(dest="action", required=True)
    fsub.add_parser("list")
    add = fsub.add_parser("add")
    add.add_argument("--contract")
    add.add_argument("--link")
    add.add_argument("--chain", choices=["ethereum", "base"])
    for p in (add, ed := fsub.add_parser("edit")):
        p.add_argument("--name")
        p.add_argument("--image")
        p.add_argument("--mint-url")
        p.add_argument("--note")
    ed.add_argument("id")
    ed.add_argument("--active", choices=["yes", "no"])
    for name in ("remove", "reanalyze", "unsponsor"):
        fsub.add_parser(name).add_argument("id")
    spn = fsub.add_parser("sponsor")
    spn.add_argument("id")
    spn.add_argument("--plan", choices=["7d", "30d"], required=True)
    spn.add_argument("--start")
    su = asub.add_parser("submissions")
    ssub = su.add_subparsers(dest="action", required=True)
    sl = ssub.add_parser("list")
    sl.add_argument("--status", default="pending", choices=["pending", "approved", "rejected", "all"])
    sa = ssub.add_parser("approve")
    sa.add_argument("id")
    sa.add_argument("--image")
    sa.add_argument("--note")
    ssub.add_parser("reject").add_argument("id")
    return ap


def _default_db() -> None:
    """Installed/frozen app: keep the database in the user's config dir, not next to the code."""
    import keystore
    if not os.environ.get("MINTSCOPE_DB_PATH"):
        d = keystore.config_dir()
        d.mkdir(parents=True, exist_ok=True)
        os.environ["MINTSCOPE_DB_PATH"] = str(d / "mintscope.db")


def main(argv=None) -> None:
    a = build_parser().parse_args(argv)
    _default_db()
    if a.cmd in (None, "local"):
        if a.cmd is None:
            a.port, a.no_browser = 8765, False
        cmd_local(a)
    elif a.cmd == "serve":
        cmd_serve(a)
    elif a.cmd == "version":
        from app import VERSION
        print(f"MintScope {VERSION}")
    elif a.cmd == "admin":
        cmd_admin(a)


if __name__ == "__main__":
    main()
