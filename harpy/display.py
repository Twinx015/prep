"""Rich-based rendering of cheat-sheet matches."""

from __future__ import annotations

from itertools import groupby

from rich.console import Console
from rich.table import Table
from rich.text import Text

from .search import Match

console = Console()


def show_matches(matches: list[Match], query: str) -> None:
    if not matches:
        console.print(f"[yellow]No cheats found for[/yellow] [bold]{query!r}[/bold]")
        console.print("Try a shorter query, or add a cheat sheet under [bold]~/.harpy/[/bold]")
        return

    entries = [m.entry for m in matches]
    entries.sort(key=lambda e: (e.tool, e.category))

    for (tool, category), group in groupby(entries, key=lambda e: (e.tool, e.category)):
        table = Table(
            title=f"[bold cyan]{tool}[/bold cyan] [dim]\u203a[/dim] [bold]{category}[/bold]",
            title_justify="left",
            show_header=False,
            box=None,
            padding=(0, 2, 0, 0),
        )
        table.add_column("command", style="bold green", no_wrap=False)
        table.add_column("description", style="dim")
        for e in group:
            # Use Text (not raw strings) so literal "[...]" placeholders in
            # commands, e.g. "gh repo create [name]", aren't parsed as
            # Rich markup tags and silently swallowed.
            table.add_row(Text(e.command), Text(e.description))
        console.print(table)
        console.print()


def list_tools(tools: list[str]) -> None:
    console.print("[bold]Cheat sheets found:[/bold]")
    for t in tools:
        console.print(f"  \u2022 {t}")
    console.print()
    console.print("Usage: [bold]harpy <tool> [category][/bold]  e.g. [green]harpy gh repo[/green]")
    console.print("       [bold]harpy <anything>[/bold]         e.g. [green]harpy clone a repo[/green]")
