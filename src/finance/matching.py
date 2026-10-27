"""Match account and category names typed by a person or a model.

Case-insensitive, in tiers: an exact name wins, then names that start with
the text, then names that contain it (or are contained by it, so "my current
account" still finds "Current"). Only the best tier counts; if it holds more
than one name the match is ambiguous and the caller must ask. Nothing is ever
created to satisfy a match.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Generic, Iterable, Optional, TypeVar

T = TypeVar("T")

# Words people add around a name that are not part of it.
_FILLER = re.compile(r"\b(?:my|the|a|an|account|acct|category|card)\b", re.I)


def _norm(text) -> str:
    return " ".join(re.sub(r"[^\w/&+-]+", " ", str(text or "").casefold()).split())


def _core(text) -> str:
    return " ".join(_FILLER.sub(" ", _norm(text)).split())


@dataclass
class Match(Generic[T]):
    status: str  # "ok", "ambiguous" or "missing"
    item: Optional[T] = None
    options: list = field(default_factory=list)  # candidate names to offer

    @property
    def ok(self) -> bool:
        return self.status == "ok"


def match_name(query, items: Iterable[T], name: Callable[[T], str] = lambda x: x["name"]) -> Match[T]:
    items = list(items)
    names = [name(i) for i in items]
    wanted = _norm(query)
    if not wanted:
        return Match("missing", options=names)
    wanted_core = _core(query) or wanted
    tiers = (
        lambda n: _norm(n) == wanted or _core(n) == wanted_core,
        lambda n: _norm(n).startswith(wanted) or _core(n).startswith(wanted_core),
        lambda n: wanted_core in _norm(n) or (_core(n) and _core(n) in wanted_core),
    )
    for test in tiers:
        hits = [i for i, n in zip(items, names) if test(n)]
        if len(hits) == 1:
            return Match("ok", item=hits[0])
        if len(hits) > 1:
            return Match("ambiguous", options=[name(i) for i in hits])
    return Match("missing", options=names)
