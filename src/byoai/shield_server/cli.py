"""``byoai-shield-server`` — ``serve`` / ``mint-token`` / ``admin-token``."""

from __future__ import annotations

import argparse
import sys

from byoai.shield_server.app import create_app
from byoai.shield_server.config import admin_token_path, load_config
from byoai.shield_server.store import ServerStore


def _cmd_serve(args: argparse.Namespace) -> int:
    cfg, fresh = load_config()
    if fresh:
        print(f"Generated a new admin token, saved to {admin_token_path(cfg.data_dir)} (0600):")
        print(f"  {cfg.admin_token}")
        print("Save it now — it is not shown again. Use it as `Authorization: Bearer <token>`.")
    app = create_app(cfg)
    try:
        import uvicorn
    except ImportError:
        print("uvicorn is required to serve: pip install 'byoai-runtime[fastapi]' uvicorn",
             file=sys.stderr)
        return 1
    print(f"ByoAI Shield server: org={cfg.org!r} data={cfg.data_dir}")
    print(f"Listening on http://{cfg.host}:{cfg.port} (public url: {cfg.public_url})")
    print(f"Console: {cfg.public_url.rstrip('/')}/console/")
    uvicorn.run(app, host=cfg.host, port=cfg.port)
    return 0


def _cmd_mint_token(args: argparse.Namespace) -> int:
    cfg, _fresh = load_config(generate_admin_token=False)
    store = ServerStore(cfg.server_db_path)
    try:
        token = store.mint_token(label=args.label, ttl_seconds=args.ttl_seconds)
    finally:
        store.close()
    print(token)
    return 0


def _cmd_admin_token(args: argparse.Namespace) -> int:
    cfg, fresh = load_config()
    if fresh:
        print("(freshly generated)", file=sys.stderr)
    print(cfg.admin_token)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="byoai-shield-server",
                                     description="Self-hosted Shield server (free, single-org).")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the server.")
    serve.set_defaults(func=_cmd_serve)

    mint = sub.add_parser("mint-token", help="Mint a single-use enrolment token.")
    mint.add_argument("--label", default=None, help="A human label for this token.")
    mint.add_argument("--ttl-seconds", type=float, default=None,
                      help="Expire the token after this many seconds (default: never).")
    mint.set_defaults(func=_cmd_mint_token)

    admin = sub.add_parser("admin-token", help="Print the current admin bearer token.")
    admin.set_defaults(func=_cmd_admin_token)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
