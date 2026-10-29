from __future__ import annotations

import re
import time
import uuid
from pathlib import Path
from typing import Optional

from .common import ensure_dir, read_json, write_json
from .config import chats_dir

ID_RE = re.compile(r"^[0-9a-f]{8,32}$")
TITLE_LEN = 60


def auto_title(text: str) -> str:
    first = " ".join(text.strip().split())
    return first if len(first) <= TITLE_LEN else first[: TITLE_LEN - 1].rstrip() + "…"


def summarize(chat: dict) -> dict:
    return {
        "id": chat["id"],
        "title": chat["title"],
        "model": chat["model"],
        "created_at": chat["created_at"],
        "updated_at": chat["updated_at"],
        "count": len(chat["messages"]),
    }


class ChatStore:
    def __init__(self, root: Optional[str] = None):
        self.root = Path(root).expanduser() if root else chats_dir()
        ensure_dir(self.root)

    def _path(self, chat_id: str) -> Path:
        if not isinstance(chat_id, str) or not ID_RE.match(chat_id):
            raise KeyError(chat_id)
        return self.root / f"{chat_id}.json"

    def _read(self, chat_id: str) -> Optional[dict]:
        chat = read_json(self._path(chat_id))
        return chat if isinstance(chat, dict) else None

    def list(self) -> list:
        chats = []
        for path in self.root.glob("*.json"):
            chat = read_json(path)
            if not isinstance(chat, dict):
                continue
            try:
                chats.append(summarize(chat))
            except KeyError:
                continue
        chats.sort(key=lambda item: item["updated_at"], reverse=True)
        return chats

    def get(self, chat_id: str) -> Optional[dict]:
        return self._read(chat_id)

    def create(self, title: str = "", model: str = "") -> dict:
        now = int(time.time())
        return self.save(
            {
                "id": uuid.uuid4().hex[:12],
                "title": title.strip(),
                "model": model.strip(),
                "created_at": now,
                "updated_at": now,
                "messages": [],
            }
        )

    def save(self, chat: dict) -> dict:
        chat["updated_at"] = int(time.time())
        write_json(self._path(chat["id"]), chat)
        return chat

    def append(self, chat_id: str, messages: list) -> Optional[dict]:
        chat = self._read(chat_id)
        if chat is None:
            return None
        chat["messages"].extend(messages)
        return self.save(chat)

    def rename(self, chat_id: str, title: str) -> Optional[dict]:
        chat = self._read(chat_id)
        if chat is None:
            return None
        chat["title"] = auto_title(title) if title else ""
        return self.save(chat)

    def set_model(self, chat_id: str, model: str) -> Optional[dict]:
        chat = self._read(chat_id)
        if chat is None or chat["model"] == model:
            return chat
        chat["model"] = model
        return self.save(chat)

    def delete(self, chat_id: str) -> bool:
        try:
            self._path(chat_id).unlink()
            return True
        except (KeyError, OSError):
            return False