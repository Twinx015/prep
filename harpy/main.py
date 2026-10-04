"""harpy: fuzzy cheat-sheet finder for markdown cheat sheets in ~/.harpy/"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, store
from .display import console, list_tools, show_matches
from .parser import load_all
from .search import search

DEFAULT_DIR = Path.home() / ".harpy"

# Non-search commands. A query whose first word is one of these is treated
# as a command; anything else is a fuzzy search.
VERBS = ("add", "delete", "rm", "edit", "new", "list", "ls", "help")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="harpy",
        description="Fuzzy-find commands from your markdown cheat sheets in ~/.harpy/",
    )
    parser.add_argument(
        "query",
        nargs="*",
        help='what to look up, e.g. "harpy gh repo", or a command: '
        "add, delete, edit, new, list",
    )
    parser.add_argument(
        "--dir",
        type=Path,
        default=DEFAULT_DIR,
        help=f"cheat sheet directory (default: {DEFAULT_DIR})",
    )
    parser.add_argument(
        "--category",
        "-c",
        metavar="CAT",
        help="section to add to / delete (add: last section or 'general' by default)",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="list available cheat sheet tools and exit",
    )
    parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="delete a whole cheat sheet without asking for confirmation",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"harpy {__version__}",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    words = args.query
    verb = words[0] if words else None

    if verb in VERBS:
        return run_verb(parser, args, verb)

    entries = load_all(args.dir)
    if not entries:
        print(f"No cheat sheets found in {args.dir}", file=sys.stderr)
        print("Add markdown files there, or run 'harpy new <tool>'", file=sys.stderr)
        return 1

    if args.list or not verb:
        list_tools(sorted({e.tool for e in entries}))
        return 0

    query = " ".join(words)
    show_matches(search(entries, query), query)
    return 0


def run_verb(parser: argparse.ArgumentParser, args: argparse.Namespace, verb: str) -> int:
    words = args.query

    if verb == "help":
        parser.print_help()
        return 0

    if verb in ("list", "ls"):
        return _list_tools(args.dir)

    if verb == "new":
        if len(words) < 2:
            parser.error("new needs a tool name: harpy new TOOL")
        if len(words) > 2:
            parser.error(f"unexpected extra arguments: {' '.join(words[2:])!r}")
        path = store.new_tool(args.dir, words[1])
        console.print(f"[green]new[/green] {path}")
        return 0

    if verb == "add":
        if len(words) < 3:
            parser.error('add needs: harpy add TOOL "COMMAND" ["DESCRIPTION"]')
        if len(words) > 4:
            parser.error(f"unexpected extra arguments: {' '.join(words[4:])!r}")
        tool, command = words[1], words[2]
        description = words[3] if len(words) > 3 else ""
        path, created, category = store.add_entry(
            args.dir, tool, command, description, args.category
        )
        noted = " (created)" if created else ""
        console.print(
            f"[green]added[/green] [bold cyan]{tool}[/bold cyan] › {category}  "
            f"{command!r}{noted}"
        )
        console.print(f"       -> {path}")
        return 0

    if verb in ("delete", "rm"):
        return _delete(parser, args, words)

    if verb == "edit":
        if len(words) < 2:
            parser.error("edit needs a tool name: harpy edit TOOL")
        if len(words) > 2:
            parser.error(f"unexpected extra arguments: {' '.join(words[2:])!r}")
        path, created = store.open_editor(args.dir, words[1])
        if created:
            console.print(f"[green]created[/green] {path}")
        return 0

    return 1


def _list_tools(directory: Path) -> int:
    entries = load_all(directory)
    if not entries:
        console.print("[yellow]No cheat sheets found.[/yellow]")
        return 0
    list_tools(sorted({e.tool for e in entries}))
    return 0


def _delete(
    parser: argparse.ArgumentParser, args: argparse.Namespace, words: list[str]
) -> int:
    if len(words) < 2:
        parser.error(
            "delete needs at least a tool: harpy delete TOOL [COMMAND] "
            "(whole sheet: --yes, a section: --category CAT)"
        )
    tool = words[1]
    rest = words[2:]

    if args.category:
        if rest:
            parser.error("cannot combine --category with a COMMAND")
        path, removed = store.delete_category(args.dir, tool, args.category)
        if not removed:
            console.print(
                f"[yellow]no such category[/yellow] {args.category!r} in [bold]{tool}[/bold]"
            )
            return 1
        console.print(
            f"[red]deleted[/red] category [bold]{args.category}[/bold] from "
            f"[bold]{tool}[/bold]: removed {removed} line(s) ({path})"
        )
        return 0

    if rest:
        command = " ".join(rest)
        path, removed = store.delete_command(args.dir, tool, command)
        if not removed:
            console.print(
                f"[yellow]no match[/yellow] for command {command!r} in [bold]{tool}[/bold]"
            )
            return 1
        console.print(
            f"[red]deleted[/red] {removed} line(s) for {command!r} "
            f"from [bold]{tool}[/bold] ({path})"
        )
        return 0

    path = store.tool_path(args.dir, tool)
    if not path.exists():
        console.print(f"[yellow]no cheat sheet[/yellow] for {tool!r} at {path}")
        return 1
    if not _confirm(f"Delete {path} and all of its cheats?", args.yes):
        console.print("canceled")
        return 0
    store.delete_file(args.dir, tool)
    console.print(f"[red]deleted[/red] {path}")
    return 0


def _confirm(message: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        reply = input(f"{message} [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return reply in {"y", "yes"}


if __name__ == "__main__":
    raise SystemExit(main())