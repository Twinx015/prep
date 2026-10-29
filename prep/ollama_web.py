from __future__ import annotations

import json
from typing import Any, Callable, Iterator

from flask import Blueprint, Response, jsonify, request, stream_with_context

from .ollama import OllamaClient, OllamaError, normalize_name
from .store import ChatStore, auto_title


def body() -> dict:
    return request.get_json(silent=True) or {}


def fail(message: str, status: int = 400) -> tuple:
    return jsonify({"error": message}), status


def ndjson(events: Callable[[], Iterator[dict]]) -> Response:
    def chunks() -> Iterator[str]:
        for event in events():
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return Response(
        stream_with_context(chunks()),
        mimetype="application/x-ndjson",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-cache"},
    )


def model_entry(info: dict, running: set) -> dict:
    details = info.get("details") or {}
    name = info.get("name") or info.get("model") or ""
    return {
        "name": name,
        "size": info.get("size", 0),
        "modified_at": info.get("modified_at", ""),
        "family": details.get("family", ""),
        "parameter_size": details.get("parameter_size", ""),
        "quantization_level": details.get("quantization_level", ""),
        "running": name in running,
    }


def pull_event(obj: dict) -> dict:
    status = obj.get("status", "")
    event = {"type": "status", "status": status}
    total, completed = obj.get("total"), obj.get("completed")
    if total and completed is not None:
        event["percent"] = round(min(100.0, completed * 100.0 / total), 1)
        event["completed"] = completed
        event["total"] = total
    if obj.get("error"):
        event = {"type": "error", "error": obj["error"]}
    elif status == "success":
        event = {"type": "done", "status": status}
    return event


def ollama_blueprint(cfg: dict, client: OllamaClient, store: ChatStore) -> Blueprint:
    blueprint = Blueprint(
        "ollama",
        __name__,
        static_folder="static/ollama",
        static_url_path="/static",
    )
    register_page_routes(blueprint, cfg, client)
    register_model_routes(blueprint, client)
    register_chat_routes(blueprint, cfg, client, store)
    return blueprint


def register_page_routes(blueprint: Blueprint, cfg: dict, client: OllamaClient) -> None:
    @blueprint.get("/")
    def index() -> Response:
        return blueprint.send_static_file("index.html")

    @blueprint.get("/api/state")
    def state() -> Response:
        try:
            version, up = client.version(), True
        except OllamaError:
            version, up = None, False
        return jsonify(
            {
                "title": cfg["title"],
                "up": up,
                "version": version,
                "ollama": client.host,
                "system": bool(cfg["system"]),
            }
        )


def register_model_routes(blueprint: Blueprint, client: OllamaClient) -> None:
    @blueprint.get("/api/models")
    def models() -> Response:
        try:
            infos = client.list_models()
            running = {m.get("name") for m in client.running_models()}
        except OllamaError as exc:
            return jsonify({"up": False, "models": [], "error": str(exc)})
        entries = [model_entry(info, running) for info in infos]
        entries.sort(key=lambda item: item["name"])
        return jsonify({"up": True, "models": entries, "error": None})

    @blueprint.post("/api/models/pull")
    def pull_model() -> Response:
        name = (body().get("name") or "").strip()
        if not name:
            return fail("missing model name")

        def events() -> Iterator[dict]:
            try:
                for obj in client.pull(name):
                    yield pull_event(obj)
            except OllamaError as exc:
                yield {"type": "error", "error": str(exc)}

        return ndjson(events)

    @blueprint.post("/api/models/rename")
    def rename_model() -> Response:
        source = normalize_name(body().get("source") or "")
        destination = normalize_name(body().get("destination") or "")
        if not source or not destination:
            return fail("both source and destination are required")
        if source == destination:
            return fail("new name must differ from the old one")
        known = {m.get("name") for m in client.list_models()}
        if source not in known:
            return fail(f"no such model: {source}", 404)
        try:
            client.copy(source, destination)
        except OllamaError as exc:
            return fail(str(exc), 502)
        try:
            client.delete(source)
        except OllamaError as exc:
            return jsonify({"ok": False, "error": f"copied, but could not remove {source}: {exc}"})
        return jsonify({"ok": True, "source": source, "destination": destination})

    @blueprint.post("/api/models/delete")
    def delete_model() -> Response:
        name = normalize_name(body().get("name") or "")
        if not name:
            return fail("missing model name")
        try:
            client.delete(name)
        except OllamaError as exc:
            return fail(str(exc), 502)
        return jsonify({"ok": True, "name": name})


def register_chat_routes(
    blueprint: Blueprint,
    cfg: dict,
    client: OllamaClient,
    store: ChatStore,
) -> None:
    @blueprint.get("/api/chats")
    def chats() -> Response:
        return jsonify({"chats": store.list()})

    @blueprint.post("/api/chats")
    def create_chat() -> Response:
        chat = store.create(title=(body().get("title") or ""), model=(body().get("model") or ""))
        return jsonify(chat), 201

    @blueprint.get("/api/chats/<chat_id>")
    def read_chat(chat_id: str) -> Response:
        chat = store.get(chat_id)
        return jsonify(chat) if chat else fail("no such chat", 404)

    @blueprint.post("/api/chats/<chat_id>/rename")
    def rename_chat(chat_id: str) -> Response:
        chat = store.rename(chat_id, (body().get("title") or "").strip())
        return jsonify(chat) if chat else fail("no such chat", 404)

    @blueprint.delete("/api/chats/<chat_id>")
    def remove_chat(chat_id: str) -> Response:
        return jsonify({"ok": store.delete(chat_id)})

    @blueprint.post("/api/chats/<chat_id>/send")
    def send(chat_id: str) -> Response:
        chat = store.get(chat_id)
        if chat is None:
            return fail("no such chat", 404)
        content = (body().get("content") or "").strip()
        model = (body().get("model") or chat.get("model") or "").strip()
        if not content:
            return fail("empty message")
        if not model:
            return fail("pick a model first")
        store.append(chat_id, [{"role": "user", "content": content}])
        store.set_model(chat_id, model)
        if not chat["title"]:
            store.rename(chat_id, auto_title(content))
        fresh = store.get(chat_id) or chat
        history = [{"role": "system", "content": cfg["system"]}] if cfg["system"] else []
        history += [m for m in fresh["messages"] if m["role"] != "system"]

        def events() -> Iterator[dict]:
            pieces, thoughts = [], []
            error = None
            try:
                chat_args = {"keep_alive": cfg["keep_alive"], "options": cfg["options"] or None}
                for obj in client.chat(history, model, **chat_args):
                    if obj.get("error"):
                        raise OllamaError(obj["error"])
                    message = obj.get("message") or {}
                    thinking = message.get("thinking") or ""
                    if thinking:
                        thoughts.append(thinking)
                        yield {"type": "thinking", "content": thinking}
                    piece = message.get("content") or ""
                    if piece:
                        pieces.append(piece)
                        yield {"type": "token", "content": piece}
                    if obj.get("done"):
                        break
            except OllamaError as exc:
                error = str(exc)
            if pieces or thoughts:
                saved = store.append(
                    chat_id,
                    [
                        {
                            "role": "assistant",
                            "content": "".join(pieces),
                            "thinking": "".join(thoughts) or None,
                            "model": model,
                        }
                    ],
                )
                yield {"type": "done", "chat": saved}
            if error:
                yield {"type": "error", "error": error}

        return ndjson(events)