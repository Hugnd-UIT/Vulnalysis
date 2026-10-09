from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import logging
from pathlib import Path
from typing import Callable


LOG = logging.getLogger(__name__)


@dataclass(frozen=True)


class LanguageResolver:
    name: str
    suffixes: frozenset
    resolve: Callable


REGISTRY: list[LanguageResolver] = []


def register(resolver: LanguageResolver) -> LanguageResolver:
    REGISTRY.append(resolver)

    return resolver


def registered() -> list[LanguageResolver]:
    return list(REGISTRY)


def run(paths: Sequence[Path], per: list[dict], nodes: list[dict], edges: list[dict], *, resolvers: Sequence[LanguageResolver] | None = None) -> None:
    active = REGISTRY if resolvers is None else resolvers
    suffixes = {p.suffix for p in paths}

    for item in active:
        if not item.suffixes & suffixes:
            continue

        try:
            item.resolve(per, nodes, edges)
        except Exception as exc:
            LOG.warning('%s resolution failed: %s', item.name, exc)