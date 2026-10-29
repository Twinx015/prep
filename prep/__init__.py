from __future__ import annotations

import argparse
import logging
from typing import Optional

from werkzeug.serving import run_simple

from .common import ensure_dir, print_dim, print_info
from .config import APP_NAME, APP_VERSION, dev_enabled, get_str, host, log_file, port
from .web import create_app


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prep", description="Ollama chat: API + web page"
    )
    parser.add_argument("--host", default=host())
    parser.add_argument("--port", type=int, default=port())
    parser.add_argument("--dev", action="store_true", default=dev_enabled())
    parser.add_argument("--log-file", default=get_str("LOG_FILE", ""))
    return parser


def silence_server_logs(target: str = "") -> str:
    path = log_file(target or None)
    ensure_dir(path.parent)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    for name in ("werkzeug", "flask.app"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    import werkzeug.serving as serving

    serving._log_add_style = False
    return str(path)


def serve(app, host_arg: str, port_arg: int, target: str, dev: bool) -> None:
    print_info(f"{APP_NAME} {APP_VERSION} -> http://{host_arg}:{port_arg}", flush=True)
    if dev:
        app.run(host=host_arg, port=port_arg, threaded=True, use_reloader=False)
        return
    path = silence_server_logs(target)
    print_dim(f"logs -> {path}", flush=True)
    run_simple(host_arg, port_arg, app, threaded=True, use_reloader=False)


def main(argv: Optional[list] = None) -> int:
    args = build_parser().parse_args(argv)
    app = create_app()
    serve(app, args.host, args.port, args.log_file, args.dev)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
