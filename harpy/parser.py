
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

TOOL_HEADING_RE = re.compile(r"^#\s+(.+?)\s*$")
CATEGORY_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
CHEAT_LINE_RE = re.compile(r"^(?P<command>.*?\S)\s{2,}#\s*(?P<description>.*)$")


@dataclass
class Entry:
    tool: str
    category: str
    command: str
    description: str
    source: Path


def parse_file(path: Path) -> list[Entry]:
    tool = path.stem
    category = "general"
    entries: list[Entry] = []

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if not stripped:
            continue

        if m := TOOL_HEADING_RE.match(line):
            tool = m.group(1).strip()
            continue

        if m := CATEGORY_HEADING_RE.match(line):
            category = m.group(1).strip()
            continue

        if stripped.startswith("#"):
            # A comment-only / note line - not a cheat, skip it.
            continue

        if m := CHEAT_LINE_RE.match(line):
            command = m.group("command").strip()
            description = m.group("description").strip()
        else:
            command = stripped
            description = ""

        entries.append(
            Entry(
                tool=tool,
                category=category,
                command=command,
                description=description,
                source=path,
            )
        )

    return entries


def load_all(directory: Path) -> list[Entry]:
    entries: list[Entry] = []
    if not directory.exists():
        return entries
    for path in sorted(directory.glob("*.md")):
        entries.extend(parse_file(path))
    return entries
