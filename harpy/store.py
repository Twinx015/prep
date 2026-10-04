"""Read/write operations on the markdown cheat-sheet files.

Everything here mutates files under the harpy directory: adding cheats,
removing cheats or categories, creating new tool files, and opening a
cheat sheet in your editor.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path

from .parser import CATEGORY_HEADING_RE, CHEAT_LINE_RE, TOOL_HEADING_RE

DEFAULT_CATEGORY = "general"


def tool_path(directory: Path, tool: str) -> Path:
    """The .md file that holds `tool`'s cheats."""
    return directory / f"{tool}.md"


def ensure_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)


def new_tool(directory: Path, tool: str) -> Path:
    """Create an empty cheat-sheet file for `tool` (returns the existing file if present)."""
    ensure_directory(directory)
    path = tool_path(directory, tool)
    if not path.exists():
        path.write_text(f"# {tool}\n\n## {DEFAULT_CATEGORY}\n", encoding="utf-8")
    return path


def add_entry(
    directory: Path,
    tool: str,
    command: str,
    description: str = "",
    category: str | None = None,
) -> tuple[Path, bool, str]:
    """Append a cheat to `tool`'s sheet, creating the file/section as needed.

    When no category is given it lands in the file's last existing section
    (usually the "current" one); brand-new files get a ``general`` section.

    Returns (path, file_was_created, category_used).
    """
    ensure_directory(directory)
    path = tool_path(directory, tool)
    created = not path.exists()
    if created:
        cat = category or DEFAULT_CATEGORY
        lines = [f"# {tool}", "", f"## {cat}"]
    else:
        lines = path.read_text(encoding="utf-8").splitlines()
        cat = category or _last_category(lines) or DEFAULT_CATEGORY
        lines = _insert_cheat(lines, cat, _format_cheat(command, description))
    if created:
        lines.append(_format_cheat(command, description))
    _write_lines(path, lines)
    return path, created, cat


def delete_command(directory: Path, tool: str, command: str) -> tuple[Path, int]:
    """Remove every cheat line whose command equals `command`. Returns (path, n_removed)."""
    path = tool_path(directory, tool)
    if not path.exists():
        return path, 0
    lines = path.read_text(encoding="utf-8").splitlines()
    needle = command.strip().lower()
    kept = [ln for ln in lines if not _is_command_line(ln, needle)]
    removed = len(lines) - len(kept)
    if removed:
        _write_lines(path, kept)
    return path, removed


def delete_category(directory: Path, tool: str, category: str) -> tuple[Path, int]:
    """Remove a whole ``## category`` section (heading plus its cheats)."""
    path = tool_path(directory, tool)
    if not path.exists():
        return path, 0
    lines = path.read_text(encoding="utf-8").splitlines()
    needle = category.strip().lower()
    kept: list[str] = []
    removed = 0
    in_section = False
    for ln in lines:
        if in_section:
            if _is_heading(ln):
                in_section = False
            else:
                removed += 1
                continue
        cat = _category_heading(ln)
        if cat is not None and cat.lower() == needle:
            in_section = True
            removed += 1
            continue
        kept.append(ln)
    if removed:
        _write_lines(path, kept)
    return path, removed


def delete_file(directory: Path, tool: str) -> tuple[Path, bool]:
    """Remove the whole tool file. Returns (path, deleted)."""
    path = tool_path(directory, tool)
    if path.exists():
        path.unlink()
        return path, True
    return path, False


def open_editor(directory: Path, tool: str) -> tuple[Path, bool]:
    """Open the tool's cheat sheet in $EDITOR (default vi), creating it if needed."""
    ensure_directory(directory)
    path = tool_path(directory, tool)
    created = False
    if not path.exists():
        path.write_text(f"# {tool}\n\n## {DEFAULT_CATEGORY}\n", encoding="utf-8")
        created = True
    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    subprocess.call(shlex.split(editor) + [str(path)])
    return path, created


# ---------------------------------------------------------------------------
# helpers

def _category_heading(line: str) -> str | None:
    m = CATEGORY_HEADING_RE.match(line)
    return m.group(1).strip() if m else None


def _is_heading(line: str) -> bool:
    return bool(TOOL_HEADING_RE.match(line) or CATEGORY_HEADING_RE.match(line))


def _last_category(lines: list[str]) -> str | None:
    last = None
    for ln in lines:
        if cat := _category_heading(ln):
            last = cat
    return last


def _cheat_command(line: str) -> str | None:
    """The command part of a cheat line, or None if the line isn't a cheat."""
    m = CHEAT_LINE_RE.match(line)
    if m:
        return m.group("command").strip()
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    return stripped


def _is_command_line(line: str, needle: str) -> bool:
    cmd = _cheat_command(line)
    return cmd is not None and cmd.lower() == needle


def _format_cheat(command: str, description: str) -> str:
    command = command.strip()
    description = description.strip()
    return f"{command}  # {description}" if description else command


def _insert_cheat(lines: list[str], category: str, cheat: str) -> list[str]:
    """Return `lines` with `cheat` appended to the `category` section."""
    cat_idx = None
    for i, ln in enumerate(lines):
        if _category_heading(ln) == category:
            cat_idx = i
            break

    if cat_idx is None:
        out = list(lines)
        if out and out[-1]:
            out.append("")
        out.append(f"## {category}")
        out.append(cheat)
        return out

    end = len(lines)
    for i in range(cat_idx + 1, len(lines)):
        if _is_heading(lines[i]):
            end = i
            break
    insert_at = end
    while insert_at - 1 > cat_idx and not lines[insert_at - 1].strip():
        insert_at -= 1

    out = list(lines)
    out.insert(insert_at, cheat)
    return out


def _write_lines(path: Path, lines: list[str]) -> None:
    text = "\n".join(lines).rstrip()
    path.write_text((text + "\n") if text else "", encoding="utf-8")