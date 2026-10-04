"""Fuzzy search and ranking over cheat-sheet entries."""

from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

from .parser import Entry

WORD_RE = re.compile(r"[a-z0-9]+")

COVERAGE_FLOOR = 0.5
FUZZY_MIN_SCORE = 70.0


@dataclass
class Match:
    entry: Entry
    score: float


def _words(text: str) -> set[str]:
    return set(WORD_RE.findall(text.lower()))


def search(entries: list[Entry], query: str, limit: int = 40) -> list[Match]:
    """Return entries matching `query`, best matches first.

    Two modes:
    1. "Scoped" - the first word of the query exactly names a known tool
       (e.g. "gh"). In that case we return every command under that tool,
       optionally narrowed to categories whose name contains the rest of
       the query (e.g. "gh repo" -> every command in the "repo" category).
    2. "Fuzzy" - otherwise every query word is checked against each entry's
       tool/category/command/description text. An entry containing all
       (or most) query words ranks highest; entries matching fewer words
       fall back to a stricter, typo-tolerant fuzzy comparison so a
       query that shares nothing meaningful with an entry doesn't match it
       just because it's short.
    """
    if not query.strip():
        return [Match(e, 0.0) for e in entries[:limit]]

    query = query.lower()
    all_words = query.split()
    # Very short tokens ("a", "at", "to") are too easy to "contain" by
    # accident, so they don't drive matching on their own.
    words = [w for w in all_words if len(w) >= 3] or all_words

    scoped = _scoped_matches(entries, all_words)
    if scoped is not None:
        return [Match(e, 100.0) for e in scoped[:limit]]

    scored: list[Match] = []
    for e in entries:
        blob = f"{e.tool} {e.category} {e.command} {e.description}"
        entry_words = _words(blob)

        hits = sum(
            1
            for w in words
            if w in entry_words or (len(w) >= 4 and any(w in ew for ew in entry_words))
        )
        coverage = hits / len(words)

        if coverage >= COVERAGE_FLOOR:
            score = 60.0 + 40.0 * coverage
        else:
            # Last resort: typo-tolerant fuzzy match against the
            # content-rich fields only (tool/category names are short
            # enough that fuzzy ratios against them are unreliable).
            content = f"{e.command} {e.description}"
            score = fuzz.WRatio(query, content)
            if score < FUZZY_MIN_SCORE:
                continue

        scored.append(Match(e, score))

    scored.sort(key=lambda m: m.score, reverse=True)
    return scored[:limit]


def _scoped_matches(entries: list[Entry], words: list[str]) -> list[Entry] | None:
    tools = {e.tool.lower() for e in entries}
    if not words or words[0] not in tools:
        return None

    tool = words[0]
    rest = words[1:]

    if not rest:
        return [e for e in entries if e.tool.lower() == tool]

    category_query = " ".join(rest)
    matches = [
        e
        for e in entries
        if e.tool.lower() == tool and category_query in e.category.lower()
    ]
    return matches or None
