from __future__ import annotations

import os
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import networkx as nx

MARKER = '_cross_repo_call'
KEY = 'unresolved_calls'

SUFFIXES: dict[str, frozenset[str]] = {
    'cpp': frozenset({'.cpp', '.cc', '.cxx', '.hpp', '.hh', '.hxx', '.h', '.cu', '.cuh'}),
    'csharp': frozenset({'.cs'}),
    'java': frozenset({'.java'}),
    'swift': frozenset({'.swift'}),
}

RELATIONS = ('defines', 'method')


def key(label: object) -> str:
    return str(label or '').strip().removeprefix('.').removesuffix('()')


def suffix(source: object) -> str:
    return os.path.splitext(str(source or ''))[1].lower()


def parked(data: dict) -> list[dict]:
    meta = data.get('metadata')

    if not isinstance(meta, dict):
        return []

    calls = meta.get(KEY)

    if not isinstance(calls, list):
        return []

    return [item for item in calls if isinstance(item, dict)]


def clear(merged: nx.Graph) -> None:
    stale = [(u, v) for u, v, data in merged.edges(data=True) if data.get(MARKER)]
    merged.remove_edges_from(stale)


def decls(merged: nx.Graph) -> tuple[dict[str, list[str]], set[str]]:
    by_name: dict[str, list[str]] = defaultdict(list)
    types: set[str] = set()

    for node, data in merged.nodes(data=True):
        if not data.get('_callable_class') or not data.get('source_file') or not data.get('repo'):
            continue

        name = key(data.get('label'))

        if not name:
            continue

        by_name[name].append(node)
        types.add(node)

    return (by_name, types)


def members(merged: nx.Graph, types: set[str]) -> dict[str, dict[tuple[str, str], list[str]]]:
    indexed: dict[str, dict[tuple[str, str], list[str]]] = {r: defaultdict(list) for r in RELATIONS}

    for u, v, data in merged.edges(data=True):
        rel = data.get('relation')

        if rel not in indexed:
            continue

        if u in types and v not in types:
            owner, member = (u, v)
        elif v in types and u not in types:
            owner, member = (v, u)
        else:
            continue

        name = key(merged.nodes[member].get('label'))

        if name:
            indexed[rel][owner, name].append(member)

    return indexed


def relations(lang: str) -> tuple[str, ...]:
    return ('method', 'defines') if lang == 'cpp' else ('method',)


def link(merged: nx.Graph) -> int:
    clear(merged)

    nodes = [(node, data) for node, data in merged.nodes(data=True) if parked(data)]

    if not nodes:
        return 0

    by_name, types = decls(merged)

    if not by_name:
        return 0

    indexed = members(merged, types)
    resolver = None

    if any((str(e.get('lang') or '') == 'csharp' for _, data in nodes for e in parked(data))):
        from graphify.langs.csharp import resolver as csresolver, type as cstype
        r_nodes = [dict(data, id=node) for node, data in merged.nodes(data=True)]
        r_edges = [dict(data, source=src, target=tgt) for src, tgt, data in merged.edges(data=True)]
        resolver = csresolver(r_nodes, r_edges)

    added = 0

    for caller, data in nodes:
        repo = data.get('repo')

        if not repo:
            continue

        for entry in parked(data):
            lang = str(entry.get('lang') or '')
            suffs = SUFFIXES.get(lang)
            rtype = key(entry.get('receiver_type'))
            callee = key(entry.get('callee'))

            if not suffs or not rtype or not callee:
                continue

            cands = [n for n in by_name.get(rtype, []) if merged.nodes[n].get('repo') != repo and suffix(merged.nodes[n].get('source_file')) in suffs]

            if len(cands) != 1:
                continue

            if lang == 'csharp' and resolver is not None:
                resolved, _ = cstype(resolver, rtype, dict(data, id=caller), str(data.get('source_file') or ''))

                if resolved != cands[0]:
                    continue

            targets: list[str] = []

            for rel in relations(lang):
                targets = indexed[rel].get((cands[0], callee), [])

                if targets:
                    break

            if len(targets) != 1:
                continue

            target = targets[0]

            if target == caller or merged.has_edge(caller, target):
                continue

            merged.add_edge(
                caller,
                target,
                relation='calls',
                context='cross_repo',
                source_file=str(data.get('source_file') or ''),
                source_location=entry.get('line'),
                _src=caller,
                _tgt=target,
                **{MARKER: True},
            )
            added += 1

    return added