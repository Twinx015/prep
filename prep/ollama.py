from __future__ import annotations

import json
import os
from typing import Any, Iterator, NoReturn, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .config import DEFAULT_OLLAMA_HOST

DEFAULT_HOST = DEFAULT_OLLAMA_HOST


class OllamaError(RuntimeError):
    pass


def resolve_host(host: Optional[str] = None) -> str:
    raw = host or os.environ.get("OLLAMA_HOST") or DEFAULT_HOST
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urlparse(raw)
    scheme = parsed.scheme or "http"
    host_name = parsed.hostname or "127.0.0.1"
    if ":" in host_name:
        host_name = f"[{host_name}]"
    port = parsed.port or (443 if scheme == "https" else 11434)
    return f"{scheme}://{host_name}:{port}"


def normalize_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        return ""
    return name if ":" in name.rsplit("/", 1)[-1] else name + ":latest"


class OllamaClient:
    def __init__(self, host: Optional[str] = None, timeout: float = 60.0):
        self.host = resolve_host(host)
        self.timeout = timeout

    def __repr__(self) -> str:
        return f"OllamaClient({self.host!r})"

    def _build(self, path: str, payload: Any = None, method: Optional[str] = None) -> Request:
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        request = Request(
            self.host + path,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
        )
        if data is not None:
            request.add_header("Content-Type", "application/json")
        return request

    @staticmethod
    def _fail(exc: Exception, path: str) -> NoReturn:
        if isinstance(exc, HTTPError):
            raw = exc.read().decode("utf-8", "replace").strip()
            message = raw
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get("error"):
                    message = str(parsed["error"])
            except ValueError:
                pass
            raise OllamaError(message or f"{path} failed with HTTP {exc.code}") from None
        reason = getattr(exc, "reason", exc)
        raise OllamaError(f"cannot reach Ollama ({reason}) - is `ollama serve` running?") from None

    def request(
        self,
        path: str,
        payload: Any = None,
        timeout: Optional[float] = None,
        method: Optional[str] = None,
    ) -> dict:
        try:
            with urlopen(
                self._build(path, payload, method),
                timeout=self.timeout if timeout is None else timeout,
            ) as response:
                raw = response.read().decode("utf-8", "replace")
        except (HTTPError, URLError, OSError) as exc:
            self._fail(exc, path)
        return json.loads(raw) if raw.strip() else {}

    def stream(self, path: str, payload: Any) -> Iterator[dict]:
        try:
            response = urlopen(self._build(path, payload), timeout=None)
        except (HTTPError, URLError, OSError) as exc:
            self._fail(exc, path)
        with response:
            for raw in response:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue

    def version(self) -> str:
        return self.request("/api/version", timeout=3.0).get("version", "?")

    def is_up(self) -> bool:
        try:
            self.version()
            return True
        except OllamaError:
            return False

    def list_models(self) -> list:
        return self.request("/api/tags").get("models", [])

    def running_models(self) -> list:
        return self.request("/api/ps").get("models", [])

    def pull(self, name: str) -> Iterator[dict]:
        payload = {"model": name, "name": name, "stream": True}
        yield from self.stream("/api/pull", payload)

    def chat(
        self,
        messages: list,
        model: str,
        keep_alive: Any = None,
        options: Optional[dict] = None,
    ) -> Iterator[dict]:
        payload: dict = {"model": model, "messages": messages, "stream": True}
        if keep_alive is not None:
            payload["keep_alive"] = keep_alive
        if options:
            payload["options"] = options
        yield from self.stream("/api/chat", payload)

    def copy(self, source: str, destination: str) -> dict:
        return self.request("/api/copy", {"source": source, "destination": destination})

    def delete(self, name: str) -> dict:
        return self.request("/api/delete", {"model": name}, method="DELETE")
