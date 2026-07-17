from __future__ import annotations

import argparse
import logging

import uvicorn


def main() -> None:
    from .config import load_config

    cfg = load_config()
    ap = argparse.ArgumentParser(
        prog="sshpeek",
        description="Local web UI for peeking at remote files and ports over SSH.",
    )
    ap.add_argument("--host", default=cfg.listen_host,
                    help=f"bind address (default: {cfg.listen_host})")
    ap.add_argument("--port", type=int, default=cfg.listen_port,
                    help=f"bind port (default: {cfg.listen_port})")
    ap.add_argument("--config", help="path to sshpeek.yaml (also: $SSHPEEK_CONFIG)")
    ap.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    args = ap.parse_args()

    if args.config:
        import os
        os.environ["SSHPEEK_CONFIG"] = args.config

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    print(f"sshpeek: http://{args.host}:{args.port}", flush=True)
    uvicorn.run("sshpeek.app:app", host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
