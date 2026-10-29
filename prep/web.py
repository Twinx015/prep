from __future__ import annotations

from pathlib import Path
from typing import Optional

from flask import Flask, Response, render_template_string, send_from_directory

from .config import DEFAULT_OLLAMA_HOST, ollama_host, prefix, title
from .ollama import OllamaClient
from .ollama_web import ollama_blueprint
from .store import ChatStore

STATIC_DIR = Path(__file__).resolve().parent / "static"


def default_config() -> dict:
    return {
        "title": title(),
        "prefix": prefix(),
        "ollama_host": ollama_host() or DEFAULT_OLLAMA_HOST,
        "state_dir": None,
        "system": "",
        "keep_alive": None,
        "options": {},
    }


def normalize_prefix(value: str) -> str:
    value = "/" + (value or "").strip().strip("/")
    return "" if value == "/" else value


def create_app(config: Optional[dict] = None) -> Flask:
    cfg = {**default_config(), **(config or {})}
    cfg["prefix"] = normalize_prefix(cfg["prefix"])
    client = OllamaClient(cfg["ollama_host"])
    store = ChatStore(cfg["state_dir"])
    app = Flask(__name__, static_folder=None)
    app.config["PREP"] = {"cfg": cfg, "client": client, "store": store}
    app.register_blueprint(
        ollama_blueprint(cfg, client, store), url_prefix=cfg["prefix"]
    )
    if cfg["prefix"]:
        register_hub_routes(app, cfg)
    return app


def register_hub_routes(app: Flask, cfg: dict) -> None:
    @app.get("/static/<path:filename>")
    def static_file(filename: str) -> Response:
        return send_from_directory(STATIC_DIR, filename)

    @app.get("/")
    def root() -> str:
        page = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        return render_template_string(
            page, prefix=cfg["prefix"], cards=render_cards(cfg)
        )


def render_cards(cfg: dict) -> str:
    cards = [
        {
            "href": f"{cfg['prefix']}/",
            "icon": "/static/ollama.svg",
            "title": "Ollama",
            "desc": "Chat with local AI models.",
        }
    ]
    return "\n".join(render_card(card) for card in cards)


def render_card(card: dict) -> str:
    return (
        f'<a class="card" href="{card["href"]}">'
        f'<img src="{card["icon"]}" alt="{card["title"]}" />'
        f"<h2>{card['title']}</h2>"
        f"<p>{card['desc']}</p>"
        f"</a>"
    )
